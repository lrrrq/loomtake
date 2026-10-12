from __future__ import annotations

import json
import os
import shutil
import tempfile
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .evolution import record_evolution_event
from .script_type_adapter import resolve_script_type_workbook
from .run_contract import _redact_config, sha256_file, sha256_json
from .run_state import (
    append_jsonl_ledger,
    create_run_dir,
    exclusive_lock,
    generate_run_id,
    write_json_atomic,
)
from .runtime_config import configured_source_commit
from .portable_paths import resolve_run_local_path
from .stage_runtime import (
    PIPELINE_SCHEMA_VERSION,
    finish_pipeline_stage,
    initialize_stage_mapping,
    prepare_resume_manifest,
    start_pipeline_stage,
)
from .stage_contract import STAGE_ORDER


SCHEMA_VERSION = PIPELINE_SCHEMA_VERSION
LOCK_FILENAME = ".pipeline.lock"
CURRENT_FILENAME = "CURRENT"
MANIFEST_FILENAME = "run_manifest.json"
LEDGER_FILENAME = "run_ledger.jsonl"
RELEASE_ARTIFACT_NAMES = frozenset({
    "workbook",
    "audit",
    "loop_log",
    "visual_review",
    "run_report",
})
CLIENT_RELEASE_ARTIFACT_NAMES = RELEASE_ARTIFACT_NAMES | frozenset({
    "review_workbook",
    "human_review_report",
    "client_workbook",
})
VISUAL_SNAPSHOT_NAMES = frozenset({"image_manifest", "image_analysis"})
_TEMPLATE_KEYS = (
    "模板工作簿路径",
    "模板工作簿",
    "template_workbook_path",
    "template_workbook",
)
_NON_SEMANTIC_BRIEF_KEYS = frozenset({"input_row"})


def _brief_spec_payload(brief) -> dict:
    payload = dict(_redact_config(brief))
    for key in _NON_SEMANTIC_BRIEF_KEYS:
        payload.pop(key, None)
    return payload


def _parent_brief_matches(parent: dict, parent_dir: Path, expected: dict) -> bool:
    if parent.get("brief_spec_sha256") == sha256_json(expected):
        return True
    freeze_path = parent_dir / "input_freeze.json"
    try:
        frozen_brief = json.loads(freeze_path.read_text(encoding="utf-8")).get("brief") or {}
    except (OSError, json.JSONDecodeError):
        return False
    return _brief_spec_payload(frozen_brief) == expected


@dataclass
class PipelineRunState:
    run_id: str
    run_dir: Path
    manifest_path: Path
    ledger_path: Path
    lock_path: Path
    cache_fingerprint: str
    category_id: str = "launch"


@contextmanager
def open_pipeline_run(
    brief,
    cache_dir,
    source_root,
    cache_fingerprint,
    category_id=None,
    parent_run_id=None,
    provenance=None,
) -> PipelineRunState:
    cache_root = Path(cache_dir)
    lock_path = cache_root / LOCK_FILENAME
    ledger_path = cache_root / LEDGER_FILENAME
    run_id = generate_run_id()
    project_name = str(brief.get("项目名称") or brief.get("品牌或客户") or "")
    resolved_category_id = str(
        category_id
        or brief.get("category_id")
        or brief.get("类别ID")
        or "launch"
    ).strip()

    with exclusive_lock(lock_path, run_id=run_id):
        _reconcile_orphaned_runs(cache_root, ledger_path, project_name)
        run_dir = create_run_dir(cache_root / "runs", run_id)
        manifest_path = run_dir / MANIFEST_FILENAME
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "source_commit": configured_source_commit(),
            "run_id": run_id,
            "project_name": project_name,
            "brief_spec_sha256": sha256_json(_brief_spec_payload(brief)),
            "cache_fingerprint": cache_fingerprint,
            "status": "running",
            "started_at": _utc_now(),
        }
        if parent_run_id:
            manifest["parent_run_id"] = str(parent_run_id)
        if provenance:
            manifest["run_provenance"] = dict(provenance)
        initialize_stage_mapping(manifest, resolved_category_id)
        template_workbook = _template_workbook_record(brief, source_root)
        if template_workbook is not None:
            manifest["template_workbook"] = template_workbook
        write_json_atomic(manifest_path, manifest)
        append_jsonl_ledger(ledger_path, run_id=run_id, event="started")

        state = PipelineRunState(
            run_id=run_id,
            run_dir=run_dir,
            manifest_path=manifest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            cache_fingerprint=cache_fingerprint,
            category_id=resolved_category_id,
        )
        try:
            yield state
        except (Exception, SystemExit, KeyboardInterrupt) as exc:
            manifest = _read_manifest(state.manifest_path)
            if not manifest.get("bundle_finalized"):
                _write_exception_event(state, exc)
            raise
        else:
            if _read_manifest(state.manifest_path).get("status") == "running":
                _write_final_event(state, "finished")


