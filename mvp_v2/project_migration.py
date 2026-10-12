from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat as stat_module
import time
from typing import Mapping
import uuid


MIGRATION_TARGETS = (
    Path("方案/mvp-v2-run"),
    Path("方案/mvp-real-run"),
    Path("方案/p0-validation"),
    Path("方案/p1-validation"),
    Path("方案/state"),
    Path("整理后脚本库"),
    Path("网页输出"),
)

MIGRATION_DISPOSITIONS = {
    "migrate_business",
    "retain_readonly",
    "rebuildable_cache",
    "broken_link_candidate",
    "quarantine_unknown",
    "unclassified",
}

DEFAULT_DISPOSITIONS = {
    "方案/mvp-v2-run": "retain_readonly",
    "方案/mvp-real-run": "retain_readonly",
    "方案/p0-validation": "retain_readonly",
    "方案/p1-validation": "retain_readonly",
    "方案/state": "retain_readonly",
    "整理后脚本库": "migrate_business",
    "网页输出": "migrate_business",
}


@dataclass(frozen=True)
class MigrationRecord:
    path: str
    kind: str
    size: int
    sha256: str = ""
    link_target: str = ""
    source_path: str = ""
    target_path: str = ""
    target_ref: str = ""
    mtime_ns: int = 0
    disposition: str = "unclassified"
    approval_status: str = "pending"
    approved_by: str = ""
    approved_at: str = ""


def inventory_targets(
    root: str | Path,
    logical_root: str | Path,
    targets: tuple[Path, ...] = MIGRATION_TARGETS,
    *,
    source_root: str | Path | None = None,
    target_root: str | Path | None = None,
    cutover_id: str = "",
    dispositions: Mapping[str, str] | None = None,
    approved_by: str = "",
    approved_at: str = "",
) -> list[MigrationRecord]:
    physical_root = Path(root).resolve()
    logical = Path(logical_root)
    source_base = Path(source_root).resolve() if source_root is not None else physical_root
    target_base = Path(target_root).resolve() if target_root is not None else physical_root
    policy = {**DEFAULT_DISPOSITIONS, **dict(dispositions or {})}
    _validate_dispositions(policy)
    normalized_approver = approved_by.strip()
    normalized_approved_at = approved_at
    if normalized_approver and not normalized_approved_at:
        normalized_approved_at = _format_utc(datetime.now(timezone.utc))
    approval_status = "approved" if normalized_approver else "pending"
    records = []
    for relative in targets:
        source = physical_root / relative
        if not source.exists() and not source.is_symlink():
            continue
        disposition = policy.get(relative.as_posix(), "unclassified")
        records.extend(
            _inventory_path(
                source,
                logical / relative,
                source_base / relative,
                target_base / relative,
                cutover_id=cutover_id,
                disposition=disposition,
                approval_status=approval_status,
                approved_by=normalized_approver,
                approved_at=normalized_approved_at,
            )
        )
    return sorted(records, key=lambda item: item.path)


