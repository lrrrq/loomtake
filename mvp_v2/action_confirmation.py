from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from .agent_tools import TOOL_REGISTRY, validate_tool_input
from .manual_actions import MANUAL_ACTION_REGISTRY, validate_manual_action_input
from .identity_service import require_permission
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    enqueue_outbox,
    new_id,
    sha256_text,
    utc_now,
)


class ActionConfirmationService:
    """Agent-only confirmation boundary. It never accepts manual page commands."""

    _registry = TOOL_REGISTRY
    _validator = staticmethod(validate_tool_input)
    _channel = "agent"
    def __init__(self, store: WorkspaceStore, signing_key: bytes):
        if not isinstance(signing_key, bytes) or len(signing_key) < 32:
            raise ValueError("confirmation signing_key 至少需要 32 bytes")
        self.store = store
        self.signing_key = signing_key
        self.store.initialize()

    def issue(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        internal_binding_sha256: str = "",
        ttl_seconds: int = 300,
    ) -> dict:
        definition = self._confirmation_tool(tool_name)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 900:
            raise WorkspaceServiceError("INVALID_INPUT", "确认有效期必须为 1 到 900 秒")
        parameters_sha256 = self._parameters_sha256(
            tool_name, parameters, internal_binding_sha256=internal_binding_sha256
        )
        now = int(time.time())
        expires = now + ttl_seconds
        confirmation_id = new_id("confirmation")
        nonce = secrets.token_urlsafe(24)
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
                    "PERMISSION_DENIED", "不能为其他操作者的 agent session 签发确认"
                )
            _require_project_binding(parameters, session["project_id"])
            connection.execute(
                """
                INSERT INTO action_confirmations(
                    confirmation_id, agent_session_id, actor_id, tool_name,
                    parameters_sha256, nonce_hash, expires_at, status,
                    created_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                """,
                (
                    confirmation_id,
                    agent_session_id,
                    actor["actor_id"],
                    tool_name,
                    parameters_sha256,
                    sha256_text(nonce),
                    _timestamp(expires),
                    utc_now(),
                ),
            )
            enqueue_outbox(
                connection,
                aggregate_type="agent_session",
                aggregate_id=agent_session_id,
                event_type=f"{self._channel}.confirmation_issued",
                payload={
                    "confirmation_id": confirmation_id,
                    "parameters_sha256": parameters_sha256,
                    "tool_name": tool_name,
                },
            )
        payload = {
            "confirmation_id": confirmation_id,
            "agent_session_id": agent_session_id,
            "actor_id": actor["actor_id"],
            "tool_name": tool_name,
            "parameters_sha256": parameters_sha256,
            "nonce": nonce,
            "expires_at": expires,
        }
        return {
            "confirmation_id": confirmation_id,
            "confirmation_token": self._encode(payload),
            "tool_name": tool_name,
            "parameters_sha256": parameters_sha256,
            "expires_at": _timestamp(expires),
        }

    def consume(
        self,
        auth_token: str,
        *,
        confirmation_token: str,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        internal_binding_sha256: str = "",
    ) -> dict:
        with self.store.transaction() as connection:
            return self.consume_in_transaction(
                connection,
                auth_token,
                confirmation_token=confirmation_token,
                agent_session_id=agent_session_id,
                tool_name=tool_name,
                parameters=parameters,
                internal_binding_sha256=internal_binding_sha256,
            )

    def consume_in_transaction(
        self,
        connection,
        auth_token: str,
        *,
        confirmation_token: str,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        internal_binding_sha256: str = "",
    ) -> dict:
        definition = self._confirmation_tool(tool_name)
        token = self._decode(confirmation_token)
        expected_parameters_sha256 = self._parameters_sha256(
            tool_name, parameters, internal_binding_sha256=internal_binding_sha256
        )
        if token.get("agent_session_id") != agent_session_id or token.get("tool_name") != tool_name:
            raise WorkspaceServiceError("PERMISSION_DENIED", "确认令牌与会话或动作不匹配")
        if token.get("parameters_sha256") != expected_parameters_sha256:
            raise WorkspaceServiceError("INVALID_INPUT", "动作参数已变化，需要重新确认")
        expires_at = token.get("expires_at")
        if type(expires_at) is not int or expires_at < int(time.time()):
            raise WorkspaceServiceError("PERMISSION_DENIED", "确认令牌已过期，需要重新确认")
        session = _agent_session(connection, agent_session_id)
        actor = require_permission(
            connection,
            auth_token,
            workspace_id=session["workspace_id"],
            project_id=session["project_id"],
            permission=definition.permission,
        )
        if actor["actor_id"] != session["actor_id"] or token.get("actor_id") != actor["actor_id"]:
            raise WorkspaceServiceError("PERMISSION_DENIED", "确认人或会话操作者不匹配")
        _require_project_binding(parameters, session["project_id"])
        row = connection.execute(
            """
            SELECT * FROM action_confirmations WHERE confirmation_id = ?
            """,
            (str(token.get("confirmation_id") or ""),),
        ).fetchone()
        if row is None:
            raise WorkspaceServiceError("PERMISSION_DENIED", "确认记录不存在")
        if row["status"] != "pending":
            raise WorkspaceServiceError("PERMISSION_DENIED", "确认令牌已消费或撤销")
        if (
            row["agent_session_id"] != agent_session_id
            or row["actor_id"] != actor["actor_id"]
            or row["tool_name"] != tool_name
            or row["parameters_sha256"] != expected_parameters_sha256
            or row["nonce_hash"] != sha256_text(str(token.get("nonce") or ""))
        ):
            raise WorkspaceServiceError("PERMISSION_DENIED", "确认记录校验失败")
        updated = connection.execute(
            """
            UPDATE action_confirmations
            SET status = 'consumed', consumed_at = ?
            WHERE confirmation_id = ? AND status = 'pending'
            """,
            (utc_now(), row["confirmation_id"]),
        )
        if updated.rowcount != 1:
            raise WorkspaceServiceError("PERMISSION_DENIED", "确认令牌已被消费")
        enqueue_outbox(
            connection,
            aggregate_type="agent_session",
            aggregate_id=agent_session_id,
            event_type=f"{self._channel}.confirmation_consumed",
            payload={
                "confirmation_id": row["confirmation_id"],
                "parameters_sha256": expected_parameters_sha256,
                "tool_name": tool_name,
            },
        )
        return {
            "confirmation_id": row["confirmation_id"],
            "agent_session_id": agent_session_id,
            "actor_id": actor["actor_id"],
            "tool_name": tool_name,
            "parameters_sha256": expected_parameters_sha256,
            "status": "consumed",
        }

    def _confirmation_tool(self, tool_name: str):
        definition = self._registry.get(str(tool_name or ""))
        if definition is None or not definition.confirmation_required:
            raise WorkspaceServiceError("INVALID_INPUT", "当前通道不支持该动作确认")
        return definition

    def _parameters_sha256(
        self,
        tool_name: str,
        parameters: dict[str, Any],
        *,
        internal_binding_sha256: str = "",
    ) -> str:
        normalized = dict(parameters or {})
        normalized.pop("confirmation_token", None)
        definition = self._registry[tool_name]
        if "confirmation_token" in definition.input_model.model_fields:
            validation_payload = {**normalized, "confirmation_token": "confirmation-placeholder-00000000"}
        else:
            validation_payload = normalized
        try:
            validated = self._validator(tool_name, validation_payload).model_dump(mode="json")
            validated.pop("confirmation_token", None)
            if internal_binding_sha256:
                validated["_internal_binding_sha256"] = internal_binding_sha256
            serialized = _canonical_json(validated)
        except (TypeError, ValueError) as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "动作参数不符合当前通道合同") from exc
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _encode(self, payload: dict[str, Any]) -> str:
        body = _b64encode(_canonical_json(payload).encode("utf-8"))
        signature = _b64encode(hmac.new(self.signing_key, body.encode("ascii"), hashlib.sha256).digest())
        return f"{body}.{signature}"

    def _decode(self, token: str) -> dict[str, Any]:
        try:
            body, supplied_signature = str(token or "").split(".", 1)
            expected_signature = _b64encode(
                hmac.new(self.signing_key, body.encode("ascii"), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_signature, expected_signature):
                raise ValueError("signature mismatch")
            payload = json.loads(_b64decode(body).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload is not object")
            return payload
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("PERMISSION_DENIED", "确认令牌无效") from exc


class ManualPageConfirmationService(ActionConfirmationService):
    """Manual-page-only R3 confirmation boundary; Agent services cannot use it."""

    _registry = MANUAL_ACTION_REGISTRY
    _validator = staticmethod(validate_manual_action_input)
    _channel = "manual_page"


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


def _require_project_binding(parameters: dict[str, Any], session_project_id: str) -> None:
    project_id = str((parameters or {}).get("project_id") or "").strip()
    if project_id and project_id != session_project_id:
        raise WorkspaceServiceError("PERMISSION_DENIED", "动作 project_id 与会话项目不一致")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _timestamp(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, timezone.utc).isoformat().replace("+00:00", "Z")
