from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Iterable

from .cloud_storage import CosObjectStorage
from .run_contract import sha256_file, sha256_json
from .run_state import write_json_atomic


LEGACY_PLAN_SCHEMA_VERSION = "mplus-legacy-upload-plan/v1"
LEGACY_RECEIPT_SCHEMA_VERSION = "mplus-legacy-upload-receipt/v1"
COLD_ARCHIVE_INVENTORY_SCHEMA_VERSION = "mplus-cold-archive-inventory/v1"
METADATA_SUFFIXES = {
    ".json", ".jsonl", ".log", ".txt", ".md", ".yaml", ".yml", ".toml", ".csv",
}
DELIVERY_KEYS = ("output_path", "client_workbook_path")
AVAILABILITIES = {"metadata_only", "delivery_available", "review_available"}


class LegacyArchiveError(RuntimeError):
    pass


def build_cold_archive_inventory(archive_root: str | Path) -> dict:
    root = Path(archive_root).expanduser().resolve()
    if not root.is_dir():
        raise LegacyArchiveError("历史归档根目录不存在")
    files = []
    links = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            links.append(_symlink_record(root, path))
        elif path.is_file():
            files.append(_file_record(root, path))
    inventory = {
        "schema_version": COLD_ARCHIVE_INVENTORY_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "archive_root": str(root),
        "files": files,
        "links": links,
        "summary": {
            "file_count": len(files),
            "total_bytes": sum(item["size_bytes"] for item in files),
            "symlink_count": len(links),
            "external_symlink_count": sum(
                1 for item in links if not item["target_within_archive"]
            ),
            "broken_symlink_count": sum(
                1 for item in links if not item["target_exists"]
            ),
        },
    }
    inventory["inventory_sha256"] = cold_archive_inventory_sha256(inventory)
    return inventory


def write_cold_archive_inventory(path: str | Path, inventory: dict) -> Path:
    verify_cold_archive_inventory(inventory, verify_files=False)
    target = Path(path).expanduser().resolve()
    if target.exists():
        raise LegacyArchiveError("冷归档清单已存在，拒绝覆盖")
    write_json_atomic(target, inventory)
    return target


def read_cold_archive_inventory(path: str | Path, *, verify_files: bool = False) -> dict:
    inventory = _read_json(Path(path).expanduser().resolve(), "cold archive inventory")
    verify_cold_archive_inventory(inventory, verify_files=verify_files)
    return inventory


def verify_cold_archive_inventory(inventory: dict, *, verify_files: bool) -> dict:
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema_version") != COLD_ARCHIVE_INVENTORY_SCHEMA_VERSION
    ):
        raise LegacyArchiveError("冷归档清单 schema 无效")
    expected = cold_archive_inventory_sha256(inventory)
    if inventory.get("inventory_sha256") != expected:
        raise LegacyArchiveError("冷归档清单 SHA-256 不一致")
    files = inventory.get("files")
    links = inventory.get("links")
    if not isinstance(files, list) or not isinstance(links, list):
        raise LegacyArchiveError("冷归档清单结构无效")
    root = Path(str(inventory.get("archive_root") or "")).expanduser().resolve()
    if verify_files and not root.is_dir():
        raise LegacyArchiveError("历史归档根目录不可用")
    seen = set()
    total = 0
    for record in files:
        relative = _safe_relative(record.get("path"))
        path_text = relative.as_posix()
        if path_text in seen:
            raise LegacyArchiveError("冷归档清单文件路径重复")
        seen.add(path_text)
        digest = _sha256(record.get("sha256"))
        size = record.get("size_bytes")
        if type(size) is not int or size < 0:
            raise LegacyArchiveError("冷归档文件大小无效")
        total += size
        if verify_files:
            source = root.joinpath(*relative.parts)
            if (
                not source.is_file()
                or source.is_symlink()
                or source.stat().st_size != size
                or sha256_file(source) != digest
            ):
                raise LegacyArchiveError(f"冷归档文件已漂移: {relative}")
    external_links = 0
    broken_links = 0
    for record in links:
        relative = _safe_relative(record.get("path"))
        path_text = relative.as_posix()
        if path_text in seen:
            raise LegacyArchiveError("冷归档清单路径重复")
        seen.add(path_text)
        target = str(record.get("link_target") or "")
        if not target or record.get("link_target_sha256") != hashlib.sha256(target.encode("utf-8")).hexdigest():
            raise LegacyArchiveError("冷归档符号链接 SHA-256 无效")
        if type(record.get("target_within_archive")) is not bool or type(record.get("target_exists")) is not bool:
            raise LegacyArchiveError("冷归档符号链接状态无效")
        external_links += int(not record["target_within_archive"])
        broken_links += int(not record["target_exists"])
        if verify_files:
            source = root.joinpath(*relative.parts)
            try:
                current_target = os.readlink(source)
            except OSError as exc:
                raise LegacyArchiveError(f"冷归档符号链接已漂移: {relative}") from exc
            if current_target != target:
                raise LegacyArchiveError(f"冷归档符号链接已漂移: {relative}")
    summary = dict(inventory.get("summary") or {})
    if (
        summary.get("file_count") != len(files)
        or summary.get("total_bytes") != total
        or summary.get("symlink_count") != len(links)
        or summary.get("external_symlink_count") != external_links
        or summary.get("broken_symlink_count") != broken_links
    ):
        raise LegacyArchiveError("冷归档清单统计不一致")
    return {"status": "pass", **summary, "inventory_sha256": expected}


