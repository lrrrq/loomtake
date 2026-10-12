from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable

from .workspace_store import WorkspaceServiceError, new_id, utc_now


@dataclass(frozen=True)
class MutationRule:
    operation: str
    mutation_kind: str
    recovery_mode: str
    scope: str
    availability: str = "available"


# M+ 业务写入总则：新增动作必须先在这里声明其修改粒度与恢复方式。
MUTATION_RULES = MappingProxyType({
    "project.create": MutationRule("project.create", "append_immutable", "retain", "workspace"),
    "project.rename": MutationRule("project.rename", "patch_field", "audit", "project"),
    "project.archive": MutationRule("project.archive", "archive_project", "restore_30_days", "project"),
    "project.restore": MutationRule("project.restore", "restore_project", "retain", "project"),
    "project.purge": MutationRule("project.purge", "purge_project", "none", "project", "blocked"),
    "brief.append": MutationRule("brief.append", "append_immutable", "retain", "project"),
    "brief.resolve": MutationRule("brief.resolve", "append_immutable", "retain", "project"),
    "brief.confirm": MutationRule("brief.confirm", "advance_pointer", "history", "project"),
    "plan.prepare": MutationRule("plan.prepare", "append_immutable", "retain", "project"),
    "script.patch": MutationRule("script.patch", "patch_field", "history", "project"),
    "script.version": MutationRule("script.version", "append_immutable", "retain", "project"),
    "image.analysis": MutationRule("image.analysis", "append_immutable", "retain", "project"),
    "image.version": MutationRule("image.version", "append_immutable", "retain", "project"),
    "review.record": MutationRule("review.record", "append_immutable", "retain", "project"),
    "projection.rebuild": MutationRule("projection.rebuild", "rebuild_projection", "rebuild", "derived"),
    "ephemeral.cleanup": MutationRule("ephemeral.cleanup", "cleanup_ephemeral", "none", "ephemeral"),
})


def require_mutation_rule(operation: str) -> MutationRule:
    rule = MUTATION_RULES.get(operation)
    if rule is None:
        raise WorkspaceServiceError(
            "MUTATION_POLICY_UNDECLARED",
            f"业务写入 {operation} 未声明修改粒度与恢复方式",
        )
    if rule.availability != "available":
        raise WorkspaceServiceError(
            "MUTATION_PURGE_UNAVAILABLE",
            "永久物理清理尚未开放；项目继续保持可读取的归档状态",
        )
    return rule


def require_active_project(project) -> None:
    if project["status"] != "active":
        raise WorkspaceServiceError(
            "PROJECT_ARCHIVED",
            "项目已归档，业务资料保持只读；请先由管理员恢复项目",
        )


def record_mutation_event(
    connection,
    *,
    workspace_id: str,
    project_id: str,
    actor_id: str,
    operation: str,
    target_type: str,
    target_id: str,
    request_sha256: str,
    changed_fields: Iterable[str],
    preserved_scope: Iterable[str],
    confirmation_id: str | None,
) -> str:
    rule = require_mutation_rule(operation)
    event_id = new_id("mutation")
    connection.execute(
        """
        INSERT INTO mutation_events(
            mutation_event_id, workspace_id, project_id, actor_id, operation,
            mutation_kind, target_type, target_id, request_sha256,
            changed_fields_json, preserved_scope_json, recovery_mode,
            confirmation_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, workspace_id, project_id, actor_id, operation,
            rule.mutation_kind, target_type, target_id, request_sha256,
            json.dumps(sorted(set(changed_fields)), ensure_ascii=False, separators=(",", ":")),
            json.dumps(sorted(set(preserved_scope)), ensure_ascii=False, separators=(",", ":")),
            rule.recovery_mode, confirmation_id, utc_now(),
        ),
    )
    return event_id
