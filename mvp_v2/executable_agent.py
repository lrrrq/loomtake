from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Optional, Protocol
import uuid

from .action_confirmation import ActionConfirmationService
from .agent_tools import ToolExecutionResult
from .artifact_service import ArtifactResolutionError
from .brief_artifact_store import BriefArtifactStore
from .project_service import ProjectService
from .readonly_agent import ReadonlyAgentService
from .task_service import TaskService
from .version_delivery import (
    VersionDeliveryArtifact,
    VersionDeliveryArtifacts,
    VersionDeliveryContext,
    VersionDeliveryService,
)
from .tool_idempotency import ToolIdempotencyService
from .workspace_store import WorkspaceServiceError


_STATE_TERMS = re.compile(r"(现在.*(到哪|状态)|到哪|当前状态|项目状态|进度)")
_BLOCKER_TERMS = re.compile(r"(为什么|阻断|卡住|停住|失败|不能|原因|怎么办)")
_RUN_DETAIL_TERMS = re.compile(r"(节点|run|运行详情|产物|进度详情)", re.IGNORECASE)
_REVIEW_READINESS_TERMS = re.compile(r"(是否|能否|可以|准备好).*(人工)?(审核|复核)|(审核|复核).*(就绪|资格)")
_PREPARE_REVIEW_TERMS = re.compile(r"(准备|导出|生成).*(人工)?(审核|复核)(包|表)?")
_START_TERMS = re.compile(r"(开始|启动|执行).*(生成|脚本)|生成(一|这|个|脚本)")
_RETRY_TERMS = re.compile(r"(重试|重跑|重新执行)")
_FORK_TERMS = re.compile(r"(修改|修订|调整|反馈|改为|替换)")
_CANCEL_TERMS = re.compile(r"(取消|停止|终止).*(运行|生成|任务)?")
_STAGE_ID = re.compile(r"\b([a-z][a-z0-9_]{2,63})\b")
_SHOT_NO = re.compile(r"第?\s*(\d+(?:\s*[-~至]\s*\d+)?)\s*镜")


class ExecutionBackend(Protocol):
    def inspect_action(self, action: str, parameters: dict, brief_snapshot: dict | None) -> dict: ...

    def start_action(self, action: str, parameters: dict, brief_snapshot: dict | None) -> dict: ...

    def get_job(self, job_id: str) -> dict: ...

    def cancel_job(self, job_id: str) -> dict: ...

    def review_readiness(self, run_id: str) -> dict: ...

    def prepare_human_review(self, run_id: str) -> dict: ...

    def finalized_artifacts(self, snapshot: dict) -> dict | None: ...

    def write_revision_report(
        self,
        *,
        parent_context: dict,
        child_context: VersionDeliveryContext,
        modification_plan: dict,
        machine_review: dict,
        human_review: dict,
    ) -> dict: ...


@dataclass(frozen=True)
class ExecutableAgentServices:
    readonly: ReadonlyAgentService
    projects: ProjectService
    tasks: TaskService
    confirmations: ActionConfirmationService
    idempotency: ToolIdempotencyService
    brief_store: BriefArtifactStore
    backend: ExecutionBackend
    delivery: Optional[VersionDeliveryService] = None

    @property
    def store(self):
        return self.readonly.services.store

    @property
    def identity(self):
        return self.readonly.services.identity

    @property
    def versions(self):
        return self.readonly.services.versions

    @property
    def artifacts(self):
        return self.readonly.services.artifacts


