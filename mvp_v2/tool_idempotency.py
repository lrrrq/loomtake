from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import ValidationError

from .agent_tools import TOOL_REGISTRY, ToolExecutionResult, validate_tool_input
from .manual_actions import MANUAL_ACTION_REGISTRY, validate_manual_action_input
from .identity_service import require_permission
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    enqueue_outbox,
    new_id,
    row_dict,
    utc_now,
)


class ToolIdempotencyService:
    """Prevent one logical agent write from being executed more than once."""

    _registry = TOOL_REGISTRY
    _validator = staticmethod(validate_tool_input)
    _channel = "agent_tool"

    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.store.initialize()

    def claim(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        internal_binding_sha256: str = "",
    ) -> dict:
        definition, validated = self._validated_write(tool_name, parameters)
        idempotency_key = validated["idempotency_key"]
        request_sha256 = _request_sha256(
            validated, internal_binding_sha256=internal_binding_sha256
        )
        timestamp = utc_now()
        with self.store.transaction() as connection:
            session = _agent_session(connection, agent_session_id)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=session["workspace_id"],
                project_id=session["project_id"],
                permission=definition.permission,
            )
            if actor["actor_id"] != session["actor_id"]:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "不能在其他操作者的 agent session 中占用幂等键"
                )
            scope_type, scope_id = _scope(definition.idempotency_scope, validated, session)
            existing = connection.execute(
                """
                SELECT * FROM tool_idempotency_records
                WHERE actor_id = ? AND scope_type = ? AND scope_id = ?
                  AND tool_name = ? AND idempotency_key = ?
                """,
                (
                    actor["actor_id"],
                    scope_type,
                    scope_id,
                    tool_name,
                    idempotency_key,
                ),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    raise WorkspaceServiceError(
                        "IDEMPOTENCY_CONFLICT",
                        "同一幂等键已用于不同动作参数，请生成新的幂等键",
                    )
                return _public_record(existing, replayed=True)

            record_id = new_id("idempotency")
            connection.execute(
                """
                INSERT INTO tool_idempotency_records(
                    idempotency_record_id, agent_session_id, actor_id,
                    scope_type, scope_id, tool_name, idempotency_key,
                    request_sha256, status, response_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', NULL, ?, ?)
                """,
                (
                    record_id,
                    agent_session_id,
                    actor["actor_id"],
                    scope_type,
                    scope_id,
                    tool_name,
                    idempotency_key,
                    request_sha256,
                    timestamp,
                    timestamp,
                ),
            )
            enqueue_outbox(
                connection,
                aggregate_type="agent_session",
                aggregate_id=agent_session_id,
                event_type=f"{self._channel}.idempotency_claimed",
                payload={
                    "idempotency_record_id": record_id,
                    "request_sha256": request_sha256,
                    "scope_id": scope_id,
                    "scope_type": scope_type,
                    "tool_name": tool_name,
                },
            )
            created = connection.execute(
                "SELECT * FROM tool_idempotency_records WHERE idempotency_record_id = ?",
                (record_id,),
            ).fetchone()
        return _public_record(created, replayed=False)

    def record_result(
        self,
        auth_token: str,
        *,
        idempotency_record_id: str,
        result: ToolExecutionResult | dict[str, Any],
    ) -> dict:
        try:
            normalized = ToolExecutionResult.model_validate(result).model_dump(mode="json")
        except ValidationError as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "工具结果不符合公共合同") from exc
        serialized = _canonical_json(normalized)
        timestamp = utc_now()
        with self.store.transaction() as connection:
            record = connection.execute(
                """
                SELECT r.*, s.project_id, p.workspace_id
                FROM tool_idempotency_records r
                JOIN agent_sessions s ON s.agent_session_id = r.agent_session_id
                JOIN projects p ON p.project_id = s.project_id
                WHERE r.idempotency_record_id = ?
                """,
                (idempotency_record_id,),
            ).fetchone()
            if record is None:
                raise WorkspaceServiceError("NOT_FOUND", "幂等记录不存在")
            definition = self._registry[record["tool_name"]]
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=record["workspace_id"],
                project_id=record["project_id"],
                permission=definition.permission,
            )
            if actor["actor_id"] != record["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "不能写入其他操作者的幂等结果")
            if record["status"] != "in_progress":
                if record["response_json"] == serialized:
                    return _public_record(record, replayed=True)
                raise WorkspaceServiceError(
                    "IDEMPOTENCY_CONFLICT", "幂等动作已经记录了不同结果"
                )
            connection.execute(
                """
                UPDATE tool_idempotency_records
                SET status = ?, response_json = ?, updated_at = ?
                WHERE idempotency_record_id = ? AND status = 'in_progress'
                """,
                (
                    normalized["status"],
                    serialized,
                    timestamp,
                    idempotency_record_id,
                ),
            )
            enqueue_outbox(
                connection,
                aggregate_type="agent_session",
                aggregate_id=record["agent_session_id"],
                event_type=f"{self._channel}.idempotency_result_recorded",
                payload={
                    "idempotency_record_id": idempotency_record_id,
                    "status": normalized["status"],
                    "task_id": normalized["task_id"],
                    "tool_name": record["tool_name"],
                },
            )
            updated = connection.execute(
                "SELECT * FROM tool_idempotency_records WHERE idempotency_record_id = ?",
                (idempotency_record_id,),
            ).fetchone()
        return _public_record(updated, replayed=False)

    def _validated_write(self, tool_name: str, parameters: dict[str, Any]):
        definition = self._registry.get(str(tool_name or ""))
        if definition is None or definition.idempotency_scope == "read":
            raise WorkspaceServiceError("INVALID_INPUT", "当前通道不支持该写动作")
        try:
            validated = self._validator(tool_name, parameters).model_dump(mode="json")
        except (ValidationError, TypeError, ValueError) as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "动作参数不符合当前通道合同") from exc
        return definition, validated