def cold_archive_inventory_sha256(inventory: dict) -> str:
    payload = {
        key: value for key, value in inventory.items() if key != "inventory_sha256"
    }
    return sha256_json(payload)


def _symlink_record(root: Path, path: Path) -> dict:
    relative = path.absolute().relative_to(root).as_posix()
    target = os.readlink(path)
    resolved_target = (path.parent / target).resolve()
    try:
        resolved_target.relative_to(root)
        within_archive = True
    except ValueError:
        within_archive = False
    return {
        "path": _safe_relative(relative).as_posix(),
        "link_target": target,
        "link_target_sha256": hashlib.sha256(target.encode("utf-8")).hexdigest(),
        "target_within_archive": within_archive,
        "target_exists": resolved_target.exists(),
    }


def build_legacy_upload_plan(
    archive_root: str | Path,
    *,
    review_run_ids: Iterable[str] = (),
) -> dict:
    root = Path(archive_root).expanduser().resolve()
    if not root.is_dir():
        raise LegacyArchiveError("历史归档根目录不存在")
    review_allowlist = {str(item).strip() for item in review_run_ids if str(item).strip()}
    manifests = sorted(root.rglob("run_manifest.json"))
    if not manifests:
        raise LegacyArchiveError("历史归档中没有 run_manifest.json")
    runs = []
    seen_run_ids = set()
    all_selected: dict[str, dict] = {}
    all_excluded: dict[str, dict] = {}
    status_counts = Counter()
    availability_counts = Counter()
    for manifest_path in manifests:
        manifest = _read_json(manifest_path, "run manifest")
        run_id = str(manifest.get("run_id") or manifest_path.parent.name).strip()
        if not run_id or run_id in seen_run_ids:
            raise LegacyArchiveError(f"历史 run_id 缺失或重复: {run_id or manifest_path}")
        seen_run_ids.add(run_id)
        report_path = manifest_path.parent / "run_report.json"
        report = _read_json(report_path, "run report") if report_path.is_file() else {}
        status = str(manifest.get("status") or report.get("status") or "unknown")
        status_counts[status] += 1
        availability = _availability_for(status, run_id, review_allowlist)
        availability_counts[availability] += 1
        metadata = _metadata_records(root, manifest_path.parent)
        artifacts = []
        if availability != "metadata_only":
            artifacts = _delivery_records(
                root,
                manifest_path.parent,
                report,
                availability=availability,
            )
            if status == "succeeded" and not artifacts:
                raise LegacyArchiveError(f"成功 run 缺少可校验交付文件: {run_id}")
        for record in (*metadata, *artifacts):
            existing = all_selected.get(record["path"])
            if existing and existing["sha256"] != record["sha256"]:
                raise LegacyArchiveError("历史文件路径与 SHA-256 冲突")
            all_selected[record["path"]] = record
        selected_paths = {item["path"] for item in (*metadata, *artifacts)}
        excluded = _excluded_records(
            root,
            manifest_path.parent,
            selected_paths=selected_paths,
            status=status,
            availability=availability,
        )
        for record in excluded:
            all_excluded[record["path"]] = record
        runs.append({
            "run_id": run_id,
            "parent_run_id": str(manifest.get("parent_run_id") or ""),
            "project_name": str(manifest.get("project_name") or report.get("project_name") or "未命名历史项目"),
            "category_id": str(manifest.get("category_id") or report.get("category_id") or ""),
            "status": status,
            "artifact_availability": availability,
            "started_at": str(manifest.get("started_at") or ""),
            "finished_at": str(manifest.get("finished_at") or ""),
            "source_manifest_path": manifest_path.relative_to(root).as_posix(),
            "source_manifest_sha256": sha256_file(manifest_path),
            "metadata_files": metadata,
            "delivery_files": artifacts,
            "excluded_files": excluded,
        })
    files = sorted(all_selected.values(), key=lambda item: item["path"])
    excluded_files = sorted(all_excluded.values(), key=lambda item: item["path"])
    plan = {
        "schema_version": LEGACY_PLAN_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "archive_root": str(root),
        "selection_policy": {
            "metadata_suffixes": sorted(METADATA_SUFFIXES),
            "delivery_statuses": ["succeeded"],
            "review_run_ids": sorted(review_allowlist),
            "excluded": [
                "best_failed", "loop_intermediate", "visual_review_html",
                "acceptance_binary", "extracted_media", "backup", "legacy_source_library",
            ],
        },
        "runs": runs,
        "files": files,
        "excluded_files": excluded_files,
        "summary": {
            "run_count": len(runs),
            "status_counts": dict(sorted(status_counts.items())),
            "availability_counts": dict(sorted(availability_counts.items())),
            "file_count": len(files),
            "metadata_bytes": sum(
                item["size_bytes"]
                for item in files
                if Path(item["path"]).suffix.casefold() in METADATA_SUFFIXES
            ),
            "delivery_bytes": sum(
                item["size_bytes"]
                for item in files
                if Path(item["path"]).suffix.casefold() not in METADATA_SUFFIXES
            ),
            "upload_bytes": sum(item["size_bytes"] for item in files),
            "excluded_file_count": len(excluded_files),
            "excluded_bytes": sum(item["size_bytes"] for item in excluded_files),
        },
    }
    plan["plan_sha256"] = legacy_plan_sha256(plan)
    return plan