class ExecutableAgentService:
    """E03 controlled actions over the E02 read-only authority boundary."""

    def __init__(self, services: ExecutableAgentServices):
        self.services = services

    def setup_status(self) -> dict:
        return {**self.services.readonly.setup_status(), "availability": "E03"}

    def enable_local_owner(self, **kwargs) -> dict:
        return self.services.readonly.enable_local_owner(**kwargs)

    def reissue_local_owner_session(self, **kwargs) -> dict:
        return self.services.readonly.reissue_local_owner_session(**kwargs)

    def list_projects(self, auth_token: str, workspace_id: str) -> list[dict]:
        return self.services.readonly.list_projects(auth_token, workspace_id)

    def create_session(self, auth_token: str, project_id: str) -> dict:
        return self.services.readonly.create_session(auth_token, project_id)

    def get_project_state(self, auth_token: str, project_id: str) -> dict:
        return self.services.readonly.get_project_state(auth_token, project_id)

    def get_session(self, auth_token: str, agent_session_id: str) -> dict:
        session = self.services.readonly.get_session(auth_token, agent_session_id)
        if not any(task['status'] == 'executing' for task in session['tasks']):
            return session
        for task in session["tasks"]:
            if task["status"] == "executing":
                self.sync_task(auth_token, task["task_id"])
        return self.services.readonly.get_session(auth_token, agent_session_id)

    def list_task_events(self, auth_token: str, task_id: str, *, after_sequence: int = 0) -> list[dict]:
        self.sync_task(auth_token, task_id)
        return _public_task_events(
            self.services.tasks.list_task_events(
                auth_token, task_id, after_sequence=after_sequence
            )
        )

    def onboard_project(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        expected_workspace_version: int,
        brief: dict,
        category_id: str,
        confirmed: bool,
    ) -> dict:
        # First-project bootstrap is deliberately outside the project-bound
        # agent tool path. Ordinary E03 writes still require an agent_session.
        if confirmed is not True:
            raise WorkspaceServiceError(
                "PERMISSION_DENIED", "建立受管项目与冻结 Brief 前必须显式确认"
            )
        projects = self.services.projects.list_projects(auth_token, workspace_id)
        frozen = self.services.brief_store.freeze(brief)
        name = str(brief.get("项目名称") or "").strip()
        if not name:
            raise WorkspaceServiceError("INVALID_INPUT", "Brief 缺少项目名称")
        for project in projects:
            if project["name"] != name:
                continue
            existing = self.services.projects.get_current_brief_snapshot(
                auth_token, project["project_id"]
            )
            if existing and existing["brief_sha256"] == frozen["brief_sha256"]:
                return {
                    "project": project,
                    "brief": frozen,
                    "replayed": True,
                    "boundary": "bootstrap_only",
                    "ordinary_agent_tool": False,
                }
        project = self.services.projects.create_project(
            auth_token,
            workspace_id=workspace_id,
            name=name,
            category_id=category_id,
            expected_workspace_version=expected_workspace_version,
            brief_ref=frozen["brief_ref"],
            brief_sha256=frozen["brief_sha256"],
        )
        return {
            "project": project,
            "brief": frozen,
            "replayed": False,
            "boundary": "bootstrap_only",
            "ordinary_agent_tool": False,
        }

    def submit_message(
        self,
        auth_token: str,
        agent_session_id: str,
        *,
        content: str,
        run_id: str | None = None,
    ) -> dict:
        message = str(content or "").strip()
        if not message:
            raise WorkspaceServiceError("INVALID_INPUT", "message 不能为空")
        read_intent = self._read_intent(message)
        if read_intent:
            return self.services.readonly.submit_message(
                auth_token, agent_session_id, content=message, run_id=run_id
            )
        session = self.services.tasks.get_agent_session(auth_token, agent_session_id)
        project = self.services.projects.get_project(auth_token, session["project_id"])
        action, clarification = self._action(message)
        task = self.services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type=action or "needs_clarification",
            payload_sha256=_sha256_text(message),
            expected_project_version=project["project_version"],
        )
        understanding = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="understanding",
            details={"intent": action or "needs_clarification"},
        )
        if clarification:
            response = self._clarification_response(clarification)
            finished = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=understanding["task_version"],
                status="needs_clarification",
                details={"response": response},
            )
            return self._message_result(auth_token, finished, response)

        target_run = self._target_run(auth_token, project["project_id"], run_id)
        try:
            parameters, clarification, delivery_binding = self._parameters(
                action,
                message,
                project,
                target_run,
                agent_session_id,
                auth_token,
            )
        except WorkspaceServiceError as exc:
            return self._block_task(auth_token, understanding, exc)
        if clarification:
            response = self._clarification_response(clarification)
            finished = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=understanding["task_version"],
                status="needs_clarification",
                details={"response": response},
            )
            return self._message_result(auth_token, finished, response)

        evidence_run_id = str(
            parameters.get("run_id") or parameters.get("parent_run_id") or ""
        )
        if evidence_run_id:
            try:
                self.services.readonly.get_run_details(auth_token, evidence_run_id)
            except (ArtifactResolutionError, WorkspaceServiceError) as exc:
                error = WorkspaceServiceError(
                    str(getattr(exc, "code", "ARTIFACT_INVALID")),
                    str(getattr(exc, "message", "run 证据无法通过完整性校验")),
                )
                return self._block_task(auth_token, understanding, error)

        if action == "get_review_readiness":
            executing = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=understanding["task_version"],
                status="executing",
                details={"intent": action},
            )
            try:
                response = self.services.backend.review_readiness(parameters["run_id"])
            except WorkspaceServiceError as exc:
                return self._block_task(auth_token, executing, exc)
            finished = self.services.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=executing["task_version"],
                status="completed",
                details={"response": response},
            )
            return self._message_result(auth_token, finished, response)

        brief_snapshot = self.services.projects.get_current_brief_snapshot(
            auth_token, project["project_id"]
        )
        if action == "cancel_task":
            inspect = {"passed": True, "calls_models": False}
        else:
            try:
                inspect = self.services.backend.inspect_action(
                    action,
                    self._execution_parameters(parameters, delivery_binding),
                    brief_snapshot,
                )
            except WorkspaceServiceError as exc:
                return self._block_task(auth_token, understanding, exc)
            if not inspect.get("passed"):
                error = WorkspaceServiceError(
                    str(inspect.get("error_code") or "INVALID_INPUT"),
                    str(inspect.get("message") or "真实预检未通过"),
                )
                return self._block_task(auth_token, understanding, error)
        proposal = self._proposal(action, parameters, inspect, delivery_binding)
        proposed = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=understanding["task_version"],
            status="proposed",
            details={
                "response": proposal,
                "internal_delivery_binding": (
                    _delivery_binding(delivery_binding)
                    if delivery_binding is not None else None
                ),
            },
        )
        return self._message_result(auth_token, proposed, proposal)

    def confirm_action(
        self,
        auth_token: str,
        task_id: str,
        *,
        proposal_digest: str,
    ) -> dict:
        task = self.services.tasks.get_task(auth_token, task_id)
        events = self.services.tasks.list_task_events(auth_token, task_id)
        proposal = _proposal_from_events(events)
        if proposal is None:
            raise WorkspaceServiceError("NOT_FOUND", "task 没有可确认的行动提案")
        if proposal.get("proposal_digest") != proposal_digest:
            raise WorkspaceServiceError("INVALID_INPUT", "行动提案已变化，需要重新确认")
        if task["status"] not in {"proposed", "waiting_confirmation", "executing"}:
            if task["status"] in {
                "waiting_human_review", "completed", "partially_completed", "blocked", "failed", "cancelled"
            }:
                return self._execution_response(auth_token, task_id)
            raise WorkspaceServiceError("INVALID_TASK_TRANSITION", "当前 task 不能确认")
        if task["status"] == "executing":
            return self.sync_task(auth_token, task_id)
        action = proposal["action"]
        parameters = dict(proposal["parameters"])
        delivery_binding = _delivery_binding_from_events(events)
        current_project = self.services.projects.get_project(
            auth_token, task["project_id"]
        )
        if current_project["project_version"] != parameters.get("expected_project_version"):
            return self._block_task(
                auth_token,
                task,
                WorkspaceServiceError(
                    "PROJECT_VERSION_CONFLICT",
                    "项目版本在提案后已变化；旧提案未执行，请重新确认影响范围。",
                ),
            )
        if action == "fork_revision":
            try:
                current_context = self._resolve_current_delivery(
                    auth_token, current_project
                )
            except WorkspaceServiceError as exc:
                return self._block_task(auth_token, task, exc)
            if _delivery_binding(current_context) != delivery_binding:
                return self._block_task(
                    auth_token,
                    task,
                    WorkspaceServiceError(
                        "PROJECT_VERSION_CONFLICT",
                        "父版本 delivery context 在确认前已变化；请重新确认修改范围。",
                    ),
                )
        execution_parameters = self._execution_parameters(
            parameters,
            current_context if action == "fork_revision" else None,
        )
        binding_sha256 = _sha256_json(delivery_binding) if delivery_binding else ""
        evidence_run_id = str(
            parameters.get("run_id") or parameters.get("parent_run_id") or ""
        )
        if evidence_run_id:
            try:
                self.services.readonly.get_run_details(auth_token, evidence_run_id)
            except (ArtifactResolutionError, WorkspaceServiceError) as exc:
                return self._block_task(
                    auth_token,
                    task,
                    WorkspaceServiceError(
                        str(getattr(exc, "code", "ARTIFACT_INVALID")),
                        str(getattr(exc, "message", "run 证据无法通过完整性校验")),
                    ),
                )
        claim = self.services.idempotency.claim(
            auth_token,
            agent_session_id=task["agent_session_id"],
            tool_name=action,
            parameters=parameters,
            internal_binding_sha256=binding_sha256,
        )
        if claim["response"] is not None:
            return self._execution_response(auth_token, claim["response"]["task_id"])
        confirmation = self.services.confirmations.issue(
            auth_token,
            agent_session_id=task["agent_session_id"],
            tool_name=action,
            parameters=parameters,
            internal_binding_sha256=binding_sha256,
        )
        self.services.confirmations.consume(
            auth_token,
            confirmation_token=confirmation["confirmation_token"],
            agent_session_id=task["agent_session_id"],
            tool_name=action,
            parameters=parameters,
            internal_binding_sha256=binding_sha256,
        )
        if task["status"] == "proposed":
            task = self.services.tasks.transition_task(
                auth_token,
                task_id=task_id,
                expected_task_version=task["task_version"],
                status="waiting_confirmation",
                details={
                    "confirmation_id": confirmation["confirmation_id"],
                    "idempotency_record_id": claim["idempotency_record_id"],
                },
            )
        task = self.services.tasks.transition_task(
            auth_token,
            task_id=task_id,
            expected_task_version=task["task_version"],
            status="executing",
            details={
                "confirmation_id": confirmation["confirmation_id"],
                "idempotency_record_id": claim["idempotency_record_id"],
            },
        )
        brief_snapshot = self.services.projects.get_current_brief_snapshot(
            auth_token, task["project_id"]
        )
        try:
            if action == "cancel_task":
                response = self._execute_cancel(auth_token, parameters)
                task = self.services.tasks.transition_task(
                    auth_token,
                    task_id=task_id,
                    expected_task_version=task["task_version"],
                    status="completed",
                    details={"response": response},
                )
                result = ToolExecutionResult(task_id=task_id, status="completed")
            elif action == "prepare_human_review":
                readiness = self.services.backend.review_readiness(parameters["run_id"])
                if not readiness.get("ready"):
                    raise WorkspaceServiceError(
                        "REVIEW_REQUIRED",
                        str(readiness.get("summary") or "当前 run 不具备人工复核包准备条件"),
                    )
                prepared = self.services.backend.prepare_human_review(parameters["run_id"])
                response = {
                    "type": "review_package_prepared",
                    "title": "人工复核包已准备",
                    "summary": "复核包已从真实 run 导出；未发布，也未更新 CURRENT。",
                    "artifact_refs": [prepared["artifact_ref"]],
                    "download_artifact": prepared.get("download_artifact"),
                    "next_steps": ["由审核人填写复核表并提交独立人工结论。"],
                }
                task = self.services.tasks.transition_task(
                    auth_token,
                    task_id=task_id,
                    expected_task_version=task["task_version"],
                    status="waiting_human_review",
                    details={"response": response},
                    result_ref=prepared["artifact_ref"],
                )
                result = ToolExecutionResult(
                    task_id=task_id,
                    status="completed",
                    artifact_refs=[prepared["artifact_ref"]],
                )
            else:
                snapshot = self.services.backend.start_action(
                    action, execution_parameters, brief_snapshot
                )
                progress = _progress(snapshot)
                self.services.tasks.record_task_progress(
                    auth_token,
                    task_id=task_id,
                    expected_task_version=task["task_version"],
                    details={"runtime_progress": progress, "signature": _progress_signature(progress)},
                )
                result = ToolExecutionResult(task_id=task_id, status="accepted")
            self.services.idempotency.record_result(
                auth_token,
                idempotency_record_id=claim["idempotency_record_id"],
                result=result,
            )
        except WorkspaceServiceError as exc:
            blocked = self._block_task(auth_token, task, exc)
            self.services.idempotency.record_result(
                auth_token,
                idempotency_record_id=claim["idempotency_record_id"],
                result=ToolExecutionResult(
                    task_id=task_id,
                    status="blocked",
                    error={"code": _stable_error(exc.code), "message": exc.message},
                ),
            )
            return blocked
        return self.sync_task(auth_token, task_id)

    def dismiss_proposal(self, auth_token: str, task_id: str) -> dict:
        task = self.services.tasks.get_task(auth_token, task_id)
        if task["status"] not in {"proposed", "waiting_confirmation", "needs_clarification"}:
            raise WorkspaceServiceError(
                "INVALID_TASK_TRANSITION", "只有未执行的提案或待澄清任务可以取消"
            )
        response = {
            "type": "proposal_cancelled",
            "title": "提案已取消",
            "summary": "未启动 JobManager，也未调用模型或修改项目产物。",
            "next_steps": ["需要时可重新提交更明确的行动要求。"],
        }
        task = self.services.tasks.transition_task(
            auth_token,
            task_id=task_id,
            expected_task_version=task["task_version"],
            status="cancelled",
            details={"response": response},
            error_code="CANCELLED",
        )
        return self._message_result(auth_token, task, response)

    def sync_task(self, auth_token: str, task_id: str) -> dict:
        task = self.services.tasks.get_task(auth_token, task_id)
        if task["status"] != "executing":
            return self._execution_response(auth_token, task_id)
        events = self.services.tasks.list_task_events(auth_token, task_id)
        job_id = _job_id_from_events(events)
        if not job_id:
            return self._recovery_block(auth_token, task)
        try:
            snapshot = self.services.backend.get_job(job_id)
        except (KeyError, WorkspaceServiceError):
            return self._recovery_block(auth_token, task)
        progress = _progress(snapshot)
        signature = _progress_signature(progress)
        if signature != _last_progress_signature(events):
            self.services.tasks.record_task_progress(
                auth_token,
                task_id=task_id,
                expected_task_version=task["task_version"],
                details={"runtime_progress": progress, "signature": signature},
            )
        state = str(snapshot.get("state") or "unknown")
        if state in {"queued", "running", "cancelling"}:
            return self._execution_response(auth_token, task_id, job=progress)
        try:
            finalization = self._finalize_project_run(auth_token, task, snapshot)
        except WorkspaceServiceError as exc:
            return self._block_task(auth_token, task, exc)
        response = _terminal_response(progress)
        if finalization.get("revision_report"):
            revision = finalization["revision_report"]
            response["revision_report"] = revision
            response.setdefault("artifact_refs", []).append(revision["artifact_ref"])
        target_status, error_code = _task_status(snapshot)
        refreshed = self.services.tasks.get_task(auth_token, task_id)
        if refreshed["status"] == "executing":
            refreshed = self.services.tasks.transition_task(
                auth_token,
                task_id=task_id,
                expected_task_version=refreshed["task_version"],
                status=target_status,
                details={"runtime_progress": progress, "response": response},
                error_code=error_code,
                result_ref=(
                    finalization.get("revision_report", {}).get("artifact_ref")
                    if finalization else None
                ),
            )
        return self._execution_response(auth_token, task_id, job=progress)

    def _execute_cancel(self, auth_token: str, parameters: dict) -> dict:
        target_id = parameters["task_id"]
        target = self.services.tasks.get_task(auth_token, target_id)
        events = self.services.tasks.list_task_events(auth_token, target_id)
        job_id = _job_id_from_events(events)
        if target["status"] != "executing" or not job_id:
            raise WorkspaceServiceError("INVALID_INPUT", "仅可取消当前仍存活的运行任务")
        try:
            current = self.services.backend.get_job(job_id)
        except (KeyError, WorkspaceServiceError) as exc:
            raise WorkspaceServiceError(
                "RECOVERY_RECONCILIATION_REQUIRED", "运行进程句柄已丢失，不能声称取消成功"
            ) from exc
        if str(current.get("state") or "") not in {"queued", "running", "cancelling"}:
            raise WorkspaceServiceError("INVALID_INPUT", "目标运行已经结束，不能再次取消")
        snapshot = self.services.backend.cancel_job(job_id)
        self.sync_task(auth_token, target_id)
        return {
            "type": "cancellation_requested",
            "title": "已提交停止请求",
            "summary": str(snapshot.get("message") or "正在停止当前运行"),
            "target_task_id": target_id,
            "next_steps": ["刷新任务状态，确认真实进程已经结束。"],
        }

    def _finalize_project_run(self, auth_token: str, task: dict, snapshot: dict) -> dict:
        evidence = self.services.backend.finalized_artifacts(snapshot)
        if not evidence:
            return {}
        project_id = task["project_id"]
        run_id = evidence["run_id"]
        try:
            binding = self.services.projects.get_run_binding(auth_token, run_id)
            if binding["project_id"] != project_id:
                raise WorkspaceServiceError("PERMISSION_DENIED", "run 已绑定到其他项目")
        except WorkspaceServiceError as exc:
            if exc.code != "NOT_FOUND":
                raise
            project = self.services.projects.get_project(auth_token, project_id)
            binding = self.services.projects.bind_run(
                auth_token,
                project_id=project_id,
                run_id=run_id,
                source_kind="native",
                manifest_ref=evidence["manifest_ref"],
                manifest_sha256=evidence["manifest_sha256"],
                expected_project_version=project["project_version"],
                parent_run_id=evidence.get("parent_run_id") or None,
            )
        if not evidence.get("content_sha256") or not evidence.get("artifact_ref"):
            return {}
        versions = self.services.projects.list_project_versions(auth_token, project_id)
        if any(item["run_id"] == run_id for item in versions):
            return {}
        events = self.services.tasks.list_task_events(auth_token, task["task_id"])
        proposal = _proposal_from_events(events) or {}
        parameters = dict(proposal.get("parameters") or {})
        parent_version = next(
            (
                item["version_id"]
                for item in reversed(versions)
                if item["run_id"] == evidence.get("parent_run_id")
            ),
            None,
        )
        if task["task_type"] == "fork_revision":
            parent_context = self._context_from_binding(
                project_id, _delivery_binding_from_events(events)
            )
            if parent_context.run_id != evidence.get("parent_run_id"):
                raise WorkspaceServiceError(
                    "INTEGRITY_ERROR", "child run 父链与已确认 VersionDeliveryContext 不一致"
                )
            parent_version = parent_context.version_id
        project = self.services.projects.get_project(auth_token, project_id)
        created = self.services.projects.create_project_version(
            auth_token,
            project_id=project_id,
            run_id=run_id,
            content_sha256=evidence["content_sha256"],
            artifact_ref=evidence["artifact_ref"],
            expected_project_version=project["project_version"],
            parent_version_id=parent_version,
        )
        if task["task_type"] != "fork_revision":
            return {}
        if self.services.delivery is None:
            raise WorkspaceServiceError(
                "VERSION_DELIVERY_UNAVAILABLE", "修订完成后无法读取 child VersionDeliveryContext"
            )
        child_context = self.services.delivery.resolve(
            auth_token,
            project_id,
            created["version_id"],
            expected_project_version=created["project_version"],
        )
        try:
            report = self.services.backend.write_revision_report(
                parent_context=parent_context.as_dict(),
                child_context=child_context,
                modification_plan=dict(parameters["modification_plan"]),
                machine_review=dict((snapshot.get("report") or {})),
                human_review={
                    "status": str((snapshot.get("report") or {}).get("human_review_status") or "pending")
                },
            )
        except AttributeError as exc:
            raise WorkspaceServiceError(
                "VERSION_DELIVERY_UNAVAILABLE", "执行后端不支持受控 revision report"
            ) from exc
        return {"revision_report": report}

    def _recovery_block(self, auth_token: str, task: dict) -> dict:
        error = WorkspaceServiceError(
            "RECOVERY_RECONCILIATION_REQUIRED",
            "程序已重启或进程句柄不可恢复；任务记录仍在，但不能声称生成仍在运行。",
        )
        return self._block_task(auth_token, task, error)

    def _block_task(self, auth_token: str, task: dict, exc: WorkspaceServiceError) -> dict:
        response = {
            "type": "action_blocked",
            "title": "动作被真实条件阻断",
            "summary": exc.message,
            "error_code": exc.code,
            "next_steps": ["修正阻断条件后重新提交动作；系统不会补造输入。"],
        }
        finished = self.services.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="blocked",
            details={"response": response},
            error_code=exc.code,
        )
        return self._message_result(auth_token, finished, response)

    def _execution_response(self, auth_token: str, task_id: str, *, job: dict | None = None) -> dict:
        task = self.services.tasks.get_task(auth_token, task_id)
        events = self.services.tasks.list_task_events(auth_token, task_id)
        if job is None and task["status"] == "executing":
            job_id = _job_id_from_events(events)
            if job_id:
                try:
                    job = _progress(self.services.backend.get_job(job_id))
                except (KeyError, WorkspaceServiceError):
                    job = None
        response = _latest_response(events)
        project = self.services.projects.get_project(auth_token, task["project_id"])
        return {
            "task": task,
            "response": response,
            "job": job,
            "events": _public_task_events(events),
            "current_project_version": project["project_version"],
        }

    def _message_result(self, auth_token: str, task: dict, response: dict) -> dict:
        return {
            "task": task,
            "response": response,
            "events": _public_task_events(
                self.services.tasks.list_task_events(auth_token, task["task_id"])
            ),
        }

    def _target_run(self, auth_token: str, project_id: str, supplied: str | None) -> str:
        if supplied:
            binding = self.services.projects.get_run_binding(auth_token, supplied)
            if binding["project_id"] != project_id:
                raise WorkspaceServiceError("PERMISSION_DENIED", "run 不属于当前项目")
            return supplied
        runs = self.services.projects.list_project_runs(auth_token, project_id)
        return str(runs[-1]["run_id"]) if runs else ""

    def _parameters(
        self,
        action: str,
        message: str,
        project: dict,
        run_id: str,
        agent_session_id: str,
        auth_token: str,
    ) -> tuple[dict, str | None, dict | None]:
        common = {
            "idempotency_key": f"e03_{uuid.uuid4().hex}",
            "project_id": project["project_id"],
            "expected_project_version": project["project_version"],
        }
        if action == "start_generation":
            return {**common, "mode": "standard", "asset_package_version": ""}, None, None
        if action == "retry_stage":
            stage = _extract_stage_id(message)
            if not run_id:
                return {}, "请明确要重试的 run；当前项目没有已绑定 run。", None
            if not stage:
                return {}, "请明确要重试的英文节点 ID，例如 machine_audit。", None
            return {**common, "run_id": run_id, "stage_id": stage}, None, None
        if action == "fork_revision":
            if self.services.delivery is None:
                raise WorkspaceServiceError(
                    "VERSION_DELIVERY_UNAVAILABLE", "当前运行未接入 VersionDeliveryService，不能建立修订链"
                )
            context = self._resolve_current_delivery(auth_token, project)
            if run_id and run_id != context.run_id:
                return {}, "只能从当前 immutable version 修订；指定的 run 已不是当前版本。", None
            plan = _modification_plan(message)
            if plan is None:
                return {}, "请明确修改类型（图片、文案、品牌、事实、结构、风格、细节或技术）和具体要求。", None
            if not plan["shot_no"] or not plan["target_field"]:
                return {}, "修订必须明确镜号与权威字段，避免误改整片或隐藏状态。", None
            return {
                **common,
                "idempotency_key": _delivery_idempotency_key(context),
                "parent_run_id": context.run_id,
                "modification_plan": plan,
            }, None, context
        if action == "get_review_readiness":
            if not run_id:
                return {}, "当前项目没有可检查的 run，请先生成。", None
            return {"project_id": project["project_id"], "run_id": run_id}, None, None
        if action == "prepare_human_review":
            if not run_id:
                return {}, "当前项目没有可准备复核包的 run。", None
            return {**common, "run_id": run_id}, None, None
        if action == "cancel_task":
            target = self._live_task(auth_token, agent_session_id)
            if target is None:
                return {}, "当前会话没有可取消的存活运行任务。", None
            return {
                **common,
                "task_id": target["task_id"],
            }, None, None
        return {}, "请明确要执行的动作。", None

    def _resolve_current_delivery(
        self, auth_token: str, project: dict
    ) -> VersionDeliveryContext:
        if self.services.delivery is None:
            raise WorkspaceServiceError(
                "VERSION_DELIVERY_UNAVAILABLE", "VersionDeliveryService 不可用"
            )
        version_id = str(project.get("current_version_id") or "").strip()
        if not version_id:
            raise WorkspaceServiceError(
                "VERSION_NOT_CURRENT", "项目没有 current immutable version，不能创建修订"
            )
        return self.services.delivery.resolve(
            auth_token,
            project["project_id"],
            version_id,
            expected_project_version=project["project_version"],
        )

    def _context_from_binding(
        self, project_id: str, binding: dict | None
    ) -> VersionDeliveryContext:
        if not isinstance(binding, dict):
            raise WorkspaceServiceError(
                "INTEGRITY_ERROR", "修订任务缺少经确认的内部 delivery binding"
            )
        if binding.get("project_id") != project_id:
            raise WorkspaceServiceError(
                "INTEGRITY_ERROR", "delivery binding project 不一致"
            )
        return VersionDeliveryContext(
            schema_version="mplus-version-delivery-context/v1",
            workspace_id=str(binding["workspace_id"]),
            project_id=project_id,
            project_lock_version=int(binding["project_lock_version"]),
            version_id=str(binding["version_id"]),
            run_id=str(binding["run_id"]),
            parent_version_id=binding.get("parent_version_id"),
            version_artifact=_binding_artifact(
                binding, "version_artifact_ref", "version_artifact_sha256"
            ),
            manifest=_binding_artifact(binding, "manifest_ref", "manifest_sha256"),
            artifacts=VersionDeliveryArtifacts(
                script_plan=_binding_artifact(
                    binding, "script_plan_ref", "script_plan_sha256"
                ),
                script_draft=_binding_artifact(
                    binding, "script_draft_ref", "script_draft_sha256"
                ),
                visual_binding=_binding_artifact(
                    binding, "visual_binding_ref", "visual_binding_sha256"
                ),
            ),
        )

    @staticmethod
    def _execution_parameters(
        parameters: dict, context: Optional[VersionDeliveryContext]
    ) -> dict:
        if context is None:
            return parameters
        return {**parameters, "delivery_context": context.as_dict()}

    def _live_task(self, auth_token: str, agent_session_id: str) -> dict | None:
        tasks = self.services.tasks.list_agent_session_tasks(
            auth_token, agent_session_id
        )
        for task in reversed(tasks):
            if task["status"] != "executing":
                continue
            events = self.services.tasks.list_task_events(
                auth_token, task["task_id"]
            )
            job_id = _job_id_from_events(events)
            if not job_id:
                continue
            try:
                snapshot = self.services.backend.get_job(job_id)
            except (KeyError, WorkspaceServiceError):
                continue
            if str(snapshot.get("state") or "") in {"queued", "running", "cancelling"}:
                return {"task_id": task["task_id"], "job_id": job_id}
        return None

    @staticmethod
    def _read_intent(message: str) -> str | None:
        if _BLOCKER_TERMS.search(message):
            return "explain_blocker"
        if _STATE_TERMS.search(message):
            return "get_project_state"
        if _RUN_DETAIL_TERMS.search(message) and not _RETRY_TERMS.search(message):
            return "get_run_details"
        return None

    @staticmethod
    def _action(message: str) -> tuple[str | None, str | None]:
        if _REVIEW_READINESS_TERMS.search(message):
            return "get_review_readiness", None
        if _PREPARE_REVIEW_TERMS.search(message):
            return "prepare_human_review", None
        if _CANCEL_TERMS.fullmatch(message) or _CANCEL_TERMS.search(message):
            return "cancel_task", None
        if _RETRY_TERMS.search(message):
            return "retry_stage", None
        if _FORK_TERMS.search(message):
            return "fork_revision", None
        if _START_TERMS.search(message):
            return "start_generation", None
        return None, "我无法确定你是要查询状态、开始生成、重试节点、提交修改、准备人工复核，还是取消当前运行。"

    @staticmethod
    def _proposal(
        action: str,
        parameters: dict,
        inspect: dict,
        delivery_context: Optional[VersionDeliveryContext] = None,
    ) -> dict:
        calls_models = bool(inspect.get("calls_models"))
        copy = {
            "start_generation": (
                "生成新的脚本 run 和候选版本",
                "不会覆盖旧 run、发布版本或更新 CURRENT",
                "整个项目的新 run",
            ),
            "retry_stage": (
                f"从 {parameters.get('stage_id')} 节点建立新的重试 run",
                "不会原地修改父 run，也不会发布",
                f"父 run {parameters.get('run_id')} 之后的节点",
            ),
            "fork_revision": (
                "按已解析的反馈建立子版本 run",
                "不会改动父 run、公司知识或资产包",
                "反馈指定字段及其下游节点",
            ),
            "prepare_human_review": (
                "从已通过机器审核的 run 导出人工复核包",
                "不会写入审核结论、发布或更新 CURRENT",
                f"run {parameters.get('run_id')} 的复核副本",
            ),
            "cancel_task": (
                "向当前存活运行发送停止请求",
                "不会删除已产生的运行证据或其他任务",
                f"task {parameters.get('task_id')}",
            ),
        }[action]
        delivery_binding = (
            _delivery_binding(delivery_context)
            if delivery_context is not None else None
        )
        digest = _sha256_json({
            "action": action,
            "parameters": parameters,
            "delivery_binding": delivery_binding,
        })
        return {
            "type": "action_proposal",
            "title": "执行前确认",
            "summary": copy[0],
            "action": action,
            "parameters": parameters,
            "proposal_digest": digest,
            "risk": "R2",
            "will_change": copy[0],
            "will_not_change": copy[1],
            "calls_models": calls_models,
            "model_call_statement": "会调用已配置模型" if calls_models else "不会新增模型调用",
            "impact_scope": copy[2],
            "preflight": inspect,
            "recoverability": "确认与幂等记录先持久化；若启动进程后、写回 job_id 前程序崩溃，恢复时会标记 RECOVERY_RECONCILIATION_REQUIRED，不会重复启动或谎报仍在运行。",
            "next_steps": ["确认后才会执行；也可以取消这条提案。"],
        }

    @staticmethod
    def _clarification_response(message: str) -> dict:
        return {
            "type": "needs_clarification",
            "title": "需要补充信息",
            "summary": message,
            "next_steps": [message],
        }


