"""Administrator-only diagnostics and immutable recovery attempts."""

from __future__ import annotations

import json
import re
from typing import Any

from .identity_service import require_permission
from .run_contract import sha256_json
from .task_service import TaskService
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    enqueue_outbox,
    new_id,
    row_dict,
    utc_now,
)


class FormalAdminService:
    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.store.initialize()
        self._recovery_executor = None

    def build_recovery_executor(self, handlers, *, authenticated_stages=frozenset()):
        """Build the outbox executor on the same task/attempt authority."""
        from .formal_recovery_executor import FormalRecoveryExecutor

        self._recovery_executor = FormalRecoveryExecutor(self.store, handlers=handlers, authenticated_stages=authenticated_stages)
        return self._recovery_executor

    def configure_connection_tests(self, tests: dict) -> None:
        """Install server-owned adapters; neither configuration nor probes run here."""
        self._connection_tests = {key: value for key, value in tests.items() if key in {"text", "vision"}}

    def connection_status(self, auth_token: str, *, workspace_id: str) -> dict:
        with self.store.connection() as connection:
            require_permission(connection, auth_token, workspace_id=workspace_id, permission="system.read")
            try:
                require_permission(connection, auth_token, workspace_id=workspace_id, permission="system.verify")
                can_verify = True
            except WorkspaceServiceError as exc:
                if exc.code != "PERMISSION_DENIED":
                    raise
                can_verify = False
            records = connection.execute(
                """SELECT operations_event_id, summary FROM operations_audit_events
                WHERE workspace_id=? AND operations_event_id LIKE 'connection_check_%'
                ORDER BY created_at DESC, operations_event_id DESC""", (workspace_id,),
            ).fetchall()
            recent = connection.execute(
                """SELECT d.provider, d.status, d.duration_ms, d.error_code, d.error_summary,
                d.created_at, p.name AS project_name FROM provider_call_diagnostics d
                JOIN tasks t ON t.task_id=d.task_id JOIN projects p ON p.project_id=t.project_id
                WHERE p.workspace_id=? ORDER BY d.created_at DESC, d.provider_call_id DESC LIMIT 20""",
                (workspace_id,),
            ).fetchall()
        receipts = {}
        seen = set()
        for row in records:
            record = json.loads(row["summary"])
            request_id = row["operations_event_id"].rsplit("_", 1)[0]
            # Terminal and start rows share a durable request identity.
            if request_id in seen:
                continue
            seen.add(request_id)
            kind = record["kind"]
            if kind not in receipts:
                receipts[kind] = record
        tests = getattr(self, "_connection_tests", {})
        return {
            "can_verify": can_verify,
            "services": [{"kind": kind, "label": label,
                "configured": bool(tests.get(kind, {}).get("configured")),
                "verification": receipts.get(kind, {"kind": kind, "status": "unverified", "summary": "尚无主动测试记录。"})}
                for kind, label in (("text", "文案服务"), ("vision", "视觉服务"))],
            "recent_calls": [{**row_dict(row), "error_summary": self._safe_text(row["error_summary"])} for row in recent],
            "scope": "当前工作区已关联项目的最近20条真实调用记录",
        }

    def test_connection(self, auth_token: str, *, workspace_id: str, kind: str, request_id: str) -> dict:
        """Persist intent before exactly one provider invocation; ambiguous work is never replayed."""
        from hashlib import sha256
        from time import monotonic
        if kind not in {"text", "vision"} or not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", request_id):
            raise WorkspaceServiceError("INVALID_INPUT", "连接测试类型或请求标识无效")
        key = "connection_check_" + sha256(f"{workspace_id}:{request_id}".encode()).hexdigest()
        spec = getattr(self, "_connection_tests", {}).get(kind, {})
        base = {"kind": kind, "method": spec.get("method", "固定匿名样例"),
            "created_at": utc_now(), "real_provider": bool(spec.get("real_provider")),
            "duration_ms": None, "status": "unknown",
            "summary": "已登记测试请求，尚无最终回执。可刷新读取，系统不会自动再次调用。"}
        with self.store.transaction() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id, permission="system.verify")
            existing = connection.execute(
                """SELECT summary FROM operations_audit_events WHERE workspace_id=?
                AND operations_event_id IN (?, ?) ORDER BY operations_event_id DESC LIMIT 1""",
                (workspace_id, key + "_start", key + "_terminal"),
            ).fetchone()
            if existing:
                receipt = json.loads(existing["summary"])
                if receipt["kind"] != kind:
                    raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "请求标识已用于另一种测试")
                return receipt
            self._record_connection_check(connection, key + "_start", workspace_id, actor["actor_id"], base)
        started = monotonic()
        result = dict(base)
        if not spec.get("configured") or not callable(spec.get("invoke")):
            result.update(status="unconfigured", summary="服务尚未配置，本次未发起模型调用。")
        else:
            try:
                verified = spec["invoke"]()
                if verified is not True:
                    result.update(status="failed", summary="服务返回内容未通过固定样例核对，请检查配置及服务响应。")
                else:
                    result.update(status="succeeded", summary="本次基本调用有效；不代表脚本质量或生产验收通过。")
            except Exception as exc:
                # Provider exception text can contain arbitrary response bodies and credentials.
                diagnostics = getattr(exc, "diagnostics", {}) or {}
                types = [type(exc).__name__] + [str(item.get("error_type", "")) for item in diagnostics.get("attempts", [])]
                uncertain = any("timeout" in value.casefold() for value in types)
                result.update(status="unknown" if uncertain else "failed", summary=(
                    "调用超时，外部是否完成尚不明确。请检查服务记录；不会自动重发。" if uncertain else
                    "连接调用失败。请检查服务凭据、网络和配额后，再明确发起一次新的测试。"))
        result["duration_ms"] = max(0, int((monotonic() - started) * 1000))
        with self.store.transaction() as connection:
            self._record_connection_check(connection, key + "_terminal", workspace_id, actor["actor_id"], result)
        return result

    @staticmethod
    def _record_connection_check(connection, record_id: str, workspace_id: str, actor_id: str, receipt: dict) -> None:
        connection.execute(
            """INSERT INTO operations_audit_events(operations_event_id, workspace_id,
            event_kind, status, summary, created_by_actor_id, created_at)
            VALUES (?, ?, 'worker', ?, ?, ?, ?)""",
            (record_id, workspace_id, receipt["status"], json.dumps(receipt, ensure_ascii=False), actor_id, utc_now()),
        )

    def access(self, auth_token: str, *, workspace_id: str) -> dict:
        with self.store.connection() as connection:
            require_permission(
                connection, auth_token, workspace_id=workspace_id, permission="project.read",
            )
            try:
                require_permission(
                    connection, auth_token, workspace_id=workspace_id, permission="system.read",
                )
            except WorkspaceServiceError as exc:
                if exc.code != "PERMISSION_DENIED":
                    raise
                return {"admin_console": "blocked"}
        return {"admin_console": "available"}

    def overview(self, auth_token: str, *, workspace_id: str, limit: int = 100,
                 page_size: int | None = None, cursor: str | None = None,
                 project_id: str | None = None, status: str | None = None,
                 query: str = "", created_from: str = "", created_to: str = "") -> dict:
        page = None
        if page_size is not None:
            page = TaskService(self.store).query_workspace_tasks(auth_token, workspace_id,
                page_size=page_size, cursor=cursor, project_id=project_id, status=status,
                query=query, created_from=created_from, created_to=created_to, administrative=True)
        safe_limit = max(1, min(int(limit), 200))
        with self.store.connection() as connection:
            actor = require_permission(
                connection, auth_token, workspace_id=workspace_id, permission="system.read",
            )
            schema_version = int(connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations",
            ).fetchone()[0])
            rows = connection.execute(
                """SELECT t.task_id, t.project_id, p.name AS project_name,
                          t.task_type, t.status, t.error_code,
                          t.created_at, t.updated_at, p.lock_version AS project_version,
                          a.attempt_id, a.attempt_no, a.origin,
                          a.requested_stage_id, a.reason AS attempt_reason,
                          actor.display_name AS actor_name
                   FROM tasks t
                   JOIN projects p ON p.project_id=t.project_id
                   JOIN actors actor ON actor.actor_id=t.actor_id
                   LEFT JOIN task_attempts a ON a.attempt_id=(
                       SELECT a2.attempt_id FROM task_attempts a2
                       WHERE a2.task_id=t.task_id
                       ORDER BY a2.attempt_no DESC LIMIT 1
                   )
                   WHERE p.workspace_id=?
                   ORDER BY t.updated_at DESC, t.task_id DESC LIMIT ?""",
                (workspace_id, safe_limit),
            ).fetchall()
            status_counts = {
                row["status"]: int(row["count"])
                for row in connection.execute(
                    """SELECT t.status, COUNT(*) AS count FROM tasks t
                    JOIN projects p ON p.project_id=t.project_id
                    WHERE p.workspace_id=? GROUP BY t.status""",
                    (workspace_id,),
                ).fetchall()
            }
            service_counts = {
                "outbox_pending": int(connection.execute(
                    """SELECT COUNT(*) FROM outbox o WHERE o.published_at IS NULL AND (
                    (o.aggregate_type='workspace' AND o.aggregate_id=?) OR
                    (o.aggregate_type='project' AND EXISTS(
                        SELECT 1 FROM projects p WHERE p.project_id=o.aggregate_id AND p.workspace_id=?
                    )) OR
                    (o.aggregate_type='task' AND EXISTS(
                        SELECT 1 FROM tasks t JOIN projects p ON p.project_id=t.project_id
                        WHERE t.task_id=o.aggregate_id AND p.workspace_id=?
                    )))""",
                    (workspace_id, workspace_id, workspace_id),
                ).fetchone()[0]),
                "dead_letter": int(connection.execute(
                    """SELECT COUNT(*) FROM dead_letters d JOIN tasks t ON t.task_id=d.task_id
                    JOIN projects p ON p.project_id=t.project_id WHERE p.workspace_id=?""",
                    (workspace_id,),
                ).fetchone()[0]),
                "provider_failures": int(connection.execute(
                    """SELECT COUNT(*) FROM provider_call_diagnostics d
                    JOIN tasks t ON t.task_id=d.task_id
                    JOIN projects p ON p.project_id=t.project_id
                    WHERE p.workspace_id=? AND d.status='failed'""",
                    (workspace_id,),
                ).fetchone()[0]),
            }
            operations = connection.execute(
                """SELECT operations_event_id, event_kind, status, summary, created_at
                FROM operations_audit_events WHERE workspace_id=?
                ORDER BY created_at DESC, operations_event_id DESC LIMIT 30""",
                (workspace_id,),
            ).fetchall()
        task_rows = page["items"] if page else [row_dict(row) for row in rows]
        for item in task_rows:
            item.update(TaskService.employee_task_fields(item, item["project_id"]))
            if item.get("attempt_reason"):
                item["attempt_reason"] = self._safe_text(item["attempt_reason"])
        return {
            **({key: value for key, value in page.items() if key != "items"} if page else {}),
            "state": "available",
            "workspace_id": workspace_id,
            "administrator": actor["display_name"],
            "schema_version": schema_version,
            "tasks": task_rows,
            "task_status_counts": page["workspace_status_counts"] if page else status_counts,
            "statistics_scope": "workspace",
            "services": service_counts,
            "operations": [
                {
                    "event_kind": row["event_kind"],
                    "status": row["status"],
                    "label": "连接测试" if row["operations_event_id"].startswith("connection_check_") else "运维记录",
                    "status_label": {"unknown": "结果尚未确认", "succeeded": "已成功", "failed": "失败", "unconfigured": "未配置"}.get(row["status"], "已记录"),
                    "summary": self._safe_text(json.loads(row["summary"]).get("summary", "")) if row["operations_event_id"].startswith("connection_check_") else self._safe_text(row["summary"]),
                    "created_at": row["created_at"],
                }
                for row in operations
            ],
            "secrets_exposed": False,
            "production_certification": "BLOCK",
        }

    def approval_summary(self, auth_token: str, *, workspace_id: str, workbench) -> dict:
        from urllib.parse import urlencode
        from .project_service import ProjectService
        from .knowledge_service import KnowledgeService
        with self.store.connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id, permission="system.read")
        projects = ProjectService(self.store).list_projects(auth_token, workspace_id)
        candidates = KnowledgeService(self.store).list_candidates(auth_token, workspace_id)
        items, failures, seen = [], [], set()
        for project in projects:
            project_id = project["project_id"]
            def permitted(permission):
                try:
                    with self.store.connection() as connection:
                        require_permission(connection, auth_token, workspace_id=workspace_id, project_id=project_id, permission=permission)
                    return True
                except WorkspaceServiceError as exc:
                    if exc.code != "PERMISSION_DENIED":
                        raise
                    return False
            targets = {"company": workspace_id, "category": project["category_id"], "project": project_id, "personal": actor["actor_id"]}
            for item in candidates:
                key = ("knowledge", item["candidate_id"])
                if key in seen or item["status"] != "pending_approval" or item["target_id"] != targets[item["scope"]]:
                    continue
                if not permitted("knowledge.approve"):
                    continue
                seen.add(key)
                items.append({"object_type": "knowledge", "label": "规则建议", "object_id": item["candidate_id"],
                    "title": item["statement"], "project_id": project_id, "project_name": project["name"],
                    "scope": item["scope"], "status": "待审批", "can_act": item["scope"] != "personal",
                    "next_action": "查看建议与依据" if item["scope"] != "personal" else "个人偏好按原流程维护，不在人工批准页推广",
                    "href": "/knowledge?" + urlencode({"project_id": project_id, "candidate_id": item["candidate_id"]}),
                    "created_at": item["created_at"]})
            try:
                context = workbench.asset_project_context(auth_token, project_id=project_id)
            except (WorkspaceServiceError, ValueError, OSError) as exc:
                failures.append({"project_name": project["name"], "area": "assets", "summary": "资产待办读取失败，请在项目资产页核对。"})
                continue
            for item in context["imports"]:
                key = ("asset", item["import_id"])
                if key in seen or item["state"] not in {"staged", "reviewed"}:
                    continue
                permission = "asset.review" if item["state"] == "staged" else "asset.publish"
                if not permitted(permission):
                    continue
                seen.add(key)
                items.append({"object_type": "asset", "label": "资产导入", "object_id": item["import_id"],
                    "title": "、".join(entry.get("filename", "导入文件") for entry in item.get("items", [])) or "资产导入记录",
                    "project_id": project_id, "project_name": project["name"], "scope": item["scope"],
                    "status": "待审核" if item["state"] == "staged" else "待登记", "can_act": True,
                    "next_action": "检查导入内容" if item["state"] == "staged" else "检查登记影响",
                    "href": "/assets?" + urlencode({"project_id": project_id, "import_id": item["import_id"]}),
                    "created_at": item.get("created_at", "")})
        return {"items": items, "count": len(items), "scope": "当前工作区中当前账号有审核权限的资产与规则待办",
                "unavailable_sources": failures, "provider_calls": 0}

    def task_detail(
        self, auth_token: str, *, workspace_id: str, task_id: str,
    ) -> dict:
        with self.store.connection() as connection:
            require_permission(
                connection, auth_token, workspace_id=workspace_id, permission="system.read",
            )
            task = connection.execute(
                """SELECT t.task_id, t.project_id, p.name AS project_name,
                          t.task_type, t.status, t.error_code,
                          t.created_at, t.updated_at, p.lock_version AS project_version,
                          actor.display_name AS actor_name
                   FROM tasks t JOIN projects p ON p.project_id=t.project_id
                   JOIN actors actor ON actor.actor_id=t.actor_id
                   WHERE t.task_id=? AND p.workspace_id=?""",
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise WorkspaceServiceError("NOT_FOUND", "管理员任务不存在")
            attempts = connection.execute(
                """SELECT attempt_id, attempt_no, origin, parent_attempt_id,
                          requested_stage_id, reason, created_at
                FROM task_attempts WHERE task_id=? ORDER BY attempt_no""",
                (task_id,),
            ).fetchall()
            checkpoints = connection.execute(
                """SELECT checkpoint_id, attempt_id, stage_id, status,
                          error_code, created_at
                FROM stage_checkpoints WHERE task_id=? ORDER BY created_at, checkpoint_id""",
                (task_id,),
            ).fetchall()
            providers = connection.execute(
                """SELECT provider_call_id, attempt_id, stage_id, provider,
                          status, duration_ms, error_code, input_summary,
                          error_summary, created_at
                FROM provider_call_diagnostics WHERE task_id=?
                ORDER BY created_at, provider_call_id""",
                (task_id,),
            ).fetchall()
            events = connection.execute(
                """SELECT sequence_no, status, details_json, created_at
                FROM task_events WHERE task_id=? ORDER BY sequence_no""",
                (task_id,),
            ).fetchall()
            dead_letters = connection.execute(
                """SELECT dead_letter_id, attempt_id, reason_code, created_at
                FROM dead_letters WHERE task_id=? ORDER BY created_at""",
                (task_id,),
            ).fetchall()
            try:
                require_permission(connection, auth_token, workspace_id=workspace_id, permission="system.recover")
                can_recover = task["status"] in {"failed", "blocked"}
            except WorkspaceServiceError as exc:
                if exc.code != "PERMISSION_DENIED":
                    raise
                can_recover = False
            recovery_events = connection.execute(
                """SELECT payload_json, last_error FROM outbox
                WHERE aggregate_type='task' AND aggregate_id=?
                AND event_type IN ('task.recovery_attempt_created','task.stage_retry_attempt_created')""",
                (task_id,),
            ).fetchall()
        from .formal_recovery_executor import FormalRecoveryExecutor
        reader = self._recovery_executor or FormalRecoveryExecutor(self.store, handlers={})
        handlers = reader.handlers
        attempts_dto = []
        for row in attempts:
            item = row_dict(row)
            terminal = reader.terminal_for_attempt(item["attempt_id"])
            running = any(json.loads(event["payload_json"]).get("attempt_id") == item["attempt_id"]
                          and event["last_error"] == "RECOVERY_EXECUTION_IN_PROGRESS" for event in recovery_events)
            status = terminal["status"] if terminal else (
                "executing" if running else (task["status"] if item["origin"] == "initial" else "created"))
            item.update(execution_status=status, reason=self._safe_text(item["reason"]), summary={
                "created": "新尝试已创建，尚无执行结果。",
                "executing": "执行已开始，尚无终态证据；请刷新读取，不要重复提交。",
                "succeeded": "本次阶段执行成功，原任务历史保持不变；后续业务状态请查看项目。",
                "failed": "本次执行失败，请核对原因后决定下一步。",
            }.get(status, "请查看项目处理当前业务待办。"))
            if terminal:
                item["error_code"] = terminal.get("error_code")
            attempts_dto.append(item)
        stage = (attempts[-1]["requested_stage_id"] if attempts else "") or task["task_type"]
        checkpoints_dto = [{**row_dict(row), "recoverable": bool(can_recover and attempts and row["attempt_id"] == attempts[-1]["attempt_id"] and row["stage_id"] in handlers)} for row in checkpoints]
        actions = []
        if can_recover and attempts:
            if stage in handlers:
                actions += [{"action": "retry_current_stage", "label": "重试当前阶段"}, {"action": "safe_reset", "label": "重新执行当前阶段（保留历史）"}]
            if any(item["recoverable"] for item in checkpoints_dto):
                actions.append({"action": "resume_checkpoint", "label": "从当前任务检查点恢复"})
        reason = "" if actions else ("当前阶段没有可用的恢复处理器，请回到项目处理或联系维护人员。" if can_recover else "当前任务状态或账号权限不允许恢复。")
        task_dto = row_dict(task)
        task_dto.update(TaskService.employee_task_fields(task_dto, task["project_id"]))
        return {
            "task": task_dto,
            "attempts": attempts_dto,
            "checkpoints": checkpoints_dto,
            "recovery_actions": actions,
            "recovery_unavailable_reason": reason,
            "provider_calls": [
                {
                    "provider_call_id": row["provider_call_id"],
                    "attempt_id": row["attempt_id"],
                    "stage_id": row["stage_id"],
                    "provider": row["provider"],
                    "status": row["status"],
                    "duration_ms": row["duration_ms"],
                    "error_code": row["error_code"],
                    "error_summary": self._safe_text(row["error_summary"]),
                    "created_at": row["created_at"],
                }
                for row in providers
            ],
            "events": [self._public_event(row) for row in events],
            "dead_letters": [row_dict(row) for row in dead_letters],
            "next_action": "填写原因并预览恢复影响；确认后创建新的执行尝试。" if actions else reason,
            "production_certification": "BLOCK",
        }

    def propose_recovery(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        task_id: str,
        action: str,
        reason: str,
        expected_project_version: int,
        idempotency_key: str,
        checkpoint_id: str = "",
    ) -> dict:
        return self._propose_attempt(
            auth_token,
            workspace_id=workspace_id,
            task_id=task_id,
            action=action,
            reason=reason,
            expected_project_version=expected_project_version,
            idempotency_key=idempotency_key,
            checkpoint_id=checkpoint_id,
            permission="system.recover",
            proposal_type="admin_recovery_proposal",
        )

    def propose_stage_retry(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        requested_stage_id: str,
        reason: str,
        expected_project_version: int,
        idempotency_key: str,
        excluding_task_id: str = "",
    ) -> dict:
        """Create the ordinary editor retry proposal on the same attempt authority."""
        stage_id = str(requested_stage_id or "").strip()
        if not stage_id or not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", stage_id):
            raise WorkspaceServiceError("INVALID_INPUT", "请明确要重试的英文阶段 ID")
        with self.store.connection() as connection:
            require_permission(
                connection, auth_token, workspace_id=workspace_id, permission="run.retry",
            )
            project = connection.execute(
                "SELECT lock_version FROM projects WHERE project_id=? AND workspace_id=?",
                (project_id, workspace_id),
            ).fetchone()
            if project is None:
                raise WorkspaceServiceError("NOT_FOUND", "待重试项目不存在")
            if int(project["lock_version"]) != int(expected_project_version):
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目状态已变化")
            target = connection.execute(
                """SELECT t.task_id FROM tasks t
                WHERE t.project_id=? AND t.status IN ('failed','blocked')
                  AND t.task_id<>?
                  AND (
                    t.task_type=? OR EXISTS(
                      SELECT 1 FROM task_attempts a
                      WHERE a.task_id=t.task_id AND a.requested_stage_id=?
                    ) OR EXISTS(
                      SELECT 1 FROM stage_checkpoints c
                      WHERE c.task_id=t.task_id AND c.stage_id=?
                    )
                  )
                ORDER BY t.updated_at DESC, t.task_id DESC LIMIT 1""",
                (project_id, excluding_task_id, stage_id, stage_id, stage_id),
            ).fetchone()
        if target is None:
            raise WorkspaceServiceError(
                "RETRY_TARGET_NOT_FOUND", "当前项目没有该阶段的失败或阻断任务",
            )
        return self._propose_attempt(
            auth_token,
            workspace_id=workspace_id,
            task_id=target["task_id"],
            action="retry_current_stage",
            reason=reason,
            expected_project_version=expected_project_version,
            idempotency_key=idempotency_key,
            checkpoint_id="",
            permission="run.retry",
            proposal_type="formal_stage_retry_proposal",
        )

    def _propose_attempt(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        task_id: str,
        action: str,
        reason: str,
        expected_project_version: int,
        idempotency_key: str,
        checkpoint_id: str,
        permission: str,
        proposal_type: str,
    ) -> dict:
        if action not in {"retry_current_stage", "resume_checkpoint", "safe_reset"}:
            raise WorkspaceServiceError("INVALID_INPUT", "未知受控恢复动作")
        if permission == "run.retry" and (action != "retry_current_stage" or checkpoint_id):
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "普通重试只能创建当前阶段的新 attempt",
            )
        reason = str(reason or "").strip()
        if not reason:
            raise WorkspaceServiceError("INVALID_INPUT", "受控恢复必须填写原因")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            actor = require_permission(
                connection, auth_token, workspace_id=workspace_id, permission=permission,
            )
            task = connection.execute(
                """SELECT t.*, p.workspace_id, p.lock_version AS project_version
                FROM tasks t JOIN projects p ON p.project_id=t.project_id
                WHERE t.task_id=? AND p.workspace_id=?""",
                (task_id, workspace_id),
            ).fetchone()
            if task is None:
                raise WorkspaceServiceError("NOT_FOUND", "待恢复任务不存在")
            if task["status"] not in {"failed", "blocked"}:
                raise WorkspaceServiceError(
                    "RECOVERY_NOT_ALLOWED", "只能对失败或阻断任务创建恢复提案",
                )
            if int(task["project_version"]) != int(expected_project_version):
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目状态已变化")
            attempt = connection.execute(
                """SELECT * FROM task_attempts WHERE task_id=?
                ORDER BY attempt_no DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
            if attempt is None:
                raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "任务缺少 attempt 证据")
            checkpoint = None
            if checkpoint_id:
                checkpoint = connection.execute(
                    "SELECT * FROM stage_checkpoints WHERE checkpoint_id=? AND task_id=?",
                    (checkpoint_id, task_id),
                ).fetchone()
                if checkpoint is None:
                    raise WorkspaceServiceError("NOT_FOUND", "checkpoint 不属于当前任务")
            if action == "resume_checkpoint" and checkpoint is None:
                raise WorkspaceServiceError("INVALID_INPUT", "从检查点恢复必须选择 checkpoint")
            stage_id = str(
                (checkpoint["stage_id"] if checkpoint is not None else "")
                or attempt["requested_stage_id"]
                or task["task_type"]
            )
            parameters = {
                "workspace_id": workspace_id,
                "project_id": task["project_id"],
                "task_id": task_id,
                "base_attempt_id": attempt["attempt_id"],
                "checkpoint_id": checkpoint_id or "",
                "action": action,
                "requested_stage_id": stage_id,
                "reason": reason,
                "expected_project_version": int(expected_project_version),
                "idempotency_key": str(idempotency_key),
            }
            digest = sha256_json({
                "action": (
                    "formal_stage_retry"
                    if permission == "run.retry" else "formal_admin_recovery"
                ),
                "parameters": parameters,
            })
            replay = connection.execute(
                """SELECT * FROM recovery_proposals
                WHERE proposed_by_actor_id=? AND project_id=? AND idempotency_key=?""",
                (actor["actor_id"], task["project_id"], idempotency_key),
            ).fetchone()
            if replay is not None:
                if replay["proposal_digest"] != digest:
                    raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "幂等键已绑定不同恢复提案")
                proposal_id = replay["recovery_proposal_id"]
            else:
                proposal_id = new_id("recovery")
                connection.execute(
                    """INSERT INTO recovery_proposals(
                    recovery_proposal_id, workspace_id, project_id, task_id,
                    base_attempt_id, checkpoint_id, action, requested_stage_id,
                    reason, expected_project_version, proposal_digest,
                    idempotency_key, proposed_by_actor_id, proposed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        proposal_id, workspace_id, task["project_id"], task_id,
                        attempt["attempt_id"], checkpoint_id or None, action, stage_id,
                        reason, expected_project_version, digest, idempotency_key,
                        actor["actor_id"], timestamp,
                    ),
                )
        return {
            "type": proposal_type,
            "proposal_id": proposal_id,
            "proposal_digest": digest,
            "action": action,
            "task_id": task_id,
            "base_attempt_id": attempt["attempt_id"],
            "requested_stage_id": stage_id,
            "before": {"task_status": task["status"], "attempt_no": attempt["attempt_no"]},
            "after": {"new_attempt": True, "old_task_unchanged": True},
            "confirmation_required": True,
            "production_certification": "BLOCK",
        }

    def confirm_recovery(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        proposal_id: str,
        proposal_digest: str,
    ) -> dict:
        return self._confirm_attempt(
            auth_token,
            workspace_id=workspace_id,
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            permission="system.recover",
            result_type="admin_recovery_attempt_created",
            outbox_event="task.recovery_attempt_created",
            require_own_proposal=False,
        )

    def confirm_stage_retry(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        proposal_id: str,
        proposal_digest: str,
    ) -> dict:
        return self._confirm_attempt(
            auth_token,
            workspace_id=workspace_id,
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            permission="run.retry",
            result_type="formal_stage_retry_attempt_created",
            outbox_event="task.stage_retry_attempt_created",
            require_own_proposal=True,
        )

    def _confirm_attempt(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        proposal_id: str,
        proposal_digest: str,
        permission: str,
        result_type: str,
        outbox_event: str,
        require_own_proposal: bool,
    ) -> dict:
        timestamp = utc_now()
        event_id = ""
        with self.store.transaction() as connection:
            actor = require_permission(
                connection, auth_token, workspace_id=workspace_id, permission=permission,
            )
            proposal = connection.execute(
                "SELECT * FROM recovery_proposals WHERE recovery_proposal_id=? AND workspace_id=?",
                (proposal_id, workspace_id),
            ).fetchone()
            if proposal is None:
                raise WorkspaceServiceError("NOT_FOUND", "恢复提案不存在")
            if proposal["proposal_digest"] != proposal_digest:
                raise WorkspaceServiceError("PROPOSAL_DIGEST_MISMATCH", "恢复提案摘要不匹配")
            if require_own_proposal and proposal["proposed_by_actor_id"] != actor["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "普通重试只能确认本人创建的提案")
            if permission == "run.retry" and (
                proposal["action"] != "retry_current_stage" or proposal["checkpoint_id"]
            ):
                raise WorkspaceServiceError("PERMISSION_DENIED", "普通重试不能执行管理员恢复动作")
            project = connection.execute(
                "SELECT lock_version FROM projects WHERE project_id=? AND workspace_id=?",
                (proposal["project_id"], workspace_id),
            ).fetchone()
            existing = connection.execute(
                "SELECT * FROM recovery_executions WHERE recovery_proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if existing is not None:
                attempt_id = existing["new_attempt_id"]
                event = connection.execute(
                    """SELECT event_id FROM outbox
                    WHERE aggregate_type='task' AND aggregate_id=? AND event_type=?
                      AND json_extract(payload_json, '$.attempt_id')=?
                    ORDER BY sequence_id LIMIT 1""",
                    (proposal["task_id"], outbox_event, attempt_id),
                ).fetchone()
                event_id = str(event["event_id"] if event is not None else "")
                replayed = True
            else:
                if project is None or int(project["lock_version"]) != int(proposal["expected_project_version"]):
                    raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目在恢复确认前已变化")
                last = connection.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) AS value FROM task_attempts WHERE task_id=?",
                    (proposal["task_id"],),
                ).fetchone()
                attempt_id = new_id("attempt")
                connection.execute(
                    """INSERT INTO task_attempts(
                    attempt_id, task_id, project_id, attempt_no, origin,
                    parent_attempt_id, requested_stage_id, reason,
                    created_by_actor_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        attempt_id, proposal["task_id"], proposal["project_id"],
                        int(last["value"]) + 1,
                        "checkpoint" if proposal["checkpoint_id"] else "retry",
                        proposal["base_attempt_id"], proposal["requested_stage_id"],
                        proposal["reason"], actor["actor_id"], timestamp,
                    ),
                )
                connection.execute(
                    """INSERT INTO recovery_executions(
                    recovery_execution_id, recovery_proposal_id, new_attempt_id,
                    confirmed_by_actor_id, confirmed_at)
                    VALUES (?, ?, ?, ?, ?)""",
                    (new_id("recovery_execution"), proposal_id, attempt_id, actor["actor_id"], timestamp),
                )
                event_id = enqueue_outbox(
                    connection,
                    aggregate_type="task",
                    aggregate_id=proposal["task_id"],
                    event_type=outbox_event,
                    payload={
                        "attempt_id": attempt_id,
                        "recovery_proposal_id": proposal_id,
                        "requested_stage_id": proposal["requested_stage_id"],
                    },
                )
                replayed = False
            task_status = connection.execute(
                "SELECT status FROM tasks WHERE task_id=?", (proposal["task_id"],),
            ).fetchone()["status"]
        response = {
            "type": result_type,
            "status": "accepted",
            "task_id": proposal["task_id"],
            "new_attempt_id": attempt_id,
            "requested_stage_id": proposal["requested_stage_id"],
            "old_task_status": task_status,
            "old_task_unchanged": True,
            "replayed": replayed,
            "production_certification": "BLOCK",
        }
        if self._recovery_executor is not None and event_id:
            response["execution"] = self._recovery_executor.consume_outbox_event(event_id, auth_token=auth_token)
        return response

    @staticmethod
    def _public_event(row) -> dict[str, Any]:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            details = {}
        return {
            "sequence_no": row["sequence_no"],
            "status": row["status"],
            "stage_id": str(details.get("stage_id") or details.get("active_stage") or ""),
            "error_code": str(details.get("error_code") or ""),
            "created_at": row["created_at"],
        }

    @staticmethod
    def _safe_text(value: Any) -> str:
        text = str(value or "")[:500]
        text = re.sub(r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
        text = re.sub(r"(?<![\w])(?:/[\w .-]+){2,}", "[LOCAL_PATH]", text)
        text = re.sub(r"\b[a-fA-F0-9]{64}\b", "[SHA256]", text)
        return text