@contextmanager
def resume_pipeline_run(
    run_id,
    cache_dir,
    *,
    category_id=None,
    input_sha256_by_stage=None,
    from_stage=None,
    retry_stage=None,
) -> PipelineRunState:
    cache_root = Path(cache_dir)
    normalized_run_id = str(run_id)
    if not normalized_run_id or Path(normalized_run_id).name != normalized_run_id:
        raise ValueError("invalid run_id")
    run_dir = cache_root / "runs" / normalized_run_id
    manifest_path = run_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"pipeline run manifest not found: {normalized_run_id}")

    lock_path = cache_root / LOCK_FILENAME
    ledger_path = cache_root / LEDGER_FILENAME
    with exclusive_lock(lock_path, run_id=normalized_run_id):
        manifest = _read_manifest(manifest_path)
        if manifest.get("run_id") != normalized_run_id:
            raise ValueError(f"run_id mismatch: {normalized_run_id}")
        current_source_commit = configured_source_commit()
        recorded_source_commit = str(manifest.get("source_commit") or "")
        legacy_without_source_commit = (
            not recorded_source_commit
            and manifest.get("schema_version") != SCHEMA_VERSION
        )
        if legacy_without_source_commit:
            manifest["source_commit"] = current_source_commit
        elif recorded_source_commit != current_source_commit:
            raise ValueError(
                "运行源提交已变化，拒绝在不同代码版本下恢复: "
                f"recorded={recorded_source_commit or 'unavailable'}, "
                f"current={current_source_commit}"
            )
        resolved_category_id = str(
            category_id
            or manifest.get("category_id")
            or "launch"
        ).strip()
        changed = prepare_resume_manifest(
            manifest,
            category_id=resolved_category_id,
            input_sha256_by_stage=input_sha256_by_stage,
            from_stage=from_stage,
            retry_stage=retry_stage,
        )
        changed |= legacy_without_source_commit
        if changed:
            write_json_atomic(manifest_path, manifest)
        yield PipelineRunState(
            run_id=normalized_run_id,
            run_dir=run_dir,
            manifest_path=manifest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            cache_fingerprint=str(manifest.get("cache_fingerprint") or ""),
            category_id=resolved_category_id,
        )


resume = resume_pipeline_run