def _modification_plan(message: str) -> dict | None:
    categories = (
        ("image", r"图片|配图|空图|视觉"),
        ("copy", r"文案|字幕|旁白|文字|表达"),
        ("brand", r"品牌|logo|标识"),
        ("fact", r"事实|参数|数据|姓名|身份"),
        ("structure", r"结构|顺序|镜数|叙事"),
        ("style", r"风格|节奏|调性"),
        ("detail", r"细节|画面|镜头"),
        ("technical", r"技术|程序|报错"),
    )
    category = next((value for value, pattern in categories if re.search(pattern, message, re.IGNORECASE)), None)
    if not category:
        return None
    shot = _SHOT_NO.search(message)
    target_field = _target_field(message, category)
    requested = _requested_value(message)
    return {
        "category": category,
        "scope": "shot" if shot else "whole_film",
        "shot_no": shot.group(1).replace(" ", "") if shot else "",
        "comment": message,
        "desired_change": message,
        "keep_fields": [],
        "target_field": target_field,
        "requested_value": requested,
    }


def _target_field(message: str, category: str) -> str:
    if category != "copy":
        return "画面描述" if category in {"style", "detail"} else ""
    if re.search(r"旁白", message, re.IGNORECASE):
        return "声音"
    return "画面描述"


def _requested_value(message: str) -> str:
    match = re.search(r"(?:改为|修改为|调整为|替换为)\s*[：:]?\s*([^，。；;]+)", message)
    return match.group(1).strip() if match else ""


