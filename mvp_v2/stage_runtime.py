from __future__ import annotations

import json
from pathlib import Path

from .run_contract import sha256_file
from .portable_paths import resolve_run_local_path
from .run_state import write_json_atomic
from .stage_contract import (
    STAGE_DEPENDENCIES,
    STAGE_OPTIONAL_DEPENDENCIES,
    STAGE_ORDER,
    StageError,
    StageErrorCategory,
    StageId,
    StageManifest,
    StageStatus,
)


PIPELINE_SCHEMA_VERSION = "stage1-pipeline-runtime/v3"
LEGACY_PIPELINE_SCHEMA_VERSIONS = {
    "stage1-pipeline-runtime/v1",
    "stage1-pipeline-runtime/v2",
}
_SUCCESS_STATUSES = {
    StageStatus.SUCCEEDED.value,
    StageStatus.SUCCEEDED_WITH_WARNINGS.value,
}
_BLOCKED_ERROR_CATEGORIES = {
    StageErrorCategory.INPUT.value,
    StageErrorCategory.DEPENDENCY.value,
    StageErrorCategory.CONTRACT.value,
}
_FAILED_ERROR_CATEGORIES = {
    StageErrorCategory.EXECUTION.value,
    StageErrorCategory.SYSTEM.value,
}


