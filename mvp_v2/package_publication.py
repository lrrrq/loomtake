from __future__ import annotations

from datetime import datetime, timezone
import json
import hashlib
import os
import re
import tempfile
from pathlib import Path
import shutil
import sqlite3
import stat
from typing import Callable, Iterable

from .asset_package import create_asset_package, package_script_catalog, MANIFEST_NAME, PACKAGE_V3_SCHEMA_VERSION, bundle_asset_package_v3, load_asset_package, _write_media_analysis, _refresh_media_searchable_text, _rebuild_fts
from .asset_storage import LocalAssetStorage
from .package_registry import LocalPackageRegistry, PackageRegistryError, build_package_record
from .run_contract import sha256_file
from .run_state import try_exclusive_lock, write_json_atomic


class PackagePublicationError(RuntimeError):
    def __init__(self, code: str, message: str = ""):
        self.code = str(code)
        super().__init__(f"{self.code}: {message}" if message else self.code)


class AssetPackagePublicationService:
    """Build one immutable package from reviewed imports, then move one alias."""

    def __init__(
        self,
        *,
        storage: LocalAssetStorage,
        registry: LocalPackageRegistry,
        packages_root: str | Path,
        clock: Callable[[], str] | None = None,
        bundler: Callable[..., Path] = bundle_asset_package_v3,
    ):
        self.storage = storage
        self.registry = registry
        self.packages_root = Path(packages_root).expanduser().resolve()
        self._clock = clock or _utc_now
        self._bundler = bundler

    def resolve_template(self, category_id: str, ref: str) -> dict:
        if not ref or "@" not in ref or not ref.rsplit("@", 1)[1].isdigit():
            raise PackagePublicationError("TEMPLATE_REF_INVALID", "请选择明确版本的模板")
        try:
            if ref not in self.registry.list()["packages"]:
                configured = self.registry.configured_record(category_id)
                if configured and configured["resolved_ref"] == ref:
                    return configured
            return self.registry.resolve(category_id, ref)
        except PackageRegistryError as exc:
            raise PackagePublicationError("TEMPLATE_UNAVAILABLE", "模板不可用，请刷新后重试") from exc

    def template_records(self, category_id: str) -> list[dict]:
        registry = self.registry.list()
        records = dict(registry["packages"])
        if not (registry["aliases"].get(category_id) or {}).get("formal"):
            configured = self.registry.configured_record(category_id)
            if configured:
                records.setdefault(configured["resolved_ref"], configured)
        return [dict(value) for value in records.values() if value.get("category_id") == category_id]

    def library_records(self, template_record: dict, *, frozen_bindings: list[dict] | None = None) -> tuple[list[dict], list[str]]:
        """Keep template category fixed while binding each active shared library explicitly."""
        category = template_record["category_id"]
        if frozen_bindings is not None:
            return [self.registry.resolve_evidence(binding.get("category_id") or category, binding) for binding in frozen_bindings], []
        aliases = self.registry.list()["aliases"]
        records, warnings = [], []
        for library_category in sorted(set(aliases) | {category}):
            ref = aliases.get(library_category, {}).get("formal")
            try:
                record = self.resolve_template(library_category, ref) if ref else (
                    self.registry.configured_record(category) if library_category == category else None)
            except (PackagePublicationError, PackageRegistryError):
                warnings.append(f"{library_category} 共享库暂不可用；本次不引用该库，可在素材库核对后重试。")
                continue
            if record and (record["package_id"], record["package_version"]) != (template_record["package_id"], template_record["package_version"]):
                records.append(record)
        return records, warnings

    def describe_sources(self, record: dict, *, frozen_bindings: list[dict] | None = None) -> dict:
        from .script_type_adapter import metadata_for_category_id
        additional, warnings = self.library_records(record, frozen_bindings=frozen_bindings)
        _, creative, _ = package_script_catalog(record["local_path"], expected_category_id=record["category_id"],
            library_paths=[item["local_path"] for item in additional])
        libraries, image_ids = [], set()
        for item in [record, *additional]:
            package = load_asset_package(item["local_path"])
            _, rows, _ = package_script_catalog(item["local_path"], expected_category_id=item["category_id"])
            connection = sqlite3.connect(Path(package["index_path"]).as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                digests = {str(value[0]) for value in connection.execute("SELECT content_sha256 FROM media_assets") if value[0]}
            finally:
                connection.close()
            image_ids.update(digests)
            libraries.append({"ref": f"{item['package_id']}@{item['package_version']}",
                "label": package.get("display_name") or Path(package["source_workbook_path"]).stem,
                "category_id": item["category_id"], "category_label": metadata_for_category_id(item["category_id"]).display_name,
                "version": item["package_version"],
                "role": "template" if item is record else "category_library",
                "creative_count": len(rows), "image_count": len(digests)})
        return {"category_id": record["category_id"],
            "category_label": metadata_for_category_id(record["category_id"]).display_name,
            "libraries": libraries, "creative_count": len(creative), "image_count": len(image_ids), "warnings": warnings}

    def describe_template(self, record: dict) -> dict:
        from .script_type_adapter import metadata_for_category_id
        package = load_asset_package(record["local_path"])
        shots, _, _ = package_script_catalog(record["local_path"], expected_category_id=record["category_id"])
        structures = [
            {"name": rule["value"], "start": rule["selector"]["slot_start"],
             "end": rule["selector"]["slot_end"],
             "shot_count": rule["selector"]["slot_end"] - rule["selector"]["slot_start"] + 1}
            for rule in package["template_rule_contract"]["payload"]["rules"] if rule["field"] == "structure"
        ]
        return {
            "ref": f"{record['package_id']}@{record['package_version']}",
            "label": package.get("display_name") or metadata_for_category_id(record["category_id"]).display_name + "通用模板",
            "version": record["package_version"],
            "source_name": Path(package["source_workbook_path"]).name,
            "shot_count": len(shots), "structures": structures,
            "warnings": list(package.get("template_warnings") or []),
            "source_semantics": package["source_semantics"],
            "tone_options": _template_tone_options(package),
            "sources": self.describe_sources(record),
        }

    def create_semantic_revision(self, *, category_id: str, source_ref: str,
                                 editing_evidence: dict | None = None) -> dict:
        """Register an immutable semantic candidate. Existing review/alias actions activate it."""
        from .asset_package import _project_unpublished_source_semantics
        from .run_state import exclusive_lock
        from .template_editing_methods import SCHEMA_VERSION, method_revision_identity
        source = self.resolve_template(category_id, source_ref)
        previous = (load_asset_package(source['local_path']).get('source_semantics') or {}).get('editing_methods') or {}
        inherited = previous if (editing_evidence is None and previous.get('schema_version') == SCHEMA_VERSION
                                 and previous.get('methods')) else None
        revision = {"source_ref": source_ref, "source_package_sha256": source["package_sha256"],
                    "projection_version": "mplus-source-semantics/v1",
                    "editing_methods": method_revision_identity(editing_evidence, compiled=inherited)}
        self.packages_root.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.packages_root / ("." + source["package_id"] + ".lock")):
            for record in self.template_records(category_id):
                candidate = load_asset_package(record["local_path"])
                if candidate.get("semantic_revision") == revision:
                    return self.resolve_template(category_id, f"{record['package_id']}@{record['package_version']}")
            next_version = self._next_version(source)
            target = self.packages_root / source["package_id"] / f"v{next_version}"
            if target.exists():
                raise PackagePublicationError("PACKAGE_VERSION_CONFLICT", "目标模板版本已存在")
            try:
                manifest_path = self._bundler(source["local_path"], target, package_version=next_version)
                _project_unpublished_source_semantics(manifest_path, editing_evidence=editing_evidence)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["semantic_revision"] = revision
                write_json_atomic(manifest_path, manifest)
                record = build_package_record(manifest_path)
                self.registry.register_records([record])
                return self.resolve_template(category_id, f"{record['package_id']}@{record['package_version']}")
            except Exception:
                if target.exists() and f"{source['package_id']}@{next_version}" not in self.registry.list()["packages"]:
                    shutil.rmtree(target)
                raise

    def import_template(self, *, category_id: str, filename: str, content: bytes, display_name: str = "") -> dict:
        """Inspect and register an immutable candidate without changing the category alias."""
        from .ai_spec import check_ai_spec, load_ai_spec_shots
        from .run_state import exclusive_lock
        from .script_type_adapter import metadata_for_category_id
        from .template_rules import rules_from_template_shots
        metadata = metadata_for_category_id(category_id)
        name = Path(str(filename).replace("\\", "/")).name
        if not name.lower().endswith(".xlsx") or not content or len(content) > 50 * 1024 * 1024:
            raise PackagePublicationError("TEMPLATE_FILE_INVALID", "请选择不超过50MB的Excel模板")
        label = str(display_name or Path(name).stem).strip()[:120]
        digest = hashlib.sha256(category_id.encode() + b"\0" + name.encode() + b"\0" + label.encode() + b"\0" + content).hexdigest()
        package_id = "template-" + digest[:24]
        ref = package_id + "@1"
        self.packages_root.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.packages_root / ("." + package_id + ".lock")):
            existing = self.registry.list()["packages"].get(ref)
            if existing:
                return self.resolve_template(category_id, ref)
            target = self.packages_root / package_id / "v1"
            if target.exists():
                raise PackagePublicationError("TEMPLATE_IMPORT_CONFLICT", "此模板已有未完成导入，请检查后重试")
            with tempfile.TemporaryDirectory(prefix=".template-", dir=self.packages_root) as temporary:
                root = Path(temporary)
                sources = root / "sources"
                sources.mkdir()
                workbook = sources / name
                workbook.write_bytes(content)
                try:
                    report = check_ai_spec(workbook)
                    if not report.in_sync or not report.usable:
                        raise PackagePublicationError("AI_SPEC_NOT_READY", "AI规范表未就绪，请同步并完成复核后导入")
                    shots, _ = load_ai_spec_shots(sources, script_type=metadata.display_name, workbook_path=workbook)
                    rules = rules_from_template_shots(category_id, shots)
                    seed = create_asset_package(root / "seed", package_id=package_id,
                        source_root=sources, source_workbook=workbook, category_id=category_id)
                    manifest_path = self._bundler(seed, target, package_version=1, template_rules=rules)
                    from .asset_package import _project_unpublished_source_semantics
                    _project_unpublished_source_semantics(manifest_path)
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["display_name"] = label
                    manifest["template_warnings"] = []
                    write_json_atomic(manifest_path, manifest)
                    record = build_package_record(manifest_path)
                    self.registry.register_records([record])
                    return self.resolve_template(category_id, ref)
                except Exception as exc:
                    # Only this call's unpublished build is removable.
                    if target.exists() and ref not in self.registry.list()["packages"]:
                        shutil.rmtree(target)
                    if isinstance(exc, PackagePublicationError):
                        raise
                    raise PackagePublicationError("TEMPLATE_IMPORT_FAILED", "模板导入失败，请检查模板表、创意库和AI规范表") from exc

    def publish(
        self,
        *,
        import_record: dict,
        items: Iterable[dict],
        published_by_actor_id: str,
        expected_revision: int,
        finalize: Callable[[dict], dict],
        publication_command: dict | None = None,
        read_stage: Callable | None = None,
        record_stage: Callable | None = None,
    ) -> dict:
        # Match the allocation lock used by image-analysis revisions, so two
        # publishers cannot construct or clean up the same target directory.
        try:
            planned = read_stage("planned") if publication_command else None
            source = (self.registry.resolve(str(import_record.get("category_id") or ""),
                f"{planned['source']['package_id']}@{planned['source']['package_version']}") if planned
                else self.registry.resolve_formal(str(import_record.get("category_id") or "")))
        except PackageRegistryError as exc:
            raise PackagePublicationError("PACKAGE_REGISTRY_UNAVAILABLE", "Formal alias 不可用") from exc
        self.packages_root.mkdir(parents=True, exist_ok=True)
        with try_exclusive_lock(self.packages_root / ("." + str(source["package_id"]) + ".lock")) as acquired:
            if not acquired:
                raise PackagePublicationError("PUBLICATION_IN_PROGRESS", "同一资产包正在发布，请稍后恢复原操作")
            return self._publish(import_record=import_record, items=items,
                published_by_actor_id=published_by_actor_id, expected_revision=expected_revision,
                finalize=finalize, publication_command=publication_command,
                read_stage=read_stage, record_stage=record_stage)

    def _publish(
        self,
        *,
        import_record: dict,
        items: Iterable[dict],
        published_by_actor_id: str,
        expected_revision: int,
        finalize: Callable[[dict], dict],
        publication_command: dict | None = None,
        read_stage: Callable | None = None,
        record_stage: Callable | None = None,
    ) -> dict:
        category_id = str(import_record.get("category_id") or "")
        try:
            planned = read_stage("planned") if publication_command else None
            source = (self.registry.resolve(category_id,
                f"{planned['source']['package_id']}@{planned['source']['package_version']}") if planned
                else self.registry.resolve_formal(category_id))
            if planned and source["package_sha256"] != planned["source"]["package_sha256"]:
                raise PackageRegistryError("原发布源包摘要已变化")
        except PackageRegistryError as exc:
            raise PackagePublicationError("PACKAGE_REGISTRY_UNAVAILABLE", "Formal alias 不可用") from exc
        if (
            str(import_record.get("package_id") or "") != str(source.get("package_id") or "")
            or int(import_record.get("package_version") or 0) != int(source.get("package_version") or 0)
        ):
            raise PackagePublicationError(
                "PACKAGE_VERSION_CONFLICT",
                "导入记录绑定的 Formal 包已不是当前 alias 版本",
            )
        if source.get("package_schema_version") != PACKAGE_V3_SCHEMA_VERSION:
            raise PackagePublicationError(
                "IMMUTABLE_PACKAGE_VERSION",
                "Formal 发布源必须是已发布 v3 不可变资产包",
            )

        rows = [dict(item) for item in items]
        if not rows or any(item.get("validation_status") not in {"valid", "duplicate"} for item in rows):
            raise PackagePublicationError(
                "MACHINE_REVIEW_FAILED",
                "只有机器审核通过且未失败的导入内容可发布",
            )
        next_version = planned["package_version"] if planned else self._next_version(source)
        target = self.packages_root / str(source["package_id"]) / f"v{next_version}"
        if planned and str(target) != planned["target"]:
            raise PackagePublicationError("PUBLICATION_RECONCILIATION_REQUIRED", "原发布目标路径不一致")
        if not planned:
            if target.exists():
                raise PackagePublicationError("PACKAGE_VERSION_CONFLICT", "目标资产包版本已存在")
            if publication_command:
                planned = record_stage("planned", {"source": source, "package_version": next_version,
                    "target": str(target)})
        proof = {"command_id": publication_command["command_id"],
            "request_sha256": publication_command["request_sha256"],
            "source_package_sha256": source["package_sha256"]} if publication_command else None
        built = read_stage("built") if publication_command else None
        try:
            if built or (publication_command and target.exists()):
                manifest_path = target / MANIFEST_NAME
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise PackagePublicationError("PUBLICATION_RECONCILIATION_REQUIRED",
                        "原目标包构建不完整，已保留现场；请核对原目标包后处理，不能自动重建或删除") from exc
                if manifest.get("publication_command") != proof:
                    raise PackagePublicationError("PUBLICATION_RECONCILIATION_REQUIRED", "原目标包尚未完整构建，需要核对原发布")
                record = build_package_record(manifest_path)
                if built and record != built:
                    raise PackagePublicationError("PUBLICATION_RECONCILIATION_REQUIRED", "原目标包与持久发布记录不一致")
            else:
                manifest_path = self._bundler(source["local_path"], target, package_version=next_version)
                self._materialize_imports(manifest_path, import_record=import_record, items=rows)
                if publication_command:
                    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
                    manifest["publication_command"] = proof
                    write_json_atomic(manifest_path, manifest)
                record = build_package_record(manifest_path)
            if (record["package_id"] != source["package_id"] or record["package_version"] != next_version
                    or Path(record["local_path"]).resolve() != target.resolve()):
                raise PackagePublicationError("PUBLICATION_RECONCILIATION_REQUIRED", "目标包身份与原发布计划不一致")
            if publication_command and not built:
                record_stage("built", record)
        except Exception as exc:
            if not publication_command and target.exists():
                shutil.rmtree(target)
            if isinstance(exc, PackagePublicationError):
                raise
            raise PackagePublicationError("PACKAGE_BUILD_FAILED", "新不可变资产包构建或完整性校验失败") from exc

        try:
            self.registry.register_records([record])
        except Exception as exc:
            if not publication_command and target.exists():
                shutil.rmtree(target)
            raise PackagePublicationError("PACKAGE_REGISTER_FAILED", "新资产包注册失败") from exc
        try:
            promoted = self.registry.promote(
                str(source["category_id"]),
                str(record["package_id"]),
                int(record["package_version"]),
                expected_current_ref=f"{source['package_id']}@{source['package_version']}",
                **({"command_id": publication_command["command_id"],
                    "request_sha256": publication_command["request_sha256"]} if publication_command else {}),
            )
        except Exception as exc:
            if publication_command:
                raise PackagePublicationError("PACKAGE_ALIAS_PROMOTE_FAILED", "原发布指针结果待恢复，请继续原命令") from exc
            try:
                self.registry.discard_unpromoted(
                    record["package_id"], record["package_version"], record["package_sha256"]
                )
                if target.exists():
                    shutil.rmtree(target)
            except (PackageRegistryError, OSError):
                pass
            raise PackagePublicationError("PACKAGE_ALIAS_PROMOTE_FAILED", "Formal alias 切换失败") from exc

        publication_result = {"package_id": record["package_id"], "package_version": record["package_version"],
            "package_sha256": record["package_sha256"], "alias": "formal",
            "alias_switched": promoted.get("previous_ref") != f"{record['package_id']}@{record['package_version']}",
            "rollback": {"attempted": False, "succeeded": False}}
        try:
            finalized = finalize({
                "published_by_actor_id": published_by_actor_id,
                "expected_revision": expected_revision,
                "source_package": source,
                "package_record": record,
                "publication_result": publication_result,
            })
        except Exception as exc:
            if publication_command:
                receipt = read_stage("completed")
                if receipt is not None:
                    return receipt
                raise PackagePublicationError("PACKAGE_FINALIZE_FAILED", "原发布已推进，继续原命令补齐导入结果") from exc
            try:
                self.registry.restore_alias(
                    str(source["category_id"]),
                    f"{record['package_id']}@{record['package_version']}",
                    promoted.get("previous_ref"),
                )
            except PackageRegistryError as rollback_exc:
                raise PackagePublicationError(
                    "PACKAGE_ALIAS_ROLLBACK_FAILED",
                    "发布状态确认失败且 alias 补偿失败",
                ) from exc
            raise PackagePublicationError("PACKAGE_FINALIZE_FAILED", "导入发布状态确认失败") from exc

        return {
            **finalized,
            "package_id": record["package_id"],
            "package_version": record["package_version"],
            "package_sha256": record["package_sha256"],
            "alias": "formal",
            "alias_switched": promoted.get("previous_ref") != f"{record['package_id']}@{record['package_version']}",
            "rollback": {"attempted": False, "succeeded": False},
        }

    def _next_version(self, source: dict) -> int:
        package_id = str(source["package_id"])
        versions = []
        for record in (self.registry.list().get("packages") or {}).values():
            if str(record.get("package_id") or "") == package_id:
                try:
                    versions.append(int(record["package_version"]))
                except (KeyError, TypeError, ValueError):
                    continue
        return max(versions or [int(source["package_version"])]) + 1

    def prepare_image_analysis_revision(self, *, category_id: str, source_ref: str, updates: Iterable[dict]) -> dict:
        """Register a reversible index candidate; changing the category alias is separate."""
        from .image_understanding import normalize_visual_analysis
        from .run_state import exclusive_lock
        from .run_contract import sha256_json

        source = self.resolve_template(category_id, source_ref)
        package = load_asset_package(source["local_path"])
        if package["schema_version"] != PACKAGE_V3_SCHEMA_VERSION:
            raise PackagePublicationError("IMMUTABLE_PACKAGE_VERSION", "请选择已发布的模板版本")
        rows = [dict(item) for item in updates]
        if not rows:
            raise PackagePublicationError("ANALYSIS_REVISION_EMPTY", "没有需要更新的图片分析")
        seen = set()
        with sqlite3.connect(f"file:{package['index_path']}?mode=ro", uri=True) as connection:
            for row in rows:
                if set(row) != {"media_id", "content_sha256", "sources", "analysis"} or not isinstance(row["analysis"], dict):
                    raise PackagePublicationError("ANALYSIS_REVISION_INVALID", "图片分析更新字段不完整")
                identity = connection.execute(
                    "SELECT content_sha256 FROM media_assets WHERE package_id=? AND media_id=?",
                    (source["package_id"], row["media_id"]),
                ).fetchone()
                if not identity or identity[0] != row["content_sha256"] or row["media_id"] in seen:
                    raise PackagePublicationError("ANALYSIS_IDENTITY_DRIFT", "图片身份已变化，请重新读取")
                seen.add(row["media_id"])
                expected_sources = set(connection.execute(
                    "SELECT DISTINCT source_file, source_sheet, source_row FROM media_script_links WHERE media_id=? AND package_id=?",
                    (row["media_id"], source["package_id"]),
                ).fetchall())
                if not isinstance(row["sources"], list) or any(
                    not isinstance(item, dict) or set(item) != {"file", "sheet", "row"}
                    or type(item["row"]) is not int for item in row["sources"]
                ):
                    raise PackagePublicationError("ANALYSIS_SOURCE_DRIFT", "图片来源字段不完整")
                supplied_sources = {(item["file"], item["sheet"], item["row"]) for item in row["sources"]}
                if supplied_sources != expected_sources:
                    raise PackagePublicationError("ANALYSIS_SOURCE_DRIFT", "图片来源已变化，请重新读取")
                row["sources"] = [dict(zip(("file", "sheet", "row"), item)) for item in sorted(expected_sources)]
                row["analysis"] = normalize_visual_analysis(row["analysis"])
        rows.sort(key=lambda item: item["media_id"])
        revision = {"source_ref": source_ref, "source_package_sha256": source["package_sha256"],
                    "updates_sha256": sha256_json(rows), "updated_media_count": len(rows)}
        self.packages_root.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.packages_root / ("." + source["package_id"] + ".lock")):
            for record in self.template_records(category_id):
                candidate = load_asset_package(record["local_path"])
                manifest = json.loads(candidate["manifest_path"].read_text(encoding="utf-8"))
                if manifest.get("image_analysis_revision") == revision:
                    return dict(record)
            next_version = self._next_version(source)
            target = self.packages_root / str(source["package_id"]) / f"v{next_version}"
            if target.exists():
                raise PackagePublicationError("PACKAGE_VERSION_CONFLICT", "目标模板版本已存在")
            try:
                manifest_path = self._bundler(source["local_path"], target, package_version=next_version)
                built = load_asset_package(manifest_path)
                index_path = built["index_path"]
                original_mode = stat.S_IMODE(index_path.stat().st_mode)
                # Published SQLite packages may carry read-only WAL/SHM files.
                # Snapshot the copied database through SQLite, then edit only
                # that consistent candidate snapshot with no inherited sidecars.
                snapshot_path = target / ".analysis-index.sqlite3"
                reader = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
                writer = sqlite3.connect(snapshot_path)
                try:
                    reader.backup(writer)
                    writer.execute("PRAGMA journal_mode=DELETE")
                    writer.commit()
                finally:
                    writer.close()
                    reader.close()
                for suffix in ("-wal", "-shm"):
                    index_path.with_name(index_path.name + suffix).unlink(missing_ok=True)
                os.replace(snapshot_path, index_path)
                try:
                    with sqlite3.connect(index_path) as connection:
                        for row in rows:
                            _write_media_analysis(connection, row["media_id"], row["analysis"])
                        _refresh_media_searchable_text(connection, source["package_id"])
                        _rebuild_fts(connection)
                finally:
                    index_path.chmod(original_mode)
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest.update({"index_sha256": sha256_file(built["index_path"]), "image_analysis_revision": revision})
                write_json_atomic(manifest_path, manifest)
                record = build_package_record(manifest_path)
                self.registry.register_records([record])
                return record
            except Exception:
                if target.exists():
                    shutil.rmtree(target)
                raise

    def _materialize_imports(
        self,
        manifest_path: str | Path,
        *,
        import_record: dict,
        items: list[dict],
    ) -> None:
        package = load_asset_package(manifest_path)
        root = Path(package["package_root"])
        materialized: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for item in items:
            digest = str(item.get("content_sha256") or "")
            kind = str(item.get("asset_kind") or "")
            key = (kind, digest)
            relative = ""
            if item.get("validation_status") == "valid" and key not in seen:
                suffix = Path(str(item.get("filename") or "")).suffix.casefold()
                relative = f"imports/{kind}/{digest[:2]}/{digest}{suffix}"
                self.storage.materialize(item["storage_ref"], root / relative)
                seen.add(key)
            elif item.get("duplicate_of"):
                relative = self._existing_relative_path(package, str(item["duplicate_of"]))
            materialized.append({
                "import_id": str(import_record["import_id"]),
                "position": int(item["position"]),
                "asset_id": str(item["asset_id"]),
                "asset_kind": kind,
                "filename": str(item["filename"]),
                "content_sha256": digest,
                "artifact_ref": str(item["artifact_ref"]),
                "scope": str(import_record["scope"]),
                "target_project_id": str(import_record.get("target_project_id") or ""),
                "materialized_path": relative,
            })

        index_path = Path(package["index_path"])
        connection = sqlite3.connect(index_path)
        try:
            connection.row_factory = sqlite3.Row
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(media_assets)")}
            for item in materialized:
                if item["asset_kind"] != "image" or not item["materialized_path"]:
                    continue
                path = root / item["materialized_path"]
                stat = path.stat()
                values = {
                    "media_id": item["asset_id"],
                    "package_id": package["package_id"],
                    "content_sha256": item["content_sha256"],
                    "current_path": item["materialized_path"],
                    "filename": item["filename"],
                    "extension": path.suffix.casefold(),
                    "size_bytes": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    "searchable_text": item["filename"].rsplit(".", 1)[0].replace("_", " ").replace("-", " "),
                    "review_status": "human_reviewed",
                    "storage_ref": f"sha256://{item['content_sha256']}",
                    "updated_at": self._clock(),
                }
                present = [name for name in values if name in columns]
                placeholders = ",".join("?" for _ in present)
                connection.execute(
                    f"INSERT INTO media_assets({','.join(present)}) VALUES({placeholders}) "
                    "ON CONFLICT(media_id) DO UPDATE SET "
                    + ",".join(f"{name}=excluded.{name}" for name in present if name != "media_id"),
                    [values[name] for name in present],
                )
                connection.execute(
                    "INSERT INTO media_locations(path, media_id, size_bytes, mtime_ns, last_seen_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET media_id=excluded.media_id, "
                    "size_bytes=excluded.size_bytes, mtime_ns=excluded.mtime_ns, last_seen_at=excluded.last_seen_at",
                    (item["materialized_path"], item["asset_id"], stat.st_size, stat.st_mtime_ns, self._clock()),
                )
            connection.commit()
            connection.execute("INSERT INTO media_fts(media_fts) VALUES('rebuild')")
            connection.commit()
        finally:
            connection.close()

        blob_manifest = str(package.get("blob_manifest") or "blob_manifest.jsonl")
        blob_path = root / blob_manifest
        existing = [
            json.loads(line) for line in blob_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ] if blob_path.is_file() else []
        known = {str(row.get("content_sha256") or "") for row in existing}
        for item in materialized:
            if item["asset_kind"] != "image" or not item["materialized_path"] or item["content_sha256"] in known:
                continue
            path = root / item["materialized_path"]
            existing.append({
                "media_id": item["asset_id"],
                "filename": item["filename"],
                "content_sha256": item["content_sha256"],
                "storage_ref": f"sha256://{item['content_sha256']}",
                "path": item["materialized_path"],
                "size_bytes": path.stat().st_size,
            })
            known.add(item["content_sha256"])
        blob_path.write_text(
            "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in existing),
            encoding="utf-8",
        )

        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        manifest["imported_assets"] = materialized
        manifest["imported_asset_count"] = len(materialized)
        manifest["updated_at"] = self._clock()
        manifest["index_sha256"] = sha256_file(index_path)
        manifest["blob_count"] = len(existing)
        manifest["blob_manifest_sha256"] = sha256_file(blob_path)
        write_json_atomic(manifest_path, manifest)

    @staticmethod
    def _existing_relative_path(package: dict, asset_id: str) -> str:
        connection = sqlite3.connect(package["index_path"])
        try:
            row = connection.execute(
                "SELECT current_path FROM media_assets WHERE media_id=?", (asset_id,)
            ).fetchone()
        finally:
            connection.close()
        return str(row[0]) if row and row[0] else ""