@contextmanager
def fork_pipeline_run(
    parent_run_id,
    cache_dir,
    *,
    brief,
    source_root,
    cache_fingerprint,
    from_stage,
    category_id=None,
    provenance=None,
) -> PipelineRunState:
    cache_root = Path(cache_dir)
    normalized_parent_id = str(parent_run_id)
    if not normalized_parent_id or Path(normalized_parent_id).name != normalized_parent_id:
        raise ValueError("invalid parent_run_id")
    if from_stage is None:
        raise ValueError("fork run requires from_stage")
    parent_dir = cache_root / "runs" / normalized_parent_id
    parent_manifest_path = parent_dir / MANIFEST_FILENAME
    if not parent_manifest_path.is_file():
        raise FileNotFoundError(f"parent pipeline run manifest not found: {normalized_parent_id}")

    lock_path = cache_root / LOCK_FILENAME
    ledger_path = cache_root / LEDGER_FILENAME
    run_id = generate_run_id()
    with exclusive_lock(lock_path, run_id=run_id):
        parent = _read_manifest(parent_manifest_path)
        if parent.get("run_id") != normalized_parent_id:
            raise ValueError(f"parent run_id mismatch: {normalized_parent_id}")
        expected_brief = _brief_spec_payload(brief)
        expected_brief_sha256 = sha256_json(expected_brief)
        if not _parent_brief_matches(parent, parent_dir, expected_brief):
            raise ValueError("父运行 Brief 已变化，不能复用上游节点")
        expected_template = _template_workbook_record(brief, source_root)
        if parent.get("template_workbook") != expected_template:
            raise ValueError("父运行来源工作簿已变化，不能复用上游节点")
        resolved_category_id = str(
            category_id or parent.get("category_id") or "launch"
        ).strip()
        run_dir = create_run_dir(cache_root / "runs", run_id)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "source_commit": configured_source_commit(),
            "run_id": run_id,
            "parent_run_id": normalized_parent_id,
            "project_name": str(parent.get("project_name") or ""),
            "brief_spec_sha256": expected_brief_sha256,
            "cache_fingerprint": cache_fingerprint,
            "category_id": resolved_category_id,
            "status": "running",
            "started_at": _utc_now(),
        }
        if expected_template is not None:
            manifest["template_workbook"] = expected_template
        if provenance:
            manifest["run_provenance"] = dict(provenance)
        initialize_stage_mapping(manifest, resolved_category_id)
        _copy_reusable_stage_outputs(
            parent,
            parent_dir=parent_dir,
            child=manifest,
            child_dir=run_dir,
            from_stage=str(from_stage),
        )
        manifest_path = run_dir / MANIFEST_FILENAME
        write_json_atomic(manifest_path, manifest)
        append_jsonl_ledger(
            ledger_path,
            run_id=run_id,
            event="started",
            details={"parent_run_id": normalized_parent_id, "from_stage": str(from_stage)},
        )
        state = PipelineRunState(
            run_id=run_id,
            run_dir=run_dir,
            manifest_path=manifest_path,
            ledger_path=ledger_path,
            lock_path=lock_path,
            cache_fingerprint=cache_fingerprint,
            category_id=resolved_category_id,
        )
        try:
            yield state
        except (Exception, SystemExit, KeyboardInterrupt) as exc:
            current = _read_manifest(manifest_path)
            if not current.get("bundle_finalized"):
                _write_exception_event(state, exc)
            raise
        else:
            if _read_manifest(manifest_path).get("status") == "running":
                _write_final_event(state, "finished")


def _copy_reusable_stage_outputs(
    parent: dict,
    *,
    parent_dir: Path,
    child: dict,
    child_dir: Path,
    from_stage: str,
) -> None:
    stage_names = [stage.value for stage in STAGE_ORDER]
    if from_stage not in stage_names:
        raise ValueError(f"unknown from_stage: {from_stage}")
    stop = stage_names.index(from_stage)
    for stage_name in stage_names[:stop]:
        source_record = deepcopy(parent.get("stages", {}).get(stage_name) or {})
        if source_record.get("status") not in {"succeeded", "succeeded_with_warnings"}:
            raise ValueError(f"父运行节点不可复用: {stage_name}")
        copied_refs = []
        for index, value in enumerate(source_record.get("output_refs") or []):
            source = resolve_run_local_path(parent_dir, value)
            if not source.is_file():
                raise FileNotFoundError(f"父运行节点产物缺失: {stage_name}: {source}")
            try:
                relative = source.resolve().relative_to(parent_dir.resolve())
            except ValueError:
                relative = Path("reused") / stage_name / f"{index:02d}-{source.name}"
            target = child_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied_refs.append(str(target.resolve()))
        record = child["stages"][stage_name]
        record.update(source_record)
        record["run_id"] = child["run_id"]
        record["output_refs"] = copied_refs
        record["reused_from_run"] = str(parent.get("run_id") or "")