def _extract_stage_id(message: str) -> str:
    ignored = {"run", "stage"}
    return next((value for value in _STAGE_ID.findall(message) if value not in ignored), "")


def _proposal_from_events(events: list[dict]) -> dict | None:
    for event in reversed(events):
        response = (event.get("details") or {}).get("response")
        if isinstance(response, dict) and response.get("type") == "action_proposal":
            return response
    return None


def _delivery_binding(context: VersionDeliveryContext) -> dict:
    return {
        "workspace_id": context.workspace_id,
        "project_id": context.project_id,
        "version_id": context.version_id,
        "run_id": context.run_id,
        "parent_version_id": context.parent_version_id,
        "project_lock_version": context.project_lock_version,
        "version_artifact_ref": context.version_artifact.ref,
        "version_artifact_sha256": context.version_artifact.sha256,
        "manifest_ref": context.manifest.ref,
        "manifest_sha256": context.manifest.sha256,
        "script_plan_ref": context.artifacts.script_plan.ref,
        "script_plan_sha256": context.artifacts.script_plan.sha256,
        "script_draft_ref": context.artifacts.script_draft.ref,
        "script_draft_sha256": context.artifacts.script_draft.sha256,
        "visual_binding_ref": context.artifacts.visual_binding.ref,
        "visual_binding_sha256": context.artifacts.visual_binding.sha256,
    }


