from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import secrets
from typing import Any

from .identity_service import require_permission
from .manual_actions import validate_manual_action_input
from .manual_version_commands import _audit, _claim_idempotency, _sha256, _token
from .mutation_policy import record_mutation_event, require_mutation_rule
from .workspace_store import WorkspaceServiceError, WorkspaceStore, enqueue_outbox, new_id, utc_now


RESTORE_WINDOW_DAYS = 30
_ACTIONS = {"archive_project", "restore_project"}
_BUSY_TASK_STATUSES = {
    "received", "understanding", "needs_clarification", "proposed",
    "waiting_confirmation", "executing", "waiting_user",
    "waiting_human_review",
}
_PRESERVED_SCOPE = (
    "brief_versions", "plans", "script_versions", "image_versions",
    "reviews", "runs", "artifacts", "task_history",
)


@dataclass(frozen=True)
class ManualProjectCommandServices:
    store: WorkspaceStore


class ManualProjectCommandService:
    """管理员项目生命周期命令；归档只切换状态，不删除业务资料。"""

    def __init__(self, services: ManualProjectCommandServices):
        self.services = services
        self.services.store.initialize()

    def prepare(self, auth_token: str, *, tool_name: str, parameters: dict[str, Any]) -> dict:
        normalized = self._validate(tool_name, parameters)
        rule = require_mutation_rule(f"project.{tool_name.removesuffix('_project')}")
        command_id, nonce = new_id("manual_command"), secrets.token_urlsafe(24)
        digest = _sha256({"tool_name": tool_name, "parameters": normalized})
        with self.services.store.transaction() as connection:
            context, project = self._context(connection, auth_token, normalized["project_id"], tool_name)
            self._validate_state(connection, project, tool_name, normalized["expected_project_version"])
            connection.execute(
                "INSERT INTO manual_command_confirmations VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (command_id, context["actor_id"], project["project_id"], tool_name, digest, _sha256(nonce), utc_now()),
            )
            _claim_idempotency(connection, context, tool_name, normalized["idempotency_key"], digest, command_id)
            _audit(connection, command_id, context, tool_name, "prepared", digest)
        action = "归档" if tool_name == "archive_project" else "恢复"
        return {
            "command_id": command_id,
            "command_token": f"{command_id}.{nonce}",
            "tool_name": tool_name,
            "parameters_sha256": digest,
            "risk": "R3",
            "channel": "manual_page",
            "will_change": f"仅将项目状态切换为{'已归档' if tool_name == 'archive_project' else '进行中'}，并增加项目锁版本。",
            "will_not_change": "不会删除或覆盖 Brief、规划、脚本、图片、审核、运行和历史产物；不会调用模型。",
            "recovery": "归档后 30 天内可由管理员恢复。" if tool_name == "archive_project" else "恢复后项目重新开放业务写入。",
            "mutation_kind": rule.mutation_kind,
            "confirmation_label": f"我已核对并确认{action}这个项目",
        }

    def execute(self, auth_token: str, *, tool_name: str, parameters: dict[str, Any], command_token: str) -> dict:
        normalized = self._validate(tool_name, parameters)
        command_id, nonce = _token(command_token)
        digest = _sha256({"tool_name": tool_name, "parameters": normalized})
        with self.services.store.transaction() as connection:
            context, project = self._context(connection, auth_token, normalized["project_id"], tool_name)
            row = connection.execute(
                "SELECT * FROM manual_command_confirmations WHERE command_id=?", (command_id,)
            ).fetchone()
            expected = (
                context["actor_id"], project["project_id"], tool_name,
                digest, _sha256(nonce),
            )
            if row is None or row["status"] != "pending" or (
                row["actor_id"], row["project_id"], row["tool_name"],
                row["parameters_sha256"], row["nonce_hash"],
            ) != expected:
                raise WorkspaceServiceError("PERMISSION_DENIED", "人工命令确认无效、已消费或不匹配")
            self._validate_state(connection, project, tool_name, normalized["expected_project_version"])
            next_status = "archived" if tool_name == "archive_project" else "active"
            updated = connection.execute(
                """
                UPDATE projects SET status=?, lock_version=lock_version+1, updated_at=?
                WHERE project_id=? AND lock_version=? AND status=?
                """,
                (
                    next_status, utc_now(), project["project_id"],
                    normalized["expected_project_version"], project["status"],
                ),
            )
            if updated.rowcount != 1:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目在确认期间已变化")
            operation = "project.archive" if tool_name == "archive_project" else "project.restore"
            mutation_event_id = record_mutation_event(
                connection,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                actor_id=context["actor_id"],
                operation=operation,
                target_type="project",
                target_id=project["project_id"],
                request_sha256=digest,
                changed_fields=("status", "lock_version", "updated_at"),
                preserved_scope=_PRESERVED_SCOPE,
                confirmation_id=command_id,
            )
            connection.execute(
                "UPDATE manual_command_confirmations SET status='consumed', consumed_at=? WHERE command_id=?",
                (utc_now(), command_id),
            )
            connection.execute(
                "UPDATE manual_command_idempotency SET status='consumed', updated_at=? WHERE command_id=?",
                (utc_now(), command_id),
            )
            _audit(connection, command_id, context, tool_name, "completed", digest)
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project["project_id"],
                event_type=f"project.{next_status}",
                payload={"mutation_event_id": mutation_event_id, "project_id": project["project_id"]},
            )
        return {
            "command_id": command_id,
            "status": "completed",
            "result": {
                "project_id": project["project_id"],
                "project_status": next_status,
                "project_version": normalized["expected_project_version"] + 1,
                "mutation_event_id": mutation_event_id,
                "preserved_scope": list(_PRESERVED_SCOPE),
            },
        }

    def recovery_status(self, auth_token: str, project_id: str) -> dict:
        with self.services.store.connection() as connection:
            project = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            if project is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            require_permission(
                connection, auth_token, workspace_id=project["workspace_id"],
                project_id=project_id, permission="project.read",
            )
            if project["status"] != "archived":
                return {"available": False, "reason": "PROJECT_NOT_ARCHIVED", "remaining_days": 0}
            archived_at = self._archive_time(connection, project_id)
            expires_at = archived_at + timedelta(days=RESTORE_WINDOW_DAYS)
            remaining = max(0, (expires_at - datetime.now(timezone.utc)).total_seconds())
            return {
                "available": remaining > 0,
                "reason": "" if remaining > 0 else "PROJECT_ARCHIVE_EXPIRED",
                "archived_at": _iso(archived_at),
                "restore_expires_at": _iso(expires_at),
                "remaining_days": int((remaining + 86399) // 86400),
            }

    def _context(self, connection, auth_token: str, project_id: str, tool_name: str):
        project = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
        if project is None:
            raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
        permission = "project.archive" if tool_name == "archive_project" else "project.restore"
        actor = require_permission(
            connection, auth_token, workspace_id=project["workspace_id"],
            project_id=project_id, permission=permission,
        )
        return {"actor_id": actor["actor_id"], "project_id": project_id}, project

    def _validate_state(self, connection, project, tool_name: str, expected_version: int) -> None:
        if project["lock_version"] != expected_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目版本已变化，请刷新后重新确认")
        if tool_name == "archive_project":
            if project["status"] != "active":
                raise WorkspaceServiceError("PROJECT_ALREADY_ARCHIVED", "项目已经归档")
            placeholders = ",".join("?" for _ in _BUSY_TASK_STATUSES)
            busy = connection.execute(
                f"SELECT 1 FROM tasks WHERE project_id=? AND status IN ({placeholders}) LIMIT 1",
                (project["project_id"], *sorted(_BUSY_TASK_STATUSES)),
            ).fetchone()
            if busy is not None:
                raise WorkspaceServiceError("PROJECT_BUSY", "项目仍有进行中或等待确认的任务，不能归档")
            return
        if project["status"] != "archived":
            raise WorkspaceServiceError("PROJECT_NOT_ARCHIVED", "项目当前不是归档状态")
        archived_at = self._archive_time(connection, project["project_id"])
        if datetime.now(timezone.utc) > archived_at + timedelta(days=RESTORE_WINDOW_DAYS):
            raise WorkspaceServiceError("PROJECT_ARCHIVE_EXPIRED", "30 天恢复窗口已结束；永久清理尚未开放")

    @staticmethod
    def _archive_time(connection, project_id: str) -> datetime:
        row = connection.execute(
            """
            SELECT created_at FROM mutation_events
            WHERE project_id=? AND operation='project.archive'
            ORDER BY created_at DESC, mutation_event_id DESC LIMIT 1
            """,
            (project_id,),
        ).fetchone()
        if row is None:
            raise WorkspaceServiceError("PROJECT_ARCHIVE_EVIDENCE_MISSING", "归档记录缺失，不能自动恢复")
        return datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))

    @staticmethod
    def _validate(tool_name: str, parameters: dict[str, Any]) -> dict:
        if tool_name not in _ACTIONS:
            raise WorkspaceServiceError("INVALID_INPUT", "未知项目生命周期命令")
        try:
            payload = validate_manual_action_input(
                tool_name,
                {**parameters, "confirmation_token": "manual-command-placeholder-000000"},
            ).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "项目生命周期命令参数不符合合同") from exc
        payload.pop("confirmation_token", None)
        return payload


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
