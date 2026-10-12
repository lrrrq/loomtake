from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import ValidationError

from .action_confirmation import ActionConfirmationService, ManualPageConfirmationService
from .agent_tools import validate_tool_input
from .manual_actions import validate_manual_action_input
from .identity_service import require_permission
from .knowledge_service import KnowledgeService
from .project_service import ProjectService
from .runtime_config import load_runtime_config
from .task_service import TaskService
from .tool_idempotency import ToolIdempotencyService, ManualPageIdempotencyService
from .workspace_store import WorkspaceServiceError, WorkspaceStore


_KNOWLEDGE_TOOLS = {"create_knowledge_candidate", "approve_knowledge"}


class KnowledgeToolRecoveryRequired(RuntimeError):
    code = "RECOVERY_RECONCILIATION_REQUIRED"

    def __init__(self, *, tool_name: str, idempotency_record_id: str):
        self.tool_name = tool_name
        self.idempotency_record_id = idempotency_record_id
        self.retryable = False
        self.recovery = {
            "automatic_retry": False,
            "reason": "知识领域写入与幂等结果分属不同事务，无法安全判断动作是否已经生效",
            "required_checks": [
                "核对 knowledge_candidates、knowledge_approvals、knowledge_rules 与 task_events",
                "由管理员人工补记幂等结果，或确认未写入后使用新幂等键",
            ],
        }
        super().__init__(f"{self.code}: {tool_name} 结果不确定，已阻断自动重放")


@dataclass(frozen=True)
class KnowledgeToolServices:
    store: WorkspaceStore
    projects: ProjectService
    knowledge: KnowledgeService
    tasks: TaskService
    confirmations: ActionConfirmationService
    idempotency: ToolIdempotencyService