def _binding_artifact(binding: dict, ref_key: str, sha_key: str) -> VersionDeliveryArtifact:
    ref = str(binding.get(ref_key) or "")
    sha256 = str(binding.get(sha_key) or "")
    if not ref or not sha256:
        raise WorkspaceServiceError("INTEGRITY_ERROR", "delivery binding artifact 缺失")
    return VersionDeliveryArtifact(ref=ref, sha256=sha256)


def _delivery_idempotency_key(context: VersionDeliveryContext) -> str:
    binding_sha256 = _sha256_json(_delivery_binding(context))
    return f"e03_{binding_sha256[:32]}_{uuid.uuid4().hex}"


def _delivery_binding_from_events(events: list[dict]) -> dict | None:
    for event in reversed(events):
        binding = (event.get("details") or {}).get("internal_delivery_binding")
        if isinstance(binding, dict):
            return dict(binding)
    return None


def _latest_response(events: list[dict]) -> dict | None:
    for event in reversed(events):
        response = (event.get("details") or {}).get("response")
        if isinstance(response, dict):
            return response
    return None


def _job_id_from_events(events: list[dict]) -> str:
    for event in reversed(events):
        progress = (event.get("details") or {}).get("runtime_progress") or {}
        if progress.get("job_id"):
            return str(progress["job_id"])
    return ""


