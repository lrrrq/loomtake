from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

from .identity_service import require_collection_read, require_permission
from .project_service import _project
from .mutation_policy import require_active_project
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    check_version,
    enqueue_outbox,
    new_id,
    require_artifact_ref,
    require_nonempty,
    require_sha256,
    row_dict,
    utc_now,
)


TASK_TRANSITIONS = {
    "received": {
        "understanding",
        "executing",
        "blocked",
        "failed",
        "cancelled",
    },
    "understanding": {
        "needs_clarification",
        "proposed",
        "executing",
        "blocked",
        "failed",
        "cancelled",
    },
    "needs_clarification": {"understanding", "cancelled"},
    "proposed": {"waiting_confirmation", "executing", "blocked", "cancelled"},
    "waiting_confirmation": {"executing", "cancelled", "blocked"},
    "executing": {
        "waiting_human_review",
        "waiting_user",
        "completed",
        "partially_completed",
        "blocked",
        "failed",
        "cancelled",
    },
    "waiting_human_review": {"completed", "partially_completed", "blocked", "cancelled"},
    "waiting_user": {"executing", "completed", "partially_completed", "cancelled"},
    "blocked": {"executing", "cancelled"},
    "failed": set(),
    "completed": set(),
    "partially_completed": set(),
    "cancelled": set(),
}


EMPLOYEE_TASK_LABELS = {
    "received": "已接收",
    "understanding": "读取中",
    "needs_clarification": "待澄清",
    "proposed": "待核对",
    "waiting_confirmation": "待确认",
    "executing": "处理中",
    "waiting_human_review": "等待人工审核",
    "waiting_user": "等待确认",
    "completed": "已完成",
    "partially_completed": "部分完成",
    "blocked": "已阻断",
    "failed": "未完成",
    "cancelled": "已取消",
}

EMPLOYEE_TASK_TYPES = {
    "formal_creative_draft": "创意初稿",
    "formal_image_creation": "图片生成",
    "formal_review_revision": "审核意见修改",
    "formal_fact_revision": "需求事实修改",
    "formal_image_feedback": "配图意见处理",
    "formal_image_candidates": "图片候选查找",
    "formal_production_advisor": "制作建议",
    "formal_stage_retry": "继续未完成操作",
    "formal_template_trial": "模板试排",
    "get_creative_strategies": "创作策略比较",
    "formal_generation": "规划与脚本生成",
    "formal_generation_plan": "规划与脚本生成",
    "formal_script_revision": "脚本修改",
    "formal_image_revision": "图片修改",
    "formal_image_version": "逐镜配图",
    "formal_default_image_adoption": "项目素材",
    "formal_historical_continuation": "历史 Brief 续作",
    "formal_historical_review_import": "历史复核导入",
}

EMPLOYEE_COMPLETED_TASKS = {
    "formal_generation": (
        "查看脚本", "script", "脚本版本已生成并保存在当前项目",
    ),
    "formal_generation_plan": (
        "核对规划", "plan", "规划任务已完成，请核对规划与实际结果",
    ),
    "formal_script_revision": (
        "查看脚本", "script", "脚本修改已完成，请查看当前草稿或版本",
    ),
    "formal_image_revision": (
        "查看逐镜配图", "shot-images", "图片修改已完成，请继续逐镜确认",
    ),
    "formal_image_version": (
        "查看逐镜配图", "shot-images", "逐镜配图已保存，请继续审核确认",
    ),
    "formal_default_image_adoption": (
        "继续逐镜确认", "shot-images", "类别默认图片已加入项目素材，请继续逐镜确认",
    ),
    "formal_historical_continuation": (
        "查看项目", "overview", "历史 Brief 续作已完成，请查看目标项目并继续确认",
    ),
    "formal_historical_review_import": (
        "进入审核", "review", "历史复核已导入，请核对审核结果",
    ),
}

_TASK_RESULT_KIND_SQL = """CASE
    WHEN EXISTS (SELECT 1 FROM task_events e JOIN project_versions v
        ON v.version_id=json_extract(e.details_json, '$.version_id') AND v.project_id=t.project_id
        WHERE e.task_id=t.task_id AND e.status='completed') THEN 'version'
    WHEN EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=t.task_id
        AND json_extract(e.details_json, '$.action')='trial_template') THEN 'trial'
    ELSE 'plan' END"""