def finalize_pipeline_run(state, status, artifacts, metrics=None, visual_snapshots=None):
    manifest = _read_manifest(state.manifest_path)
    if manifest.get("status") != "running":
        return manifest
    manifest["status"] = status
    manifest["finished_at"] = _utc_now()
    manifest["artifacts"] = _artifact_records(artifacts)
    if visual_snapshots is not None:
        manifest["visual_snapshots"] = _artifact_records(visual_snapshots)
    if metrics is not None:
        manifest["metrics"] = metrics
    write_json_atomic(state.manifest_path, manifest)
    append_jsonl_ledger(
        state.ledger_path,
        run_id=state.run_id,
        event=status,
        details=_finalize_details(manifest["artifacts"], metrics),
    )
    return manifest


def publish_pipeline_run(
    state,
    status,
    artifacts,
    metrics=None,
    visual_snapshots=None,
    final_stage=None,
):
    """Finalize a legacy run without selecting an official project version.

    Gate-1 makes VersionService -> publication -> outbox the only formal
    publication authority. Legacy runs cannot update CURRENT from this path.
    """
    release_artifacts = _validated_release_artifacts(state, artifacts)
    release_snapshots = _validated_visual_snapshots(state, visual_snapshots)
    for path in (*release_artifacts.values(), *release_snapshots.values()):
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    if final_stage is not None:
        return _publish_pipeline_run_with_final_stage(
            state,
            status,
            release_artifacts,
            metrics,
            release_snapshots,
            final_stage,
        )
    manifest = finalize_pipeline_run(
        state,
        status,
        release_artifacts,
        metrics=metrics,
        visual_snapshots=release_snapshots or None,
    )
    manifest["bundle_finalized"] = True
    write_json_atomic(state.manifest_path, manifest)
    with state.manifest_path.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(state.run_dir)
    return manifest


def _publish_pipeline_run_with_final_stage(
    state,
    status,
    release_artifacts,
    metrics,
    release_snapshots,
    final_stage,
):
    initial = _read_manifest(state.manifest_path)
    stage_id = str(final_stage.get("stage_id") or "")
    if stage_id != "publish_feedback":
        raise ValueError("final_stage must be publish_feedback")
    record = initial.get("stages", {}).get(stage_id)
    if not isinstance(record, dict) or record.get("status") != "running":
        raise ValueError("publish_feedback must be running before publish")

    prepared = deepcopy(initial)
    prepared["status"] = "publishing"
    prepared.pop("finished_at", None)
    prepared.pop("bundle_finalized", None)
    prepared["artifacts"] = _artifact_records(release_artifacts)
    if release_snapshots:
        prepared["visual_snapshots"] = _artifact_records(release_snapshots)
    if metrics is not None:
        prepared["metrics"] = metrics

    try:
        write_json_atomic(state.manifest_path, prepared)
        with state.manifest_path.open("rb") as handle:
            os.fsync(handle.fileno())
        _fsync_directory(state.run_dir)
        completed = deepcopy(prepared)
        completed["status"] = status
        completed["finished_at"] = _utc_now()
        completed["bundle_finalized"] = True
        _complete_final_stage(completed, final_stage)
        write_json_atomic(state.manifest_path, completed)
        with state.manifest_path.open("rb") as handle:
            os.fsync(handle.fileno())
        _fsync_directory(state.run_dir)
        append_jsonl_ledger(
            state.ledger_path,
            run_id=state.run_id,
            event=status,
            details=_finalize_details(completed["artifacts"], metrics),
        )
        record_evolution_event(
            state.run_dir.parent.parent,
            run_id=state.run_id,
            event="version_adopted",
            details={
                "previous_run_id": "",
                "artifacts": completed["artifacts"],
                "quality_gate": (
                    "machine_pass_and_authorized_independent_ai_quality_pass"
                    if (metrics or {}).get("reviewer_kind") == "independent_ai"
                    else "machine_pass_and_human_quality_pass"
                ),
                "reviewer_kind": (metrics or {}).get("reviewer_kind", "human"),
            },
        )
        return completed
    except Exception as exc:
        failed = deepcopy(initial)
        failed["status"] = "failed"
        failed["finished_at"] = _utc_now()
        failed.pop("bundle_finalized", None)
        _fail_final_stage(failed, final_stage, exc)
        write_json_atomic(state.manifest_path, failed)
        with state.manifest_path.open("rb") as handle:
            os.fsync(handle.fileno())
        _fsync_directory(state.run_dir)
        append_jsonl_ledger(
            state.ledger_path,
            run_id=state.run_id,
            event="failed",
            details={"stage_id": stage_id, "code": "publish_feedback_publish_failed"},
        )
        raise