def _last_progress_signature(events: list[dict]) -> str:
    for event in reversed(events):
        signature = (event.get("details") or {}).get("signature")
        if signature:
            return str(signature)
    return ""


def _progress(snapshot: dict) -> dict:
    stages = [
        {
            "stage_id": str(item.get("stage_id") or ""),
            "status": str(item.get("status") or "not_started"),
            "attempt": int(item.get("attempt") or 0),
            "error": item.get("error"),
        }
        for item in (snapshot.get("stages") or [])
    ]
    return {
        "job_id": str(snapshot.get("job_id") or ""),
        "state": str(snapshot.get("state") or "unknown"),
        "message": str(snapshot.get("message") or ""),
        "run_id": str(snapshot.get("run_id") or ""),
        "active_stage": snapshot.get("active_stage"),
        "stages": stages,
        "progress": dict(snapshot.get("progress") or {}),
        "blocker": _snapshot_blocker(snapshot),
    }


def _snapshot_blocker(snapshot: dict) -> dict | None:
    state = str(snapshot.get("state") or "")
    if state in {"machine_blocked", "review_invalid", "failed", "cancelled"}:
        return {"code": state.upper(), "detail": str(snapshot.get("message") or state)}
    if state == "waiting_review":
        return {
            "code": "WAITING_HUMAN_REVIEW",
            "detail": "机器流程已到人工审核门；这不是失败，也不是质量通过。",
        }
    return None