def initialize_stage_mapping(manifest: dict, category_id: str | None = None) -> bool:
    schema_version = manifest.get("schema_version")
    if schema_version not in LEGACY_PIPELINE_SCHEMA_VERSIONS | {PIPELINE_SCHEMA_VERSION}:
        raise ValueError(f"unsupported pipeline manifest schema: {schema_version}")

    resolved_category_id = str(
        category_id
        or manifest.get("category_id")
        or _existing_category_id(manifest)
        or "launch"
    ).strip()
    if not resolved_category_id:
        raise ValueError("category_id must not be blank")
    existing_category_id = manifest.get("category_id")
    if existing_category_id and existing_category_id != resolved_category_id:
        raise ValueError(
            f"category_id mismatch: {existing_category_id} != {resolved_category_id}"
        )

    changed = False
    if schema_version != PIPELINE_SCHEMA_VERSION:
        manifest["schema_version"] = PIPELINE_SCHEMA_VERSION
        changed = True
    if existing_category_id != resolved_category_id:
        manifest["category_id"] = resolved_category_id
        changed = True

    stages = manifest.get("stages")
    if stages is None:
        stages = {}
        manifest["stages"] = stages
        changed = True
    if not isinstance(stages, dict):
        raise ValueError("pipeline manifest stages must be a mapping")

    run_id = str(manifest.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run_id must not be blank")
    for stage_id in STAGE_ORDER:
        key = stage_id.value
        if key in stages:
            record = stages[key]
            if not isinstance(record, dict) or record.get("stage_id") != key:
                raise ValueError(f"invalid stage manifest: {key}")
            continue
        stages[key] = StageManifest(
            run_id=run_id,
            category_id=resolved_category_id,
            stage_id=stage_id,
            status=StageStatus.NOT_STARTED,
        ).to_dict()
        changed = True
    return changed


def start_pipeline_stage(
    state,
    stage_id,
    *,
    input_sha256: str | None = None,
    input_refs=(),
    log_ref: str | None = None,
    force: bool = False,
) -> dict:
    manifest = _read_manifest(state.manifest_path)
    _reject_finalized_bundle(manifest)
    changed = initialize_stage_mapping(
        manifest,
        getattr(state, "category_id", None),
    )
    normalized_stage_id = _stage_id(stage_id)
    _validate_dependencies(manifest, normalized_stage_id)
    record = manifest["stages"][normalized_stage_id.value]
    status = record.get("status")

    if not force and input_sha256 == record.get("input_sha256"):
        if status in _SUCCESS_STATUSES or status == StageStatus.RUNNING.value:
            if changed:
                write_json_atomic(state.manifest_path, manifest)
            return record

    if force or (
        status in _SUCCESS_STATUSES | {StageStatus.RUNNING.value}
        and input_sha256 != record.get("input_sha256")
    ):
        _invalidate_records(manifest, _descendants(normalized_stage_id))

    _start_record(
        record,
        input_sha256=input_sha256,
        input_refs=input_refs,
        log_ref=log_ref,
    )
    _reopen_run(manifest)
    write_json_atomic(state.manifest_path, manifest)
    return record


def reused_fork_stage_output(
    state,
    stage_id,
    filename: str,
) -> Path | None:
    """返回 fork 时复制并冻结的上游单文件产物。"""
    manifest = _read_manifest(state.manifest_path)
    record = (manifest.get("stages") or {}).get(_stage_id(stage_id).value) or {}
    if record.get("status") not in _SUCCESS_STATUSES:
        return None
    if not str(record.get("reused_from_run") or "").strip():
        return None
    refs = list(record.get("output_refs") or [])
    if len(refs) != 1:
        return None
    path = resolve_run_local_path(state.run_dir, refs[0])
    expected = state.run_dir / filename
    if path.resolve() != expected.resolve() or not path.is_file():
        return None
    expected_sha256 = str(record.get("output_sha256") or "")
    if expected_sha256 and sha256_file(path) != expected_sha256:
        return None
    return path


def finish_pipeline_stage(
    state,
    stage_id,
    status,
    *,
    output_sha256: str | None = None,
    output_refs=(),
    log_ref: str | None = None,
    error: StageError | dict | None = None,
) -> dict:
    normalized_status = _stage_status(status)
    allowed_statuses = {
        StageStatus.SUCCEEDED,
        StageStatus.SUCCEEDED_WITH_WARNINGS,
        StageStatus.BLOCKED,
        StageStatus.FAILED,
        StageStatus.WAITING_HUMAN_REVIEW,
        StageStatus.INVALIDATED,
    }
    if normalized_status not in allowed_statuses:
        raise ValueError(f"stage cannot finish with status: {normalized_status.value}")

    manifest = _read_manifest(state.manifest_path)
    _reject_finalized_bundle(manifest)
    initialize_stage_mapping(manifest, getattr(state, "category_id", None))
    normalized_stage_id = _stage_id(stage_id)
    record = manifest["stages"][normalized_stage_id.value]
    current_status = record.get("status")
    if current_status == StageStatus.BLOCKED.value and normalized_status == StageStatus.FAILED:
        raise ValueError("blocked stage cannot be converted to failed")
    if normalized_status == StageStatus.INVALIDATED:
        _invalidate_records(manifest, (normalized_stage_id,))
        write_json_atomic(state.manifest_path, manifest)
        return record
    if current_status != StageStatus.RUNNING.value:
        raise ValueError(
            f"stage must be running before finish: {normalized_stage_id.value}"
        )

    error_payload = _error_payload(error)
    if normalized_status == StageStatus.BLOCKED:
        _validate_error_category(error_payload, _BLOCKED_ERROR_CATEGORIES, "blocked")
    elif normalized_status == StageStatus.FAILED:
        _validate_error_category(error_payload, _FAILED_ERROR_CATEGORIES, "failed")
    elif error_payload is not None:
        raise ValueError("only blocked or failed stage may include an error")

    record["status"] = normalized_status.value
    record["output_sha256"] = output_sha256
    record["output_refs"] = list(output_refs)
    record["finished_at"] = _utc_now()
    if log_ref is not None:
        record["log_ref"] = log_ref
    record["error"] = error_payload
    write_json_atomic(state.manifest_path, manifest)
    return record


def prepare_resume_manifest(
    manifest: dict,
    *,
    category_id: str | None = None,
    input_sha256_by_stage=None,
    from_stage=None,
    retry_stage=None,
) -> bool:
    if from_stage is not None and retry_stage is not None:
        raise ValueError("from_stage and retry_stage cannot be used together")
    if manifest.get("bundle_finalized"):
        if from_stage is not None or retry_stage is not None or input_sha256_by_stage:
            raise ValueError("finalized bundle cannot be reopened")
        return False

    changed = initialize_stage_mapping(manifest, category_id)
    stages = manifest["stages"]
    normalized_hashes = {
        _stage_id(stage_id): sha256
        for stage_id, sha256 in (input_sha256_by_stage or {}).items()
    }
    for stage_id in STAGE_ORDER:
        if stage_id not in normalized_hashes:
            continue
        record = stages[stage_id.value]
        expected_sha256 = normalized_hashes[stage_id]
        if (
            record.get("status") in _SUCCESS_STATUSES
            and record.get("input_sha256") != expected_sha256
        ):
            changed |= _invalidate_records(
                manifest,
                (stage_id, *_descendants(stage_id)),
            )
            record["input_sha256"] = expected_sha256
            changed = True

    if from_stage is not None:
        normalized_from_stage = _stage_id(from_stage)
        changed |= _invalidate_records(
            manifest,
            (normalized_from_stage, *_descendants(normalized_from_stage)),
        )

    if retry_stage is not None:
        normalized_retry_stage = _stage_id(retry_stage)
        record = stages[normalized_retry_stage.value]
        if int(record.get("attempt") or 0) <= 0:
            raise ValueError(
                f"stage has no attempt to retry: {normalized_retry_stage.value}"
            )
        _validate_dependencies(manifest, normalized_retry_stage)
        changed |= _invalidate_records(
            manifest,
            _descendants(normalized_retry_stage),
        )
        if record.get("status") != StageStatus.RUNNING.value:
            _start_record(
                record,
                input_sha256=record.get("input_sha256"),
                input_refs=record.get("input_refs") or (),
                log_ref=None,
            )
            changed = True
        changed |= _reopen_run(manifest)
    return changed


def _start_record(record: dict, *, input_sha256, input_refs, log_ref) -> None:
    record.pop("reused_from_run", None)
    record["status"] = StageStatus.RUNNING.value
    record["attempt"] = int(record.get("attempt") or 0) + 1
    record["input_sha256"] = input_sha256
    record["input_refs"] = list(input_refs)
    record["output_sha256"] = None
    record["output_refs"] = []
    record["started_at"] = _utc_now()
    record["finished_at"] = None
    record["log_ref"] = log_ref
    record["error"] = None


def _invalidate_records(manifest: dict, stage_ids) -> bool:
    changed = False
    invalidated_at = None
    for stage_id in stage_ids:
        record = manifest["stages"][_stage_id(stage_id).value]
        if record.get("status") == StageStatus.INVALIDATED.value:
            continue
        if invalidated_at is None:
            invalidated_at = _utc_now()
        record["status"] = StageStatus.INVALIDATED.value
        record["finished_at"] = invalidated_at
        record["output_sha256"] = None
        record["output_refs"] = []
        record["error"] = None
        changed = True
    return changed


def _descendants(stage_id: StageId) -> tuple[StageId, ...]:
    descendants = set()
    for candidate in STAGE_ORDER:
        if any(
            dependency == stage_id or dependency in descendants
            for dependency in (
                *STAGE_DEPENDENCIES[candidate],
                *STAGE_OPTIONAL_DEPENDENCIES[candidate],
            )
        ):
            descendants.add(candidate)
    return tuple(candidate for candidate in STAGE_ORDER if candidate in descendants)


def _existing_category_id(manifest: dict) -> str | None:
    stages = manifest.get("stages")
    if not isinstance(stages, dict):
        return None
    for record in stages.values():
        if isinstance(record, dict) and record.get("category_id"):
            return str(record["category_id"])
    return None


def _validate_dependencies(manifest: dict, stage_id: StageId) -> None:
    stages = manifest["stages"]
    missing = [
        dependency.value
        for dependency in STAGE_DEPENDENCIES[stage_id]
        if stages[dependency.value].get("status") not in _SUCCESS_STATUSES
    ]
    if missing:
        raise ValueError(
            f"stage dependencies not satisfied for {stage_id.value}: {', '.join(missing)}"
        )


def _reject_finalized_bundle(manifest: dict) -> None:
    if manifest.get("bundle_finalized"):
        raise ValueError("finalized bundle cannot be reopened")


def _reopen_run(manifest: dict) -> bool:
    changed = manifest.get("status") != "running" or "finished_at" in manifest
    manifest["status"] = "running"
    manifest.pop("finished_at", None)
    return changed


def _stage_id(value) -> StageId:
    if isinstance(value, StageId):
        return value
    return StageId(str(value))


def _stage_status(value) -> StageStatus:
    if isinstance(value, StageStatus):
        return value
    return StageStatus(str(value))


def _error_payload(error) -> dict | None:
    if error is None:
        return None
    if isinstance(error, StageError):
        return error.to_dict()
    if not isinstance(error, dict):
        raise TypeError("stage error must be StageError or mapping")
    required = {"category", "code", "message", "retryable"}
    if not required <= set(error):
        raise ValueError("stage error fields are incomplete")
    normalized = StageError(
        category=StageErrorCategory(error["category"]),
        code=str(error["code"]),
        message=str(error["message"]),
        retryable=bool(error["retryable"]),
    )
    return normalized.to_dict()


def _validate_error_category(error: dict | None, allowed: set[str], status: str) -> None:
    if error is None:
        raise ValueError(f"{status} stage must include an error")
    if error["category"] not in allowed:
        raise ValueError(f"{status} stage uses invalid error category")


def _read_manifest(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