EMPLOYEE_TASK_ISSUES = {
    "confirmed_brief_needs_clarification": "已确认资料仍有规划缺项或待澄清内容，请返回需求核对。",
    "SCRIPT_VERSION_AUDIT_FAILED": "脚本校验未通过，请返回脚本查看当前稿问题；核对后再保存。",
    "SCRIPT_TYPE_UNSUPPORTED": "Brief 中的脚本类型尚不支持，请返回 Brief 补充可用类型。",
    "BRIEF_DURATION_INVALID": "Brief 缺少可执行的成片时长，请补充正整数秒。",
    "FORMAL_PLAN_VISUAL_ANALYSIS_REQUIRED": "项目图片尚未全部完成理解，请先处理图片。",
    "CREATIVE_STANDARD_MISSING": "请先确认 Brief 和项目创作标准卡。",
    "CONFIRMED_BRIEF_REQUIRED": "请先完成并确认 Brief。",
    "FORMAL_EXECUTION_FAILED": "本次操作已停止，请核对当前脚本和版本记录后重新提交。",
    "RECOVERY_RECONCILIATION_REQUIRED": "原操作已停止，现有记录不足以确认完成；请先核对当前版本。",
}


class TaskService:
    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.store.initialize()

    @staticmethod
    def employee_task_fields(row: dict, project_id: str) -> dict:
        """Additive, employee-safe task projection; raw task authority stays in this service."""
        task_type = str(row.get("task_type") or "")
        status = str(row.get("status") or "")
        error_code = str(row.get("error_code") or "")
        is_image_task = task_type in {
            "formal_image_revision", "formal_image_version", "formal_default_image_adoption",
            "formal_image_candidates", "formal_image_feedback",
        }
        completed_next_action = ""
        if status == "completed":
            action_label, action_section, completed_next_action = (
                EMPLOYEE_COMPLETED_TASKS.get(task_type)
                or ("查看项目", "overview", "任务已完成，请查看项目最新状态")
            )
            if task_type == "formal_generation_plan" and row.get("result_kind") == "version":
                action_label, action_section, completed_next_action = "查看脚本", "script", "脚本版本已生成并保存在当前项目"
            elif task_type == "formal_generation_plan" and row.get("result_kind") == "trial":
                action_label, action_section, completed_next_action = "查看试排", "plan", "模板试排已完成，请核对结果；尚未创建正式版本"
        elif status == "waiting_user":
            action_label, action_section = "核对规划", "plan"
        elif status == "waiting_human_review":
            action_label, action_section = "进入审核", "review"
        elif status in {"blocked", "failed", "partially_completed"}:
            action_label = "查看并处理"
            if error_code == "SCRIPT_VERSION_AUDIT_FAILED":
                action_section = "script"
            elif error_code in {"FORMAL_EXECUTION_FAILED", "RECOVERY_RECONCILIATION_REQUIRED"}:
                action_section = "shot-images" if is_image_task else (
                    "script" if task_type == "formal_script_revision" else "plan")
            else:
                action_section = "brief" if error_code in EMPLOYEE_TASK_ISSUES else "plan"
        elif task_type == "formal_fact_revision":
            action_label, action_section = "核对需求", "brief"
        elif is_image_task:
            action_label, action_section = "处理项目素材", "shot-images"
        elif task_type in {"formal_script_revision", "formal_review_revision"}:
            action_label, action_section = "查看脚本", "script"
        else:
            action_label, action_section = "查看进度", "plan"
        next_actions = {
            "needs_clarification": "请补充本次操作缺少的信息，再提交明确指令",
            "proposed": "请核对本次建议的修改内容与范围",
            "waiting_confirmation": "请查看本次提案，确认后才会执行",
            "received": "等待系统开始处理",
            "understanding": "正在整理项目资料",
            "executing": "正在生成规划或脚本",
            "waiting_user": "请回到规划页核对并确认",
            "waiting_human_review": "请进入审核与交付",
            "completed": completed_next_action,
            "partially_completed": "请查看当前页面提示并继续处理",
            "blocked": "请按页面提示补齐资料或处理冲突",
            "failed": "请联系管理员查看失败原因",
            "cancelled": "如仍需执行，请重新创建规划",
        }
        title = EMPLOYEE_TASK_TYPES.get(task_type) or "项目任务"
        return {
            "title": title,
            "state_label": EMPLOYEE_TASK_LABELS.get(status, "状态待读取"),
            "next_action": next_actions.get(status, "请刷新后查看最新状态"),
            "issue": EMPLOYEE_TASK_ISSUES.get(error_code, ""),
            "action_label": action_label,
            "action_href": f"/projects/{project_id}/{action_section}",
        }

    def create_agent_session(self, auth_token: str, *, project_id: str) -> dict:
        timestamp = utc_now()
        agent_session_id = new_id("agent_session")
        with self.store.transaction() as connection:
            project = _project(connection, project_id)
            require_active_project(project)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="agent_session.create",
            )
            connection.execute(
                """
                INSERT INTO agent_sessions(
                    agent_session_id, auth_session_id, actor_id, project_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                """,
                (
                    agent_session_id,
                    actor["auth_session_id"],
                    actor["actor_id"],
                    project_id,
                    timestamp,
                    timestamp,
                ),
            )
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project_id,
                event_type="project.agent_session_created",
                payload={"agent_session_id": agent_session_id},
            )
        return {
            "agent_session_id": agent_session_id,
            "auth_session_id": actor["auth_session_id"],
            "actor_id": actor["actor_id"],
            "project_id": project_id,
            "status": "active",
        }

    def get_agent_session(self, auth_token: str, agent_session_id: str) -> dict:
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT s.*, p.workspace_id
                FROM agent_sessions s JOIN projects p ON p.project_id = s.project_id
                WHERE s.agent_session_id = ?
                """,
                (agent_session_id,),
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("NOT_FOUND", "agent session 不存在")
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                permission="project.read",
            )
            if actor["actor_id"] != row["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "不能读取其他操作者的 agent session")
            result = row_dict(row)
        result.pop("workspace_id", None)
        return result

    def create_task(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        task_type: str,
        payload_sha256: str,
        expected_project_version: int,
        reuse_payload: bool = False,
    ) -> dict:
        task_type = require_nonempty(task_type, "task_type")
        payload_sha256 = require_sha256(payload_sha256, "payload_sha256")
        timestamp = utc_now()
        task_id = new_id("task")
        with self.store.transaction() as connection:
            session = connection.execute(
                """
                SELECT s.*, p.workspace_id, p.lock_version AS project_version,
                       p.status AS project_status
                FROM agent_sessions s JOIN projects p ON p.project_id = s.project_id
                WHERE s.agent_session_id = ?
                """,
                (agent_session_id,),
            ).fetchone()
            if session is None or session["status"] != "active":
                raise WorkspaceServiceError("NOT_FOUND", "active agent session 不存在")
            if session["project_status"] != "active":
                raise WorkspaceServiceError("PROJECT_ARCHIVED", "项目已归档，不能创建新任务")
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=session["workspace_id"],
                project_id=session["project_id"],
                permission="task.create",
            )
            if actor["actor_id"] != session["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "不能在其他操作者会话中创建 task")
            check_version(
                session["project_version"],
                expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            if reuse_payload:
                existing = connection.execute(
                    """
                    SELECT * FROM tasks
                    WHERE actor_id = ? AND project_id = ?
                      AND task_type = ? AND payload_sha256 = ?
                    ORDER BY created_at, task_id
                    LIMIT 1
                    """,
                    (
                        actor["actor_id"],
                        session["project_id"],
                        task_type,
                        payload_sha256,
                    ),
                ).fetchone()
                if existing is not None:
                    result = row_dict(existing)
                    result["task_version"] = result.pop("lock_version")
                    result["replayed"] = True
                    return result
            connection.execute(
                """
                INSERT INTO tasks(
                    task_id, agent_session_id, auth_session_id, actor_id, project_id,
                    task_type, payload_sha256, status, result_ref, error_code,
                    lock_version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'received', NULL, NULL, 0, ?, ?)
                """,
                (
                    task_id,
                    agent_session_id,
                    actor["auth_session_id"],
                    actor["actor_id"],
                    session["project_id"],
                    task_type,
                    payload_sha256,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO task_events(
                    task_event_id, task_id, sequence_no, status, details_json, created_at
                ) VALUES (?, ?, 1, 'received', '{}', ?)
                """,
                (new_id("task_event"), task_id, timestamp),
            )
            connection.execute(
                """INSERT INTO task_attempts(
                attempt_id, task_id, project_id, attempt_no, origin,
                parent_attempt_id, requested_stage_id, reason,
                created_by_actor_id, created_at)
                VALUES (?, ?, ?, 1, 'initial', NULL, NULL, ?, ?, ?)""",
                (
                    new_id("attempt"), task_id, session["project_id"],
                    "初始执行", actor["actor_id"], timestamp,
                ),
            )
            enqueue_outbox(
                connection,
                aggregate_type="task",
                aggregate_id=task_id,
                event_type="task.created",
                payload={
                    "agent_session_id": agent_session_id,
                    "project_id": session["project_id"],
                    "status": "received",
                    "task_type": task_type,
                },
            )
        result = {
            "task_id": task_id,
            "agent_session_id": agent_session_id,
            "auth_session_id": actor["auth_session_id"],
            "actor_id": actor["actor_id"],
            "project_id": session["project_id"],
            "task_type": task_type,
            "payload_sha256": payload_sha256,
            "status": "received",
            "result_ref": None,
            "error_code": None,
            "task_version": 0,
        }
        if reuse_payload:
            result["replayed"] = False
        return result

    def append_task_event(
        self,
        auth_token: str,
        *,
        task_id: str,
        expected_task_version: int,
        status: str,
        details: dict | None = None,
        result_ref: str | None = None,
        error_code: str | None = None,
    ) -> dict:
        status = require_nonempty(status, "status")
        if result_ref is not None:
            result_ref = require_artifact_ref(result_ref, "result_ref")
        if error_code is not None:
            error_code = require_nonempty(error_code, "error_code")
        if details is not None and not isinstance(details, dict):
            raise WorkspaceServiceError("INVALID_INPUT", "details 必须是 object")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            task = connection.execute(
                """
                SELECT t.*, p.workspace_id, p.status AS project_status
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise WorkspaceServiceError("NOT_FOUND", "task 不存在")
            if task["project_status"] != "active":
                raise WorkspaceServiceError("PROJECT_ARCHIVED", "项目已归档，不能更新任务")
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=task["workspace_id"],
                project_id=task["project_id"],
                permission="task.update_own",
            )
            if actor["actor_id"] != task["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "不能更新其他操作者的 task")
            check_version(
                task["lock_version"],
                expected_task_version,
                "TASK_VERSION_CONFLICT",
            )
            allowed = TASK_TRANSITIONS.get(task["status"])
            if allowed is None or status not in allowed:
                raise WorkspaceServiceError(
                    "INVALID_TASK_TRANSITION",
                    f"task 状态不能从 {task['status']} 变为 {status}",
                )
            updated = connection.execute(
                """
                UPDATE tasks
                SET status = ?, result_ref = ?, error_code = ?,
                    lock_version = lock_version + 1, updated_at = ?
                WHERE task_id = ? AND lock_version = ?
                """,
                (
                    status,
                    result_ref,
                    error_code,
                    timestamp,
                    task_id,
                    expected_task_version,
                ),
            )
            if updated.rowcount != 1:
                raise WorkspaceServiceError(
                    "TASK_VERSION_CONFLICT", "task 在写入期间已变化"
                )
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO task_events(
                    task_event_id, task_id, sequence_no, status, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("task_event"),
                    task_id,
                    sequence,
                    status,
                    json.dumps(
                        details or {},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                ),
            )
            enqueue_outbox(
                connection,
                aggregate_type="task",
                aggregate_id=task_id,
                event_type="task.status_changed",
                payload={
                    "error_code": error_code,
                    "result_ref": result_ref,
                    "status": status,
                    "task_version": expected_task_version + 1,
                },
            )
        return {
            "task_id": task_id,
            "agent_session_id": task["agent_session_id"],
            "auth_session_id": task["auth_session_id"],
            "actor_id": task["actor_id"],
            "project_id": task["project_id"],
            "task_type": task["task_type"],
            "payload_sha256": task["payload_sha256"],
            "status": status,
            "result_ref": result_ref,
            "error_code": error_code,
            "task_version": expected_task_version + 1,
        }

    def transition_task(self, auth_token: str, **kwargs) -> dict:
        return self.append_task_event(auth_token, **kwargs)

    def record_task_progress(
        self,
        auth_token: str,
        *,
        task_id: str,
        expected_task_version: int,
        details: dict,
        advance_version: bool = False,
        result_annotation: bool = False,
    ) -> dict:
        """Persist observed runtime progress without inventing a task transition."""
        if not isinstance(details, dict) or not details:
            raise WorkspaceServiceError("INVALID_INPUT", "progress details 必须是非空 object")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            task = connection.execute(
                """
                SELECT t.*, p.workspace_id, p.status AS project_status
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise WorkspaceServiceError("NOT_FOUND", "task 不存在")
            if task["project_status"] != "active":
                raise WorkspaceServiceError("PROJECT_ARCHIVED", "项目已归档，不能记录任务进度")
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=task["workspace_id"],
                project_id=task["project_id"],
                permission="task.update_own",
            )
            if actor["actor_id"] != task["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "不能更新其他操作者的 task")
            check_version(
                task["lock_version"],
                expected_task_version,
                "TASK_VERSION_CONFLICT",
            )
            allowed_statuses = ({"completed", "partially_completed", "cancelled"} if result_annotation
                                else {"executing", "waiting_human_review", "waiting_user"})
            if task["status"] not in allowed_statuses:
                raise WorkspaceServiceError(
                    "INVALID_TASK_TRANSITION", "仅活动 task 可以记录运行进度"
                )
            if advance_version:
                connection.execute(
                    "UPDATE tasks SET lock_version=lock_version+1, updated_at=? WHERE task_id=?",
                    (timestamp, task_id),
                )
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM task_events WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO task_events(
                    task_event_id, task_id, sequence_no, status, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("task_event"),
                    task_id,
                    sequence,
                    task["status"],
                    json.dumps(
                        details,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                ),
            )
            enqueue_outbox(
                connection,
                aggregate_type="task",
                aggregate_id=task_id,
                event_type="task.progress_observed",
                payload={"sequence_no": sequence, "status": task["status"]},
            )
        result = row_dict(task)
        result.pop("workspace_id", None)
        result.pop("project_status", None)
        result["task_version"] = result.pop("lock_version") + int(advance_version)
        return result

    def get_task(self, auth_token: str, task_id: str) -> dict:
        with self.store.connection() as connection:
            task = connection.execute(
                """
                SELECT t.*, p.workspace_id
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise WorkspaceServiceError("NOT_FOUND", "task 不存在")
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=task["workspace_id"],
                project_id=task["project_id"],
                permission="task.read",
            )
            if actor["actor_id"] != task["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "不能读取其他操作者的 task")
            result = row_dict(task)
        result.pop("workspace_id", None)
        result["task_version"] = result.pop("lock_version")
        return result

    def list_agent_session_tasks(
        self,
        auth_token: str,
        agent_session_id: str,
    ) -> list[dict]:
        with self.store.connection() as connection:
            session = connection.execute(
                """
                SELECT s.*, p.workspace_id
                FROM agent_sessions s JOIN projects p ON p.project_id = s.project_id
                WHERE s.agent_session_id = ?
                """,
                (agent_session_id,),
            ).fetchone()
            if session is None:
                raise WorkspaceServiceError("NOT_FOUND", "agent session 不存在")
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=session["workspace_id"],
                project_id=session["project_id"],
                permission="project.read",
            )
            if actor["actor_id"] != session["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "不能读取其他操作者的 task")
            rows = connection.execute(
                """
                SELECT * FROM tasks WHERE agent_session_id = ?
                ORDER BY created_at, task_id
                """,
                (agent_session_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = row_dict(row)
            item["task_version"] = item.pop("lock_version")
            result.append(item)
        return result

    def list_project_tasks(self, auth_token: str, project_id: str) -> list[dict]:
        """Read persisted project tasks for an authenticated project member."""
        with self.store.connection() as connection:
            project = _project(connection, project_id)
            require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="task.read",
            )
            rows = connection.execute(
                f"""SELECT task_type, status, error_code, created_at, updated_at,
                {_TASK_RESULT_KIND_SQL} AS result_kind
                FROM tasks t WHERE project_id=?
                ORDER BY created_at DESC, task_id DESC""",
                (project_id,),
            ).fetchall()
        return [
            {
                **(item := row_dict(row)),
                **self.employee_task_fields(item, project_id),
            }
            for row in rows
        ]

    def list_workspace_tasks(
        self,
        auth_token: str,
        workspace_id: str,
        *,
        project_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Read employee-safe tasks across a workspace without selecting a project."""
        safe_limit = max(1, min(int(limit), 200))
        with self.store.connection() as connection:
            _, scope = require_collection_read(
                connection,
                auth_token,
                workspace_id=workspace_id,
                permission="task.read",
            )
            clauses = ["p.workspace_id = ?"]
            parameters: list[object] = [workspace_id]
            if scope is not None:
                clauses.append(f"t.project_id IN ({','.join('?' for _ in scope)})")
                parameters.extend(scope)
            if project_id:
                if scope is not None and project_id not in scope:
                    raise WorkspaceServiceError("PERMISSION_DENIED", "操作者无权限访问该项目任务")
                project = _project(connection, project_id)
                if project["workspace_id"] != workspace_id:
                    raise WorkspaceServiceError(
                        "CROSS_PROJECT_DENIED", "project 不属于当前 workspace"
                    )
                clauses.append("t.project_id = ?")
                parameters.append(project_id)
            if status:
                clauses.append("t.status = ?")
                parameters.append(status)
            parameters.append(safe_limit)
            rows = connection.execute(
                f"""
                SELECT t.task_id, t.project_id, p.name AS project_name,
                       t.task_type, t.status, t.error_code,
                       t.created_at, t.updated_at, {_TASK_RESULT_KIND_SQL} AS result_kind
                FROM tasks t
                JOIN projects p ON p.project_id = t.project_id
                WHERE {' AND '.join(clauses)}
                ORDER BY t.updated_at DESC, t.task_id DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [
            {
                **(item := row_dict(row)),
                **self.employee_task_fields(item, str(item["project_id"])),
            }
            for row in rows
        ]

    def query_workspace_tasks(
        self, auth_token: str, workspace_id: str, *, project_id: str | None = None,
        status: str | None = None, query: str = "", created_from: str = "",
        created_to: str = "", page_size: int = 50, cursor: str | None = None,
        administrative: bool = False,
    ) -> dict:
        """Stable keyset query shared by employee and administrator views.

        The cursor binds the query and actor, not a historical snapshot. Each read
        applies current authorization and filters; legacy limit queries stay intact.
        """
        if not 1 <= page_size <= 200:
            raise WorkspaceServiceError("INVALID_INPUT", "每页数量须在1至200之间")
        states = sorted(set(filter(None, (status or "").split(","))))
        if any(value not in TASK_TRANSITIONS for value in states):
            raise WorkspaceServiceError("INVALID_INPUT", "任务状态筛选无效")
        query = query.strip()
        if len(query) > 200:
            raise WorkspaceServiceError("INVALID_INPUT", "搜索内容最多200字")

        def date_bound(value, *, end=False):
            if not value:
                return ""
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if len(value) == 10 and end:
                    parsed += timedelta(days=1)
                if len(value) == 10:
                    return parsed.date().isoformat()
                return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            except (ValueError, TypeError) as exc:
                raise WorkspaceServiceError("INVALID_INPUT", "日期筛选无效") from exc

        start, end = date_bound(created_from), date_bound(created_to, end=True)
        if start and end and start >= end:
            raise WorkspaceServiceError("INVALID_INPUT", "开始日期必须早于结束范围")
        with self.store.connection() as connection:
            if administrative:
                actor = require_permission(connection, auth_token, workspace_id=workspace_id, permission="system.read")
                scope = None
            else:
                actor, scope = require_collection_read(connection, auth_token, workspace_id=workspace_id, permission="task.read")
            clauses, parameters = ["p.workspace_id = ?"], [workspace_id]
            if scope is not None:
                clauses.append(f"t.project_id IN ({','.join('?' for _ in scope)})")
                parameters.extend(scope)
            scope_clauses, scope_parameters = list(clauses), list(parameters)
            if project_id:
                if scope is not None and project_id not in scope:
                    raise WorkspaceServiceError("PERMISSION_DENIED", "操作者无权限访问该项目任务")
                project = _project(connection, project_id)
                if project["workspace_id"] != workspace_id:
                    raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
                clauses.append("t.project_id = ?")
                parameters.append(project_id)
            if states:
                clauses.append(f"t.status IN ({','.join('?' for _ in states)})")
                parameters.extend(states)
            if query:
                # Literal search: '%' and '_' from the input are not SQL wildcards.
                pattern = '%' + query.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'
                matching_types = [key for key, label in EMPLOYEE_TASK_TYPES.items() if query in label]
                searches = [f"LOWER({field}) LIKE LOWER(?) ESCAPE '!'" for field in ('p.name', 't.task_id', 't.task_type')]
                parameters.extend([pattern] * 3)
                if matching_types:
                    searches.append(f"t.task_type IN ({','.join('?' for _ in matching_types)})")
                    parameters.extend(matching_types)
                clauses.append('(' + ' OR '.join(searches) + ')')
            if start:
                clauses.append("t.created_at >= ?")
                parameters.append(start)
            if end:
                clauses.append("t.created_at < ?")
                parameters.append(end)
            binding = hashlib.sha256(json.dumps([workspace_id, actor["actor_id"], project_id or "",
                states, query, start, end, administrative], ensure_ascii=False).encode()).hexdigest()
            where = " AND ".join(clauses)
            joined = "FROM tasks t JOIN projects p ON p.project_id=t.project_id"
            matched_total = int(connection.execute(f"SELECT COUNT(*) {joined} WHERE {where}", parameters).fetchone()[0])
            workspace_counts = {row["status"]: int(row["count"]) for row in connection.execute(
                f"SELECT t.status, COUNT(*) AS count {joined} WHERE {' AND '.join(scope_clauses)} GROUP BY t.status", scope_parameters).fetchall()}
            page_clauses, page_parameters = list(clauses), list(parameters)
            if cursor:
                try:
                    if len(cursor) > 2048:
                        raise ValueError("oversized cursor")
                    anchor = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
                    if anchor["binding"] != binding or not isinstance(anchor["created_at"], str) or not isinstance(anchor["task_id"], str):
                        raise ValueError("different query")
                except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                    raise WorkspaceServiceError("INVALID_CURSOR", "翻页条件已改变，请重新读取第一页") from exc
                page_clauses.append("(t.created_at < ? OR (t.created_at = ? AND t.task_id < ?))")
                page_parameters.extend([anchor["created_at"], anchor["created_at"], anchor["task_id"]])
            rows = connection.execute(f"""SELECT t.task_id, t.project_id, p.name AS project_name,
                t.task_type, t.status, t.error_code, t.created_at, t.updated_at,
                p.lock_version AS project_version, {_TASK_RESULT_KIND_SQL} AS result_kind
                {joined} WHERE {' AND '.join(page_clauses)}
                ORDER BY t.created_at DESC, t.task_id DESC LIMIT ?""", [*page_parameters, page_size + 1]).fetchall()
            has_more = len(rows) > page_size
            items = []
            for row in rows[:page_size]:
                item = row_dict(row)
                item.update(self.employee_task_fields(item, item["project_id"]))
                if administrative:
                    latest = connection.execute("""SELECT attempt_id, attempt_no, origin,
                        requested_stage_id, reason AS attempt_reason FROM task_attempts
                        WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1""", (item["task_id"],)).fetchone()
                    item.update(row_dict(latest) if latest else {})
                items.append(item)
            next_cursor = None
            if has_more:
                next_cursor = base64.urlsafe_b64encode(json.dumps({"binding": binding,
                    "created_at": items[-1]["created_at"], "task_id": items[-1]["task_id"]}).encode()).decode()
        return {"items": items, "count": len(items), "matched_total": matched_total,
            "workspace_status_counts": workspace_counts, "next_cursor": next_cursor,
            "page_size": page_size, "sort": "created_at_desc_task_id_desc",
            "statistics_scope": "workspace", "snapshot": False}

    def list_task_events(
        self,
        auth_token: str,
        task_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[dict]:
        with self.store.connection() as connection:
            task = connection.execute(
                """
                SELECT t.*, p.workspace_id
                FROM tasks t JOIN projects p ON p.project_id = t.project_id
                WHERE t.task_id = ?
                """,
                (task_id,),
            ).fetchone()
            if task is None:
                raise WorkspaceServiceError("NOT_FOUND", "task 不存在")
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=task["workspace_id"],
                project_id=task["project_id"],
                permission="task.read",
            )
            if actor["actor_id"] != task["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "不能读取其他操作者的 task")
            rows = connection.execute(
                """
                SELECT * FROM task_events
                WHERE task_id = ? AND sequence_no > ?
                ORDER BY sequence_no
                """,
                (task_id, after_sequence),
            ).fetchall()
        result = []
        for row in rows:
            item = row_dict(row)
            item["details"] = json.loads(item.pop("details_json"))
            result.append(item)
        return result
