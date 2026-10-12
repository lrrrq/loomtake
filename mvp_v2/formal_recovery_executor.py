"""Execute administrator-created attempts through the existing task authority."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Union

from .run_state import exclusive_lock
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    new_id,
    require_artifact_ref,
    require_sha256,
    row_dict,
    utc_now,
)


RECOVERY_EVENTS = frozenset({
    "task.recovery_attempt_created",
    "task.stage_retry_attempt_created",
})


@dataclass(frozen=True)
class RecoveryExecutionContext:
    """Stable, attempt-scoped input passed to a registered stage handler."""

    event_id: str
    task_id: str
    attempt_id: str
    stage_id: str
    action: str
    checkpoint_id: str
    idempotency_key: str
    attempt: dict[str, Any]
    auth_token: str = field(default="", repr=False, compare=False)


@dataclass(frozen=True)
class RecoveryStageResult:
    """The only handler result that can produce a successful terminal record."""

    status: str
    output_ref: str | None = None
    output_sha256: str | None = None
    input_ref: str | None = None
    input_sha256: str | None = None
    error_code: str | None = None
    error_summary: str = ""


RecoveryStageHandler = Callable[
    [RecoveryExecutionContext], Union[RecoveryStageResult, Mapping[str, Any]]
]


class FormalRecoveryExecutor:
    """Consume recovery outbox events without creating a second task state machine.

    A handler owns the actual stage work and artifact materialization.  The
    executor only records the handler's result in the existing immutable
    checkpoint and task-event authorities.  The handler receives the same
    attempt id on every replay, so an idempotent existing stage handler can
    safely reconcile its own side effects.
    """

    def __init__(
        self,
        store: WorkspaceStore,
        *,
        handlers: Mapping[str, RecoveryStageHandler],
        authenticated_stages: frozenset[str] = frozenset(),
    ):
        self.store = store
        self.store.initialize()
        self.handlers = dict(handlers)
        self.authenticated_stages = authenticated_stages

    def consume_pending(self, *, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 200))
        with self.store.connection() as connection:
            rows = connection.execute(
                """SELECT event_id FROM outbox
                WHERE event_type IN (?, ?)
                  AND status IN ('pending', 'failed')
                ORDER BY sequence_id LIMIT ?""",
                (*sorted(RECOVERY_EVENTS), safe_limit),
            ).fetchall()
        results = []
        for row in rows:
            with self.store.connection() as connection:
                event = connection.execute("SELECT payload_json FROM outbox WHERE event_id=?", (row["event_id"],)).fetchone()
            if json.loads(event["payload_json"]).get("requested_stage_id") in self.authenticated_stages:
                continue
            results.append(self.consume_outbox_event(row["event_id"]))
        return results

    def consume_outbox_event(self, event_id: str, *, auth_token: str = "") -> dict[str, Any]:
        database = self.store.database_path.resolve()
        event_key = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
        lock = exclusive_lock(
            database.parent / f".{database.name}.formal-recovery" / f"{event_key}.lock",
            run_id=event_id,
        )
        try:
            lock.__enter__()
        except RuntimeError as exc:
            if isinstance(exc.__cause__, BlockingIOError):
                raise WorkspaceServiceError(
                    "RECOVERY_EXECUTION_IN_PROGRESS", "本次重试仍在执行，请稍后查看结果",
                ) from exc
            raise WorkspaceServiceError(
                "RECOVERY_RECONCILIATION_REQUIRED", "无法核实本次重试是否仍在执行",
            ) from exc
        try:
            return self._consume_claimed_event(event_id, auth_token=auth_token)
        finally:
            lock.__exit__(None, None, None)

    def _consume_claimed_event(self, event_id: str, *, auth_token: str) -> dict[str, Any]:
        event, context = self._claim_event(event_id)
        if context is None:
            terminal = self._terminal_for_event(event)
            self._ack_event(event["event_id"])
            return self._response(event, terminal, replayed=True)

        context = replace(context, auth_token=auth_token)
        if context.stage_id in self.authenticated_stages and not auth_token:
            raise WorkspaceServiceError("AUTH_SESSION_INVALID", "请在当前会话确认继续生成")
        handler = self.handlers.get(context.stage_id)
        if handler is None:
            result = RecoveryStageResult(
                status="failed",
                error_code="HANDLER_NOT_REGISTERED",
                error_summary=f"未注册阶段 handler: {context.stage_id}",
            )
        else:
            try:
                result = self._normalize_result(handler(context))
            except Exception as exc:
                result = RecoveryStageResult(
                    status="failed",
                    error_code="HANDLER_ERROR",
                    error_summary=self._safe_error(exc),
                )

        terminal = self._persist_terminal(event, context, result)
        self._ack_event(event["event_id"])
        return self._response(event, terminal, replayed=False)

    def terminal_for_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self.store.connection() as connection:
            row = connection.execute(
                """SELECT checkpoint_id, attempt_id, task_id, stage_id, status,
                          input_ref, input_sha256, output_ref, output_sha256,
                          error_code, created_at
                FROM stage_checkpoints WHERE attempt_id=?
                ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1""",
                (attempt_id,),
            ).fetchone()
        return self._public_terminal(row)

    def reconcile_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        """Return only authority-backed terminal evidence; never invent status."""

        return self.terminal_for_attempt(attempt_id)

    def _claim_event(
        self, event_id: str,
    ) -> tuple[sqlite3.Row, RecoveryExecutionContext | None]:
        with self.store.transaction() as connection:
            event = connection.execute(
                "SELECT * FROM outbox WHERE event_id=?", (event_id,),
            ).fetchone()
            if event is None:
                raise WorkspaceServiceError("NOT_FOUND", "恢复 outbox 事件不存在")
            if event["event_type"] not in RECOVERY_EVENTS:
                raise WorkspaceServiceError("INVALID_INPUT", "不是受支持的恢复 outbox 事件")
            if event["aggregate_type"] != "task":
                raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "恢复事件不是 task 聚合")
            payload = self._payload(event)
            context = self._context(connection, event, payload)
            terminal = self._terminal_for_context(connection, context)
            if terminal is not None:
                return event, None
            if event["status"] == "published":
                raise WorkspaceServiceError(
                    "RECOVERY_RECONCILIATION_REQUIRED",
                    "恢复事件已发布但缺少 attempt 终态证据",
                )
            if event["status"] not in {"pending", "failed"}:
                raise WorkspaceServiceError("INVALID_INPUT", "恢复 outbox 事件状态不可消费")
            connection.execute(
                """UPDATE outbox SET status='failed', attempts=attempts + 1,
                    last_error=? WHERE event_id=?""",
                ("RECOVERY_EXECUTION_IN_PROGRESS", event_id),
            )
            return event, context

    def _context(
        self,
        connection: sqlite3.Connection,
        event: sqlite3.Row,
        payload: Mapping[str, Any],
    ) -> RecoveryExecutionContext:
        attempt_id = str(payload.get("attempt_id") or "").strip()
        task_id = str(event["aggregate_id"] or "").strip()
        if not attempt_id or not task_id:
            raise WorkspaceServiceError(
                "RECOVERY_RECONCILIATION_REQUIRED", "恢复事件缺少 task 或 attempt 身份",
            )
        row = connection.execute(
            """SELECT a.*, t.task_type, t.payload_sha256, t.status AS task_status,
                      t.project_id AS task_project_id
            FROM task_attempts a JOIN tasks t ON t.task_id=a.task_id
            WHERE a.attempt_id=? AND a.task_id=?""",
            (attempt_id, task_id),
        ).fetchone()
        if row is None:
            raise WorkspaceServiceError(
                "RECOVERY_RECONCILIATION_REQUIRED", "恢复事件引用的 attempt 不存在",
            )
        if row["project_id"] != row["task_project_id"]:
            raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "attempt 项目范围不一致")
        proposal = connection.execute(
            """SELECT action, checkpoint_id, idempotency_key
            FROM recovery_proposals WHERE recovery_proposal_id=?""",
            (payload.get("recovery_proposal_id"),),
        ).fetchone()
        if proposal is None:
            raise WorkspaceServiceError(
                "RECOVERY_RECONCILIATION_REQUIRED", "恢复事件缺少原始恢复提案",
            )
        execution = connection.execute(
            """SELECT 1 FROM recovery_executions
            WHERE recovery_proposal_id=? AND new_attempt_id=?""",
            (payload.get("recovery_proposal_id"), attempt_id),
        ).fetchone()
        if execution is None:
            raise WorkspaceServiceError(
                "RECOVERY_RECONCILIATION_REQUIRED", "恢复事件未绑定已确认的新 attempt",
            )
        stage_id = str(payload.get("requested_stage_id") or row["requested_stage_id"] or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", stage_id):
            raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "恢复事件缺少有效阶段 ID")
        if row["requested_stage_id"] and stage_id != row["requested_stage_id"]:
            raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "恢复事件阶段与 attempt 不一致")
        return RecoveryExecutionContext(
            event_id=event["event_id"],
            task_id=task_id,
            attempt_id=attempt_id,
            stage_id=stage_id,
            action=str(proposal["action"]),
            checkpoint_id=str(proposal["checkpoint_id"] or ""),
            idempotency_key=str(proposal["idempotency_key"]),
            attempt=row_dict(row) or {},
        )

    def _persist_terminal(
        self,
        event: sqlite3.Row,
        context: RecoveryExecutionContext,
        result: RecoveryStageResult,
    ) -> dict[str, Any]:
        with self.store.transaction() as connection:
            existing = self._terminal_for_context(connection, context)
            if existing is not None:
                return existing
            output_ref = result.output_ref
            output_sha256 = result.output_sha256
            input_ref = result.input_ref
            input_sha256 = result.input_sha256
            if context.checkpoint_id and not input_ref:
                source = connection.execute(
                    """SELECT output_ref, output_sha256 FROM stage_checkpoints
                    WHERE checkpoint_id=? AND task_id=?""",
                    (context.checkpoint_id, context.task_id),
                ).fetchone()
                if source is None:
                    raise WorkspaceServiceError(
                        "RECOVERY_RECONCILIATION_REQUIRED", "恢复来源 checkpoint 不存在",
                    )
                input_ref = source["output_ref"]
                input_sha256 = source["output_sha256"]
            if result.status == "succeeded":
                output_ref = require_artifact_ref(output_ref or "", "output_ref")
                if output_sha256:
                    output_sha256 = require_sha256(output_sha256, "output_sha256")
            elif output_ref:
                output_ref = require_artifact_ref(output_ref, "output_ref")
                if output_sha256:
                    output_sha256 = require_sha256(output_sha256, "output_sha256")
            if input_ref:
                input_ref = require_artifact_ref(input_ref, "input_ref")
            if input_sha256:
                input_sha256 = require_sha256(input_sha256, "input_sha256")
            checkpoint_id = new_id("checkpoint")
            checkpoint_status = "completed" if result.status == "succeeded" else "failed"
            connection.execute(
                """INSERT INTO stage_checkpoints(
                    checkpoint_id, attempt_id, task_id, stage_id, status,
                    input_ref, input_sha256, output_ref, output_sha256,
                    error_code, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id, context.attempt_id, context.task_id, context.stage_id,
                    checkpoint_status, input_ref, input_sha256, output_ref, output_sha256,
                    result.error_code, utc_now(),
                ),
            )
            task = connection.execute(
                "SELECT status FROM tasks WHERE task_id=?", (context.task_id,),
            ).fetchone()
            if task is None:
                raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "恢复 task 不存在")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM task_events WHERE task_id=?",
                (context.task_id,),
            ).fetchone()[0]
            details = {
                "attempt_id": context.attempt_id,
                "checkpoint_id": checkpoint_id,
                "recovery_proposal_id": self._payload(event).get("recovery_proposal_id"),
                "stage_id": context.stage_id,
                "recovery_terminal": result.status,
            }
            if result.error_code:
                details["error_code"] = result.error_code
            connection.execute(
                """INSERT INTO task_events(
                    task_event_id, task_id, sequence_no, status, details_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    new_id("task_event"), context.task_id, sequence, task["status"],
                    json.dumps(details, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM stage_checkpoints WHERE checkpoint_id=?", (checkpoint_id,),
            ).fetchone()
        return self._public_terminal(row) or {}

    def _ack_event(self, event_id: str) -> None:
        with self.store.transaction() as connection:
            updated = connection.execute(
                """UPDATE outbox SET status='published', published_at=?, last_error=NULL
                WHERE event_id=? AND status <> 'published'""",
                (utc_now(), event_id),
            )
            if updated.rowcount == 1:
                return
            current = connection.execute(
                "SELECT status FROM outbox WHERE event_id=?", (event_id,),
            ).fetchone()
            if current is not None and current["status"] == "published":
                return
            if current is None:
                raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "恢复事件 ack 失败")

    @staticmethod
    def _terminal_for_context(
        connection: sqlite3.Connection, context: RecoveryExecutionContext,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """SELECT * FROM stage_checkpoints
            WHERE attempt_id=? AND task_id=? AND stage_id=?
            ORDER BY created_at DESC, checkpoint_id DESC LIMIT 1""",
            (context.attempt_id, context.task_id, context.stage_id),
        ).fetchone()
        return FormalRecoveryExecutor._public_terminal(row)

    def _terminal_for_event(self, event: sqlite3.Row) -> dict[str, Any]:
        with self.store.connection() as connection:
            context = self._context(connection, event, self._payload(event))
            terminal = self._terminal_for_context(connection, context)
        if terminal is None:
            raise WorkspaceServiceError(
                "RECOVERY_RECONCILIATION_REQUIRED", "恢复事件缺少 attempt 终态证据",
            )
        return terminal

    @staticmethod
    def _public_terminal(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = row_dict(row) or {}
        result["status"] = "succeeded" if result["status"] == "completed" else "failed"
        result.pop("task_id", None)
        return result

    @staticmethod
    def _payload(event: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(event["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "恢复事件 payload 无法解析") from exc
        if not isinstance(payload, dict):
            raise WorkspaceServiceError("RECOVERY_RECONCILIATION_REQUIRED", "恢复事件 payload 必须是 object")
        return payload

    @staticmethod
    def _normalize_result(value: RecoveryStageResult | Mapping[str, Any]) -> RecoveryStageResult:
        if isinstance(value, RecoveryStageResult):
            result = value
        elif isinstance(value, Mapping):
            result = RecoveryStageResult(
                status=str(value.get("status") or ""),
                output_ref=value.get("output_ref"),
                output_sha256=value.get("output_sha256"),
                input_ref=value.get("input_ref"),
                input_sha256=value.get("input_sha256"),
                error_code=value.get("error_code"),
                error_summary=str(value.get("error_summary") or ""),
            )
        else:
            raise WorkspaceServiceError("HANDLER_INVALID_RESULT", "阶段 handler 必须返回 object")
        if result.status not in {"succeeded", "failed"}:
            raise WorkspaceServiceError("HANDLER_INVALID_RESULT", "阶段 handler 未返回合法终态")
        if result.status == "failed" and not result.error_code:
            result = RecoveryStageResult(
                **{**result.__dict__, "error_code": "HANDLER_FAILED"},
            )
        return result

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        text = str(exc or "")[:500]
        text = re.sub(r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
        text = re.sub(r"(?<![\w])(?:/[\w .-]+){2,}", "[LOCAL_PATH]", text)
        return text or exc.__class__.__name__

    @staticmethod
    def _response(
        event: sqlite3.Row,
        terminal: dict[str, Any],
        *,
        replayed: bool,
    ) -> dict[str, Any]:
        return {
            "event_id": event["event_id"],
            "attempt_id": terminal["attempt_id"],
            "stage_id": terminal["stage_id"],
            "terminal_status": terminal["status"],
            "checkpoint_id": terminal["checkpoint_id"],
            "output_ref": terminal["output_ref"],
            "output_sha256": terminal["output_sha256"],
            "error_code": terminal["error_code"],
            "replayed": replayed,
        }