def _progress_signature(progress: dict) -> str:
    return _sha256_json(progress)


def _terminal_response(progress: dict) -> dict:
    state = progress["state"]
    waiting = state == "waiting_review"
    return {
        "type": "execution_status",
        "title": "等待人工审核" if waiting else "运行状态已更新",
        "summary": (
            "机器流程已到人工审核门；这不代表质量通过。"
            if waiting
            else progress["message"] or "运行已结束"
        ),
        "job": progress,
        "next_steps": [
            "由人工审核真实脚本和复核包。"
            if waiting
            else "查看节点证据和阻断原因后决定下一步。"
        ],
    }


def _task_status(snapshot: dict) -> tuple[str, str | None]:
    state = str(snapshot.get("state") or "")
    report = dict(snapshot.get("report") or {})
    if state == "waiting_review" or (
        state == "complete" and str(report.get("human_review_status") or "") != "quality_pass"
    ):
        return "waiting_human_review", None
    if state == "complete":
        return "completed", None
    if state == "cancelled":
        return "cancelled", "CANCELLED"
    if state == "failed":
        return "failed", "INTERNAL_ERROR"
    return "blocked", state.upper() or "RUN_BLOCKED"


def _stable_error(code: str) -> str:
    allowed = {
        "INVALID_INPUT", "NOT_FOUND", "REVIEW_REQUIRED", "PERMISSION_DENIED",
        "WORKSPACE_VERSION_CONFLICT", "PROJECT_VERSION_CONFLICT",
        "PACKAGE_VERSION_CONFLICT", "KNOWLEDGE_VERSION_CONFLICT",
        "IDEMPOTENCY_CONFLICT", "TOOL_TIMEOUT", "CANCELLED", "INTERNAL_ERROR",
        "BRIEF_ARTIFACT_UNAVAILABLE", "RECOVERY_RECONCILIATION_REQUIRED",
    }
    return code if code in allowed else "INTERNAL_ERROR"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: dict) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(encoded)


def _public_task_events(events: list[dict]) -> list[dict]:
    return [_without_internal_fields(event) for event in events]


def _without_internal_fields(value):
    if isinstance(value, dict):
        return {
            key: _without_internal_fields(item)
            for key, item in value.items()
            if not str(key).startswith("internal_")
        }
    if isinstance(value, list):
        return [_without_internal_fields(item) for item in value]
    return value