def write_legacy_upload_plan(path: str | Path, plan: dict) -> Path:
    verify_legacy_upload_plan(plan, verify_files=True)
    target = Path(path).expanduser().resolve()
    if target.exists():
        raise LegacyArchiveError("历史上云计划已存在，拒绝覆盖")
    write_json_atomic(target, plan)
    return target


def read_legacy_catalog(path: str | Path, *, verify_files: bool = False) -> dict:
    catalog = _read_json(Path(path).expanduser().resolve(), "legacy catalog")
    schema = catalog.get("schema_version")
    if schema == LEGACY_PLAN_SCHEMA_VERSION:
        verify_legacy_upload_plan(catalog, verify_files=verify_files)
    elif schema == LEGACY_RECEIPT_SCHEMA_VERSION:
        verify_legacy_upload_receipt(catalog)
    else:
        raise LegacyArchiveError("历史目录 schema 不受支持")
    return catalog


def verify_legacy_upload_plan(plan: dict, *, verify_files: bool) -> dict:
    if not isinstance(plan, dict) or plan.get("schema_version") != LEGACY_PLAN_SCHEMA_VERSION:
        raise LegacyArchiveError("历史上云计划 schema 无效")
    expected = legacy_plan_sha256(plan)
    if plan.get("plan_sha256") != expected:
        raise LegacyArchiveError("历史上云计划 SHA-256 不一致")
    runs = plan.get("runs")
    files = plan.get("files")
    excluded_files = plan.get("excluded_files")
    if not isinstance(runs, list) or not isinstance(files, list) or not isinstance(excluded_files, list):
        raise LegacyArchiveError("历史上云计划结构无效")
    if len({item.get("run_id") for item in runs}) != len(runs):
        raise LegacyArchiveError("历史上云计划含重复 run_id")
    status_counts = Counter()
    availability_counts = Counter()
    referenced_files = {}
    referenced_excluded = {}
    for run in runs:
        status_counts[str(run.get("status") or "unknown")] += 1
        availability = str(run.get("artifact_availability") or "")
        if availability not in AVAILABILITIES:
            raise LegacyArchiveError("历史 run artifact_availability 无效")
        availability_counts[availability] += 1
        _safe_relative(run.get("source_manifest_path"))
        _sha256(run.get("source_manifest_sha256"))
        for record in list(run.get("metadata_files") or []) + list(run.get("delivery_files") or []):
            path_text = _safe_relative(record.get("path")).as_posix()
            existing = referenced_files.get(path_text)
            if existing and existing != record:
                raise LegacyArchiveError("历史 run 引用的文件记录冲突")
            referenced_files[path_text] = record
        for record in list(run.get("excluded_files") or []):
            path_text = _safe_relative(record.get("path")).as_posix()
            if not str(record.get("exclusion_reason") or "").strip():
                raise LegacyArchiveError("历史排除文件缺少原因")
            existing = referenced_excluded.get(path_text)
            if existing and existing != record:
                raise LegacyArchiveError("历史 run 引用的排除文件记录冲突")
            referenced_excluded[path_text] = record
    archive_root = Path(str(plan.get("archive_root") or "")).expanduser().resolve()
    if verify_files and not archive_root.is_dir():
        raise LegacyArchiveError("历史归档根目录不可用")
    paths = set()
    total = 0
    for record in files:
        relative = _safe_relative(record.get("path"))
        if relative.as_posix() in paths:
            raise LegacyArchiveError("历史上云计划文件路径重复")
        paths.add(relative.as_posix())
        digest = _sha256(record.get("sha256"))
        size = record.get("size_bytes")
        if type(size) is not int or size < 0:
            raise LegacyArchiveError("历史文件大小无效")
        total += size
        if verify_files:
            source = archive_root.joinpath(*relative.parts)
            if not source.is_file() or source.stat().st_size != size or sha256_file(source) != digest:
                raise LegacyArchiveError(f"历史文件已漂移: {relative}")
    summary = dict(plan.get("summary") or {})
    if summary.get("run_count") != len(runs) or summary.get("file_count") != len(files):
        raise LegacyArchiveError("历史上云计划统计不一致")
    if summary.get("upload_bytes") != total:
        raise LegacyArchiveError("历史上云计划上传字节数不一致")
    excluded_paths = set()
    excluded_bytes = 0
    for record in excluded_files:
        relative = _safe_relative(record.get("path")).as_posix()
        if relative in excluded_paths or relative in paths:
            raise LegacyArchiveError("历史上云计划排除路径重复或与选中文件冲突")
        excluded_paths.add(relative)
        size = record.get("size_bytes")
        if type(size) is not int or size < 0:
            raise LegacyArchiveError("历史排除文件大小无效")
        excluded_bytes += size
        if not str(record.get("exclusion_reason") or "").strip():
            raise LegacyArchiveError("历史排除文件缺少原因")
    if (
        summary.get("excluded_file_count") != len(excluded_files)
        or summary.get("excluded_bytes") != excluded_bytes
    ):
        raise LegacyArchiveError("历史上云计划排除统计不一致")
    if set(referenced_excluded) != excluded_paths:
        raise LegacyArchiveError("历史上云计划存在未关联或缺失的排除文件")
    if summary.get("status_counts") != dict(sorted(status_counts.items())):
        raise LegacyArchiveError("历史上云计划状态统计不一致")
    if summary.get("availability_counts") != dict(sorted(availability_counts.items())):
        raise LegacyArchiveError("历史上云计划可用性统计不一致")
    if set(referenced_files) != paths:
        raise LegacyArchiveError("历史上云计划存在未关联或缺失文件")
    return {"status": "pass", **summary, "plan_sha256": expected}


