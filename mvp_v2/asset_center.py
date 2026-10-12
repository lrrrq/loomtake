from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Literal, Optional, Sequence
from urllib.parse import quote
import uuid
from zipfile import BadZipFile, ZipFile

from PIL import Image as PILImage, UnidentifiedImageError

from .asset_package import (
    PACKAGE_SCHEMA_VERSION,
    load_asset_package,
    search_asset_package,
    asset_search_queries,
)
from .asset_storage import AssetStorageError, LocalAssetStorage
from .excel_io import IMAGE_EXTENSIONS
from .package_registry import LocalPackageRegistry, PackageRegistryError
from .package_publication import AssetPackagePublicationService, PackagePublicationError


AssetKind = Literal["image", "workbook"]
ImportScope = Literal["project", "shared"]

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_ARTIFACT_REF_PATTERN = re.compile(r"^artifact://[^\s\\]+$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_XLSX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


class AssetCenterError(ValueError):
    def __init__(self, code: str, message: str = ""):
        self.code = str(code)
        super().__init__(f"{self.code}: {message}" if message else self.code)


@dataclass(frozen=True)
class AssetImportFile:
    artifact_ref: str
    storage_ref: str
    content_sha256: str
    filename: str
    asset_kind: AssetKind


class AssetCenterService:
    """现有资产包索引上的导入审核层，不复制资产或调用视觉 Provider。"""

    def __init__(
        self,
        package: str | Path,
        *,
        storage: LocalAssetStorage,
        clock: Optional[Callable[[], str]] = None,
        package_registry_path: str | Path | None = None,
        packages_root: str | Path | None = None,
        import_catalog_path: str | Path | None = None,
    ):
        self.package_manifest = Path(package).expanduser().resolve()
        self.storage = storage
        self._clock = clock or _utc_now
        loaded = load_asset_package(self.package_manifest)
        discovered_registry = package_registry_path or _discover_package_registry(
            self.package_manifest
        )
        self.package_registry = (
            LocalPackageRegistry(discovered_registry)
            if discovered_registry else None
        )
        if self.package_registry:
            self.catalog_path = Path(
                import_catalog_path
                or Path(self.package_registry.path).parent / "asset_imports.sqlite3"
            ).expanduser().resolve()
            self.package_publication = AssetPackagePublicationService(
                storage=storage,
                registry=self.package_registry,
                packages_root=(
                    packages_root
                    or Path(self.package_registry.path).parent / "packages"
                ),
                clock=self._clock,
            )
        else:
            self.catalog_path = Path(import_catalog_path or loaded["index_path"])
            self.package_publication = None

    def stage_import(
        self,
        *,
        import_id: str,
        category_id: str,
        scope: ImportScope,
        project_id: Optional[str] = None,
        files: Sequence[AssetImportFile],
        requested_by_actor_id: str,
        expected_package_version: int,
    ) -> dict:
        package = self._write_package(expected_package_version)
        target_project_id = _validate_scope_target(scope, project_id)
        normalized_files = self._validate_request(
            import_id=import_id,
            category_id=category_id,
            scope=scope,
            files=files,
            actor_id=requested_by_actor_id,
            package=package,
        )
        request_sha256 = _request_sha256(
            category_id,
            scope,
            target_project_id,
            normalized_files,
        )
        with self._connect() as connection:
            self._ensure_schema(connection)
            existing = connection.execute(
                "SELECT request_sha256 FROM asset_imports WHERE import_id=?",
                (import_id,),
            ).fetchone()
            if existing:
                if str(existing[0]) != request_sha256:
                    raise AssetCenterError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一 import_id 不得提交不同文件",
                    )
                return self._get_import(connection, import_id)

            inspected = self._inspect_files(connection, normalized_files, package)
            machine_status = "passed" if all(item["validation_status"] != "failed" for item in inspected) else "failed"
            last_error_code = next(
                (str(item["error_code"]) for item in inspected if item.get("error_code")),
                "",
            )
            now = self._clock()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO asset_imports(
                       import_id, package_id, package_version, category_id, scope,
                       target_project_id,
                       state, revision, request_sha256, machine_review_status,
                       last_error_code, requested_by_actor_id, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,'staged',1,?,?,?,?,?,?)""",
                (
                    import_id,
                    package["package_id"],
                    self._package_version(package),
                    category_id,
                    scope,
                    target_project_id,
                    request_sha256,
                    machine_status,
                    last_error_code,
                    requested_by_actor_id,
                    now,
                    now,
                ),
            )
            for position, item in enumerate(inspected, 1):
                connection.execute(
                    """INSERT INTO asset_import_items(
                           import_id, position, asset_id, asset_kind, artifact_ref,
                           storage_ref, content_sha256, filename, searchable_text,
                           validation_status, duplicate_of, validation_json, availability
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'unavailable')""",
                    (
                        import_id,
                        position,
                        item["asset_id"],
                        item["asset_kind"],
                        item["artifact_ref"],
                        item["storage_ref"],
                        item["content_sha256"],
                        item["filename"],
                        _searchable_filename(item["filename"]),
                        item["validation_status"],
                        item.get("duplicate_of") or "",
                        json.dumps(item["validation"], ensure_ascii=False, sort_keys=True),
                    ),
                )
            self._append_event(
                connection,
                import_id=import_id,
                event_type="asset_import.staged",
                actor_id=requested_by_actor_id,
                payload={
                    "machine_review_status": machine_status,
                    "item_count": len(inspected),
                    "duplicate_count": sum(item["validation_status"] == "duplicate" for item in inspected),
                },
            )
            if machine_status == "failed":
                self._append_event(
                    connection,
                    import_id=import_id,
                    event_type="asset_import.preflight_failed",
                    actor_id=requested_by_actor_id,
                    payload={"error_code": last_error_code},
                )
            connection.commit()
            return self._get_import(connection, import_id)

    def retry_preflight(
        self,
        import_id: str,
        *,
        requested_by_actor_id: str,
        expected_revision: int,
    ) -> dict:
        _validate_entity_id(import_id, "import_id")
        _validate_entity_id(requested_by_actor_id, "requested_by_actor_id")
        package = self._write_package(None)
        with self._connect() as connection:
            self._ensure_schema(connection)
            record = self._require_import(connection, import_id)
            self._require_package_identity(record, package)
            self._require_revision(record, expected_revision)
            if record["state"] != "staged":
                raise AssetCenterError("INVALID_STATE", "只有 staged 导入可重试预检")
            files = [
                AssetImportFile(
                    artifact_ref=str(row["artifact_ref"]),
                    storage_ref=str(row["storage_ref"]),
                    content_sha256=str(row["content_sha256"]),
                    filename=str(row["filename"]),
                    asset_kind=str(row["asset_kind"]),
                )
                for row in connection.execute(
                    "SELECT * FROM asset_import_items WHERE import_id=? ORDER BY position",
                    (import_id,),
                ).fetchall()
            ]
            inspected = self._inspect_files(
                connection,
                files,
                package,
                exclude_import_id=import_id,
            )
            machine_status = "passed" if all(item["validation_status"] != "failed" for item in inspected) else "failed"
            last_error_code = next(
                (str(item["error_code"]) for item in inspected if item.get("error_code")),
                "",
            )
            new_revision = int(record["revision"]) + 1
            connection.execute("BEGIN IMMEDIATE")
            for position, item in enumerate(inspected, 1):
                connection.execute(
                    """UPDATE asset_import_items
                       SET asset_id=?, validation_status=?, duplicate_of=?, validation_json=?
                       WHERE import_id=? AND position=?""",
                    (
                        item["asset_id"],
                        item["validation_status"],
                        item.get("duplicate_of") or "",
                        json.dumps(item["validation"], ensure_ascii=False, sort_keys=True),
                        import_id,
                        position,
                    ),
                )
            updated = connection.execute(
                """UPDATE asset_imports SET machine_review_status=?, last_error_code=?,
                       revision=?, updated_at=? WHERE import_id=? AND revision=? AND state='staged'""",
                (
                    machine_status,
                    last_error_code,
                    new_revision,
                    self._clock(),
                    import_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise AssetCenterError("PACKAGE_VERSION_CONFLICT", "导入修订版本已变化")
            self._append_event(
                connection,
                import_id=import_id,
                event_type="asset_import.preflight_retried",
                actor_id=requested_by_actor_id,
                payload={"machine_review_status": machine_status, "revision": new_revision},
            )
            if machine_status == "failed":
                self._append_event(
                    connection,
                    import_id=import_id,
                    event_type="asset_import.preflight_failed",
                    actor_id=requested_by_actor_id,
                    payload={"error_code": last_error_code},
                )
            connection.commit()
            return self._get_import(connection, import_id)

    def review_import(
        self,
        import_id: str,
        *,
        reviewed_by_actor_id: str,
        expected_revision: int,
    ) -> dict:
        return self._transition(
            import_id,
            actor_id=reviewed_by_actor_id,
            expected_revision=expected_revision,
            from_state="staged",
            to_state="reviewed",
            event_type="asset_import.reviewed",
        )

    def bind_publication_command(self, import_id: str, command: dict) -> dict:
        """Bind a verified human command before its workspace token is consumed."""
        self._validate_publication_command(command)
        with self._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            record = self._require_import(connection, import_id)
            prepared = self._publication_stage(connection, import_id, command, "prepared")
            if prepared is not None:
                return prepared
            self._require_revision(record, command["expected_revision"])
            if record["state"] != "reviewed" or record["machine_review_status"] != "passed":
                raise AssetCenterError("REVIEW_REQUIRED", "发布命令要求已通过审核的导入")
            binding = {"package_id": record["package_id"], "package_version": record["package_version"],
                "category_id": record["category_id"], "scope": record["scope"],
                "target_project_id": record["target_project_id"]}
            self._append_publication_stage(connection, import_id, command, "prepared", binding)
            connection.commit()
            return binding

    def publication_stage(self, import_id: str, command: dict, stage: str) -> dict | None:
        self._validate_publication_command(command)
        with self._connect() as connection:
            self._require_schema(connection)
            self._require_import(connection, import_id)
            return self._publication_stage(connection, import_id, command, stage)

    def record_publication_stage(self, import_id: str, command: dict, stage: str, data: dict) -> dict:
        if stage not in {"planned", "built"}:
            raise AssetCenterError("INVALID_INPUT", "发布阶段只能由对应领域事务登记")
        self._validate_publication_command(command)
        with self._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            if self._publication_stage(connection, import_id, command, "prepared") is None:
                raise AssetCenterError("PUBLICATION_RECONCILIATION_REQUIRED", "发布命令缺少持久关联")
            previous = self._publication_stage(connection, import_id, command, stage)
            if previous is not None:
                if previous != data:
                    raise AssetCenterError("PUBLICATION_RECONCILIATION_REQUIRED", "原发布阶段已绑定其他内容")
                return previous
            self._append_publication_stage(connection, import_id, command, stage, data)
            connection.commit()
            return data

    @staticmethod
    def _validate_publication_command(command):
        fields = {"command_id", "request_sha256", "actor_id", "workspace_id", "project_id", "expected_revision"}
        if not isinstance(command, dict) or set(command) != fields:
            raise AssetCenterError("INVALID_INPUT", "发布命令关联字段不完整")
        for name in ("command_id", "actor_id", "workspace_id", "project_id"):
            _validate_entity_id(command[name], name)
        if (not _SHA256_PATTERN.fullmatch(str(command["request_sha256"]))
                or type(command["expected_revision"]) is not int or command["expected_revision"] < 0):
            raise AssetCenterError("INVALID_INPUT", "发布命令摘要或修订无效")

    def _publication_stage(self, connection, import_id, command, stage):
        rows = connection.execute("""SELECT payload_json FROM asset_import_events
            WHERE import_id=? AND event_type='asset_publication.prepared' ORDER BY sequence_no""",
            (import_id,)).fetchall()
        for row in rows:
            if json.loads(row["payload_json"])["command"] != command:
                raise AssetCenterError("PUBLICATION_COMMAND_CONFLICT", "导入已绑定另一发布命令或不同请求")
        row = connection.execute("""SELECT payload_json FROM asset_import_events
            WHERE import_id=? AND event_type=? ORDER BY sequence_no DESC LIMIT 1""",
            (import_id, "asset_publication." + stage)).fetchone()
        if row is None:
            return None
        value = json.loads(row["payload_json"])
        if value.get("command") != command:
            raise AssetCenterError("PUBLICATION_COMMAND_CONFLICT", "发布阶段不属于原请求")
        return value["data"]

    def _append_publication_stage(self, connection, import_id, command, stage, data):
        self._append_event(connection, import_id=import_id, event_type="asset_publication." + stage,
            actor_id=command["actor_id"], payload={"command": command, "data": data})

    def publish_import(
        self,
        import_id: str,
        *,
        published_by_actor_id: str,
        expected_revision: int,
        publication_command: dict | None = None,
    ) -> dict:
        if publication_command is not None:
            if (publication_command.get("actor_id") != published_by_actor_id
                    or publication_command.get("expected_revision") != expected_revision):
                raise AssetCenterError("PUBLICATION_COMMAND_CONFLICT", "发布人与原命令不一致")
            self.bind_publication_command(import_id, publication_command)
            receipt = self.publication_stage(import_id, publication_command, "completed")
            if receipt is not None:
                return receipt
        else:
            with self._connect() as connection:
                self._require_schema(connection)
                if connection.execute("SELECT 1 FROM asset_import_events WHERE import_id=? AND event_type='asset_publication.prepared'",
                        (import_id,)).fetchone():
                    raise AssetCenterError("PUBLICATION_COMMAND_CONFLICT", "此导入已绑定人工发布命令，请恢复原操作")
        if self.package_publication:
            return self._publish_import(
                import_id,
                published_by_actor_id=published_by_actor_id,
                expected_revision=expected_revision,
                publication_command=publication_command,
            )
        return self._transition(
            import_id,
            actor_id=published_by_actor_id,
            expected_revision=expected_revision,
            from_state="reviewed",
            to_state="published",
            event_type="asset_import.published",
            publication_command=publication_command,
        )

    def _publish_import(
        self,
        import_id: str,
        *,
        published_by_actor_id: str,
        expected_revision: int,
        publication_command: dict | None = None,
    ) -> dict:
        _validate_entity_id(import_id, "import_id")
        _validate_entity_id(published_by_actor_id, "published_by_actor_id")
        with self._connect() as connection:
            self._require_schema(connection)
            record = self._require_import(connection, import_id)
            self._require_revision(record, expected_revision)
            if record["state"] != "reviewed":
                raise AssetCenterError("INVALID_STATE", "发布要求当前状态为 reviewed")
            if record["machine_review_status"] != "passed":
                raise AssetCenterError("MACHINE_REVIEW_FAILED", "机器预检未通过，不能发布")
            items = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM asset_import_items WHERE import_id=? ORDER BY position",
                    (import_id,),
                ).fetchall()
            ]
            import_payload = self._get_import(connection, import_id)
        try:
            return self.package_publication.publish(
                import_record=import_payload,
                items=items,
                published_by_actor_id=published_by_actor_id,
                expected_revision=expected_revision,
                publication_command=publication_command,
                read_stage=lambda stage: self.publication_stage(import_id, publication_command, stage),
                record_stage=lambda stage, data: self.record_publication_stage(import_id, publication_command, stage, data),
                finalize=lambda context: self._transition(
                    import_id,
                    actor_id=context["published_by_actor_id"],
                    expected_revision=context["expected_revision"],
                    from_state="reviewed",
                    to_state="published",
                    event_type="asset_import.published",
                    package=context["source_package"],
                    published_package=context["package_record"],
                    publication_command=publication_command,
                    publication_result=context.get("publication_result"),
                ),
            )
        except PackagePublicationError as exc:
            raise AssetCenterError(exc.code, str(exc)) from exc

    def get_import(self, import_id: str) -> dict:
        _validate_entity_id(import_id, "import_id")
        with self._connect() as connection:
            self._require_schema(connection)
            return self._get_import(connection, import_id)

    def list_events(self, import_id: str) -> list[dict]:
        _validate_entity_id(import_id, "import_id")
        with self._connect() as connection:
            self._require_schema(connection)
            rows = connection.execute(
                """SELECT event_id, event_type, actor_id, payload_json, created_at
                   FROM asset_import_events WHERE import_id=? ORDER BY sequence_no""",
                (import_id,),
            ).fetchall()
            return [
                {
                    "event_id": str(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "actor_id": str(row["actor_id"]),
                    "payload": json.loads(str(row["payload_json"])),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

    def search_assets(
        self,
        query: str,
        *,
        kind: Literal["all", "script", "image", "workbook"] = "all",
        current_project_id: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict]:
        if kind not in {"all", "script", "image", "workbook"}:
            raise AssetCenterError("INVALID_INPUT", "未知资产类型")
        if limit < 1 or limit > 200:
            raise AssetCenterError("INVALID_INPUT", "limit 必须在 1 到 200 之间")
        if current_project_id is not None:
            _validate_entity_id(current_project_id, "current_project_id")
        package = self._write_package(None)
        package_path = package["manifest_path"]
        results = []
        if kind in {"all", "script"}:
            results.extend(
                self._normalize_package_results(
                    search_asset_package(package_path, query, kind="script", limit=limit),
                    package,
                    kind="script",
                )
            )
        if kind in {"all", "image"}:
            results.extend(
                self._normalize_package_results(
                    search_asset_package(package_path, query, kind="media", limit=limit),
                    package,
                    kind="image",
                )
            )
        if kind in {"all", "image", "workbook"}:
            results.extend(
                self._search_imports(
                    query,
                    package,
                    kind=kind,
                    current_project_id=current_project_id,
                    limit=limit,
                )
            )
        unique = {}
        for item in results:
            identity = (
                str(item["asset_kind"]),
                str(item.get("content_sha256") or item["asset_id"]),
            )
            unique.setdefault(identity, item)
        ordered = sorted(unique.values(), key=lambda item: (item["asset_kind"], item["asset_id"]))
        return ordered[:limit]

    def _transition(
        self,
        import_id: str,
        *,
        actor_id: str,
        expected_revision: int,
        from_state: str,
        to_state: str,
        event_type: str,
        package: dict | None = None,
        published_package: dict | None = None,
        publication_command: dict | None = None,
        publication_result: dict | None = None,
    ) -> dict:
        _validate_entity_id(import_id, "import_id")
        _validate_entity_id(actor_id, "actor_id")
        package = package or self._write_package(None)
        with self._connect() as connection:
            self._require_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            if publication_command is not None:
                receipt = self._publication_stage(connection, import_id, publication_command, "completed")
                if receipt is not None:
                    return receipt
                if self._publication_stage(connection, import_id, publication_command, "prepared") is None:
                    raise AssetCenterError("PUBLICATION_RECONCILIATION_REQUIRED", "发布命令缺少持久关联")
            record = self._require_import(connection, import_id)
            self._require_package_identity(record, package)
            self._require_revision(record, expected_revision)
            if record["state"] != from_state:
                code = "REVIEW_REQUIRED" if to_state == "published" else "INVALID_STATE"
                raise AssetCenterError(code, f"{to_state} 要求当前状态为 {from_state}")
            if record["machine_review_status"] != "passed":
                raise AssetCenterError("MACHINE_REVIEW_FAILED", "机器预检未通过，不能进入人工审核")
            new_revision = int(record["revision"]) + 1
            now = self._clock()
            reviewer_field = "reviewed_by_actor_id" if to_state == "reviewed" else "published_by_actor_id"
            time_field = "reviewed_at" if to_state == "reviewed" else "published_at"
            availability = "awaiting_publish" if to_state == "reviewed" else "available"
            updated = connection.execute(
                f"""UPDATE asset_imports SET state=?, revision=?, {reviewer_field}=?,
                       {time_field}=?, package_id=COALESCE(?, package_id),
                       package_version=COALESCE(?, package_version), updated_at=?
                    WHERE import_id=? AND revision=? AND state=?""",
                (
                    to_state,
                    new_revision,
                    actor_id,
                    now,
                    (published_package or {}).get("package_id"),
                    (published_package or {}).get("package_version"),
                    now,
                    import_id,
                    expected_revision,
                    from_state,
                ),
            )
            if updated.rowcount != 1:
                connection.rollback()
                raise AssetCenterError("PACKAGE_VERSION_CONFLICT", "导入修订版本已变化")
            connection.execute(
                "UPDATE asset_import_items SET availability=? WHERE import_id=?",
                (availability, import_id),
            )
            self._append_event(
                connection,
                import_id=import_id,
                event_type=event_type,
                actor_id=actor_id,
                payload={"from_state": from_state, "to_state": to_state, "revision": new_revision},
            )
            result = {**self._get_import(connection, import_id), **(publication_result or {})}
            if publication_command is not None:
                self._append_publication_stage(connection, import_id, publication_command, "completed", result)
            connection.commit()
            return result

    def _write_package(self, expected_version: Optional[int]) -> dict:
        package = self._current_package()
        if not self.package_registry and package.get("schema_version") != PACKAGE_SCHEMA_VERSION:
            raise AssetCenterError(
                "IMMUTABLE_PACKAGE_VERSION",
                "已发布 v2 包不可原地导入；应在可更新源包审核后构建新版本",
            )
        actual_version = self._package_version(package)
        if expected_version is not None and int(expected_version) != actual_version:
            raise AssetCenterError(
                "PACKAGE_VERSION_CONFLICT",
                f"期望版本 {expected_version}，实际版本 {actual_version}",
            )
        return package

    def _current_package(self) -> dict:
        if not self.package_registry:
            return load_asset_package(self.package_manifest)
        try:
            base = load_asset_package(self.package_manifest)
            resolved = self.package_registry.resolve_formal(
                str(base.get("category_id") or "")
            )
            return load_asset_package(resolved["local_path"])
        except (PackageRegistryError, OSError, ValueError) as exc:
            raise AssetCenterError("PACKAGE_REGISTRY_UNAVAILABLE", str(exc)) from exc

    @staticmethod
    def _package_version(package: dict) -> int:
        return int(package.get("package_version") or 0)

    def _validate_request(
        self,
        *,
        import_id: str,
        category_id: str,
        scope: str,
        files: Sequence[AssetImportFile],
        actor_id: str,
        package: dict,
    ) -> list[AssetImportFile]:
        _validate_entity_id(import_id, "import_id")
        _validate_entity_id(actor_id, "requested_by_actor_id")
        if category_id != str(package.get("category_id") or ""):
            raise AssetCenterError("INVALID_INPUT", "导入类别与资产包类别不一致")
        if scope not in {"project", "shared"}:
            raise AssetCenterError("INVALID_INPUT", "scope 仅支持 project/shared")
        if not files or len(files) > 500:
            raise AssetCenterError("INVALID_INPUT", "每次必须导入 1 到 500 个文件")
        normalized = []
        for item in files:
            if not isinstance(item, AssetImportFile):
                raise AssetCenterError("INVALID_INPUT", "files 必须为 AssetImportFile")
            artifact_ref = str(item.artifact_ref or "").strip()
            if (
                not _ARTIFACT_REF_PATTERN.fullmatch(artifact_ref)
                or artifact_ref.startswith("artifact:///")
                or ".." in artifact_ref.split("//", 1)[1].split("/")
            ):
                raise AssetCenterError("INVALID_INPUT", "artifact_ref 必须是非路径逻辑引用")
            storage_ref = str(item.storage_ref or "").strip().casefold()
            digest = str(item.content_sha256 or "").strip().casefold()
            if not storage_ref.startswith("sha256://") or not _SHA256_PATTERN.fullmatch(digest):
                raise AssetCenterError("INVALID_INPUT", "storage_ref/content_sha256 无效")
            if storage_ref != f"sha256://{digest}":
                raise AssetCenterError("INVALID_INPUT", "storage_ref 与 content_sha256 不一致")
            filename = str(item.filename or "").strip()
            if not filename or Path(filename).name != filename or "/" in filename or "\\" in filename:
                raise AssetCenterError("INVALID_INPUT", "filename 只能是文件名，不能包含路径")
            asset_kind = str(item.asset_kind or "")
            suffix = Path(filename).suffix.casefold()
            if asset_kind == "image" and suffix not in IMAGE_EXTENSIONS:
                raise AssetCenterError("INVALID_INPUT", "图片扩展名不受支持")
            if asset_kind == "workbook" and suffix != ".xlsx":
                raise AssetCenterError("INVALID_INPUT", "工作簿仅支持 .xlsx")
            if asset_kind not in {"image", "workbook"}:
                raise AssetCenterError("INVALID_INPUT", "仅支持 image/workbook")
            normalized.append(AssetImportFile(
                artifact_ref=artifact_ref,
                storage_ref=storage_ref,
                content_sha256=digest,
                filename=filename,
                asset_kind=asset_kind,
            ))
        return normalized

    def _inspect_file(
        self,
        connection: sqlite3.Connection,
        item: AssetImportFile,
        package: dict,
        *,
        exclude_import_id: str = "",
    ) -> dict:
        asset_id = (
            f"media_{item.content_sha256[:20]}"
            if item.asset_kind == "image"
            else f"workbook_{item.content_sha256[:20]}"
        )
        result = {
            **item.__dict__,
            "asset_id": asset_id,
            "validation_status": "valid",
            "duplicate_of": "",
            "error_code": "",
            "validation": {},
        }
        try:
            self.storage.verify(item.storage_ref, item.content_sha256)
            with self.storage.open(item.storage_ref) as handle:
                content = handle.read()
            if item.asset_kind == "image":
                result["validation"] = _validate_image(content)
            else:
                result["validation"] = _validate_xlsx(content)
            duplicate_of = self._find_duplicate(
                connection,
                item,
                package,
                exclude_import_id=exclude_import_id,
            )
            if duplicate_of:
                result["validation_status"] = "duplicate"
                result["duplicate_of"] = duplicate_of
        except (AssetStorageError, AssetCenterError) as exc:
            code = exc.code if isinstance(exc, AssetCenterError) else str(exc)
            result["validation_status"] = "failed"
            result["error_code"] = code
            result["validation"] = {"error_code": code}
        return result

    def _inspect_files(
        self,
        connection: sqlite3.Connection,
        files: Sequence[AssetImportFile],
        package: dict,
        *,
        exclude_import_id: str = "",
    ) -> list[dict]:
        inspected = []
        seen: dict[tuple[str, str], str] = {}
        for item in files:
            result = self._inspect_file(
                connection,
                item,
                package,
                exclude_import_id=exclude_import_id,
            )
            key = (item.asset_kind, item.content_sha256)
            if result["validation_status"] != "failed" and key in seen:
                result["validation_status"] = "duplicate"
                result["duplicate_of"] = seen[key]
            elif result["validation_status"] != "failed":
                seen[key] = str(result["duplicate_of"] or result["asset_id"])
            inspected.append(result)
        return inspected

    def _find_duplicate(
        self,
        connection: sqlite3.Connection,
        item: AssetImportFile,
        package: dict,
        *,
        exclude_import_id: str,
    ) -> str:
        if item.asset_kind == "image":
            package_connection = sqlite3.connect(package["index_path"])
            try:
                row = package_connection.execute(
                    "SELECT media_id FROM media_assets WHERE content_sha256=? LIMIT 1",
                    (item.content_sha256,),
                ).fetchone()
            finally:
                package_connection.close()
            if row:
                return str(row[0])
        elif str(package.get("source_workbook_sha256") or "") == item.content_sha256:
            return f"workbook_{item.content_sha256[:20]}"
        if _table_exists(connection, "asset_import_items"):
            sql = (
                "SELECT items.asset_id FROM asset_import_items AS items "
                "JOIN asset_imports AS imports ON imports.import_id=items.import_id "
                "WHERE items.content_sha256=? AND items.asset_kind=? "
                "AND items.validation_status<>'failed' "
                "AND imports.machine_review_status='passed'"
            )
            parameters = [item.content_sha256, item.asset_kind]
            if exclude_import_id:
                sql += " AND items.import_id<>?"
                parameters.append(exclude_import_id)
            sql += " ORDER BY items.import_id LIMIT 1"
            row = connection.execute(sql, parameters).fetchone()
            if row:
                return str(row[0])
        return ""

    def _search_imports(
        self,
        query: str,
        package: dict,
        *,
        kind: str,
        current_project_id: Optional[str],
        limit: int,
    ) -> list[dict]:
        with self._connect() as connection:
            if not _table_exists(connection, "asset_import_items"):
                return []
            where = []
            parameters = []
            if kind in {"image", "workbook"}:
                where.append("items.asset_kind=?")
                parameters.append(kind)
            where.append("imports.state='published'")
            if current_project_id is None:
                where.append("imports.scope='shared'")
            else:
                where.append(
                    "(imports.scope='shared' OR "
                    "(imports.scope='project' AND imports.target_project_id=?))"
                )
                parameters.append(current_project_id)
            normalized_query = _searchable_filename(query)
            if normalized_query:
                queries = asset_search_queries(normalized_query)
                where.append("(" + " OR ".join("items.searchable_text LIKE ?" for _ in queries) + ")")
                parameters.extend(f"%{value}%" for value in queries)
            clause = " AND ".join(where) if where else "1=1"
            parameters.append(limit)
            rows = connection.execute(
                f"""SELECT items.*, imports.package_id, imports.package_version,
                           imports.state AS import_state, imports.scope AS import_scope,
                           imports.target_project_id
                    FROM asset_import_items AS items
                    JOIN asset_imports AS imports ON imports.import_id=items.import_id
                    WHERE {clause}
                    ORDER BY items.filename, items.asset_id LIMIT ?""",
                parameters,
            ).fetchall()
            return [
                {
                    "asset_id": str(row["asset_id"]),
                    "asset_kind": str(row["asset_kind"]),
                    "title": str(row["filename"]),
                    "content_sha256": str(row["content_sha256"]),
                    "storage_ref": str(row["storage_ref"]),
                    "availability": str(row["availability"]),
                    "workflow_state": str(row["import_state"]),
                    "source": {
                        "artifact_ref": str(row["artifact_ref"]),
                        "package_id": str(row["package_id"]),
                        "package_version": int(row["package_version"]),
                        "import_id": str(row["import_id"]),
                        "scope": str(row["import_scope"]),
                        "target_project_id": str(row["target_project_id"] or ""),
                    },
                }
                for row in rows
            ]

    def _normalize_package_results(self, rows: list[dict], package: dict, *, kind: str) -> list[dict]:
        version = self._package_version(package)
        package_id = str(package["package_id"])
        normalized = []
        for row in rows:
            asset_id = str(row["asset_id"] if kind == "script" else row["media_id"])
            safe_package_id = quote(package_id, safe="A-Za-z0-9._~:-")
            source = {
                "artifact_ref": (
                    f"artifact://asset-package/{safe_package_id}/v{version}/{kind}/"
                    f"{quote(asset_id, safe='A-Za-z0-9._~:-')}"
                ),
                "package_id": package_id,
                "package_version": version,
            }
            if kind == "script":
                source.update({
                    "source_file": _safe_source_name(row.get("source_file")),
                    "source_sheet": str(row.get("source_sheet") or ""),
                    "source_row": int(row.get("source_row") or 0),
                })
                title = str(row.get("visual_desc") or row.get("shot_no") or asset_id)
                digest = str(row.get("source_sha256") or "")
                storage_ref = ""
            else:
                source["source_links"] = [
                    {
                        "source_file": _safe_source_name(link.get("source_file")),
                        "source_sheet": str(link.get("source_sheet") or ""),
                        "source_row": int(link.get("source_row") or 0),
                    }
                    for link in row.get("source_links") or []
                ]
                title = str(row.get("filename") or asset_id)
                digest = str(row.get("content_sha256") or "")
                candidate_ref = str(row.get("storage_ref") or "")
                storage_ref = candidate_ref if candidate_ref.startswith("sha256://") else ""
            item = {
                "asset_id": asset_id,
                "asset_kind": kind,
                "title": title,
                "content_sha256": digest,
                "availability": "available",
                "workflow_state": str(package.get("status") or "active_source"),
                "source": source,
            }
            if kind == "script":
                item.update(digest_scope="source_record", source_record_sha256=digest,
                    packaged_payload_sha256=str(row.get("packaged_payload_sha256") or ""),
                    packaged_payload_verified=bool(row.get("packaged_payload_verified")))
            if storage_ref:
                item["storage_ref"] = storage_ref
            normalized.append(item)
        return normalized

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.catalog_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS asset_imports(
                import_id TEXT PRIMARY KEY,
                package_id TEXT NOT NULL,
                package_version INTEGER NOT NULL,
                category_id TEXT NOT NULL,
                scope TEXT NOT NULL CHECK(scope IN ('project', 'shared')),
                target_project_id TEXT,
                state TEXT NOT NULL CHECK(state IN ('staged', 'reviewed', 'published')),
                revision INTEGER NOT NULL,
                request_sha256 TEXT NOT NULL,
                machine_review_status TEXT NOT NULL CHECK(machine_review_status IN ('passed', 'failed')),
                last_error_code TEXT NOT NULL DEFAULT '',
                requested_by_actor_id TEXT NOT NULL,
                reviewed_by_actor_id TEXT,
                published_by_actor_id TEXT,
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                published_at TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS asset_import_items(
                import_id TEXT NOT NULL REFERENCES asset_imports(import_id) ON DELETE RESTRICT,
                position INTEGER NOT NULL,
                asset_id TEXT NOT NULL,
                asset_kind TEXT NOT NULL CHECK(asset_kind IN ('image', 'workbook')),
                artifact_ref TEXT NOT NULL,
                storage_ref TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                filename TEXT NOT NULL,
                searchable_text TEXT NOT NULL,
                validation_status TEXT NOT NULL CHECK(validation_status IN ('valid', 'duplicate', 'failed')),
                duplicate_of TEXT NOT NULL DEFAULT '',
                validation_json TEXT NOT NULL,
                availability TEXT NOT NULL CHECK(availability IN ('unavailable', 'awaiting_publish', 'available')),
                PRIMARY KEY(import_id, position)
            );
            CREATE INDEX IF NOT EXISTS asset_import_items_sha_idx
                ON asset_import_items(content_sha256, asset_kind);
            CREATE TABLE IF NOT EXISTS asset_import_events(
                sequence_no INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                import_id TEXT NOT NULL REFERENCES asset_imports(import_id) ON DELETE RESTRICT,
                event_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS asset_import_events_no_update
            BEFORE UPDATE ON asset_import_events
            BEGIN
                SELECT RAISE(ABORT, 'ASSET_AUDIT_IMMUTABLE');
            END;
            CREATE TRIGGER IF NOT EXISTS asset_import_events_no_delete
            BEFORE DELETE ON asset_import_events
            BEGIN
                SELECT RAISE(ABORT, 'ASSET_AUDIT_IMMUTABLE');
            END;
        """)
        import_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(asset_imports)").fetchall()
        }
        if "target_project_id" not in import_columns:
            connection.execute("ALTER TABLE asset_imports ADD COLUMN target_project_id TEXT")
        connection.commit()

    @staticmethod
    def _require_schema(connection: sqlite3.Connection) -> None:
        if not _table_exists(connection, "asset_imports"):
            raise AssetCenterError("NOT_FOUND", "资产中心尚无导入记录")

    @staticmethod
    def _require_import(connection: sqlite3.Connection, import_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM asset_imports WHERE import_id=?",
            (import_id,),
        ).fetchone()
        if not row:
            raise AssetCenterError("NOT_FOUND", "导入任务不存在")
        return row

    @staticmethod
    def _require_revision(record: sqlite3.Row, expected_revision: int) -> None:
        if int(record["revision"]) != int(expected_revision):
            raise AssetCenterError(
                "PACKAGE_VERSION_CONFLICT",
                f"导入修订版本已变化：期望 {expected_revision}，实际 {record['revision']}",
            )

    def _require_package_identity(self, record: sqlite3.Row, package: dict) -> None:
        if (
            str(record["package_id"]) != str(package.get("package_id") or "")
            or int(record["package_version"]) != self._package_version(package)
        ):
            raise AssetCenterError(
                "PACKAGE_VERSION_CONFLICT",
                "导入绑定的资产包版本已不是当前可更新版本",
            )

    def _get_import(self, connection: sqlite3.Connection, import_id: str) -> dict:
        row = self._require_import(connection, import_id)
        items = connection.execute(
            "SELECT * FROM asset_import_items WHERE import_id=? ORDER BY position",
            (import_id,),
        ).fetchall()
        return {
            "import_id": str(row["import_id"]),
            "package_id": str(row["package_id"]),
            "package_version": int(row["package_version"]),
            "category_id": str(row["category_id"]),
            "scope": str(row["scope"]),
            "target_project_id": str(row["target_project_id"] or ""),
            "state": str(row["state"]),
            "revision": int(row["revision"]),
            "machine_review_status": str(row["machine_review_status"]),
            "last_error_code": str(row["last_error_code"]),
            "items": [
                {
                    "position": int(item["position"]),
                    "asset_id": str(item["asset_id"]),
                    "asset_kind": str(item["asset_kind"]),
                    "artifact_ref": str(item["artifact_ref"]),
                    "storage_ref": str(item["storage_ref"]),
                    "content_sha256": str(item["content_sha256"]),
                    "filename": str(item["filename"]),
                    "validation_status": str(item["validation_status"]),
                    "duplicate_of": str(item["duplicate_of"]),
                    "validation": json.loads(str(item["validation_json"])),
                    "availability": str(item["availability"]),
                }
                for item in items
            ],
        }

    def _append_event(
        self,
        connection: sqlite3.Connection,
        *,
        import_id: str,
        event_type: str,
        actor_id: str,
        payload: dict,
    ) -> None:
        connection.execute(
            """INSERT INTO asset_import_events(
                   event_id, import_id, event_type, actor_id, payload_json, created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                f"event_{uuid.uuid4().hex}",
                import_id,
                event_type,
                actor_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                self._clock(),
            ),
        )


def _validate_entity_id(value: str, field: str) -> None:
    if not _ID_PATTERN.fullmatch(str(value or "")):
        raise AssetCenterError("INVALID_INPUT", f"{field} 格式无效")


def _request_sha256(
    category_id: str,
    scope: str,
    target_project_id: str,
    files: Sequence[AssetImportFile],
) -> str:
    payload = {
        "category_id": category_id,
        "scope": scope,
        "target_project_id": target_project_id,
        "files": [item.__dict__ for item in files],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_scope_target(scope: str, project_id: Optional[str]) -> str:
    target = str(project_id or "").strip()
    if scope == "project":
        if not target:
            raise AssetCenterError("INVALID_INPUT", "project scope 必须提供 project_id")
        _validate_entity_id(target, "project_id")
        return target
    if scope == "shared":
        if target:
            raise AssetCenterError("INVALID_INPUT", "shared scope 不得提供 project_id")
        return ""
    raise AssetCenterError("INVALID_INPUT", "scope 仅支持 project/shared")


def _validate_image(content: bytes) -> dict:
    try:
        with PILImage.open(BytesIO(content)) as image:
            width, height = image.size
            image_format = str(image.format or "").casefold()
            image.verify()
    except (OSError, UnidentifiedImageError, PILImage.DecompressionBombError) as exc:
        raise AssetCenterError("IMAGE_INVALID", "图片无法安全解码") from exc
    if width < 1 or height < 1:
        raise AssetCenterError("IMAGE_INVALID", "图片尺寸无效")
    return {"width": width, "height": height, "format": image_format}


def _validate_xlsx(content: bytes) -> dict:
    try:
        with ZipFile(BytesIO(content)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                raise AssetCenterError("XLSX_INVALID", "工作簿结构不完整")
            if any(info.flag_bits & 0x1 for info in archive.infolist()):
                raise AssetCenterError("XLSX_INVALID", "不接受加密工作簿")
            total_size = sum(info.file_size for info in archive.infolist())
            if total_size > _MAX_XLSX_UNCOMPRESSED_BYTES:
                raise AssetCenterError("XLSX_INVALID", "工作簿解压后体积超限")
            sheet_count = sum(
                name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
                for name in names
            )
    except BadZipFile as exc:
        raise AssetCenterError("XLSX_INVALID", "工作簿不是有效 ZIP 包") from exc
    return {"sheet_count": sheet_count, "uncompressed_bytes": total_size}


def _searchable_filename(value: str) -> str:
    return " ".join(
        Path(str(value or "")).stem.casefold().replace("_", " ").replace("-", " ").split()
    )


def _safe_source_name(value) -> str:
    text = str(value or "")
    return text.replace("\\", "/").rsplit("/", 1)[-1] if text else ""


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _discover_package_registry(package_manifest: Path) -> Path | None:
    for parent in (package_manifest.parent, *package_manifest.parents):
        candidate = parent / "package_registry.json"
        if candidate.is_file():
            return candidate.resolve()
    return None