def resolve_current_release(cache_dir) -> dict[str, Path]:
    cache_root = Path(cache_dir)
    current_path = cache_root / CURRENT_FILENAME
    if not current_path.is_symlink():
        raise RuntimeError("CURRENT 不是发布目录指针")
    run_dir = current_path.resolve()
    runs_root = (cache_root / "runs").resolve()
    if run_dir.parent != runs_root or not run_dir.is_dir():
        raise RuntimeError("CURRENT 指向无效发布目录")
    run_id = run_dir.name
    manifest = _read_manifest(run_dir / MANIFEST_FILENAME)
    if manifest.get("run_id") != run_id:
        raise RuntimeError(f"CURRENT 与 run manifest 不匹配: {run_id}")
    return _resolve_run_artifacts(run_dir, manifest, context="CURRENT")


def resolve_pipeline_run(
    cache_dir,
    run_id,
    *,
    allowed_statuses: set[str] | frozenset[str] | None = None,
    artifact_names: set[str] | frozenset[str] | None = None,
) -> dict[str, Path]:
    cache_root = Path(cache_dir).resolve()
    normalized_run_id = str(run_id)
    if not normalized_run_id or Path(normalized_run_id).name != normalized_run_id:
        raise RuntimeError("run_id 无效")
    run_dir = (cache_root / "runs" / normalized_run_id).resolve()
    if run_dir.parent != (cache_root / "runs").resolve() or not run_dir.is_dir():
        raise RuntimeError(f"run 不存在: {normalized_run_id}")
    manifest = _read_manifest(run_dir / MANIFEST_FILENAME)
    if manifest.get("run_id") != normalized_run_id:
        raise RuntimeError(f"run 与 manifest 不匹配: {normalized_run_id}")
    if allowed_statuses is not None and manifest.get("status") not in allowed_statuses:
        raise RuntimeError(
            f"run 状态不允许当前操作: {manifest.get('status')}"
        )
    return _resolve_run_artifacts(
        run_dir,
        manifest,
        context="run",
        artifact_names=artifact_names,
    )


def _resolve_run_artifacts(
    run_dir: Path,
    manifest: dict,
    *,
    context: str,
    artifact_names: set[str] | frozenset[str] | None = None,
) -> dict[str, Path]:
    run_id = run_dir.name
    artifacts = manifest.get("artifacts") or {}
    if artifact_names is None:
        if frozenset(artifacts) not in {RELEASE_ARTIFACT_NAMES, CLIENT_RELEASE_ARTIFACT_NAMES}:
            raise RuntimeError(f"{context} 成员不完整: {run_id}")
        required_names = frozenset(artifacts)
    else:
        required_names = frozenset(artifact_names)
    if not required_names <= set(artifacts):
        raise RuntimeError(f"{context} 成员不完整: {run_id}")
    resolved = {}
    for name in required_names:
        record = artifacts[name]
        if not isinstance(record, dict) or not {"path", "sha256", "size_bytes"} <= set(record):
            raise RuntimeError(f"{context} 成员完整性记录缺失: {name}")
        path = resolve_run_local_path(run_dir, record["path"])
        if path.parent != run_dir or not path.is_file():
            raise RuntimeError(f"{context} 成员无效: {name}")
        if path.stat().st_size != record["size_bytes"] or sha256_file(path) != record["sha256"]:
            raise RuntimeError(f"{context} 成员完整性校验失败: {name}")
        resolved[name] = path
    return resolved