def upload_legacy_plan(
    plan: dict,
    storage: CosObjectStorage,
    *,
    repository=None,
    receipt_path: str | Path | None = None,
) -> dict:
    verify_legacy_upload_plan(plan, verify_files=True)
    root = Path(plan["archive_root"])
    uploaded = {}
    for record in plan["files"]:
        relative = _safe_relative(record["path"])
        cloud = storage.put_file(
            root.joinpath(*relative.parts),
            key=storage.content_key(record["sha256"], namespace="legacy/objects"),
            expected_sha256=record["sha256"],
        )
        uploaded[record["path"]] = {**record, "storage_ref": cloud.storage_ref}
    runs = []
    for run in plan["runs"]:
        metadata = [uploaded[item["path"]] for item in run["metadata_files"]]
        delivery = [uploaded[item["path"]] for item in run["delivery_files"]]
        catalog_payload = {
            "schema_version": "mplus-legacy-run-catalog/v1",
            **run,
            "metadata_files": metadata,
            "delivery_files": delivery,
        }
        catalog_bytes = _json_bytes(catalog_payload)
        catalog_digest = hashlib.sha256(catalog_bytes).hexdigest()
        catalog_cloud = storage.put_bytes(
            catalog_bytes,
            key=storage.content_key(catalog_digest, namespace="legacy/catalogs"),
            expected_sha256=catalog_digest,
        )
        runs.append({
            **catalog_payload,
            "catalog_ref": catalog_cloud.storage_ref,
            "catalog_sha256": catalog_cloud.sha256,
        })
    receipt = {
        "schema_version": LEGACY_RECEIPT_SCHEMA_VERSION,
        "created_at": _utc_now(),
        "plan_sha256": plan["plan_sha256"],
        "archive_root": plan["archive_root"],
        "selection_policy": plan["selection_policy"],
        "runs": runs,
        "files": sorted(uploaded.values(), key=lambda item: item["path"]),
        "summary": plan["summary"],
    }
    receipt["receipt_sha256"] = legacy_receipt_sha256(receipt)
    receipt_bytes = _json_bytes(receipt)
    receipt_digest = hashlib.sha256(receipt_bytes).hexdigest()
    storage.put_bytes(
        receipt_bytes,
        key=storage.content_key(receipt_digest, namespace="legacy/receipts"),
        expected_sha256=receipt_digest,
    )
    if repository is not None:
        for run in runs:
            first_delivery = (run["delivery_files"] or [{}])[0]
            repository.register_legacy_run({
                "run_id": run["run_id"],
                "parent_run_id": run.get("parent_run_id") or None,
                "project_name": run["project_name"],
                "category_id": run.get("category_id") or None,
                "status": run["status"],
                "artifact_availability": run["artifact_availability"],
                "source_manifest_sha256": run["source_manifest_sha256"],
                "catalog_ref": run["catalog_ref"],
                "catalog_sha256": run["catalog_sha256"],
                "delivery_ref": first_delivery.get("storage_ref") or None,
                "delivery_sha256": first_delivery.get("sha256") or None,
                "delivery_filename": Path(str(first_delivery.get("path") or "")).name or None,
                "delivery_files": run["delivery_files"],
                "started_at": run.get("started_at") or None,
                "finished_at": run.get("finished_at") or None,
            })
    if receipt_path is not None:
        target = Path(receipt_path).expanduser().resolve()
        if target.exists():
            raise LegacyArchiveError("历史上云回执已存在，拒绝覆盖")
        write_json_atomic(target, receipt)
    return receipt


