from __future__ import annotations

from .revision_service import REVISION_REPORT_SCHEMA_VERSION
from .workspace_store import WorkspaceServiceError, require_artifact_ref, require_nonempty


def summarize_revision_experience(
    revision_report: dict,
    *,
    evidence_refs: list[str],
) -> dict:
    """Create a pending project-level learning proposal from verified revision facts."""
    if not isinstance(revision_report, dict) or revision_report.get(
        "schema_version"
    ) != REVISION_REPORT_SCHEMA_VERSION:
        raise WorkspaceServiceError("INVALID_INPUT", "revision report 合同无效")
    completion = dict(revision_report.get("completion") or {})
    human = dict((revision_report.get("statuses") or {}).get("human_review") or {})
    machine = dict((revision_report.get("statuses") or {}).get("machine_review") or {})
    if (
        completion.get("status") != "completed"
        or completion.get("completed") is not True
        or machine.get("status") != "pass"
        or human.get("status") != "quality_pass"
    ):
        raise WorkspaceServiceError(
            "REVIEW_REQUIRED",
            "只有机器审核与人工质量均通过的完整修订，才能形成经验候选",
        )
    project_id = require_nonempty(revision_report.get("project_id"), "project_id")
    feedback = require_nonempty(
        revision_report.get("original_feedback"), "original_feedback"
    )
    refs = [require_artifact_ref(value, "evidence_ref") for value in evidence_refs]
    if not refs or len(refs) != len(set(refs)):
        raise WorkspaceServiceError(
            "INVALID_INPUT", "evidence_refs 必须非空且不能重复"
        )
    actual = dict(revision_report.get("actual_impact") or {})
    fields = [str(value) for value in actual.get("changed_fields") or []]
    shots = [str(value) for value in actual.get("changed_shot_nos") or []]
    rerun_nodes = [str(value) for value in revision_report.get("rerun_nodes") or []]
    locks = [
        f"镜{item.get('shot_no') or item.get('shot_id')}:{item.get('field')}"
        for item in (revision_report.get("locked_field_verification") or {}).get(
            "checks", []
        )
        if item.get("status") == "preserved"
    ]
    statement = (
        f"已验证反馈“{feedback}”："
        f"修改镜头 {('、'.join(shots) or '未标镜号')} 的 "
        f"{('、'.join(fields) or '已核定字段')}；"
        f"重跑 {('、'.join(rerun_nodes) or '无额外节点')}；"
        f"保持 {('、'.join(locks) or '报告所列锁定内容')}。"
    )
    return {
        "schema_version": "mplus-knowledge-learning-proposal/v1",
        "scope": "project",
        "target_id": project_id,
        "statement": statement[:4000],
        "evidence_refs": refs,
        "source": {
            "parent_version_id": revision_report.get("parent_version_id"),
            "child_version_id": revision_report.get("child_version_id"),
            "completion_status": completion["status"],
        },
        "status": "pending_approval",
        "provider_calls": 0,
        "active_rule": False,
        "company_rule": False,
        "next_step": "由用户核对后调用 create_knowledge_candidate；公司级推广必须另建候选并由管理员批准。",
    }