def migrate_legacy_release(cache_dir, public_paths, artifact_paths) -> dict[str, Path] | None:
    cache_root = Path(cache_dir).resolve()
    if set(public_paths) != RELEASE_ARTIFACT_NAMES or set(artifact_paths) != RELEASE_ARTIFACT_NAMES:
        raise RuntimeError("旧正式文件成员定义不完整")

    legacy_sources = {
        name: Path(path).resolve()
        for name, path in public_paths.items()
    }
    present = {
        name for name, path in legacy_sources.items()
        if path.exists() or path.is_symlink()
    }
    if not present:
        return None
    existing_legacy = _resolve_legacy_migration(cache_root, legacy_sources)
    if existing_legacy is not None:
        return existing_legacy
    if present == {"run_report"}:
        source = legacy_sources["run_report"]
        digest = sha256_file(source)
        backup = cache_root / "legacy-unbundled" / f"run_report-{digest[:16]}.json"
        if not backup.is_file():
            publish_file_atomic(source, backup)
        elif sha256_file(backup) != digest:
            raise RuntimeError("旧运行报告备份完整性校验失败")
        return {"run_report": backup}
    ordinary_files = {
        name for name, path in legacy_sources.items()
        if path.is_file() and not path.is_symlink()
    }
    if ordinary_files != RELEASE_ARTIFACT_NAMES:
        missing = sorted(RELEASE_ARTIFACT_NAMES - ordinary_files)
        raise RuntimeError(f"旧正式文件不完整，拒绝迁移: {missing}")

    filenames = {
        name: Path(path).name
        for name, path in artifact_paths.items()
    }
    if len(set(filenames.values())) != len(RELEASE_ARTIFACT_NAMES):
        raise RuntimeError("发布成员文件名冲突")

    source_records = {
        name: {
            "path": str(path.resolve()),
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for name, path in sorted(legacy_sources.items())
    }
    run_id = f"legacy-{sha256_json(source_records)[:24]}"
    existing_run = cache_root / "runs" / run_id
    if existing_run.is_dir():
        existing_run = existing_run.resolve()
        manifest = _read_manifest(existing_run / MANIFEST_FILENAME)
        if manifest.get("legacy_migration", {}).get("source_artifacts") != source_records:
            raise RuntimeError("旧正式文件迁移记录不一致")
        return _resolve_run_artifacts(existing_run, manifest, context="legacy migration")
    run_dir = create_run_dir(cache_root / "runs", run_id)
    migrated = {}
    for name, source in legacy_sources.items():
        migrated[name] = publish_file_atomic(source, run_dir / filenames[name])
    for path in migrated.values():
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "category_id": "launch",
        "status": "legacy",
        "started_at": _utc_now(),
        "finished_at": _utc_now(),
        "artifacts": _artifact_records(migrated),
        "bundle_finalized": True,
        "legacy_migration": {
            "source_artifacts": source_records,
            "migration_record": str((run_dir / "legacy_migration.json").resolve()),
        },
    }
    initialize_stage_mapping(manifest, "launch")
    write_json_atomic(run_dir / "legacy_migration.json", manifest["legacy_migration"])
    manifest_path = run_dir / MANIFEST_FILENAME
    write_json_atomic(manifest_path, manifest)
    with manifest_path.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(run_dir)
    return migrated


def _resolve_legacy_migration(
    cache_root: Path, legacy_sources: dict[str, Path]
) -> dict[str, Path] | None:
    if not all(path.is_symlink() for path in legacy_sources.values()):
        return None
    targets = {path.resolve().parent for path in legacy_sources.values()}
    if len(targets) != 1:
        return None
    run_dir = targets.pop()
    if run_dir.parent != (cache_root / "runs").resolve() or not run_dir.name.startswith("legacy-"):
        return None
    manifest = _read_manifest(run_dir / MANIFEST_FILENAME)
    if manifest.get("status") != "legacy" or not isinstance(manifest.get("legacy_migration"), dict):
        return None
    return _resolve_run_artifacts(run_dir, manifest, context="legacy migration")


def publish_symlink_atomic(target, link_path):
    target_path = Path(target)
    link = Path(link_path)
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.parent / f".{link.name}.{generate_run_id()}.tmp"
    try:
        temporary.symlink_to(target_path)
        os.replace(temporary, link)
        _fsync_directory(link.parent)
    except Exception:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise
    return link