def verify_legacy_upload_receipt(receipt: dict, storage: CosObjectStorage | None = None) -> dict:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != LEGACY_RECEIPT_SCHEMA_VERSION:
        raise LegacyArchiveError("历史上云回执 schema 无效")
    expected = legacy_receipt_sha256(receipt)
    if receipt.get("receipt_sha256") != expected:
        raise LegacyArchiveError("历史上云回执 SHA-256 不一致")
    _sha256(receipt.get("plan_sha256"))
    runs = receipt.get("runs")
    files = receipt.get("files")
    if not isinstance(runs, list) or not isinstance(files, list):
        raise LegacyArchiveError("历史上云回执结构无效")
    if len({item.get("run_id") for item in runs}) != len(runs):
        raise LegacyArchiveError("历史上云回执含重复 run_id")
    summary = dict(receipt.get("summary") or {})
    if summary.get("run_count") != len(runs) or summary.get("file_count") != len(files):
        raise LegacyArchiveError("历史上云回执统计不一致")
    for record in files:
        _safe_relative(record.get("path"))
        _sha256(record.get("sha256"))
        _required_storage_ref(record.get("storage_ref"))
        if storage is not None:
            storage.verify(record.get("storage_ref") or "", record.get("sha256") or "", record.get("size_bytes"))
    for run in runs:
        if str(run.get("artifact_availability") or "") not in AVAILABILITIES:
            raise LegacyArchiveError("历史上云回执可用性无效")
        _sha256(run.get("catalog_sha256"))
        _required_storage_ref(run.get("catalog_ref"))
        if storage is not None:
            catalog_size = len(_json_bytes({
                key: value
                for key, value in run.items()
                if key not in {"catalog_ref", "catalog_sha256"}
            }))
            storage.verify(
                run.get("catalog_ref") or "",
                run.get("catalog_sha256") or "",
                catalog_size,
            )
    return {"status": "pass", **dict(receipt.get("summary") or {}), "receipt_sha256": expected}


def legacy_plan_sha256(plan: dict) -> str:
    payload = {key: value for key, value in plan.items() if key != "plan_sha256"}
    return sha256_json(payload)


