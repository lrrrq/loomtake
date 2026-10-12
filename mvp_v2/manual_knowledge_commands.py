from __future__ import annotations

from dataclasses import dataclass
import secrets
from typing import Any

from .identity_service import require_permission
from .manual_version_commands import _read_confirmation_result, _consume_confirmation, _save_confirmation_result
from .knowledge_service import KnowledgeService, _target_for_scope
from .manual_actions import validate_manual_action_input
from .manual_version_commands import _audit, _claim_idempotency, _sha256, _token
from .project_service import ProjectService
from .workspace_store import WorkspaceServiceError, WorkspaceStore, new_id, utc_now


@dataclass(frozen=True)
class ManualKnowledgeApprovalCommandServices:
    store: WorkspaceStore
    projects: ProjectService
    knowledge: KnowledgeService


class ManualKnowledgeApprovalCommandService:
    """Human-only E07 approval command; personal scope is rejected by its schema."""

    tool_name = "approve_knowledge"

    def __init__(self, services: ManualKnowledgeApprovalCommandServices):
        self.services = services
        self.services.store.initialize()

    def prepare(self, auth_token: str, *, project_id: str, parameters: dict[str, Any]) -> dict:
        normalized = self._prepare_parameters(auth_token, project_id, parameters)
        context = self._context(auth_token, project_id, normalized)
        command_id, nonce = new_id("manual_command"), secrets.token_urlsafe(24)
        digest = _sha256({"tool_name": self.tool_name, "project_id": project_id, "parameters": normalized})
        with self.services.store.transaction() as connection:
            connection.execute(
                "INSERT INTO manual_command_confirmations VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL)",
                (command_id, context["actor_id"], project_id, self.tool_name, digest, _sha256(nonce), utc_now()),
            )
            _claim_idempotency(connection, context, self.tool_name, normalized["idempotency_key"], digest, command_id)
            _audit(connection, command_id, context, self.tool_name, "prepared", digest)
        return {"command_id": command_id, "command_token": f"{command_id}.{nonce}", "risk": "R3", "channel": "manual_page", "parameters": normalized, "will_not_change": "不会调用 Agent、不会更新 CURRENT 或推广个人偏好。"}

    def execute(self, auth_token: str, *, project_id: str, parameters: dict[str, Any], command_token: str) -> dict:
        normalized = self._validate(parameters)
        context = self._context(auth_token, project_id, normalized)
        command_id, nonce = _token(command_token)
        digest = _sha256({"tool_name": self.tool_name, "project_id": project_id, "parameters": normalized})
        with self.services.store.transaction() as connection:
            response = _read_confirmation_result(connection, command_id=command_id, nonce=nonce,
                context=context, tool_name=self.tool_name, digest=digest)
            if response is not None:
                return response
            _consume_confirmation(connection, command_id, context, self.tool_name, digest)
            result = self.services.knowledge.approve_candidate(
                auth_token, workspace_id=context["workspace_id"], project_id=project_id,
                candidate_id=normalized["candidate_id"], level=normalized["level"],
                review_evidence=normalized["review_evidence"],
                expected_knowledge_version=normalized["expected_knowledge_version"], _connection=connection,
            )
            response = {"command_id": command_id, "status": "completed", "result": result}
            _save_confirmation_result(connection, response, context, self.tool_name, digest)
            return response

    def _validate(self, parameters: dict[str, Any]) -> dict:
        try:
            payload = validate_manual_action_input(self.tool_name, {**parameters, "confirmation_token": "manual-command-placeholder-000000"}).model_dump(mode="json")
        except (TypeError, ValueError) as exc:
            raise WorkspaceServiceError("INVALID_INPUT", "人工知识批准参数不符合合同") from exc
        payload.pop("confirmation_token", None)
        return payload

    def _prepare_parameters(self, auth_token: str, project_id: str, parameters: dict[str, Any]) -> dict:
        supplied = dict(parameters or {})
        if {"level", "expected_knowledge_version"} <= set(supplied):
            return self._validate(supplied)
        project = self.services.projects.get_project(auth_token, project_id)
        with self.services.store.connection() as connection:
            candidate = connection.execute(
                "SELECT scope FROM knowledge_candidates WHERE candidate_id=?",
                (supplied.get("candidate_id"),),
            ).fetchone()
        if candidate is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前工作区没有该知识候选")
        state = self.services.knowledge.get_state(
            auth_token, project["workspace_id"], project_id=project_id
        )
        return self._validate({
            **supplied,
            "level": candidate["scope"],
            "expected_knowledge_version": state["knowledge_version"],
        })

    def _context(self, auth_token: str, project_id: str, parameters: dict) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        with self.services.store.connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=project["workspace_id"], project_id=project_id, permission="knowledge.approve")
            candidate = connection.execute("SELECT * FROM knowledge_candidates WHERE candidate_id=?", (parameters["candidate_id"],)).fetchone()
        if candidate is None or candidate["workspace_id"] != project["workspace_id"]:
            raise WorkspaceServiceError("NOT_FOUND", "当前工作区没有该知识候选")
        if candidate["scope"] != parameters["level"]:
            raise WorkspaceServiceError("PERMISSION_DENIED", "批准范围必须与候选范围一致")
        _target_for_scope(candidate["scope"], candidate["target_id"], workspace_id=project["workspace_id"], project=project, actor_id=actor["actor_id"])
        return {"actor_id": actor["actor_id"], "project_id": project_id, "workspace_id": project["workspace_id"]}