def publish_file_atomic(source, target):
    source_path = Path(source)
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.",
        suffix=".tmp",
        dir=target_path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with source_path.open("rb") as source_handle, os.fdopen(fd, "wb") as temp_handle:
            while True:
                chunk = source_handle.read(1024 * 1024)
                if not chunk:
                    break
                temp_handle.write(chunk)
            temp_handle.flush()
            os.fsync(temp_handle.fileno())
        os.replace(temp_path, target_path)
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    return target_path


def _validated_release_artifacts(state, artifacts) -> dict[str, Path]:
    artifact_names = frozenset(artifacts)
    if artifact_names not in {RELEASE_ARTIFACT_NAMES, CLIENT_RELEASE_ARTIFACT_NAMES}:
        expected = (
            CLIENT_RELEASE_ARTIFACT_NAMES
            if artifact_names & (CLIENT_RELEASE_ARTIFACT_NAMES - RELEASE_ARTIFACT_NAMES)
            else RELEASE_ARTIFACT_NAMES
        )
        missing = sorted(expected - artifact_names)
        extra = sorted(artifact_names - expected)
        raise RuntimeError(f"发布成员不完整: missing={missing}, extra={extra}")
    absent = sorted(name for name, path in artifacts.items() if path is None)
    if absent:
        raise RuntimeError(f"发布成员未生成: {absent}")
    run_dir = state.run_dir.resolve()
    resolved = {name: Path(path).resolve() for name, path in artifacts.items()}
    invalid = sorted(
        name
        for name, path in resolved.items()
        if path.parent != run_dir or not path.is_file()
    )
    if invalid:
        raise RuntimeError(f"发布成员未完整写入 run 目录: {invalid}")
    return resolved


def _validated_visual_snapshots(state, snapshots) -> dict[str, Path]:
    if snapshots is None:
        return {}
    if set(snapshots) != VISUAL_SNAPSHOT_NAMES:
        missing = sorted(VISUAL_SNAPSHOT_NAMES - set(snapshots))
        extra = sorted(set(snapshots) - VISUAL_SNAPSHOT_NAMES)
        raise RuntimeError(f"视觉快照成员不完整: missing={missing}, extra={extra}")
    run_dir = state.run_dir.resolve()
    resolved = {name: Path(path).resolve() for name, path in snapshots.items()}
    invalid = sorted(
        name
        for name, path in resolved.items()
        if path.parent != run_dir or not path.is_file() or Path(snapshots[name]).is_symlink()
    )
    if invalid:
        raise RuntimeError(f"视觉快照未完整写入 run 目录: {invalid}")
    return resolved


def _complete_final_stage(manifest: dict, final_stage: dict) -> None:
    record = manifest["stages"][final_stage["stage_id"]]
    record["status"] = "succeeded"
    record["output_sha256"] = final_stage.get("output_sha256")
    record["output_refs"] = list(final_stage.get("output_refs") or ())
    record["finished_at"] = _utc_now()
    record["error"] = None