def legacy_receipt_sha256(receipt: dict) -> str:
    payload = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    return sha256_json(payload)


def _availability_for(status: str, run_id: str, review_allowlist: set[str]) -> str:
    if status == "succeeded":
        return "delivery_available"
    if status == "waiting_human_review" and run_id in review_allowlist:
        return "review_available"
    return "metadata_only"


def _metadata_records(root: Path, run_dir: Path) -> list[dict]:
    return [
        {**_file_record(root, path), "selection_reason": "trace_metadata"}
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and not path.is_symlink() and path.suffix.casefold() in METADATA_SUFFIXES
    ]


def _delivery_records(
    root: Path,
    run_dir: Path,
    report: dict,
    *,
    availability: str,
) -> list[dict]:
    records = {}
    for key in DELIVERY_KEYS:
        value = str(report.get(key) or "").strip()
        if not value:
            continue
        path = _resolve_report_path(root, run_dir, value)
        if path is None or "best_failed" in path.name.casefold():
            continue
        if path.suffix.casefold() not in {".xlsx", ".xlsm"}:
            continue
        record = {
            **_file_record(root, path),
            "selection_reason": (
                "successful_delivery_workbook"
                if availability == "delivery_available"
                else "allowlisted_latest_review_workbook"
            ),
        }
        records[record["path"]] = record
    return sorted(records.values(), key=lambda item: item["path"])


def _excluded_records(
    root: Path,
    run_dir: Path,
    *,
    selected_paths: set[str],
    status: str,
    availability: str,
) -> list[dict]:
    records = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.resolve().relative_to(root).as_posix()
        if relative in selected_paths:
            continue
        name = path.name.casefold()
        suffix = path.suffix.casefold()
        if "best_failed" in name:
            reason = "best_failed_binary"
        elif suffix in {".html", ".htm"}:
            reason = "visual_review_html"
        elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov"}:
            reason = "extracted_media_or_video"
        elif any(marker in name for marker in ("backup", ".bak", "copy")):
            reason = "backup_file"
        elif suffix in {".xlsx", ".xlsm", ".xls", ".pdf"}:
            if status == "waiting_human_review" and availability == "metadata_only":
                reason = "review_run_not_allowlisted"
            elif status in {"blocked", "failed"}:
                reason = "blocked_or_failed_binary"
            else:
                reason = "intermediate_or_acceptance_binary"
        elif suffix in {".py", ".sh", ".command"}:
            reason = "legacy_source_library"
        else:
            reason = "not_selected_by_policy"
        records.append({
            "path": _safe_relative(relative).as_posix(),
            "size_bytes": path.stat().st_size,
            "exclusion_reason": reason,
        })
    return records


def _resolve_report_path(root: Path, run_dir: Path, value: str) -> Path | None:
    source = Path(value).expanduser()
    candidates = []
    if source.is_absolute():
        try:
            candidates.append(root / source.resolve().relative_to(root))
        except (OSError, ValueError):
            pass
    else:
        candidates.extend((root / source, run_dir / source, run_dir / source.name))
    normalized = value.replace("\\", "/")
    for marker in ("/mvp-v2-run/", "方案/mvp-v2-run/"):
        if marker in normalized:
            suffix = normalized.split(marker, 1)[1]
            matches = [path for path in (root, *root.parents) if path.name == "mvp-v2-run"]
            if root.name == "mvp-v2-run":
                matches.insert(0, root)
            candidates.extend(base / suffix for base in matches)
            candidates.append(root / "方案" / "mvp-v2-run" / suffix)
    candidates.append(run_dir / source.name)
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.is_file() and not resolved.is_symlink():
            return resolved
    return None


def _file_record(root: Path, path: Path) -> dict:
    resolved = path.resolve()
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise LegacyArchiveError("历史文件越出归档根目录") from exc
    return {
        "path": _safe_relative(relative).as_posix(),
        "size_bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def _safe_relative(value: object) -> PurePosixPath:
    text = str(value or "")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise LegacyArchiveError("历史上云路径无效")
    return path


def _sha256(value: object) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise LegacyArchiveError("SHA-256 无效")
    return digest


def _required_storage_ref(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise LegacyArchiveError("历史上云对象引用缺失")
    return normalized


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyArchiveError(f"{label} 无法读取: {path}") from exc
    if not isinstance(value, dict):
        raise LegacyArchiveError(f"{label} 必须是 JSON 对象")
    return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