def execute_cutover(
    project_root: str | Path,
    data_root: str | Path,
    cutover_id: str,
    *,
    targets: tuple[Path, ...] = MIGRATION_TARGETS,
    dispositions: Mapping[str, str] | None = None,
    approved_by: str = "",
    cutover_at: datetime | None = None,
    observation_days: int = 14,
) -> dict:
    project = Path(project_root).resolve()
    data = Path(data_root).expanduser().resolve()
    if not cutover_id or Path(cutover_id).name != cutover_id:
        raise ValueError("cutover_id 无效")
    if not approved_by.strip():
        raise ValueError("迁移批准人 approved_by 不能为空")
    if not isinstance(observation_days, int) or observation_days <= 0:
        raise ValueError("observation_days 必须为正整数")
    started_at = _as_utc(cutover_at or datetime.now(timezone.utc))
    approved_at = _format_utc(started_at)
    observation_until = _format_utc(started_at + timedelta(days=observation_days))
    archive = data / "legacy-archive" / cutover_id
    if archive.exists():
        raise ValueError(f"归档目录已存在: {archive}")
    present = tuple(
        relative
        for relative in targets
        if (project / relative).exists() or (project / relative).is_symlink()
    )
    if not present:
        raise ValueError("没有可迁移的业务目录")
    evidence = data / "logs" / "governance" / cutover_id
    source_records = inventory_targets(
        project,
        project,
        present,
        source_root=project,
        target_root=archive,
        cutover_id=cutover_id,
        dispositions=dispositions,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    unclassified_count = sum(item.disposition == "unclassified" for item in source_records)
    if unclassified_count:
        raise ValueError(f"迁移 manifest 仍有 unclassified={unclassified_count}，拒绝移动")
    unapproved_count = sum(item.approval_status != "approved" for item in source_records)
    if unapproved_count:
        raise ValueError(f"迁移 manifest 仍有未批准记录={unapproved_count}，拒绝移动")
    _write_records(evidence / "source-manifest.jsonl", source_records)
    archive.mkdir(parents=True)
    if project.stat().st_dev != archive.stat().st_dev:
        raise ValueError("项目区与归档区不在同一文件系统，拒绝非原子移动")
    moved: list[tuple[Path, Path]] = []
    try:
        for relative in present:
            source = project / relative
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, target)
            moved.append((source, target))
        target_records = inventory_targets(
            archive,
            project,
            present,
            source_root=project,
            target_root=archive,
            cutover_id=cutover_id,
            dispositions=dispositions,
            approved_by=approved_by,
            approved_at=approved_at,
        )
        _write_records(evidence / "target-manifest.jsonl", target_records)
        if _record_signatures(source_records) != _record_signatures(target_records):
            raise ValueError("迁移前后文件清单、分类或 SHA-256 不一致")
        mounts = {
            "schema_version": "mplus-legacy-mounts/v1",
            "cutover_id": cutover_id,
            "created_at": approved_at,
            "mounts": [
                {
                    "mount_id": f"{cutover_id}-runs",
                    "kind": "runs",
                    "mode": "read_only",
                    "original_path": str(project / "方案/mvp-v2-run/assets"),
                    "path": str(archive / "方案/mvp-v2-run/assets"),
                },
                {
                    "mount_id": f"{cutover_id}-sources",
                    "kind": "sources",
                    "mode": "read_only",
                    "original_path": str(project / "整理后脚本库"),
                    "path": str(archive / "整理后脚本库"),
                },
                {
                    "mount_id": f"{cutover_id}-exports",
                    "kind": "exports",
                    "mode": "read_only",
                    "original_path": str(project / "网页输出"),
                    "path": str(archive / "网页输出"),
                },
            ],
        }
        _write_json_atomic(data / "legacy-mounts.json", mounts)
        classification_counts = {
            disposition: sum(item.disposition == disposition for item in source_records)
            for disposition in sorted(MIGRATION_DISPOSITIONS)
            if disposition != "unclassified"
        }
        summary = {
            "status": "complete",
            "cutover_id": cutover_id,
            "archive_root": str(archive),
            "record_count": len(source_records),
            "file_count": sum(item.kind == "file" for item in source_records),
            "size_bytes": sum(item.size for item in source_records if item.kind == "file"),
            "moved": [str(relative) for relative in present],
            "classification_counts": classification_counts,
            "unclassified_count": unclassified_count,
            "approval_status": "approved",
            "approved_by": approved_by.strip(),
            "approved_at": approved_at,
            "observation_started_at": approved_at,
            "observation_days": observation_days,
            "observation_until": observation_until,
            "deletion_authorized": False,
        }
        _write_json_atomic(evidence / "cutover-summary.json", summary)
        return summary
    except Exception:
        for source, target in reversed(moved):
            if target.exists() or target.is_symlink():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(target, source)
        raise