def _fail_final_stage(manifest: dict, final_stage: dict, error: Exception) -> None:
    record = manifest["stages"][final_stage["stage_id"]]
    record["status"] = "failed"
    record["output_sha256"] = final_stage.get("output_sha256")
    record["output_refs"] = list(final_stage.get("output_refs") or ())
    record["finished_at"] = _utc_now()
    record["error"] = {
        "category": "system",
        "code": "publish_feedback_publish_failed",
        "message": f"正式 bundle 原子发布失败: {error}",
        "retryable": True,
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_final_event(state: PipelineRunState, status: str) -> dict:
    manifest = _read_manifest(state.manifest_path)
    previous_status = manifest.get("status")
    if previous_status == status:
        return manifest
    manifest["status"] = status
    manifest["finished_at"] = _utc_now()
    write_json_atomic(state.manifest_path, manifest)
    details = None
    if previous_status != "running":
        details = {"supersedes": previous_status}
    append_jsonl_ledger(
        state.ledger_path,
        run_id=state.run_id,
        event=status,
        details=details,
    )
    return manifest


def _write_exception_event(state: PipelineRunState, exc: BaseException) -> dict:
    manifest = _read_manifest(state.manifest_path)
    previous_status = str(manifest.get("status") or "")
    selected_stage = ""
    selected_status = "failed"
    selected_error = None
    for stage in reversed(STAGE_ORDER):
        record = dict((manifest.get("stages") or {}).get(stage.value) or {})
        if record.get("status") in {"blocked", "failed"}:
            selected_stage = stage.value
            selected_status = str(record["status"])
            selected_error = dict(record.get("error") or {})
            break
    if not selected_error:
        selected_error = {
            "category": "system",
            "code": str(getattr(exc, "code", type(exc).__name__)),
            "message": str(exc) or "pipeline run failed",
            "retryable": False,
        }
    top_error = {"stage_id": selected_stage, **selected_error}
    manifest["status"] = selected_status
    manifest["error"] = top_error
    manifest["finished_at"] = _utc_now()
    write_json_atomic(state.manifest_path, manifest)
    append_jsonl_ledger(
        state.ledger_path,
        run_id=state.run_id,
        event=selected_status,
        details=(
            {"supersedes": previous_status}
            if previous_status and previous_status != "running"
            else {"error": top_error}
        ),
    )
    return manifest


def _reconcile_orphaned_runs(cache_root: Path, ledger_path: Path, project_name: str) -> None:
    current_run_dir = _resolve_current_run_dir(cache_root)
    recovered_run_ids = {
        row.get("run_id")
        for row in _read_ledger(ledger_path)
        if row.get("event") == "recovered"
    }
    for manifest_path in sorted((cache_root / "runs").glob(f"*/{MANIFEST_FILENAME}")):
        if current_run_dir is not None and manifest_path.parent.resolve() == current_run_dir:
            continue
        manifest = _read_manifest(manifest_path)
        if manifest.get("status") != "running" or manifest.get("project_name") != project_name:
            continue
        run_id = str(manifest.get("run_id") or manifest_path.parent.name)
        if run_id not in recovered_run_ids:
            append_jsonl_ledger(
                ledger_path,
                run_id=run_id,
                event="recovered",
                details={
                    "final_status": "failed",
                    "reason": "orphaned_running_run",
                },
            )
        recovered_at = _utc_now()
        manifest["status"] = "failed"
        manifest["finished_at"] = recovered_at
        manifest["recovered_at"] = recovered_at
        manifest["recovery_reason"] = "orphaned_running_run"
        write_json_atomic(manifest_path, manifest)


def _resolve_current_run_dir(cache_root: Path) -> Path | None:
    current_path = cache_root / CURRENT_FILENAME
    if not current_path.exists() and not current_path.is_symlink():
        return None
    artifacts = resolve_current_release(cache_root)
    return next(iter(artifacts.values())).parent


def _read_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _template_workbook_record(brief, source_root) -> dict | None:
    source_root_path = Path(source_root)
    for key in _TEMPLATE_KEYS:
        raw_value = str(brief.get(key) or "").strip()
        if not raw_value:
            continue
        candidate = Path(raw_value)
        if not candidate.is_absolute():
            candidate = source_root_path / candidate
        if candidate.exists() and candidate.is_file():
            resolved = candidate.resolve()
            return {
                "path": str(resolved),
                "sha256": sha256_file(resolved),
            }
    candidate = resolve_script_type_workbook(
        source_root_path,
        brief.get("脚本类型") or "发布会",
    )
    if candidate.exists() and candidate.is_file():
        resolved = candidate.resolve()
        return {
            "path": str(resolved),
            "sha256": sha256_file(resolved),
        }
    return None


def _artifact_records(artifacts) -> dict:
    records = {}
    for name, raw_path in artifacts.items():
        resolved = Path(raw_path).resolve()
        if resolved.exists() and resolved.is_file():
            records[name] = {
                "path": str(resolved),
                "sha256": sha256_file(resolved),
                "size_bytes": resolved.stat().st_size,
            }
            continue
        records[name] = {
            "path": str(resolved),
            "missing": True,
        }
    return records


def _finalize_details(artifacts: dict, metrics) -> dict:
    details = {"artifacts": artifacts}
    if metrics is not None:
        details["metrics"] = metrics
    return details


def _read_manifest(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
