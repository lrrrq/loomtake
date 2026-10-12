from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import uuid

from .evolution import record_evolution_event
from .run_state import append_jsonl_ledger, write_json_atomic


FEEDBACK_SCHEMA_VERSION = "mplus-user-feedback/v1"
FEEDBACK_STATUS_SCHEMA_VERSION = "mplus-user-feedback-status/v1"
FEEDBACK_STATUSES = {"received", "processing", "waiting_user", "resolved", "rejected"}
FEEDBACK_STAGES = {
    "image": "visual_binding",
    "style": "script_draft",
    "detail": "script_draft",
    "copy": "script_draft",
    "brand": "script_plan",
    "fact": "script_plan",
    "structure": "script_plan",
    "technical": "input_freeze",
}


def store_user_feedback(
    run_dir: str | Path,
    *,
    run_id: str,
    category: str,
    comment: str,
    scope: str = "whole_film",
    shot_no: str = "",
    severity: str = "normal",
    desired_change: str = "",
    keep_fields: list[str] | None = None,
    selected_image_id: str = "",
    selected_image_path: str = "",
    supplemental_image_path: str = "",
    approve_text_only: bool = False,
    prior_constraints_path: str | Path | None = None,
    prior_constraints: dict | None = None,
) -> dict:
    normalized_category = str(category).strip().casefold()
    if normalized_category not in FEEDBACK_STAGES:
        raise ValueError(f"未知反馈类型: {category}")
    if not str(comment).strip():
        raise ValueError("反馈内容不得为空")
    feedback_id = uuid.uuid4().hex
    payload = {
        "schema_version": FEEDBACK_SCHEMA_VERSION,
        "feedback_id": feedback_id,
        "parent_run_id": run_id,
        "scope": scope,
        "shot_no": str(shot_no).strip(),
        "category": normalized_category,
        "severity": severity,
        "comment": str(comment).strip(),
        "desired_change": str(desired_change).strip(),
        "keep_fields": [str(item).strip() for item in (keep_fields or []) if str(item).strip()],
        "selected_image_id": str(selected_image_id).strip(),
        "selected_image_path": str(selected_image_path).strip(),
        "supplemental_image_path": str(supplemental_image_path).strip(),
        "approve_text_only": bool(approve_text_only),
        "restart_stage": (
            "media_manifest"
            if normalized_category == "image" and str(supplemental_image_path).strip()
            else FEEDBACK_STAGES[normalized_category]
        ),
        "status": "received",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    feedback_dir = Path(run_dir) / "feedback"
    feedback_dir.mkdir(parents=True, exist_ok=True)
    feedback_path = feedback_dir / f"{feedback_id}.json"
    constraints_path = feedback_dir / f"{feedback_id}.constraints.json"
    write_json_atomic(feedback_path, payload)
    previous = dict(prior_constraints or load_revision_constraints(prior_constraints_path))
    user_feedback = list(previous.get("user_feedback") or [])
    user_feedback.append({
        "scope": scope,
        "shot_no": payload["shot_no"],
        "category": normalized_category,
        "comment": payload["comment"],
        "desired_change": payload["desired_change"],
        "keep_fields": payload["keep_fields"],
        "selected_image_id": payload["selected_image_id"],
        "selected_image_path": payload["selected_image_path"],
        "supplemental_image_path": payload["supplemental_image_path"],
        "approve_text_only": payload["approve_text_only"],
    })
    constraints = {
        "schema_version": "mplus-ui-revision-constraints/v1",
        "parent_run_id": run_id,
        "feedback_id": feedback_id,
        "restart_stage": payload["restart_stage"],
        "user_feedback": user_feedback,
    }
    write_json_atomic(constraints_path, constraints)
    record_evolution_event(
        Path(run_dir).parent.parent,
        run_id=run_id,
        event="feedback_received",
        details={
            "feedback_id": feedback_id,
            "category": normalized_category,
            "scope": scope,
            "shot_no": payload["shot_no"],
            "severity": severity,
            "restart_stage": payload["restart_stage"],
            "selected_image_id": payload["selected_image_id"],
            "reason": payload["comment"],
        },
    )
    record_feedback_status(constraints_path, "received")
    return {
        **payload,
        "feedback_path": str(feedback_path.resolve()),
        "constraints_path": str(constraints_path.resolve()),
    }


def record_feedback_status(
    constraints_path: str | Path | None,
    status: str,
    *,
    child_job_id: str = "",
    child_run_id: str = "",
    resolution_note: str = "",
) -> dict:
    if not constraints_path:
        return {}
    normalized = str(status).strip().casefold()
    if normalized not in FEEDBACK_STATUSES:
        raise ValueError(f"未知反馈状态: {status}")
    constraints_file = Path(constraints_path)
    constraints = json.loads(constraints_file.read_text(encoding="utf-8"))
    feedback_id = str(constraints.get("feedback_id") or "").strip()
    if not feedback_id:
        raise ValueError("修改约束缺少 feedback_id")
    feedback_path = constraints_file.with_name(f"{feedback_id}.json")
    feedback = json.loads(feedback_path.read_text(encoding="utf-8"))
    status_path = feedback_path.with_name(f"{feedback_id}.status.json")
    current = {}
    if status_path.is_file():
        current = json.loads(status_path.read_text(encoding="utf-8"))
    payload = {
        "schema_version": FEEDBACK_STATUS_SCHEMA_VERSION,
        "feedback_id": feedback_id,
        "parent_run_id": str(feedback.get("parent_run_id") or ""),
        "status": normalized,
        "child_job_id": str(child_job_id or current.get("child_job_id") or ""),
        "child_run_id": str(child_run_id or current.get("child_run_id") or ""),
        "resolution_note": str(resolution_note or current.get("resolution_note") or ""),
        "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    comparable = {key: value for key, value in payload.items() if key != "updated_at"}
    current_comparable = {key: value for key, value in current.items() if key != "updated_at"}
    if comparable == current_comparable:
        return {**current, "status_path": str(status_path.resolve())}
    write_json_atomic(status_path, payload)
    append_jsonl_ledger(
        feedback_path.with_name(f"{feedback_id}.events.jsonl"),
        run_id=payload["parent_run_id"],
        event="feedback_status_changed",
        details={
            "feedback_id": feedback_id,
            "status": normalized,
            "child_job_id": payload["child_job_id"],
            "child_run_id": payload["child_run_id"],
            "resolution_note": payload["resolution_note"],
        },
    )
    try:
        project_cache = feedback_path.parents[3]
        record_evolution_event(
            project_cache,
            run_id=payload["child_run_id"] or payload["parent_run_id"],
            event="feedback_status_changed",
            details={
                "feedback_id": feedback_id,
                "parent_run_id": payload["parent_run_id"],
                "status": normalized,
                "child_job_id": payload["child_job_id"],
                "child_run_id": payload["child_run_id"],
                "resolution_note": payload["resolution_note"],
            },
        )
    except IndexError:
        pass
    return {**payload, "status_path": str(status_path.resolve())}


def load_feedback_status(constraints_path: str | Path | None) -> dict:
    if not constraints_path:
        return {}
    try:
        constraints_file = Path(constraints_path)
        constraints = json.loads(constraints_file.read_text(encoding="utf-8"))
        feedback_id = str(constraints.get("feedback_id") or "").strip()
        status_path = constraints_file.with_name(f"{feedback_id}.status.json")
        return json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def load_revision_constraints(path: str | Path | None) -> dict:
    if not path:
        return {}
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "user_feedback": list(payload.get("user_feedback") or payload.get("constraints") or []),
        "parent_run_id": str(payload.get("parent_run_id") or ""),
        "feedback_id": str(payload.get("feedback_id") or ""),
    }


def find_child_revision_constraints(
    project_cache_dir: str | Path,
    child_run_id: str,
) -> Path | None:
    """Recover the feedback constraints that created a child run.

    A manual resume only supplies the child run id. Without restoring these
    constraints, input hashing can invalidate ScriptPlan and silently rebuild
    the uncorrected parent draft.
    """
    cache_dir = Path(project_cache_dir)
    child_id = str(child_run_id or "").strip()
    if not child_id or Path(child_id).name != child_id:
        return None
    manifest_path = cache_dir / "runs" / child_id / "run_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    parent_id = str(manifest.get("parent_run_id") or "").strip()
    if not parent_id or Path(parent_id).name != parent_id:
        return None
    feedback_dir = cache_dir / "runs" / parent_id / "feedback"
    for status_path in sorted(feedback_dir.glob("*.status.json")):
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(status.get("child_run_id") or "").strip() != child_id:
            continue
        feedback_id = str(status.get("feedback_id") or "").strip()
        constraints_path = feedback_dir / f"{feedback_id}.constraints.json"
        if feedback_id and constraints_path.is_file():
            return constraints_path.resolve()
    return None