def run_rollback_drill(
    source_manifest: str | Path,
    target_manifest: str | Path,
    incremental_manifest: str | Path | None,
    *,
    rto_limit_seconds: float = 60 * 60,
) -> dict:
    """只读核对迁移基线和观察期增量，不执行配置切换或文件写入。"""
    if rto_limit_seconds <= 0:
        raise ValueError("rto_limit_seconds 必须大于 0")
    started_wall = datetime.now(timezone.utc)
    started = time.perf_counter()
    source_records = _load_records(source_manifest)
    target_records = _load_records(target_manifest)
    incremental_records = (
        _load_records(incremental_manifest) if incremental_manifest is not None else []
    )
    blockers: list[str] = []
    if incremental_manifest is None:
        blockers.append("缺少切换后增量 manifest")

    source_by_path, source_duplicates = _records_by_path(source_records)
    target_by_path, target_duplicates = _records_by_path(target_records)
    if source_duplicates:
        blockers.append("源 manifest 存在重复 path")
    if target_duplicates:
        blockers.append("目标 manifest 存在重复 path")

    baseline_missing = sorted(set(source_by_path) - set(target_by_path))
    baseline_changed = sorted(
        path
        for path in set(source_by_path) & set(target_by_path)
        if _migration_signature(source_by_path[path])
        != _migration_signature(target_by_path[path])
    )
    if baseline_missing:
        blockers.append("目标 manifest 未完整覆盖迁移前基线")
    if baseline_changed:
        blockers.append("迁移前后基线指纹不一致")

    protected_records = list(target_records) + list(incremental_records)
    unclassified_count = sum(item.disposition == "unclassified" for item in protected_records)
    unapproved_count = sum(
        item.approval_status != "approved" or not item.approved_by or not item.approved_at
        for item in protected_records
    )
    missing_refs = sorted({item.path for item in protected_records if not item.target_ref})
    missing_or_changed = sorted(
        {
            item.path
            for item in protected_records
            if not _target_matches_record(item)
        }
    )
    if unclassified_count:
        blockers.append(f"仍有 unclassified={unclassified_count}")
    if unapproved_count:
        blockers.append(f"仍有未批准记录={unapproved_count}")
    if missing_refs:
        blockers.append("存在无回滚可达引用的记录")
    if missing_or_changed:
        blockers.append("目标文件缺失或当前指纹与 manifest 不一致")

    duration = time.perf_counter() - started
    finished_wall = datetime.now(timezone.utc)
    rpo_zero_data_ready = not blockers
    rto_within_limit = duration <= rto_limit_seconds
    ready_for_live_switch_drill = rpo_zero_data_ready and rto_within_limit
    readiness_blockers = list(blockers)
    if not rto_within_limit:
        readiness_blockers.append("只读 manifest 核对耗时超过 RTO 上限")
    return {
        "schema_version": "mplus-rollback-drill/v1",
        "status": "ready_for_live_switch_drill" if ready_for_live_switch_drill else "blocked",
        "read_only": True,
        "started_at": _format_utc(started_wall),
        "finished_at": _format_utc(finished_wall),
        "duration_seconds": duration,
        "rto_limit_seconds": rto_limit_seconds,
        "rto_within_limit": rto_within_limit,
        "rto_evidence_scope": "read_only_manifest_drill",
        "live_switch_verified": False,
        "rpo_zero_data_ready": rpo_zero_data_ready,
        "source_record_count": len(source_records),
        "target_record_count": len(target_records),
        "incremental_record_count": len(incremental_records),
        "unclassified_count": unclassified_count,
        "unapproved_count": unapproved_count,
        "baseline_missing": baseline_missing,
        "baseline_changed": baseline_changed,
        "missing_target_refs": missing_refs,
        "missing_or_changed_records": missing_or_changed,
        "blockers": readiness_blockers,
        "rollback_plan": [
            "阻止新的 R2/R3 任务并等待在途写入结束",
            "冻结目标区增量 manifest 并再次运行本只读核对",
            "保持目标数据根可写，或将全部新增记录挂载到旧程序",
            "在维护窗口切换旧程序与 registry/config 投影",
            "执行冻结 smoke 后再开放写入",
        ],
        "manual_followups": [
            "本报告不执行真实配置或程序回切；"
            "live_switch_verified 只能由维护窗口演练置为真",
            "观察期结束和删除归档仍需单独人工批准",
        ],
    }