class KnowledgeToolService:
    """E07 knowledge writes with authority, confirmation and recovery gates."""

    def __init__(
        self,
        services: KnowledgeToolServices,
        *,
        checkpoint: Callable[[str], None] | None = None,
    ):
        self.services = services
        self._checkpoint = checkpoint or (lambda _name: None)

    @classmethod
    def from_runtime_config(
        cls,
        *,
        confirmation_signing_key: bytes,
        config: dict | None = None,
    ) -> "KnowledgeToolService":
        runtime = config or load_runtime_config()
        store = WorkspaceStore.from_runtime_config(runtime)
        return cls(
            KnowledgeToolServices(
                store=store,
                projects=ProjectService(store),
                knowledge=KnowledgeService(store),
                tasks=TaskService(store),
                confirmations=ActionConfirmationService(
                    store, confirmation_signing_key
                ),
                idempotency=ToolIdempotencyService(store),
            )
        )

    def issue_confirmation(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        ttl_seconds: int = 300,
    ) -> dict:
        if tool_name not in _KNOWLEDGE_TOOLS:
            raise WorkspaceServiceError("INVALID_INPUT", "不是知识治理写动作")
        normalized = _validated_parameters(
            tool_name, parameters, for_confirmation=True
        )
        self._resolve_context(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=normalized,
        )
        return self._confirmations_for(tool_name).issue(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
            ttl_seconds=ttl_seconds,
        )

    def create_knowledge_candidate(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        return self._controlled_write(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="create_knowledge_candidate",
            parameters=parameters,
            confirmation_token=confirmation_token,
        )

    def approve_knowledge(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        parameters: dict[str, Any],
    ) -> dict:
        confirmation_token = str(
            (parameters or {}).get("confirmation_token") or ""
        )
        return self._controlled_write(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name="approve_knowledge",
            parameters=parameters,
            confirmation_token=confirmation_token,
        )

    def _controlled_write(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
        confirmation_token: str,
    ) -> dict:
        normalized = _validated_parameters(tool_name, parameters)
        context = self._resolve_context(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=normalized,
        )
        idempotency = self._idempotency_for(tool_name)
        claim = idempotency.claim(
            auth_token,
            agent_session_id=agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
        )
        if claim["replayed"]:
            if claim["response"] is None:
                raise KnowledgeToolRecoveryRequired(
                    tool_name=tool_name,
                    idempotency_record_id=claim["idempotency_record_id"],
                )
            return self._replayed_result(auth_token, tool_name, claim["response"])

        self._checkpoint("after_idempotency_claim")
        task = self.services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type=tool_name,
            payload_sha256=claim["request_sha256"],
            expected_project_version=context["project_version"],
        )
        task = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="executing",
            details={"scope": context["scope"], "tool_name": tool_name},
        )
        self._checkpoint("after_task_started")
        try:
            self._confirmations_for(tool_name).consume(
                auth_token,
                confirmation_token=confirmation_token,
                agent_session_id=agent_session_id,
                tool_name=tool_name,
                parameters=parameters,
            )
            self._checkpoint("after_confirmation_consumed")
            business_result = self._domain_action(
                auth_token,
                tool_name=tool_name,
                parameters=normalized,
                context=context,
            )
            self._checkpoint("after_domain_write")
        except WorkspaceServiceError as exc:
            error = _public_tool_error(exc)
            self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="failed",
                details={"error": error, "tool_name": tool_name},
                error_code=error["code"],
            )
            stored = idempotency.record_result(
                auth_token,
                idempotency_record_id=claim["idempotency_record_id"],
                result={
                    "task_id": task["task_id"],
                    "status": "failed",
                    "artifact_refs": [],
                    "warnings": [],
                    "error": error,
                },
            )
            return {
                "tool_name": tool_name,
                "task_id": task["task_id"],
                "status": "failed",
                "replayed": stored["replayed"],
                "result": None,
                "error": error,
            }

        logical_ref, warnings = _result_contract(tool_name, business_result)
        finished = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="completed",
            details={"result": business_result, "tool_name": tool_name},
            result_ref=logical_ref,
        )
        self._checkpoint("after_task_completed")
        stored = idempotency.record_result(
            auth_token,
            idempotency_record_id=claim["idempotency_record_id"],
            result={
                "task_id": finished["task_id"],
                "status": "completed",
                "artifact_refs": [logical_ref],
                "warnings": warnings,
                "error": None,
            },
        )
        return {
            "tool_name": tool_name,
            "task_id": finished["task_id"],
            "status": "completed",
            "replayed": stored["replayed"],
            "result": business_result,
            "error": None,
        }

    def _confirmations_for(self, tool_name: str):
        if tool_name != "approve_knowledge":
            return self.services.confirmations
        return ManualPageConfirmationService(
            self.services.store, self.services.confirmations.signing_key
        )

    def _idempotency_for(self, tool_name: str):
        if tool_name != "approve_knowledge":
            return self.services.idempotency
        return ManualPageIdempotencyService(self.services.store)

    def _resolve_context(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        tool_name: str,
        parameters: dict[str, Any],
    ) -> dict:
        permission = {
            "create_knowledge_candidate": "knowledge.propose",
            "approve_knowledge": "knowledge.approve",
        }[tool_name]
        context = self._session(
            auth_token,
            agent_session_id=agent_session_id,
            permission=permission,
        )
        if tool_name == "create_knowledge_candidate":
            scope = parameters["scope"]
            target_id = parameters["target_id"]
        else:
            candidate = self._candidate(parameters["candidate_id"])
            if candidate["workspace_id"] != context["workspace_id"]:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "知识候选不属于当前工作区"
                )
            scope = candidate["scope"]
            target_id = candidate["target_id"]
            if parameters["level"] != scope:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED",
                    "批准层级必须与候选范围一致；跨层级推广必须重新建立候选",
                )
        expected_target = {
            "company": context["workspace_id"],
            "category": context["category_id"],
            "project": context["project_id"],
            "personal": context["actor_id"],
        }[scope]
        if target_id != expected_target:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED",
                "知识 target_id 与 agent session 的身份、项目或类别不一致",
            )
        return {**context, "scope": scope, "target_id": target_id}

    def _session(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        permission: str,
    ) -> dict:
        session = self.services.tasks.get_agent_session(
            auth_token, agent_session_id
        )
        project = self.services.projects.get_project(
            auth_token, session["project_id"]
        )
        with self.services.store.connection() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                permission=permission,
            )
            if actor["actor_id"] != session["actor_id"]:
                raise WorkspaceServiceError(
                    "PERMISSION_DENIED", "不能使用其他操作者的 agent session"
                )
        return {
            **session,
            "workspace_id": project["workspace_id"],
            "category_id": project["category_id"],
            "project_version": project["project_version"],
        }

    def _candidate(self, candidate_id: str) -> dict:
        with self.services.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM knowledge_candidates WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise WorkspaceServiceError("NOT_FOUND", "知识候选不存在")
        return dict(row)

    def _domain_action(
        self,
        auth_token: str,
        *,
        tool_name: str,
        parameters: dict[str, Any],
        context: dict,
    ) -> dict:
        if tool_name == "create_knowledge_candidate":
            return self.services.knowledge.create_candidate(
                auth_token,
                workspace_id=context["workspace_id"],
                project_id=context["project_id"],
                scope=parameters["scope"],
                target_id=parameters["target_id"],
                statement=parameters["statement"],
                evidence_refs=parameters["evidence_refs"],
                expected_knowledge_version=parameters[
                    "expected_knowledge_version"
                ],
            )
        return self.services.knowledge.approve_candidate(
            auth_token,
            workspace_id=context["workspace_id"],
            project_id=context["project_id"],
            candidate_id=parameters["candidate_id"],
            level=parameters["level"],
            review_evidence=parameters["review_evidence"],
            expected_knowledge_version=parameters["expected_knowledge_version"],
        )

    def _replayed_result(
        self, auth_token: str, tool_name: str, stored: dict
    ) -> dict:
        if stored["status"] == "completed":
            events = self.services.tasks.list_task_events(
                auth_token, stored["task_id"]
            )
            completed = [
                event for event in events if event["status"] == "completed"
            ]
            if not completed or "result" not in completed[-1]["details"]:
                raise KnowledgeToolRecoveryRequired(
                    tool_name=tool_name,
                    idempotency_record_id="recorded-result-missing-task-evidence",
                )
            result = completed[-1]["details"]["result"]
        else:
            result = None
        return {
            "tool_name": tool_name,
            "task_id": stored["task_id"],
            "status": stored["status"],
            "replayed": True,
            "result": result,
            "error": stored.get("error"),
        }