class ManualPageIdempotencyService(ToolIdempotencyService):
    """Manual-page-only idempotency boundary for R3 commands."""

    _registry = MANUAL_ACTION_REGISTRY
    _validator = staticmethod(validate_manual_action_input)
    _channel = "manual_page"


def _request_sha256(
    parameters: dict[str, Any], *, internal_binding_sha256: str = ""
) -> str:
    business_parameters = dict(parameters)
    business_parameters.pop("confirmation_token", None)
    business_parameters.pop("idempotency_key", None)
    if internal_binding_sha256:
        business_parameters["_internal_binding_sha256"] = internal_binding_sha256
    return hashlib.sha256(_canonical_json(business_parameters).encode("utf-8")).hexdigest()


def _scope(scope_type: str, parameters: dict[str, Any], session) -> tuple[str, str]:
    if scope_type == "project":
        scope_id = str(parameters.get("project_id") or "")
        if scope_id != session["project_id"]:
            raise WorkspaceServiceError("PERMISSION_DENIED", "动作 project_id 与会话项目不一致")
    elif scope_type == "workspace":
        scope_id = str(parameters.get("workspace_id") or "")
        if scope_id != session["workspace_id"]:
            raise WorkspaceServiceError("PERMISSION_DENIED", "动作 workspace_id 与会话工作区不一致")
    elif scope_type == "package":
        scope_id = str(parameters.get("package_id") or "")
    elif scope_type == "knowledge":
        scope_id = str(session["workspace_id"])
    else:
        raise WorkspaceServiceError("INVALID_INPUT", "工具幂等范围不受支持")
    if not scope_id:
        raise WorkspaceServiceError("INVALID_INPUT", "幂等范围标识不能为空")
    return scope_type, scope_id


def _agent_session(connection, agent_session_id: str):
    row = connection.execute(
        """
        SELECT s.*, p.workspace_id
        FROM agent_sessions s JOIN projects p ON p.project_id = s.project_id
        WHERE s.agent_session_id = ?
        """,
        (str(agent_session_id or ""),),
    ).fetchone()
    if row is None or row["status"] != "active":
        raise WorkspaceServiceError("NOT_FOUND", "active agent session 不存在")
    return row


def _public_record(row, *, replayed: bool) -> dict:
    record = row_dict(row)
    response = json.loads(record["response_json"]) if record.get("response_json") else None
    return {
        "idempotency_record_id": record["idempotency_record_id"],
        "agent_session_id": record["agent_session_id"],
        "scope_type": record["scope_type"],
        "scope_id": record["scope_id"],
        "tool_name": record["tool_name"],
        "idempotency_key": record["idempotency_key"],
        "request_sha256": record["request_sha256"],
        "status": record["status"],
        "response": response,
        "replayed": replayed,
    }


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