def _inventory_path(
    physical: Path,
    logical: Path,
    source_path: Path,
    target_path: Path,
    *,
    cutover_id: str,
    disposition: str,
    approval_status: str,
    approved_by: str,
    approved_at: str,
) -> list[MigrationRecord]:
    record = _record(
        physical,
        logical,
        source_path,
        target_path,
        cutover_id=cutover_id,
        disposition=disposition,
        approval_status=approval_status,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    records = [record]
    if physical.is_dir() and not physical.is_symlink():
        for child in sorted(physical.iterdir(), key=lambda item: item.name):
            records.extend(
                _inventory_path(
                    child,
                    logical / child.name,
                    source_path / child.name,
                    target_path / child.name,
                    cutover_id=cutover_id,
                    disposition=disposition,
                    approval_status=approval_status,
                    approved_by=approved_by,
                    approved_at=approved_at,
                )
            )
    return records


def _record(
    physical: Path,
    logical: Path,
    source_path: Path,
    target_path: Path,
    *,
    cutover_id: str,
    disposition: str,
    approval_status: str,
    approved_by: str,
    approved_at: str,
) -> MigrationRecord:
    stat = physical.lstat()
    kind = "unknown"
    sha256 = ""
    link_target = ""
    resolved_disposition = disposition
    if stat_module.S_ISLNK(stat.st_mode):
        kind = "symlink"
        link_target = os.readlink(physical)
        if not physical.exists():
            resolved_disposition = "broken_link_candidate"
    elif stat_module.S_ISDIR(stat.st_mode):
        kind = "directory"
    elif stat_module.S_ISREG(stat.st_mode):
        kind = "file"
        sha256 = _sha256(physical)
    else:
        resolved_disposition = "quarantine_unknown"
    target_ref = str(target_path)
    if cutover_id:
        target_ref = f"legacy://{cutover_id}/{logical.as_posix().lstrip('/')}"
    return MigrationRecord(
        path=str(logical),
        kind=kind,
        size=stat.st_size,
        sha256=sha256,
        link_target=link_target,
        source_path=str(source_path),
        target_path=str(target_path),
        target_ref=target_ref,
        mtime_ns=stat.st_mtime_ns,
        disposition=resolved_disposition,
        approval_status=approval_status,
        approved_by=approved_by,
        approved_at=approved_at,
    )


def _record_signatures(records: list[MigrationRecord]) -> dict[str, tuple]:
    return {item.path: _migration_signature(item) for item in records}


def _migration_signature(record: MigrationRecord) -> tuple:
    return (
        record.kind,
        record.size,
        record.sha256,
        record.link_target,
        record.source_path,
        record.target_path,
        record.target_ref,
        record.mtime_ns,
        record.disposition,
        record.approval_status,
        record.approved_by,
        record.approved_at,
    )


def _target_matches_record(record: MigrationRecord) -> bool:
    if not record.target_path:
        return False
    target = Path(record.target_path)
    try:
        target_stat = target.lstat()
    except OSError:
        return False
    if record.kind == "symlink":
        return stat_module.S_ISLNK(target_stat.st_mode) and os.readlink(target) == record.link_target
    if record.kind == "directory":
        return stat_module.S_ISDIR(target_stat.st_mode) and target_stat.st_mtime_ns == record.mtime_ns
    if record.kind == "file":
        return (
            stat_module.S_ISREG(target_stat.st_mode)
            and target_stat.st_size == record.size
            and target_stat.st_mtime_ns == record.mtime_ns
            and _sha256(target) == record.sha256
        )
    return False


def _records_by_path(records: list[MigrationRecord]) -> tuple[dict[str, MigrationRecord], list[str]]:
    indexed: dict[str, MigrationRecord] = {}
    duplicates = []
    for record in records:
        if record.path in indexed:
            duplicates.append(record.path)
        indexed[record.path] = record
    return indexed, sorted(set(duplicates))


def _load_records(path: str | Path) -> list[MigrationRecord]:
    manifest = Path(path)
    allowed = {field.name for field in fields(MigrationRecord)}
    records = []
    with manifest.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                normalized = {
                    key: value for key, value in payload.items() if key in allowed
                }
                records.append(MigrationRecord(**normalized))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError(f"manifest 第 {line_number} 行无效: {manifest}") from exc
    return records


def _validate_dispositions(policy: Mapping[str, str]) -> None:
    invalid = sorted({value for value in policy.values() if value not in MIGRATION_DISPOSITIONS})
    if invalid:
        raise ValueError(f"未知迁移处置: {', '.join(invalid)}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_records(path: Path, records: list[MigrationRecord]) -> None:
    lines = [json.dumps(asdict(item), ensure_ascii=False, sort_keys=True) for item in records]
    _write_text_atomic(path, "\n".join(lines) + ("\n" if lines else ""))


def _write_json_atomic(path: Path, payload: dict) -> None:
    _write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("cutover_at 必须包含时区")
    return value.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