def _validated_parameters(
    tool_name: str,
    parameters: dict[str, Any],
    *,
    for_confirmation: bool = False,
) -> dict:
    payload = dict(parameters or {})
    if for_confirmation and tool_name == "approve_knowledge":
        payload.setdefault(
            "confirmation_token", "confirmation-placeholder-00000000"
        )
    try:
        validator = validate_manual_action_input if tool_name == "approve_knowledge" else validate_tool_input
        normalized = validator(tool_name, payload).model_dump(mode="json")
    except (ValidationError, TypeError, ValueError) as exc:
        raise WorkspaceServiceError(
            "INVALID_INPUT", "动作参数不符合工具合同"
        ) from exc
    if for_confirmation:
        normalized.pop("confirmation_token", None)
    return normalized


def _result_contract(tool_name: str, result: dict) -> tuple[str, list[str]]:
    if tool_name == "create_knowledge_candidate":
        return (
            f"artifact://knowledge/candidate/{result['candidate_id']}",
            ["候选仍待管理员批准，尚未进入有效规则"],
        )
    return f"artifact://knowledge/rule/{result['rule_id']}", []


def _public_tool_error(exc: WorkspaceServiceError) -> dict:
    stable = {
        "INVALID_INPUT",
        "NOT_FOUND",
        "PERMISSION_DENIED",
        "PROJECT_VERSION_CONFLICT",
        "KNOWLEDGE_VERSION_CONFLICT",
        "IDEMPOTENCY_CONFLICT",
    }
    source_code = str(exc.code)
    if source_code == "AUTH_SESSION_INVALID":
        code = "PERMISSION_DENIED"
    else:
        code = source_code if source_code in stable else "INTERNAL_ERROR"
    return {
        "code": code,
        "message": str(exc.message),
        "retryable": code in {
            "PROJECT_VERSION_CONFLICT",
            "KNOWLEDGE_VERSION_CONFLICT",
        },
        "details": {"source_code": source_code},
    }