def _template_tone_options(package: dict) -> list[dict]:
    """Expose explicit template style choices with their original cell evidence."""
    semantics = package.get("source_semantics") or {}
    options, seen = [], set()
    for sheet in semantics.get("sheets", []):
        if sheet.get("source_sheet") != "模版":
            continue
        for cell in sheet.get("overview_cells", []):
            active = False
            for raw in str(cell.get("text") or "").splitlines():
                line = raw.strip()
                heading = re.match(r"^(?:\d+[、.．）)]\s*)?(?:风格类型|风格调性|风格|调性|情绪基调|画面色调)(?:[（(][^）)]*[）)])?\s*[：:]\s*(.*)$", line)
                if heading:
                    active = True
                    line = heading.group(1).strip()
                    if not line:
                        continue
                elif re.match(r"^\d+[、.．）)]", line):
                    active = False
                if not active or not line:
                    continue
                value = re.sub(r"^[•●·\-]\s*", "", line).strip()
                if value in seen:
                    continue
                seen.add(value)
                label = re.split(r"[：:]", value, maxsplit=1)[0]
                source = {"source_file": semantics.get("source_file", ""),
                          "source_sheet": sheet["source_sheet"], "source_cell": cell.get("cell", ""),
                          "source_sha256": semantics.get("workbook_sha256", ""), "quote": raw.strip()}
                options.append({"value": value, "label": label, "description": value[len(label):].lstrip("：:"),
                    "source_label": " / ".join(filter(None, (source["source_file"], source["source_sheet"], source["source_cell"]))),
                    "source_ref": f"{package['package_id']}@{package['package_version']}", "evidence_refs": [source]})
    return options


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
