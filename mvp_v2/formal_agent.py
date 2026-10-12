from __future__ import annotations

import hashlib
import json
import logging
import re
from contextlib import contextmanager
from urllib.parse import quote, urlencode

from .agent_tools import ToolExecutionResult
from .artifact_service import ArtifactResolutionError
from .executable_agent import ExecutableAgentService, ExecutableAgentServices
from .formal_advisor import FormalAdvisorDecision, FormalAdvisorService, _redact_runtime_secrets
from .formal_admin_service import FormalAdminService
from .formal_generation_service import FormalGenerationService
from .formal_script_revision import FormalScriptRevisionService
from .formal_default_images import FormalDefaultImageService
from .formal_review_workspace import FormalReviewWorkspaceService
from .run_state import exclusive_lock
from .workspace_store import WorkspaceServiceError


_LOGGER = logging.getLogger(__name__)


_WRITE_REQUEST = re.compile(r"(生成|创建|准备).*(规划|脚本)|(确认|执行).*(生成|脚本)")
_CONFIRM_REQUEST = re.compile(r"(确认|执行).*(生成|脚本)|生成脚本")
_SCRIPT_REVISION_REQUEST = re.compile(r"第?\s*\d+\s*镜.*(修改|替换|调整|删除|删掉|移除|清空|去掉)")
_IMAGE_SHOT = re.compile(r"第?\s*(\d+)\s*镜")
_IMAGE_INSPECT_REQUEST = re.compile(r"(?:查看|看看|为什么|候选|找几张|找图片|检索).*(?:图片|图|候选)|(?:图片|图).*(?:候选|为什么|查看)")
_IMAGE_REVISION_REQUEST = re.compile(r"(?:换成|换|更换|替换为|替换|改用).*(?:刚上传|产品图|图片|图)|(?:重新)?配图")
_IMAGE_FEEDBACK_REQUEST = re.compile(r"(?:图片|图).*(?:不合适|不要再推荐|排除)|(?:不合适|不要再推荐|排除).*(?:图片|图)")
_TEMPORARY_TEXT_BASIS_REQUEST = re.compile(r"暂准文字依据")
_IMAGE_RELATED_REQUEST = re.compile(r"图片|候选|暂准文字依据|刚上传|产品图|配图|换图")
_TEXT_ONLY_REQUEST = re.compile(r"(只生成|仅生成|先生成).*(文字|文案)|(文字|文案).*(草稿|模式)|无图.*草稿")
_IMAGE_BACKED_REQUEST = re.compile(r"(采用|使用|上传|带).*(图片|默认图|类别图|项目图)|(图片|默认图|类别图|项目图).*(规划|脚本|模式)")
_STAGE_RETRY_REQUEST = re.compile(
    r"(?:重试|重跑|重新执行)\s*(?:当前)?\s*([a-z][a-z0-9_]{1,79})(?:\s*(?:节点|阶段))?"
)
_GENERATION_RETRY_REQUEST = re.compile(r"(?:重试|重跑|重新执行).{0,16}(?:生成|写稿|脚本)|(?:生成|写稿|脚本).{0,16}(?:重试|重跑)")
_FORCE_RECOVERY_REQUEST = re.compile(r"(?:强制|回滚|重置|checkpoint|检查点).*(?:恢复|重试)|(?:恢复|重试).*(?:强制|回滚|重置|checkpoint|检查点)", re.IGNORECASE)


class FormalExecutionBackend:
    """The Formal agent executes only server-rebuilt Formal actions."""

    def __getattr__(self, _name):
        def blocked(*_args, **_kwargs):
            raise WorkspaceServiceError(
                "FORMAL_AGENT_ACTION_UNSUPPORTED", "Formal 智能体不执行旧 JobManager 动作"
            )
        return blocked


class FormalExecutableAgentService(ExecutableAgentService):
    def __init__(
        self,
        services: ExecutableAgentServices,
        *,
        generation: FormalGenerationService,
        advisor: FormalAdvisorService | None,
        writes_enabled: bool = True,
        read_only_deployment: bool = False,
        script_revision: FormalScriptRevisionService | None = None,
        default_images: FormalDefaultImageService | None = None,
        operations: FormalAdminService | None = None,
        review_workspace: FormalReviewWorkspaceService | None = None,
    ):
        super().__init__(services)
        self.generation = generation
        self.advisor = advisor
        self.read_only_deployment = bool(read_only_deployment)
        self.writes_enabled = bool(writes_enabled) and not self.read_only_deployment
        self.script_revision = script_revision
        self.default_images = default_images
        self.operations = operations
        self.review_workspace = review_workspace

    def get_session(self, auth_token: str, agent_session_id: str) -> dict:
        result = super().get_session(auth_token, agent_session_id)
        project_id = result["session"]["project_id"]
        workbench_parents = {}
        session_task_ids = {task["task_id"] for task in result.get("tasks") or []}
        for task in result.get("tasks") or []:
            if task.get("task_type") != "formal_production_advisor":
                continue
            events = self.services.tasks.list_task_events(auth_token, task["task_id"])
            data = next((event.get("details", {}).get("workbench") for event in reversed(events)
                         if event.get("details", {}).get("workbench")), None)
            if data and data.get("project_id") == project_id:
                for key in ("text_task_id", "image_task_id"):
                    child_id = data.get(key)
                    if child_id in session_task_ids:
                        workbench_parents[child_id] = task["task_id"]
        for task in result.get("tasks") or []:
            if task["task_id"] not in workbench_parents and task.get('task_type') in {
                    'formal_creative_draft', 'formal_script_revision', 'formal_image_candidates'}:
                from .workbench_task_recovery import parent_for_child
                parent = parent_for_child(self.services.tasks, auth_token, task['task_id'])
                if parent:
                    workbench_parents[task['task_id']] = parent
            if task["task_id"] in workbench_parents:
                task["workbench_parent_task_id"] = workbench_parents[task["task_id"]]
        for item in result.get("timeline") or []:
            if item.get("task_id") in workbench_parents:
                item["workbench_parent_task_id"] = workbench_parents[item["task_id"]]
            response = item.get("response") or {}
            if response.get("analysis_task_id"):
                events = self.services.tasks.list_task_events(auth_token, item["task_id"])
                instruction = next((
                    event["details"]["assistant_instruction"] for event in reversed(events)
                    if event.get("details", {}).get("assistant_instruction")
                ), "")
                response = {**response, "instruction": _redact_runtime_secrets(str(instruction))}
            item["response"] = _public_formal_response(response, project_id)
        return result

    def start_workbench(self, auth_token, agent_session_id, **kwargs):
        from .creative_workbench import start
        return start(self, auth_token, agent_session_id, **kwargs)

    def execute_workbench(self, auth_token, task_id):
        from .creative_workbench import execute
        return execute(self, auth_token, task_id)

    def read_workbench(self, auth_token, task_id):
        from .workbench_task_recovery import sync_workbench
        return sync_workbench(self, auth_token, task_id)

    def confirm_workbench(self, auth_token, task_id, **kwargs):
        from .creative_workbench import confirm
        return confirm(self, auth_token, task_id, **kwargs)

    def undo_workbench(self, auth_token, task_id):
        from .creative_workbench import undo
        return undo(self, auth_token, task_id)

    def setup_status(self) -> dict:
        actions = self.action_readiness()
        return {
            **super().setup_status(),
            "formal_advisor": "available" if self.advisor is not None else "blocked",
            "formal_agent_reads": "available",
            # Kept as an aggregate compatibility field.  Callers must use
            # formal_actions for the actual action/provider readiness.
            "formal_agent_writes": "available" if self.writes_enabled else "blocked",
            "formal_actions": actions,
            "formal_script_revision": (
                "available"
                if self.writes_enabled
                and self.script_revision is not None and self.script_revision.writes_enabled
                else "blocked"
            ),
            "formal_default_image_adoption": (
                "available"
                if self.writes_enabled
                and self.default_images is not None and self.default_images.writes_enabled
                else "blocked"
            ),
            "formal_image_revision": (
                "available"
                if self.writes_enabled
                and self.script_revision is not None
                and self.script_revision.writes_enabled
                and self.script_revision.image_workspace is not None
                else "blocked"
            ),
            "formal_image_creation": "available" if self.writes_enabled and getattr(self.generation, "image_provider", None) is not None else "blocked",
            "formal_stage_retry": "available" if not self.read_only_deployment and self.operations is not None else "blocked",
        }

    def action_readiness(self) -> dict[str, dict[str, str | bool]]:
        """Expose readiness per action, without making one provider a global gate."""
        readonly = self.read_only_deployment or not self.writes_enabled
        readonly_code = (
            "FORMAL_AGENT_READ_ONLY"
            if self.read_only_deployment else
            "FORMAL_AGENT_WRITES_DISABLED"
            if not self.writes_enabled else
            ""
        )

        blocked_reasons = {
            "FORMAL_AGENT_READ_ONLY": "当前部署已显式设为只读",
            "FORMAL_AGENT_WRITES_DISABLED": "当前部署未开放写动作",
            "FORMAL_PLAN_UNAVAILABLE": "规划服务未接入",
            "FORMAL_PROVIDER_UNAVAILABLE": "脚本生成所需的受控文本 Provider 未就绪",
            "FORMAL_SCRIPT_REVISION_UNAVAILABLE": "脚本修订服务未接入",
            "FORMAL_IMAGE_REVISION_UNAVAILABLE": "图片修订服务未接入",
            "FORMAL_FACT_REVISION_UNAVAILABLE": "Brief 事实修订服务未接入",
            "FORMAL_DEFAULT_IMAGE_ADOPTION_UNAVAILABLE": "默认图片采纳服务未接入",
            "FORMAL_STAGE_RETRY_UNAVAILABLE": "阶段重试服务未接入",
        }

        def state(available: bool, code: str = "") -> dict[str, str | bool]:
            return {
                "state": "available" if available else "blocked",
                "available": available,
                "code": "" if available else code,
                "reason": "" if available else blocked_reasons.get(code, "当前动作不可用"),
            }

        generation = self.generation is not None
        provider_ready = generation and (
            not hasattr(self.generation, "provider_policy")
            or getattr(self.generation, "provider_policy", None) is not None
        )
        script_revision = (
            self.script_revision is not None
            and self.script_revision.writes_enabled
        )
        image_revision = script_revision and self.script_revision.image_workspace is not None
        default_images = self.default_images is not None and self.default_images.writes_enabled
        retry = self.operations is not None and not self.read_only_deployment
        return {
            "prepare_formal_plan": state(
                generation and not readonly,
                readonly_code if readonly_code else "FORMAL_PLAN_UNAVAILABLE",
            ),
            "confirm_formal_plan_generation": state(
                provider_ready and not readonly,
                readonly_code if readonly_code else "FORMAL_PROVIDER_UNAVAILABLE",
            ),
            "apply_formal_script_revision": state(
                script_revision and not readonly,
                readonly_code if readonly_code else "FORMAL_SCRIPT_REVISION_UNAVAILABLE",
            ),
            "apply_formal_image_revision": state(
                image_revision and not readonly,
                readonly_code if readonly_code else "FORMAL_IMAGE_REVISION_UNAVAILABLE",
            ),
            "record_formal_image_feedback": state(
                image_revision and not readonly,
                readonly_code if readonly_code else "FORMAL_IMAGE_REVISION_UNAVAILABLE",
            ),
            "apply_formal_fact_revision": state(
                script_revision and not readonly,
                readonly_code if readonly_code else "FORMAL_FACT_REVISION_UNAVAILABLE",
            ),
            "adopt_formal_default_images": state(
                default_images and not readonly,
                readonly_code if readonly_code else "FORMAL_DEFAULT_IMAGE_ADOPTION_UNAVAILABLE",
            ),
            "retry_formal_stage": state(
                retry,
                readonly_code if readonly_code else "FORMAL_STAGE_RETRY_UNAVAILABLE",
            ),
            "inspect_image_candidates": state(
                self.script_revision is not None and self.script_revision.image_workspace is not None,
                "FORMAL_IMAGE_REVISION_UNAVAILABLE",
            ),
        }

    def analyze_brief(self, auth_token: str, agent_session_id: str, *, content: str) -> dict:
        """Analyze the canonical Brief in the existing project conversation.

        Intake uses the same advisor and task history, but never dispatches an
        executable action. Facts are imported through FormalBriefInputAdapter.
        """
        message = str(content or "").strip()
        if not message or len(message) > 4000:
            raise WorkspaceServiceError("INVALID_INPUT", "请用 4000 字以内说明本次需求或问题")
        if self.advisor is None:
            raise WorkspaceServiceError("FORMAL_ADVISOR_UNAVAILABLE", "需求分析模型暂不可用；已录入的资料保留，可继续核对详细需求。")
        session = self.services.tasks.get_agent_session(auth_token, agent_session_id)
        project = self.services.projects.get_project(auth_token, session["project_id"])
        adapter = getattr(self.script_revision, "brief_adapter", None)
        if adapter is None:
            raise WorkspaceServiceError("FORMAL_BRIEF_ADAPTER_UNAVAILABLE", "需求资料服务尚未接入")
        scope = {"workspace_id": project["workspace_id"], "project_id": project["project_id"]}
        brief = adapter.read_current_formal(auth_token, **scope)
        context = {
            "mode": "brief_analysis", "project": {"name": project["name"]},
            'question_fields': adapter.question_fields(),
            "brief": brief, "creative_standard": brief.get("creative_standard", {}),
            "input_questions": [{key: question[key] for key in ("field", "label", "required_for") if key in question}
                for question in adapter.input_questions(auth_token, **scope)],
            "conversation": self._recent_conversation(auth_token, project, agent_session_id),
            "evidence_ids": [f"project:{project['project_id']}:v{project['project_version']}"]
                + ([f"brief:{brief['brief_key']}"] if brief.get("brief_key") else []),
        }
        task = self.services.tasks.create_task(auth_token, agent_session_id=agent_session_id,
            task_type="formal_production_advisor",
            payload_sha256=_sha256_json({"mode": "brief_analysis", "content": message,
                "project_version": project["project_version"]}),
            expected_project_version=project["project_version"])
        task = self.services.tasks.transition_task(auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={"user_message": _redact_runtime_secrets(message), "brief_analysis": True})
        try:
            decision = self.advisor.decide(message, context)
            if decision.intent not in {"explain_status", "explain_issue", "needs_clarification"}:
                raise WorkspaceServiceError("BRIEF_ANALYSIS_INVALID", "助手未按需求分析方式返回，本次未执行创作；可重新分析已存需求。")
            # Model intentions are advisory here; this entry cannot create or
            # confirm a plan, draft, fact or version, regardless of the response.
            response = {"type": "brief_analysis", "title": "需求分析",
                "user_message": _redact_runtime_secrets(message),
                "summary": decision.summary, "questions": decision.questions[:3],
                "evidence_ids": decision.evidence_ids,
                "brief_key": brief.get("brief_key", ""),
                "project_version": project["project_version"]}
            response['question_bindings'] = [
                {'question_id': f"{task['task_id']}:{index}", 'analysis_task_id': task['task_id'],
                 'question': question, 'field': decision.question_fields[index]}
                for index, question in enumerate(response['questions'])
                if index < len(decision.question_fields) and decision.question_fields[index] in adapter.question_fields()]
            task = self.services.tasks.transition_task(auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="executing", details={"read_only": True})
            task = self.services.tasks.transition_task(auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="completed", details={"response": response})
            return self._message_result(auth_token, task, response)

        except WorkspaceServiceError as exc:
            response = {"type": "brief_analysis", "title": "需求分析未完成",
                "user_message": _redact_runtime_secrets(message), "summary": exc.message,
                "questions": [], "error_code": exc.code, "brief_key": brief.get("brief_key", "")}
            task = self.services.tasks.transition_task(auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="failed", error_code=exc.code,
                details={"response": response})
            return self._message_result(auth_token, task, response)

    def answer_brief_question(self, auth_token, agent_session_id, *, question_id="", analysis_task_id="",
                              content="", expected_project_version, answers=None, idempotency_key=""):
        session = self.services.tasks.get_agent_session(auth_token, agent_session_id)
        if answers is not None:
            if not isinstance(answers, list) or len(answers) > 20:
                raise WorkspaceServiceError('INVALID_INPUT', '一次最多回答20个问题')
            if question_id or analysis_task_id:
                raise WorkspaceServiceError('INVALID_INPUT', '请使用同一种回答方式')
            resolved, seen = [], set()
            for answer in answers:
                if not isinstance(answer, dict) or not isinstance(answer.get('content'), str):
                    raise WorkspaceServiceError('INVALID_INPUT', '回答内容无效，输入已保留')
                identity = answer.get('question_id')
                if not identity or identity in seen:
                    raise WorkspaceServiceError('INVALID_INPUT', '本次回答包含重复或缺失的问题')
                seen.add(identity)
                binding = self._brief_question_binding(auth_token, session, agent_session_id,
                    identity, answer.get('analysis_task_id'))
                resolved.append({'binding': binding, 'value': answer['content']})
            project = self.services.projects.get_project(auth_token, session['project_id'])
            return self.script_revision.brief_adapter.import_question_answers(auth_token,
                workspace_id=project['workspace_id'], project_id=project['project_id'],
                answers=resolved, content=content, expected_project_version=expected_project_version,
                idempotency_key=f'{agent_session_id}:{idempotency_key}' if idempotency_key else '')
        binding = self._brief_question_binding(auth_token, session, agent_session_id,
            question_id, analysis_task_id)
        project = self.services.projects.get_project(auth_token, session['project_id'])
        return self.script_revision.brief_adapter.import_question_answer(auth_token,
            workspace_id=project['workspace_id'], project_id=project['project_id'],
            binding=binding, value=content, expected_project_version=expected_project_version)

    def _brief_question_binding(self, auth_token, session, agent_session_id, question_id, analysis_task_id):
        if not isinstance(analysis_task_id, str) or not analysis_task_id:
            raise WorkspaceServiceError('INVALID_INPUT', '问题记录无效，输入已保留')
        source = self.services.tasks.get_task(auth_token, analysis_task_id)
        if source['project_id'] != session['project_id'] or source['agent_session_id'] != agent_session_id:
            raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '该问题不属于当前项目对话')
        events = self.services.tasks.list_task_events(auth_token, analysis_task_id)
        binding = next((item for event in events for item in event['details'].get('response', {}).get('question_bindings', [])
                        if item.get('question_id') == question_id), None)
        if binding is None:
            raise WorkspaceServiceError('INVALID_INPUT', '问题记录已不可用，请重新分析或录入完整资料')
        return binding

    def submit_message(
        self, auth_token: str, agent_session_id: str, *, content: str,
        run_id: str | None = None, context: dict | None = None, operation_key: str | None = None,
    ) -> dict:
        del run_id
        if context is not None:
            return self.start_workbench(auth_token, agent_session_id, content=content,
                context=context, operation_key=operation_key)
        message = str(content or "").strip()
        if not message:
            raise WorkspaceServiceError("INVALID_INPUT", "message 不能为空")
        session = self.services.tasks.get_agent_session(auth_token, agent_session_id)
        project = self.services.projects.get_project(auth_token, session["project_id"])
        if self.script_revision is not None and re.search(r'生图|(?:生成|生|画|做).{0,8}(?:一张|图片|参考图)|保留.{0,25}(?:换|改).{0,12}(?:背景|场景)|改图|编辑.{0,8}(?:图片|原图)', message):
            from .formal_script_assistant import _referenced_shot_ids
            current = self.script_revision.workspace.read_current(auth_token,
                workspace_id=project['workspace_id'], project_id=project['project_id'])
            targets = _referenced_shot_ids(message, current.get('shots') or [])
            if not targets or len(targets) != 1:
                return self._image_clarification(auth_token, agent_session_id, project,
                    '请指定要生图或改图的一镜，例如“为第2镜生成一张参考图”；也可在创作页面选择当前画面。')
            context = self.generation.image_creation_context(auth_token, workspace_id=project['workspace_id'],
                project_id=project['project_id'], target_kind='formal_shot', target_id=targets[0])
            task = self.services.tasks.create_task(auth_token, agent_session_id=agent_session_id,
                task_type='formal_image_candidates', payload_sha256=_sha256_json({'image_creation':message,'target_id':targets[0]}),
                expected_project_version=project['project_version'])
            response = {'type':'image_creation_request', 'title':'图片创作要求',
                'summary': '已定位目标画面，请核对要求与参考图后生成一张。' if context['available'] else context['unavailable_reason'],
                'project_id':project['project_id'], 'workspace_id':project['workspace_id'],
                'target_kind':'formal_shot', 'target_id':targets[0], 'target_label':context['target']['label'],
                'instruction':_redact_runtime_secrets(message)}
            for status, details in [('understanding', {'user_message':_redact_runtime_secrets(message)}),
                                    ('executing', {'read_only':True}), ('completed', {'response':response})]:
                task = self.services.tasks.transition_task(auth_token, task_id=task['task_id'],
                    expected_task_version=task['task_version'], status=status, details=details)
            return self._message_result(auth_token, task, response)
        if self.script_revision is not None and re.search(r'(?:配|换|替换|找|检索|搜索).{0,20}(?:图|素材)|(?:图片|配图).{0,12}(?:换|替换|检索|搜索|推荐)', message):
            from .formal_script_assistant import _referenced_shot_ids
            broad_scope = re.search(r'整[份稿片]|全[稿部片]|后半段|前半段', message)
            targets = []
            if not broad_scope:
                current = self.script_revision.workspace.read_current(auth_token,
                    workspace_id=project['workspace_id'], project_id=project['project_id'])
                targets = _referenced_shot_ids(message, current.get('shots') or [])
            if broad_scope or (targets and len(targets) > 1):
                return self.script_revision.propose_image_batch(auth_token,
                    workspace_id=project['workspace_id'], project_id=project['project_id'],
                    instruction=message, expected_project_version=project['project_version'],
                    idempotency_key='agent-batch-' + hashlib.sha256(message.encode()).hexdigest()[:24],
                    agent_session_id=agent_session_id)
        retry = _STAGE_RETRY_REQUEST.search(message)
        if retry or _GENERATION_RETRY_REQUEST.search(message):
            return self._propose_stage_retry(
                auth_token,
                agent_session_id=agent_session_id,
                project=project,
                stage_id=retry.group(1) if retry else "formal_generation_plan",
                message=message,
                force_requested=bool(_FORCE_RECOVERY_REQUEST.search(message)),
            )
        strict_image = self._strict_image_intent(message)
        if strict_image is not None:
            intent, shot_no, image_query = strict_image
            if not shot_no:
                return self._image_clarification(
                    auth_token, agent_session_id, project, "请只指定一个有效镜号；助手不会猜测目标镜头。",
                )
            image_action = {
                "propose_image_revision": "apply_formal_image_revision",
                "propose_image_feedback": "record_formal_image_feedback",
                "propose_temporary_text_basis": "apply_formal_image_revision",
            }.get(intent)
            if image_action is not None:
                readiness = self.action_readiness()[image_action]
                if not readiness["available"]:
                    task = self.services.tasks.create_task(
                        auth_token, agent_session_id=agent_session_id,
                        task_type="formal_production_advisor",
                        payload_sha256=_sha256_json({"action": image_action, "message": message}),
                        expected_project_version=project["project_version"],
                    )
                    return self._block_task(
                        auth_token, task,
                        WorkspaceServiceError(
                            str(readiness["code"]), "当前图片写动作不可用，未创建图片版本",
                        ),
                    )
            try:
                candidates = self._inspect_image_candidates(
                    auth_token, agent_session_id=agent_session_id, project=project,
                    shot_no=shot_no, query=image_query, instruction=message,
                )
                if intent == "propose_image_feedback":
                    return self.script_revision.propose_image_feedback(
                        auth_token, candidate_task_id=candidates["task"]["task_id"],
                        reason=message,
                        idempotency_key=f"agent-image-feedback-{candidates['task']['task_id']}",
                    )
                if intent == "propose_temporary_text_basis":
                    return self.script_revision.propose_temporary_text_basis(
                        auth_token, candidate_task_id=candidates["task"]["task_id"],
                        reason=self._temporary_reason(message),
                        idempotency_key=f"agent-image-temporary-{candidates['task']['task_id']}",
                    )
                return candidates
            except WorkspaceServiceError as exc:
                return self._image_clarification(
                    auth_token, agent_session_id, project, exc.message, error_code=exc.code,
                )
        if self.script_revision is not None and _SCRIPT_REVISION_REQUEST.search(message):
            current = self.script_revision.workspace.read_current(
                auth_token, workspace_id=project["workspace_id"], project_id=project["project_id"],
            )
            task = self.services.tasks.create_task(
                auth_token,
                agent_session_id=agent_session_id,
                task_type="formal_script_revision",
                payload_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
                expected_project_version=project["project_version"],
            )
            task = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="understanding",
                details={
                    "formal_context_version": project["project_version"],
                    "user_message": _redact_runtime_secrets(message),
                },
            )
            result = self.script_revision.propose_on_task(
                auth_token, task=task, project=project, instruction=message,
                expected_revision=int(current.get("revision") or 0),
                idempotency_key=f"agent-script-revision-{task['task_id']}",
            )
            return self._script_message_result(result, project, message)
        context = self._context(auth_token, project, agent_session_id=agent_session_id)
        if _TEXT_ONLY_REQUEST.search(message):
            context["requested_generation_mode"] = "text_only_draft"
        elif _IMAGE_BACKED_REQUEST.search(message):
            context["requested_generation_mode"] = "image_backed"
        task = self.services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type="formal_production_advisor",
            payload_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            expected_project_version=project["project_version"],
        )
        task = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={
                "formal_context_version": project["project_version"],
                "user_message": _redact_runtime_secrets(message),
            },
        )
        handoff = self._existing_page_handoff(message, project["project_id"])
        if handoff is not None:
            task = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="executing", details={"read_only": True},
            )
            finished = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="completed", details={"response": handoff},
            )
            return self._message_result(auth_token, finished, handoff)
        try:
            decision = self._decision(message, context)
        except WorkspaceServiceError as exc:
            if exc.code == "FORMAL_ADVISOR_UNAVAILABLE":
                return self._block_task(auth_token, task, exc)
            response = {
                "type": "needs_clarification", "title": "需要重新确认请求",
                "summary": exc.message, "error_code": exc.code,
                "next_steps": ["请说明是查看状态、准备规划，还是确认当前规划生成脚本。"],
            }
            finished = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="needs_clarification", details={"response": response},
            )
            return self._message_result(auth_token, finished, response)

        if decision.intent in {"explain_status", "explain_issue"}:
            response = self._read_response(decision, context)
            task = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="executing",
                details={"read_only": True},
            )
            finished = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="completed",
                details={"response": response},
            )
            return self._message_result(auth_token, finished, response)
        if decision.intent == "needs_clarification":
            response = {
                "type": "needs_clarification", "title": "需要补充信息",
                "summary": decision.summary,
                "questions": decision.questions,
                "next_steps": decision.questions or ["请明确下一步。"],
            }
            finished = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="needs_clarification", details={"response": response},
            )
            return self._message_result(auth_token, finished, response)
        required_action = {
            "prepare_plan": "prepare_formal_plan",
            "confirm_generation": "confirm_formal_plan_generation",
        }.get(decision.intent)
        if required_action is not None:
            readiness = self.action_readiness()[required_action]
            if not readiness["available"]:
                return self._block_task(
                    auth_token,
                    task,
                    WorkspaceServiceError(str(readiness["code"]), self._readiness_message(required_action)),
                )
        if decision.intent in {
            "inspect_image_candidates", "propose_image_revision",
            "propose_image_feedback", "propose_temporary_text_basis",
        }:
            image_action = {
                "propose_image_revision": "apply_formal_image_revision",
                "propose_image_feedback": "record_formal_image_feedback",
                "propose_temporary_text_basis": "apply_formal_image_revision",
            }.get(decision.intent)
            if image_action is not None:
                readiness = self.action_readiness()[image_action]
                if not readiness["available"]:
                    return self._block_task(
                        auth_token, task,
                        WorkspaceServiceError(
                            str(readiness["code"]), "当前图片写动作不可用，未创建图片版本",
                        ),
                    )
            shots = _IMAGE_SHOT.findall(message)
            if len(shots) != 1:
                response = {
                    "type": "needs_clarification", "title": "需要明确一个镜头",
                    "summary": "图片操作每次只处理一个明确镜头；模型返回的镜号不会被采用。",
                    "error_code": "IMAGE_SHOT_CLARIFICATION_REQUIRED",
                    "next_steps": ["请使用“查看第 3 镜当前图片和候选”这样的表达。"],
                }
                finished = self.services.tasks.transition_task(
                    auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
                    status="needs_clarification", details={"response": response},
                )
                return self._message_result(auth_token, finished, response)
            routed = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
                status="executing", details={"routed_intent": decision.intent, "read_only": True},
            )
            self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"], expected_task_version=routed["task_version"],
                status="completed", details={"response": {
                    "type": "intent_routed", "summary": "已转交权威图片工作区。",
                }},
            )
            try:
                candidates = self._inspect_image_candidates(
                    auth_token, agent_session_id=agent_session_id, project=project,
                    shot_no=shots[0], query=self._validated_advisor_image_query(decision.image_query),
                    instruction=message,
                )
                if decision.intent == "propose_image_feedback":
                    return self.script_revision.propose_image_feedback(
                        auth_token, candidate_task_id=candidates["task"]["task_id"], reason=message,
                        idempotency_key=f"agent-image-feedback-{candidates['task']['task_id']}",
                    )
                if decision.intent == "propose_temporary_text_basis":
                    return self.script_revision.propose_temporary_text_basis(
                        auth_token, candidate_task_id=candidates["task"]["task_id"],
                        reason=self._temporary_reason(message),
                        idempotency_key=f"agent-image-temporary-{candidates['task']['task_id']}",
                    )
                return candidates
            except WorkspaceServiceError as exc:
                return self._image_clarification(
                    auth_token, agent_session_id, project, exc.message, error_code=exc.code,
                )
        if decision.intent == "propose_script_revision":
            if self.script_revision is None:
                return self._block_task(
                    auth_token, task,
                    WorkspaceServiceError(
                        "FORMAL_SCRIPT_REVISION_UNAVAILABLE",
                        "Formal 脚本修改服务未安装",
                    ),
                )
            try:
                current = self.script_revision.workspace.read_current(
                    auth_token, workspace_id=project["workspace_id"],
                    project_id=project["project_id"],
                )
            except WorkspaceServiceError as exc:
                response = {
                    "type": "needs_clarification", "title": "需要明确修改范围",
                    "summary": exc.message, "error_code": exc.code,
                    "next_steps": ["请明确镜号、字段，以及删除、清空或替换操作。"],
                }
                finished = self.services.tasks.transition_task(
                    auth_token, task_id=task["task_id"],
                    expected_task_version=task["task_version"],
                    status="needs_clarification", details={"response": response},
                )
                return self._message_result(auth_token, finished, response)
            try:
                proposal = self.script_revision.propose(
                    auth_token, agent_session_id=agent_session_id,
                    project_id=project["project_id"], instruction=message,
                    expected_revision=int(current.get("revision") or 0),
                    expected_project_version=project["project_version"],
                    idempotency_key=f"agent-script-revision-{task['task_id']}",
                )
            except WorkspaceServiceError as exc:
                return self._block_task(auth_token, task, exc)
            task = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="executing",
                details={"routed_intent": "propose_script_revision"},
            )
            self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="completed",
                details={"response": {
                    "type": "intent_routed", "summary": "已转交脚本修改提案服务。",
                }},
            )
            return self._script_message_result(proposal, project, message)
        try:
            action, parameters, _ = self._authoritative_action(
                auth_token, project, decision, context
            )
        except WorkspaceServiceError as exc:
            return self._block_task(auth_token, task, exc)
        digest = _sha256_json({"action": action, "parameters": parameters})
        response = {
            "type": "action_proposal", "title": "执行前确认",
            "summary": decision.summary, "action": action,
            "proposal_digest": digest,
            "confirmation_stage": "confirmation_required",
            "next_href": self._next_href(project["project_id"], action),
            "risk": "R2",
            "will_change": "创建规划" if action == "prepare_formal_plan" else "从已确认规划创建脚本版本",
            "will_not_change": "不会修改 Brief、模板库、AI 规范表、历史 run 或发布状态",
            "calls_models": action == "confirm_formal_plan_generation",
            "model_call_statement": "会调用已配置写稿和编辑模型" if action == "confirm_formal_plan_generation" else "不会调用写稿或编辑模型",
            "impact_scope": "当前项目／当前规划",
            "next_steps": ["确认后才执行；项目状态变化会使提案失效。"],
        }
        proposed = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="proposed",
            details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._message_result(auth_token, proposed, response)

    @staticmethod
    def _readiness_message(action: str) -> str:
        return {
            "prepare_formal_plan": "规划服务不可用，或当前部署处于显式只读模式",
            "confirm_formal_plan_generation": "脚本生成所需的受控文本 Provider 不可用，或当前部署处于显式只读模式",
        }.get(action, "当前动作不可用")

    @staticmethod
    def _next_href(project_id: str, action: str) -> str:
        suffix = {
            "prepare_formal_plan": "plan",
            "confirm_formal_plan_generation": "shot-images",
            "apply_formal_script_revision": "script",
            "apply_formal_image_revision": "shot-images",
            "record_formal_image_feedback": "shot-images",
            "apply_formal_fact_revision": "brief",
            "adopt_formal_default_images": "shot-images",
            "retry_formal_stage": "overview",
        }.get(action, "overview")
        return f"/projects/{project_id}/{suffix}"

    @staticmethod
    def _script_message_result(result: dict, project: dict, instruction: str) -> dict:
        response = dict(result.get("response") or {})
        if response.get("analysis_task_id"):
            response["instruction"] = _redact_runtime_secrets(instruction)
        return {**result, "response": _public_formal_response(response, project["project_id"])}

    @staticmethod
    def _existing_page_handoff(message: str, project_id: str) -> dict | None:
        # These requests need page-owned fields or a selection; a link is not an execution.
        routes = (
            (r"(?:保存|另存).*(?:草稿|版本|脚本)|(?:草稿|版本|脚本).*保存", "script", "保存脚本版本", "请核对草稿并保存新版本。"),
            (r"(?:修改|更新|更正|反馈).*(?:Brief|事实|项目资料)|(?:Brief|事实).*(?:修改|更新|更正|反馈)", "brief", "修改 Brief", "请在 Brief 中核对并提交事实修改。"),
            (r"(?:审核|复核).*(?:退回|不通过)|(?:退回|不通过).*(?:审核|复核)", "review", "查看审核意见", "请查看退回意见，再修改对应内容。"),
        )
        for pattern, section, label, summary in routes:
            if re.search(pattern, message, re.IGNORECASE):
                return {
                    "type": "project_next_step", "title": label, "summary": summary,
                    "next_href": f"/projects/{project_id}/{section}", "next_label": label,
                }
        return None

    def select_image_candidate(
        self, auth_token: str, candidate_task_id: str, *,
        candidate_key: str, reuse_reason: str = "",
    ) -> dict:
        if self.script_revision is None or self.script_revision.image_workspace is None:
            raise WorkspaceServiceError("FORMAL_IMAGE_REVISION_UNAVAILABLE", "Formal 图片修订服务未安装")
        if not self.writes_enabled or not self.script_revision.writes_enabled:
            raise WorkspaceServiceError("FORMAL_IMAGE_REVISION_DISABLED", "Formal 图片修订能力当前关闭")
        return self.script_revision.propose_image_change(
            auth_token, candidate_task_id=candidate_task_id,
            candidate_key=str(candidate_key or ""), reuse_reason=str(reuse_reason or ""),
            idempotency_key=f"agent-image-selection-{candidate_task_id}-{candidate_key}",
        )

    def select_temporary_text_basis(
        self, auth_token: str, candidate_task_id: str, *, reason: str,
    ) -> dict:
        if self.script_revision is None or self.script_revision.image_workspace is None:
            raise WorkspaceServiceError("FORMAL_IMAGE_REVISION_UNAVAILABLE", "Formal 图片修订服务未安装")
        if not self.writes_enabled or not self.script_revision.writes_enabled:
            raise WorkspaceServiceError("FORMAL_IMAGE_REVISION_DISABLED", "Formal 图片修订能力当前关闭")
        return self.script_revision.propose_temporary_text_basis(
            auth_token, candidate_task_id=candidate_task_id, reason=str(reason or ""),
            idempotency_key=f"agent-image-temporary-{candidate_task_id}",
        )

    @staticmethod
    def _strict_image_intent(message: str) -> tuple[str, str, str] | None:
        if not _IMAGE_RELATED_REQUEST.search(message):
            return None
        intent = None
        if _IMAGE_FEEDBACK_REQUEST.search(message):
            intent = "propose_image_feedback"
        elif _TEMPORARY_TEXT_BASIS_REQUEST.search(message):
            intent = "propose_temporary_text_basis"
        elif _IMAGE_REVISION_REQUEST.search(message):
            intent = "propose_image_revision"
        elif _IMAGE_INSPECT_REQUEST.search(message):
            intent = "inspect_image_candidates"
        if intent is None:
            return None
        shots = _IMAGE_SHOT.findall(message)
        if len(shots) != 1:
            return intent, "", ""
        query = ""
        if intent in {"inspect_image_candidates", "propose_image_revision"}:
            query = _IMAGE_SHOT.sub("", message).strip()[:300]
        return intent, shots[0], query

    @staticmethod
    def _temporary_reason(message: str) -> str:
        reason = _IMAGE_SHOT.sub("", str(message or ""))
        reason = _TEMPORARY_TEXT_BASIS_REQUEST.sub("", reason).strip(" ，,：:")
        if not reason:
            raise WorkspaceServiceError("INVALID_INPUT", "暂准文字依据必须填写非空理由")
        return reason[:1000]

    @staticmethod
    def _validated_advisor_image_query(value: str) -> str:
        query = str(value or "").strip()
        if len(query) > 300 or re.search(r"artifact://|(?:^|\s)/|[0-9a-f]{64}", query, re.IGNORECASE):
            raise WorkspaceServiceError(
                "FORMAL_ADVISOR_INVALID_RESPONSE", "图片检索描述包含服务端身份或路径信息",
            )
        return query

    def _inspect_image_candidates(
        self, auth_token: str, *, agent_session_id: str, project: dict,
        shot_no: str, query: str, instruction: str = "",
    ) -> dict:
        if self.script_revision is None or self.script_revision.image_workspace is None:
            raise WorkspaceServiceError("FORMAL_IMAGE_REVISION_UNAVAILABLE", "Formal 图片工作区未安装")
        current = self.script_revision.image_workspace.read_current(
            auth_token, workspace_id=project["workspace_id"], project_id=project["project_id"],
            expected_project_version=project["project_version"],
        )
        if not current.get("base_version_id"):
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可检索图片的脚本版本")
        task = self.services.tasks.create_task(
            auth_token, agent_session_id=agent_session_id, task_type="formal_image_candidates",
            payload_sha256=_sha256_json({"shot_no": shot_no, "query": query}),
            expected_project_version=project["project_version"],
        )
        task = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="understanding", details={
                "formal_context_version": project["project_version"],
                "user_message": _redact_runtime_secrets(instruction),
            },
        )
        searched = self.script_revision.image_workspace.search_shot(
            auth_token, workspace_id=project["workspace_id"], project_id=project["project_id"],
            shot_no=str(shot_no), query=str(query or ""), base_version_id=current["base_version_id"],
            expected_project_version=project["project_version"],
        )
        task = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="executing", details={"read_only": True},
        )
        candidates = []
        internal_candidates = []
        for item in searched.get("candidates") or []:
            candidate_key = "candidate_" + hashlib.sha256(
                f"{task['task_id']}:{item['image_key']}".encode("utf-8")
            ).hexdigest()[:24]
            candidates.append({
                **item, "candidate_key": candidate_key,
                "preview_url": self._image_preview_href(project, item["image_key"]),
            })
            internal_candidates.append({
                "candidate_key": candidate_key, "image_key": item["image_key"],
                "public": {**item, "candidate_key": candidate_key},
            })
        response = {
            **searched,
            "type": "formal_image_candidates", "title": f"第 {shot_no} 镜选图",
            "summary": (
                f"找到 {len(candidates)} 张候选，请核对后选择。"
                if candidates else "暂无匹配图片，可从创意库选图或补图。"
            ),
            "candidates": candidates,
            "current_image": (
                {
                    **searched["current_image"],
                    "preview_url": self._image_preview_href(project, searched["current_image"]["image_key"]),
                } if searched.get("current_image") else None
            ),
            "next_href": (
                f"/projects/{project['project_id']}/shot-images?focus_shot={quote(str(shot_no), safe='')}"
                if candidates else (
                    f"/projects/{project['project_id']}/materials?return_to=shot-images"
                    f"&focus_shot={quote(str(shot_no), safe='')}"
                )
            ),
            "next_label": "查看逐镜配图" if candidates else "补充图片",
            "next_steps": ["选择图片，或填写暂准理由。" if candidates else "补充图片，或填写暂准理由。"],
        }
        internal = {
            "workspace_id": project["workspace_id"], "project_id": project["project_id"],
            "expected_project_version": project["project_version"],
            "base_version_id": searched["base_version_id"], "shot_no": str(shot_no),
            "query": str(query or ""), "candidate_snapshot_digest": searched["candidate_snapshot_digest"],
            "candidates": internal_candidates, "current_image": searched.get("current_image"),
        }
        finished = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="completed", details={"response": response, "internal_formal_parameters": internal},
        )
        return self._message_result(auth_token, finished, response)

    @staticmethod
    def _image_preview_href(project: dict, image_key: str) -> str:
        query = urlencode({
            "workspace_id": project["workspace_id"],
            "expected_project_version": project["project_version"],
        })
        return (
            f"/api/formal/projects/{quote(project['project_id'], safe='')}/images/"
            f"{quote(str(image_key), safe='')}/preview?{query}"
        )

    def _image_clarification(
        self, auth_token: str, agent_session_id: str, project: dict, summary: str,
        *, error_code: str = "IMAGE_SHOT_CLARIFICATION_REQUIRED",
    ) -> dict:
        task = self.services.tasks.create_task(
            auth_token, agent_session_id=agent_session_id, task_type="formal_image_candidates",
            payload_sha256=_sha256_json({"clarification": summary}),
            expected_project_version=project["project_version"],
        )
        task = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="understanding", details={"formal_context_version": project["project_version"]},
        )
        response = {
            "type": "needs_clarification", "title": "需要明确图片操作",
            "summary": summary, "error_code": error_code,
            "next_steps": ["请明确一个镜号；图片身份不唯一时请从可见候选中点击选择。"],
        }
        finished = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="needs_clarification", details={"response": response},
        )
        return self._message_result(auth_token, finished, response)

    def _message_result(self, auth_token: str, task: dict, response: dict) -> dict:
        return super()._message_result(
            auth_token, task, _public_formal_response(response, task.get("project_id")),
        )

    def _execution_response(
        self, auth_token: str, task_id: str, *, job: dict | None = None,
    ) -> dict:
        result = super()._execution_response(auth_token, task_id, job=job)
        response = result.get("response")
        if isinstance(response, dict) and response.get("analysis_task_id"):
            instruction = next((
                event["details"]["assistant_instruction"] for event in reversed(result.get("events") or [])
                if event.get("details", {}).get("assistant_instruction")
            ), "")
            response = {**response, "instruction": _redact_runtime_secrets(str(instruction))}
        result["response"] = _public_formal_response(
            response, result.get("task", {}).get("project_id"),
        )
        return result

    def confirm_action(
        self, auth_token: str, task_id: str, *, proposal_digest: str,
    ) -> dict:
        from .workbench_task_recovery import require_standalone_child
        require_standalone_child(self.services.tasks, auth_token, task_id)
        with self._formal_task_execution(auth_token, task_id) as acquired:
            if not acquired:
                return self._execution_response(auth_token, task_id)
            return self._confirm_action(auth_token, task_id, proposal_digest=proposal_digest)

    @contextmanager
    def _formal_task_execution(self, auth_token: str, task_id: str):
        """Keep live confirmation and interrupted-action recovery mutually exclusive."""
        self.services.tasks.get_task(auth_token, task_id)
        response, parameters = _formal_proposal(
            self.services.tasks.list_task_events(auth_token, task_id)
        )
        if response is None or parameters is None:
            yield True
            return
        database = self.services.store.database_path.resolve()
        task_key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
        lock = exclusive_lock(
            database.parent / f".{database.name}.formal-actions" / f"{task_key}.lock",
            run_id=task_id,
        )
        try:
            lock.__enter__()
        except RuntimeError as exc:
            if isinstance(exc.__cause__, BlockingIOError):
                yield False
                return
            raise WorkspaceServiceError(
                "RECOVERY_RECONCILIATION_REQUIRED",
                "无法核实当前操作是否仍在执行，请先核对任务与版本记录。",
            ) from exc
        try:
            yield True
        finally:
            lock.__exit__(None, None, None)

    def _confirm_action(
        self, auth_token: str, task_id: str, *, proposal_digest: str,
    ) -> dict:
        task = self.services.tasks.get_task(auth_token, task_id)
        events = self.services.tasks.list_task_events(auth_token, task_id)
        response, parameters = _formal_proposal(events)
        if response is None or parameters is None:
            return super().confirm_action(
                auth_token, task_id, proposal_digest=proposal_digest
            )
        if response.get("proposal_digest") != proposal_digest:
            raise WorkspaceServiceError("INVALID_INPUT", "行动提案已变化，需要重新确认")
        if task["status"] not in {"proposed", "waiting_confirmation"}:
            if task["status"] == "executing":
                return self._sync_task(auth_token, task_id)
            return self._repair_terminal_formal_receipt(auth_token, task) or self._execution_response(auth_token, task_id)
        project = self.services.projects.get_project(auth_token, task["project_id"])
        if project["project_version"] != parameters["expected_project_version"]:
            return self._block_task(
                auth_token, task,
                WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目状态在提案后已变化；旧提案未执行"),
            )
        action = str(response["action"])
        confirmation_parameters = _formal_confirmation_parameters(action, parameters)
        binding = (
            proposal_digest
            if action == "apply_formal_script_revision"
            else str(
                parameters.get("plan_sha256")
                or parameters.get("expected_revision")
                or parameters.get("base_version_id")
                or parameters.get("retry_proposal_digest")
                or ""
            )
        )
        claim = self.services.idempotency.claim(
            auth_token, agent_session_id=task["agent_session_id"],
            tool_name=action, parameters=confirmation_parameters,
            internal_binding_sha256=binding,
        )
        if claim["response"] is not None:
            return self._execution_response(auth_token, claim["response"]["task_id"])
        if claim.get("replayed") and task["status"] != "proposed":
            return self._sync_task(auth_token, task_id)
        confirmation = self.services.confirmations.issue(
            auth_token, agent_session_id=task["agent_session_id"],
            tool_name=action, parameters=confirmation_parameters,
            internal_binding_sha256=binding,
        )
        self.services.confirmations.consume(
            auth_token, confirmation_token=confirmation["confirmation_token"],
            agent_session_id=task["agent_session_id"], tool_name=action,
            parameters=confirmation_parameters, internal_binding_sha256=binding,
        )
        if task["status"] == "proposed":
            task = self.services.tasks.transition_task(
                auth_token, task_id=task_id, expected_task_version=task["task_version"],
                status="waiting_confirmation",
                details={
                    "confirmation_id": confirmation["confirmation_id"],
                    "idempotency_record_id": claim["idempotency_record_id"],
                    "execution_task_id": task_id,
                },
            )
        task = self.services.tasks.transition_task(
            auth_token, task_id=task_id, expected_task_version=task["task_version"],
            status="executing",
            details={
                "confirmation_id": confirmation["confirmation_id"],
                "idempotency_record_id": claim["idempotency_record_id"],
                "execution_task_id": task_id,
            },
        )
        if action in {"apply_formal_image_revision", "record_formal_image_feedback"}:
            try:
                if self.script_revision is None or not self.script_revision.writes_enabled or self.read_only_deployment:
                    raise WorkspaceServiceError(
                        "FORMAL_IMAGE_REVISION_DISABLED", "Formal 图片修订能力当前关闭",
                    )
                public = (
                    self.script_revision.execute_image_feedback(
                        auth_token, project=project, parameters=parameters,
                    )
                    if action == "record_formal_image_feedback"
                    else self.script_revision.execute_image(
                        auth_token, project=project, parameters=parameters,
                    )
                )
                latest = self.services.tasks.get_task(auth_token, task_id)
                finished = self.services.tasks.transition_task(
                    auth_token, task_id=task_id,
                    expected_task_version=latest["task_version"],
                    status="completed", details={"response": public},
                )
                self.services.idempotency.record_result(
                    auth_token, idempotency_record_id=claim["idempotency_record_id"],
                    result=ToolExecutionResult(task_id=task_id, status="completed"),
                )
                return self._message_result(auth_token, finished, public)
            except WorkspaceServiceError as exc:
                latest = self.services.tasks.get_task(auth_token, task_id)
                terminal_response = self._repair_terminal_formal_receipt(auth_token, latest)
                if terminal_response is not None:
                    return terminal_response
                blocked = self._block_task(auth_token, latest, exc)
                self.services.idempotency.record_result(
                    auth_token, idempotency_record_id=claim["idempotency_record_id"],
                    result=ToolExecutionResult(
                        task_id=task_id, status="blocked",
                        error={"code": "INTERNAL_ERROR", "message": exc.message},
                    ),
                )
                return blocked
            except Exception:
                return self._finish_unexpected_formal_error(
                    auth_token, task=task, action=action, parameters=parameters,
                    record_id=claim["idempotency_record_id"],
                )
        try:
            if action == "apply_formal_script_revision":
                if self.script_revision is None or not self.script_revision.writes_enabled or self.read_only_deployment:
                    raise WorkspaceServiceError(
                        "FORMAL_SCRIPT_REVISION_DISABLED", "Formal 脚本修改能力当前关闭",
                    )
                public = self.script_revision.execute(
                    auth_token, project=project, parameters=parameters,
                )
                terminal = "completed"
            elif action == "apply_formal_fact_revision":
                if self.script_revision is None or not self.script_revision.writes_enabled or self.read_only_deployment:
                    raise WorkspaceServiceError(
                        "FORMAL_FACT_REVISION_DISABLED", "Formal 事实修订能力当前关闭",
                    )
                public = self.script_revision.execute_fact_revision(
                    auth_token, project=project, parameters=parameters,
                )
                terminal = "completed"
            elif action == "adopt_formal_default_images":
                if self.default_images is None or not self.default_images.writes_enabled or self.read_only_deployment:
                    raise WorkspaceServiceError(
                        "FORMAL_DEFAULT_IMAGE_ADOPTION_DISABLED",
                        "Formal 默认图片采用能力当前关闭",
                    )
                public = self.default_images.execute(
                    auth_token, project=project, parameters=parameters, task_id=task["task_id"],
                )
                terminal = "completed"
            elif action == "retry_formal_stage":
                if self.operations is None:
                    raise WorkspaceServiceError(
                        "FORMAL_STAGE_RETRY_UNAVAILABLE", "Formal 阶段重试服务未安装",
                    )
                public = self.operations.confirm_stage_retry(
                    auth_token,
                    workspace_id=parameters["workspace_id"],
                    proposal_id=parameters["retry_proposal_id"],
                    proposal_digest=parameters["retry_proposal_digest"],
                )
                public = self._stage_retry_response(auth_token, project["project_id"], public)
                if public["status"] == "failed":
                    raise WorkspaceServiceError(public["error_code"], public["summary"])
                terminal = "completed"
            elif action == "prepare_formal_plan":
                result = self.generation.prepare_plan(
                    auth_token, workspace_id=project["workspace_id"],
                    project_id=project["project_id"],
                    expected_project_version=parameters["expected_project_version"],
                    generation_mode=parameters["generation_mode"],
                    operation_key=task["task_id"],
                )
                public = {
                    "type": "formal_plan_prepared", "status": "waiting_user",
                    "plan_id": result["plan_id"],
                    "needs_clarification": result["needs_clarification"],
                    "confirmation_stage": "plan_review",
                    "draft_status": "waiting_user",
                    "result_version": None,
                    "next_href": self._next_href(project["project_id"], action),
                    "summary": "规划已保存，请在规划页核对覆盖矩阵。",
                }
                terminal = "completed"
            else:
                current = self.services.projects.get_project(auth_token, project["project_id"])
                if current["project_version"] != parameters["expected_project_version"]:
                    raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目状态在执行前已变化")
                plan_event = self.generation._plan_event(auth_token, parameters["plan_id"])
                if str(plan_event.get("plan_sha256") or "") != parameters["plan_sha256"]:
                    raise WorkspaceServiceError(
                        "PLAN_CONFIRMATION_MISMATCH", "规划 SHA 在提案后已变化；旧提案未执行"
                    )
                result = self.generation.confirm_plan_and_generate(
                    auth_token, workspace_id=project["workspace_id"],
                    project_id=project["project_id"], plan_id=parameters["plan_id"],
                    expected_project_version=parameters["expected_project_version"],
                )
                public = {
                    "type": "formal_generation_completed",
                    "status": "waiting_human_review",
                    "confirmation_stage": "waiting_human_review",
                    "draft_status": "saved",
                    "version_status": "draft",
                    "version_id": result["version_id"],
                    "pending_confirmations": result.get("pending_confirmations") or [],
                    "summary": "机器审核通过，等待人工复核。",
                }
                terminal = "waiting_human_review"
            finished = self.services.tasks.transition_task(
                auth_token, task_id=task_id, expected_task_version=task["task_version"],
                status=terminal, details={"response": public},
            )
            tool_result = ToolExecutionResult(task_id=task_id, status="completed")
            self.services.idempotency.record_result(
                auth_token, idempotency_record_id=claim["idempotency_record_id"],
                result=tool_result,
            )
            return self._message_result(auth_token, finished, public)
        except WorkspaceServiceError as exc:
            latest = self.services.tasks.get_task(auth_token, task_id)
            terminal_response = self._repair_terminal_formal_receipt(auth_token, latest)
            if terminal_response is not None:
                return terminal_response
            blocked = self._block_task(auth_token, latest, exc)
            self.services.idempotency.record_result(
                auth_token, idempotency_record_id=claim["idempotency_record_id"],
                result=ToolExecutionResult(
                    task_id=task_id, status="blocked",
                    error={"code": "INTERNAL_ERROR", "message": exc.message},
                ),
            )
            return blocked
        except Exception:
            return self._finish_unexpected_formal_error(
                auth_token, task=task, action=action, parameters=parameters,
                record_id=claim["idempotency_record_id"],
            )

    def _finish_unexpected_formal_error(
        self, auth_token: str, *, task: dict, action: str, parameters: dict,
        record_id: str,
    ) -> dict:
        """Close an interrupted response without retrying a possibly durable write."""
        _LOGGER.exception("Formal action failed: task=%s action=%s", task["task_id"], action)
        latest = self.services.tasks.get_task(auth_token, task["task_id"])
        terminal_response = self._repair_terminal_formal_receipt(auth_token, latest)
        if terminal_response is not None:
            return terminal_response
        reconciled = self._reconcile_formal_action(
            auth_token, task=task, action=action, parameters=parameters,
        )
        if reconciled is not None:
            return self._finish_reconciled_formal_action(
                auth_token, task=task, reconciled=reconciled, record_id=record_id,
            )
        public = {
            "type": "action_failed", "status": "failed",
            "title": "修改未完成", "error_code": "FORMAL_EXECUTION_FAILED",
            "summary": "本次操作未完成。请核对项目当前状态后重新提交。",
            "next_steps": ["查看当前脚本和版本记录；原任务不会重复执行。"],
        }
        latest = self.services.tasks.get_task(auth_token, task["task_id"])
        finished = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=latest["task_version"],
            status="failed", details={"response": public}, error_code="FORMAL_EXECUTION_FAILED",
        )
        self.services.idempotency.record_result(
            auth_token, idempotency_record_id=record_id,
            result=ToolExecutionResult(
                task_id=task["task_id"], status="failed",
                error={"code": "INTERNAL_ERROR", "message": public["summary"]},
            ),
        )
        return self._message_result(auth_token, finished, public)

    def _repair_terminal_formal_receipt(self, auth_token: str, task: dict) -> dict | None:
        """Fill only the trailing idempotency receipt of an already finished task."""
        if task["status"] not in {"completed", "waiting_human_review", "blocked", "failed"}:
            return None
        result = self._execution_response(auth_token, task["task_id"])
        events = self.services.tasks.list_task_events(auth_token, task["task_id"])
        record_id = _idempotency_record_id(events)
        if record_id:
            status = "completed" if task["status"] in {"completed", "waiting_human_review"} else task["status"]
            error = None if status == "completed" else {
                "code": "INTERNAL_ERROR", "message": result["response"]["summary"],
            }
            self.services.idempotency.record_result(
                auth_token, idempotency_record_id=record_id,
                result=ToolExecutionResult(task_id=task["task_id"], status=status, error=error),
            )
        return result

    def sync_task(self, auth_token: str, task_id: str) -> dict:
        """Reconcile direct Formal writes from persisted authority evidence."""
        with self._formal_task_execution(auth_token, task_id) as acquired:
            if not acquired:
                return self._execution_response(auth_token, task_id)
            return self._sync_task(auth_token, task_id)

    def _sync_task(self, auth_token: str, task_id: str) -> dict:
        task = self.services.tasks.get_task(auth_token, task_id)
        events = self.services.tasks.list_task_events(auth_token, task_id)
        if any(event.get('details', {}).get('workbench') for event in events):
            from .workbench_task_recovery import sync_workbench
            return sync_workbench(self, auth_token, task_id)
        if any(event.get('details', {}).get('internal_image_batch') for event in events):
            from .formal_image_batch import sync
            return sync(self.script_revision, auth_token, task_id=task_id)
        if task['task_type'] == 'formal_creative_draft' and self.generation is not None:
            # Creative generation/revision owns its own execution lock and
            # recovery view; these tasks never have a legacy JobManager job.
            project = self.services.projects.get_project(auth_token, task['project_id'])
            self.generation.read_creative_draft(auth_token, workspace_id=project['workspace_id'],
                                                project_id=project['project_id'])
            return self._execution_response(auth_token, task_id)
        response, parameters = _formal_proposal(events)
        if response is None or parameters is None:
            return super().sync_task(auth_token, task_id)
        if task["status"] != "executing":
            return self._repair_terminal_formal_receipt(auth_token, task) or self._execution_response(auth_token, task_id)
        action = str(response.get("action") or "")
        reconciled = self._reconcile_formal_action(
            auth_token, task=task, action=action, parameters=parameters,
        )
        if reconciled is None:
            blocked = self._recovery_block(auth_token, task)
            record_id = _idempotency_record_id(events)
            if record_id:
                self.services.idempotency.record_result(
                    auth_token, idempotency_record_id=record_id,
                    result=ToolExecutionResult(
                        task_id=task_id, status="blocked",
                        error={"code": "INTERNAL_ERROR", "message": blocked["response"]["summary"]},
                    ),
                )
            return blocked
        return self._finish_reconciled_formal_action(
            auth_token, task=task, reconciled=reconciled, record_id=_idempotency_record_id(events),
        )

    def _finish_reconciled_formal_action(
        self, auth_token: str, *, task: dict, reconciled: dict, record_id: str,
    ) -> dict:
        task_id = task["task_id"]
        latest = self.services.tasks.get_task(auth_token, task_id)
        terminal_response = self._repair_terminal_formal_receipt(auth_token, latest)
        if terminal_response is not None:
            return terminal_response
        status = reconciled.get("status")
        terminal = status if status in {"waiting_human_review", "failed", "blocked"} else "completed"
        finished = self.services.tasks.transition_task(
            auth_token, task_id=task_id, expected_task_version=latest["task_version"],
            status=terminal, details={"response": reconciled, "reconciled": True},
        )
        if record_id:
            self.services.idempotency.record_result(
                auth_token,
                idempotency_record_id=record_id,
                result=ToolExecutionResult(
                    task_id=task_id, status=terminal if terminal in {"failed", "blocked"} else "completed",
                    error={"code": "INTERNAL_ERROR", "message": reconciled["summary"]} if terminal in {"failed", "blocked"} else None,
                ),
            )
        return self._message_result(auth_token, finished, reconciled)

    def _reconcile_formal_action(
        self, auth_token: str, *, task: dict, action: str, parameters: dict,
    ) -> dict | None:
        project_id = str(task["project_id"])
        if action == "prepare_formal_plan":
            project = self.services.projects.get_project(auth_token, project_id)
            operation_key = str(task["task_id"])
            if parameters.get("expected_project_version") is None:
                return None
            for candidate in self.services.tasks.list_workspace_tasks(
                auth_token, project["workspace_id"], project_id=project_id,
            ):
                if candidate["task_id"] == task["task_id"] or candidate["task_type"] != "formal_generation_plan":
                    continue
                candidate_task = self.services.tasks.get_task(auth_token, candidate["task_id"])
                if (
                    candidate_task["project_id"] != project_id
                    or candidate_task["actor_id"] != task["actor_id"]
                ):
                    continue
                for event in reversed(self.services.tasks.list_task_events(auth_token, candidate["task_id"])):
                    details = event.get("details") or {}
                    if (
                        details.get("action") == "prepare_plan"
                        and details.get("generation_mode") == parameters.get("generation_mode")
                        and details.get("operation_key") == operation_key
                        and details.get("outer_task_id") == task["task_id"]
                        and details.get("project_id") == project_id
                        and str(details.get("expected_project_version")) == str(parameters.get("expected_project_version"))
                        and candidate["status"] in {"waiting_user", "completed"}
                    ):
                        return {
                            "type": "formal_plan_prepared",
                            "status": "waiting_user",
                            "plan_id": candidate["task_id"],
                            "needs_clarification": False,
                            "confirmation_stage": "plan_review",
                            "draft_status": "waiting_user",
                            "result_version": None,
                            "next_href": self._next_href(project_id, action),
                            "summary": "规划已保存，请在规划页核对覆盖矩阵。",
                        }
            return None
        if action == "confirm_formal_plan_generation":
            plan_id = str(parameters.get("plan_id") or "")
            if not plan_id:
                return None
            for event in reversed(self.services.tasks.list_task_events(auth_token, plan_id)):
                details = event.get("details") or {}
                version_id = str(details.get("version_id") or "")
                if details.get("action") == "generate_from_confirmed_plan" and version_id:
                    return {
                        "type": "formal_generation_completed",
                        "status": "waiting_human_review",
                        "confirmation_stage": "waiting_human_review",
                        "draft_status": "saved",
                        "version_status": "draft",
                        "result_version": version_id,
                        "version_id": version_id,
                        "next_href": self._next_href(project_id, action),
                        "pending_confirmations": [],
                        "summary": "机器审核通过，等待人工复核。",
                    }
            return None
        if action == "apply_formal_script_revision":
            return self._reconcile_script_revision(
                auth_token, task=task, parameters=parameters,
            )
        if action == "apply_formal_image_revision":
            return self._reconcile_image_version(
                auth_token, task=task, parameters=parameters,
            )
        if action == "record_formal_image_feedback":
            # The existing feedback authority has no idempotency-key or task
            # binding.  Matching by payload would falsely claim an identical
            # feedback created through another key, so this stays blocked.
            return None
        if action == "apply_formal_fact_revision":
            return self._reconcile_fact_revision(
                auth_token, task=task, parameters=parameters,
            )
        if action == "retry_formal_stage":
            return self._reconcile_stage_retry(
                auth_token, task=task, parameters=parameters,
            )
        if action == "adopt_formal_default_images":
            return self.default_images.read_adoption_receipt(
                auth_token, task_id=task["task_id"], parameters=parameters,
            ) if self.default_images is not None else None
        return None

    def _reconcile_script_revision(
        self, auth_token: str, *, task: dict, parameters: dict,
    ) -> dict | None:
        del auth_token
        key = str(parameters.get("idempotency_key") or "").strip()
        base_version_id = str(parameters.get("base_version_id") or "")
        if not key or not base_version_id:
            return None
        stored_keys = (key, f"{key}:version")
        with self.services.projects.store.connection() as connection:
            row = connection.execute(
                """SELECT materialized_version_id, base_version_id, revision
                FROM version_review_draft_submissions
                WHERE submitted_by_actor_id=? AND project_id=?
                  AND idempotency_key IN (?, ?)
                ORDER BY submitted_at DESC LIMIT 1""",
                (
                    task["actor_id"], task["project_id"],
                    *stored_keys,
                ),
            ).fetchone()
            if row is None and parameters.get("image_snapshot_digest"):
                row = connection.execute("""SELECT materialized_version_id, base_version_id, 0 AS revision
                    FROM version_image_decision_snapshots_v3
                    WHERE created_by_actor_id=? AND project_id=? AND idempotency_key IN (?, ?)
                      AND candidate_snapshot_sha256=? ORDER BY created_at DESC LIMIT 1""",
                    (task["actor_id"], task["project_id"], *stored_keys, parameters["image_snapshot_digest"])).fetchone()
            if row is None or str(row["base_version_id"]) != base_version_id:
                return None
            child = connection.execute(
                """SELECT version_id FROM project_versions
                WHERE version_id=? AND project_id=? AND parent_version_id=?""",
                (row["materialized_version_id"], task["project_id"], base_version_id),
            ).fetchone()
        if child is None:
            return None
        version_id = str(child["version_id"])
        return {
            "type": "formal_script_revision_completed",
            "status": "completed",
            "confirmation_stage": "completed",
            "draft_status": "saved",
            "version_status": "draft",
            "result_version": version_id,
            "version_id": version_id,
            "revision": int(row["revision"]),
            "next_href": self._next_href(task["project_id"], "apply_formal_script_revision"),
            "summary": "已按当前任务幂等键对账到既有脚本子版本，未重复创建版本。",
        }

    def _reconcile_image_version(
        self, auth_token: str, *, task: dict, parameters: dict,
    ) -> dict | None:
        del auth_token
        key = str(parameters.get("idempotency_key") or "").strip()
        base_version_id = str(parameters.get("base_version_id") or "")
        if not key or not base_version_id:
            return None
        row = None
        with self.services.projects.store.connection() as connection:
            for table in (
                "version_image_decision_snapshots_v3",
                "version_image_decision_snapshots_v2",
                "version_image_decision_snapshots",
            ):
                try:
                    row = connection.execute(
                        f"""SELECT materialized_version_id, base_version_id
                        FROM {table}
                        WHERE created_by_actor_id=? AND project_id=? AND idempotency_key=?""",
                        (task["actor_id"], task["project_id"], key),
                    ).fetchone()
                except Exception as exc:
                    if "no such table" not in str(exc).lower():
                        raise
                if row is not None:
                    break
            if row is None or str(row["base_version_id"]) != base_version_id:
                return None
            child = connection.execute(
                """SELECT version_id FROM project_versions
                WHERE version_id=? AND project_id=? AND parent_version_id=?""",
                (row["materialized_version_id"], task["project_id"], base_version_id),
            ).fetchone()
        if child is None:
            return None
        version_id = str(child["version_id"])
        return {
            "type": "formal_image_revision_completed",
            "status": "completed",
            "confirmation_stage": "completed",
            "draft_status": "saved",
            "version_status": "draft",
            "result_version": version_id,
            "version_id": version_id,
            "next_href": self._next_href(task["project_id"], "apply_formal_image_revision"),
            "summary": "已按当前任务图片幂等键对账到既有图片子版本，未重复创建版本。",
        }

    def _reconcile_fact_revision(
        self, auth_token: str, *, task: dict, parameters: dict,
    ) -> dict | None:
        del auth_token
        key = str(parameters.get("idempotency_key") or "").strip()
        if not key:
            return None
        fact_text = (
            f"{parameters.get('field', '')}: {parameters.get('value', '')}\n"
            f"特殊需求: 事实修订入口={parameters.get('entrypoint', '')}；"
            f"证据说明={parameters.get('evidence', '')}\n"
        ).encode("utf-8")
        document_id = f"source_doc_{hashlib.sha256(fact_text).hexdigest()[:20]}"
        with self.services.projects.store.connection() as connection:
            source = connection.execute(
                """SELECT source_document_id, sha256 FROM project_files
                WHERE project_id=? AND created_by_actor_id=? AND source_document_id=?
                  AND filename=? AND sha256=?""",
                (
                    task["project_id"], task["actor_id"], document_id,
                    f"事实修订-{_sha256_json({'idempotency_key': key})[:16]}.txt",
                    hashlib.sha256(fact_text).hexdigest(),
                ),
            ).fetchone()
            if source is None:
                return None
            rows = connection.execute(
                """SELECT brief_version_id, parent_brief_version_id,
                          source_document_ids_json
                FROM brief_versions
                WHERE project_id=? AND created_by_actor_id=?
                  AND parent_brief_version_id=?""",
                (
                    task["project_id"], task["actor_id"],
                    parameters.get("parent_brief_version_id"),
                ),
            ).fetchall()
            for row in rows:
                try:
                    source_ids = json.loads(row["source_document_ids_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
                if document_id not in source_ids:
                    continue
                feedback_rows = connection.execute(
                    """SELECT feedback_id, payload_ref, payload_sha256
                    FROM formal_version_feedback_records
                    WHERE project_id=? AND base_version_id=? AND feedback_kind='fact'
                      AND child_version_id IS NULL AND created_by_actor_id=?""",
                    (
                        task["project_id"], parameters.get("base_version_id"),
                        task["actor_id"],
                    ),
                ).fetchall()
                brief_version_id = str(row["brief_version_id"])
                for feedback in feedback_rows:
                    try:
                        payload = self.services.brief_store.read(
                            feedback["payload_ref"], feedback["payload_sha256"],
                        )
                    except WorkspaceServiceError:
                        continue
                    if (
                        payload.get("feedback_kind") != "fact"
                        or payload.get("base_version_id") != parameters.get("base_version_id")
                        or payload.get("parent_brief_version_id") != parameters.get("parent_brief_version_id")
                        or payload.get("child_brief_version_id") != brief_version_id
                        or payload.get("entrypoint") != parameters.get("entrypoint")
                        or payload.get("field") != parameters.get("field")
                        or payload.get("value") != parameters.get("value")
                        or payload.get("evidence") != parameters.get("evidence")
                    ):
                        continue
                    break
                else:
                    continue
                return {
                    "type": "formal_fact_revision_completed",
                    "status": "completed",
                    "confirmation_stage": "waiting_human_review",
                    "draft_status": "saved",
                    "version_status": "draft",
                    "result_version": brief_version_id,
                    "brief_version_id": brief_version_id,
                    "brief_confirmation_required": True,
                    "brief_url": f"/projects/{task['project_id']}/brief",
                    "next_href": self._next_href(task["project_id"], "apply_formal_fact_revision"),
                    "summary": "已按当前任务事实输入对账到既有 Brief 子版本，未重复导入。",
                }
        return None

    def _reconcile_stage_retry(
        self, auth_token: str, *, task: dict, parameters: dict,
    ) -> dict | None:
        proposal_id = str(parameters.get("retry_proposal_id") or "").strip()
        key = str(parameters.get("idempotency_key") or "").strip()
        target_task_id = str(parameters.get("target_task_id") or "").strip()
        if not proposal_id or not key or not target_task_id:
            return None
        with self.services.projects.store.connection() as connection:
            row = connection.execute(
                """SELECT p.task_id, p.project_id, p.requested_stage_id,
                          e.new_attempt_id
                FROM recovery_proposals p
                JOIN recovery_executions e ON e.recovery_proposal_id=p.recovery_proposal_id
                WHERE p.recovery_proposal_id=? AND p.task_id=? AND p.project_id=?
                  AND p.idempotency_key=? AND p.action='retry_current_stage'""",
                (proposal_id, target_task_id, task["project_id"], key),
            ).fetchone()
        if row is None:
            return None
        if self.operations is None:
            return None
        project = self.services.projects.get_project(auth_token, task["project_id"])
        result = self.operations.confirm_stage_retry(
            auth_token, workspace_id=project["workspace_id"], proposal_id=proposal_id,
            proposal_digest=parameters["retry_proposal_digest"],
        )
        return self._stage_retry_response(auth_token, task["project_id"], result)

    def _stage_retry_response(self, auth_token: str, project_id: str, result: dict) -> dict:
        execution = result.get("execution") or {}
        failed = execution.get("terminal_status") == "failed"
        succeeded = execution.get("terminal_status") == "succeeded"
        self.services.projects.get_project(auth_token, project_id)
        result_version = None
        if succeeded:
            matches = [
                version for version in self.services.projects.list_project_versions(auth_token, project_id)
                if version.get("artifact_ref") == execution.get("output_ref")
            ]
            if len(matches) == 1:
                version = matches[0]
                artifact = self.services.artifacts.resolve_artifact(version["run_id"], "workbook")
                if (
                    (artifact.ref, artifact.sha256) != (version["artifact_ref"], version["content_sha256"])
                    or (execution.get("output_sha256") and execution["output_sha256"] != artifact.sha256)
                ):
                    raise WorkspaceServiceError("ARTIFACT_INVALID", "重试回执与已保存脚本不一致")
                result_version = version["version_id"]
            elif execution.get("stage_id") == "formal_generation_plan":
                raise WorkspaceServiceError("ARTIFACT_INVALID", "重试回执缺少唯一的已保存脚本")
        return {
            **result,
            "type": "action_failed" if failed else "formal_stage_retry_completed" if succeeded else result["type"],
            "status": "failed" if failed else "completed" if succeeded else "accepted",
            "summary": "本次重试未完成，请查看失败原因后重试。" if failed else "重试已完成，脚本已保存，可继续核对配图和人工审核。" if result_version else "重试已完成，可查看执行结果。" if succeeded else "重试已提交，等待执行结果。",
            "error_code": str(execution.get("error_code") or "FORMAL_RETRY_FAILED") if failed else None,
            "result_version": result_version,
            "next_href": f"/projects/{project_id}/script" if succeeded else self._next_href(project_id, "retry_formal_stage"),
            "admin_recovery_used": False,
        }

    def _propose_stage_retry(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        project: dict,
        stage_id: str,
        message: str,
        force_requested: bool,
    ) -> dict:
        task = self.services.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type="formal_stage_retry",
            payload_sha256=hashlib.sha256(message.encode("utf-8")).hexdigest(),
            expected_project_version=project["project_version"],
        )
        task = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={"formal_context_version": project["project_version"]},
        )
        if force_requested:
            response = {
                "type": "admin_handoff",
                "title": "需要管理员受控恢复",
                "summary": "强制恢复、检查点继续和安全重置不属于普通阶段重试。",
                "error_code": "ADMIN_RECOVERY_REQUIRED",
                "admin_path": "/admin",
                "will_change": "无",
                "next_steps": ["请由管理员在诊断页查看 attempt 与 checkpoint 后创建恢复提案。"],
            }
            executing = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="executing",
                details={"response": response, "read_only": True},
            )
            finished = self.services.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=executing["task_version"], status="completed",
                details={"response": response, "read_only": True},
            )
            return self._message_result(auth_token, finished, response)
        readiness = self.action_readiness()["retry_formal_stage"]
        if not readiness["available"]:
            return self._block_task(
                auth_token,
                task,
                WorkspaceServiceError(
                    str(readiness["code"]), "当前部署未开放普通阶段重试写入",
                ),
            )
        if self.operations is None:
            return self._block_task(
                auth_token, task,
                WorkspaceServiceError(
                    "FORMAL_STAGE_RETRY_UNAVAILABLE", "Formal 阶段重试服务未安装",
                ),
            )
        try:
            retry = self.operations.propose_stage_retry(
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                requested_stage_id=stage_id,
                reason=f"用户确认前预览：重试 {stage_id}",
                expected_project_version=project["project_version"],
                idempotency_key=f"agent-stage-retry-{task['task_id']}",
                excluding_task_id=task["task_id"],
            )
        except WorkspaceServiceError as exc:
            return self._block_task(auth_token, task, exc)
        parameters = {
            "workspace_id": project["workspace_id"],
            "project_id": project["project_id"],
            "expected_project_version": project["project_version"],
            "retry_proposal_id": retry["proposal_id"],
            "retry_proposal_digest": retry["proposal_digest"],
            "requested_stage_id": retry["requested_stage_id"],
            "target_task_id": retry["task_id"],
            "idempotency_key": f"agent-stage-retry-{task['task_id']}",
        }
        response = {
            "type": "action_proposal",
            "title": "确认普通阶段重试",
            "summary": "按已确认规划重试生成脚本。" if stage_id == "formal_generation_plan" else "重试当前失败阶段。",
            "action": "retry_formal_stage",
            "proposal_digest": retry["proposal_digest"],
            "risk": "R2",
            "before": retry["before"],
            "after": retry["after"],
            "affected_fields": ["task_attempt"],
            "rerun_nodes": [retry["requested_stage_id"]],
            "confirmation_required": True,
            "will_change": "重新生成并保存脚本子版本" if stage_id == "formal_generation_plan" else "执行当前失败阶段的重试",
            "will_not_change": "不执行强制恢复、checkpoint 回滚或安全重置；不覆盖原任务、版本和产物",
            "calls_models": stage_id == "formal_generation_plan",
            "model_call_statement": "确认后按已确认规划调用写稿和编辑模型。" if stage_id == "formal_generation_plan" else "执行时按原阶段配置运行。",
            "impact_scope": "当前项目／当前失败阶段",
            "next_steps": ["确认后进入任务队列；强制恢复仍只能从管理员页发起。"],
        }
        proposed = self.services.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="proposed",
            details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._message_result(auth_token, proposed, response)

    def _decision(self, message: str, context: dict) -> FormalAdvisorDecision:
        if self.advisor is not None:
            return self.advisor.decide(message, context)
        # These two actions have an unambiguous, server-owned shape. They do
        # not need the model-based advisor to invent parameters, so a missing
        # advisor Provider must not close the deterministic local path.
        if context.get("requested_generation_mode") and re.search(r"准备.*规划", message):
            return FormalAdvisorDecision(
                intent="prepare_plan",
                summary="已按明确的图片策略准备 Formal 规划提案。",
                evidence_ids=context["evidence_ids"],
                questions=[], candidate_action="prepare_formal_plan",
            )
        if _CONFIRM_REQUEST.search(message):
            return FormalAdvisorDecision(
                intent="confirm_generation",
                summary="已请求确认当前 Formal 规划并生成脚本。",
                evidence_ids=context["evidence_ids"],
                questions=[], candidate_action="confirm_formal_plan_generation",
            )
        if _WRITE_REQUEST.search(message):
            raise WorkspaceServiceError(
                "FORMAL_ADVISOR_UNAVAILABLE",
                "生产协作 Provider 或凭据不可用；当前请求无法安全分类，未执行业务写入",
            )
        intent = "explain_issue" if re.search(r"为什么|问题|阻断|失败", message) else "explain_status"
        return FormalAdvisorDecision(
            intent=intent, summary="已读取当前项目的确定性状态。",
            evidence_ids=context["evidence_ids"], questions=[], candidate_action="none",
        )

    def _context(
        self, auth_token: str, project: dict, *, agent_session_id: str = "",
    ) -> dict:
        evidence_ids = [
            f"project:{project['project_id']}:v{project['project_version']}",
        ]
        blockers = []
        context = {
            "project_id": project["project_id"],
            "project_version": project["project_version"],
            "project": {key: project[key] for key in ("name", "category_id", "status") if key in project},
            "brief_version_id": "", "plan": None,
            "blockers": blockers, "evidence_ids": evidence_ids,
            "read_errors": [],
            "conversation": self._recent_conversation(auth_token, project, agent_session_id),
        }

        def read_section(name, reader):
            try:
                return reader()
            except (WorkspaceServiceError, ArtifactResolutionError) as exc:
                # A missing optional read must not become a new generation gate.
                context["read_errors"].append({"section": name, "code": exc.code})
                return {}

        scope = {"workspace_id": project["workspace_id"], "project_id": project["project_id"]}
        creative_reader = getattr(self.generation, "read_creative_draft", None)
        creative = read_section("creative", lambda: creative_reader(auth_token, **scope)) if callable(creative_reader) else {}
        if not callable(creative_reader):
            context["read_errors"].append({"section": "creative", "code": "MODULE_UNAVAILABLE"})
        context["creative"] = {key: creative[key] for key in ("revision", "draft_revision", "protected_block_ids", "task", "task_kind", "image_references") if key in creative}
        context["creative"]["blocks"] = creative.get("blocks", [])
        context["creative"]["block_count"] = len(creative.get("blocks", [])) if creative else None
        adapter = getattr(self.script_revision, "brief_adapter", None)
        if adapter is not None:
            brief = read_section("brief", lambda: adapter.read_current_formal(auth_token, **scope))
            context["brief"] = {
                "confirmation_status": brief.get("confirmation_status"),
                "facts": (brief.get("brief") or {}).get("facts") or [],
                "conflicts": (brief.get("brief") or {}).get("conflicts") or [],
                "warnings": (brief.get("brief") or {}).get("warnings") or [],
            }
        workspace = getattr(self.script_revision, "workspace", None)
        if workspace is not None:
            script = read_section("script", lambda: workspace.read_current(auth_token, **scope))
            context["script"] = {
                key: script[key] for key in (
                    "state", "message", "version_label", "revision", "shot_count", "can_save_version",
                ) if key in script
            }
            context["script"]["shots"] = [{
                key: shot[key] for key in (
                    "shot_no", "structure", "camera_type", "shot_size", "camera_move",
                    "duration_seconds", "visual", "sound", "voiceover", "subtitle", "notes",
                ) if key in shot
            } for shot in script.get("shots") or []]
            if script.get("shots"):
                evidence_ids.append(f"script:{project['project_id']}:v{project['project_version']}:r{script.get('revision', 0)}")
        image_workspace = getattr(self.script_revision, "image_workspace", None)
        if image_workspace is not None:
            images = read_section("images", lambda: image_workspace.read_current(
                auth_token, **scope, expected_project_version=project["project_version"],
            ))
            context["images"] = {
                key: images[key] for key in (
                    "state", "message", "version_label", "shot_count", "available_image_count",
                    "usable_image_count", "pending_image_count", "low_confidence_image_count",
                    "blocked_image_count", "failed_image_count", "review_eligible", "delivery_eligible",
                    "pending_reconfirmation_shot_nos", "unavailable_asset_warning",
                ) if key in images
            }
            context["images"]["shots"] = [{
                key: shot[key] for key in (
                    "shot_no", "confirmed", "decision_kind", "temporary_reason", "reuse_reason",
                    "image_reconfirmation_required", "gap_reason",
                ) if key in shot
            } for shot in images.get("shots") or []]
        if self.review_workspace is not None:
            review = read_section("review", lambda: self.review_workspace.read_current(
                auth_token, **scope, expected_project_version=project["project_version"],
            ))
            context["review"] = {
                key: review[key] for key in (
                    "state", "message", "version_label", "decision", "decision_counts",
                    "review_started", "ready_for_final", "next_action", "edit_actions",
                ) if key in review
            }
            context["review"]["issues"] = [{
                key: issue[key] for key in (
                    "shot_no", "kind", "severity", "status", "note", "reason", "title", "summary",
                ) if key in issue
            } for issue in review.get("issues") or []]
        try:
            confirmed = self.generation.current_confirmed_brief(
                auth_token, workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                expected_project_version=project["project_version"],
            )
        except WorkspaceServiceError as exc:
            blockers.append({"code": exc.code, "message": exc.message})
            return context
        context["brief_version_id"] = confirmed.brief_version_id
        try:
            waiting = self.generation.list_waiting_plans(
                auth_token, workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                expected_project_version=project["project_version"],
            ).get("items") or []
        except WorkspaceServiceError as exc:
            blockers.append({"code": exc.code, "message": exc.message})
            waiting = []
        plan = None
        if waiting:
            try:
                plan = self.generation.get_plan_preview(
                    auth_token, workspace_id=project["workspace_id"],
                    project_id=project["project_id"], plan_id=waiting[0]["plan_id"],
                    expected_project_version=project["project_version"],
                )
            except WorkspaceServiceError as exc:
                blockers.append({"code": exc.code, "message": exc.message})
        evidence_ids.append(f"brief:{confirmed.brief_version_id}")
        if plan:
            evidence_ids.append(f"plan:{plan['plan_id']}")
            evidence_ids.extend(
                f"requirement:{item['requirement_id']}" for item in plan.get("coverage") or []
            )
            evidence_ids.extend(
                f"trace:{item['requirement_id']}" for item in plan.get("traceability") or []
            )
        context["plan"] = plan
        image_reader = getattr(self.generation, "get_image_readiness", None)
        project_images = read_section("project_images", lambda: image_reader(
            auth_token, **scope, expected_project_version=project["project_version"])) if callable(image_reader) else {}
        if not callable(image_reader):
            context["read_errors"].append({"section": "project_images", "code": "MODULE_UNAVAILABLE"})
        context["project_images"] = {key: value for key, value in project_images.items() if key != "images"}
        if project_images:
            evidence_ids.append(f"project-images:{project['project_id']}:v{project['project_version']}")
        return context

    def _recent_conversation(self, auth_token: str, project: dict, agent_session_id: str) -> list[dict]:
        if not agent_session_id:
            return []
        session = self.services.tasks.get_agent_session(auth_token, agent_session_id)
        if session["project_id"] != project["project_id"]:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "会话不属于当前项目")
        tasks = self.services.tasks.list_agent_session_tasks(auth_token, agent_session_id)[-6:]
        history = []
        for task in tasks:
            events = self.services.tasks.list_task_events(auth_token, task["task_id"])
            instruction = next((
                event["details"].get("user_message") or event["details"].get("assistant_instruction")
                for event in events
                if event.get("details", {}).get("user_message") or event.get("details", {}).get("assistant_instruction")
            ), "")
            response = next((
                event["details"]["response"] for event in reversed(events)
                if isinstance(event.get("details", {}).get("response"), dict)
            ), {})
            if instruction:
                history.append({"role": "user", "content": _redact_runtime_secrets(str(instruction))[:1000]})
            if response.get("summary"):
                history.append({
                    "role": "assistant", "content": _redact_runtime_secrets(str(response["summary"]))[:1000],
                    "status": task["status"],
                })
        return history

    @staticmethod
    def _read_response(decision: FormalAdvisorDecision, context: dict) -> dict:
        plan = context.get("plan") or {}
        return {
            "type": "formal_status" if decision.intent == "explain_status" else "formal_issue_explanation",
            "title": "Formal 项目状态" if decision.intent == "explain_status" else "Formal 问题说明",
            "summary": decision.summary,
            "evidence_ids": decision.evidence_ids,
            "plan_status": (plan.get("audit") or {}).get("status"),
            "project": context.get("project") or {},
            "creative": context.get("creative") or {},
            "project_images": context.get("project_images") or {},
            "script": {key: value for key, value in (context.get("script") or {}).items() if key != "shots"},
            "images": {key: value for key, value in (context.get("images") or {}).items() if key != "shots"},
            "review": {key: value for key, value in (context.get("review") or {}).items() if key != "issues"},
            "read_errors": context.get("read_errors") or [],
            "coverage": plan.get("coverage") or [],
            "capacity_report": plan.get("capacity_report") or {},
            "traceability": plan.get("traceability") or [],
            "template_rule_contract": plan.get("template_rule_contract") or {},
            "conflicts": plan.get("conflicts") or [],
            "decision_counts": plan.get("decision_counts") or {},
            "field_decisions": [
                {
                    "shot_no": shot.get("shot_no"),
                    "decisions": shot.get("field_decisions") or [],
                }
                for shot in plan.get("shots") or []
            ],
            "blockers": context.get("blockers") or [],
            "needs_clarification": bool(plan.get("needs_clarification")),
            "next_steps": decision.questions,
            "next_href": f"/projects/{context['project_id']}/overview",
            "next_label": "查看项目进度",
        }

    def _authoritative_action(
        self, auth_token: str, project: dict, decision: FormalAdvisorDecision,
        context: dict,
    ) -> tuple[str, dict, dict]:
        if decision.intent == "propose_script_revision":
            raise WorkspaceServiceError(
                "FORMAL_SCRIPT_REVISION_UNAVAILABLE", "脚本修改意图未进入专用提案服务",
            )
        if decision.intent == "prepare_plan":
            if not context.get("brief_version_id"):
                blocker = (context.get("blockers") or [{}])[0]
                raise WorkspaceServiceError(
                    str(blocker.get("code") or "CONFIRMED_BRIEF_REQUIRED"),
                    str(blocker.get("message") or "当前项目尚无已确认 Brief"),
                )
            requested_mode = context.get("requested_generation_mode")
            if requested_mode not in {"image_backed", "text_only_draft"}:
                raise WorkspaceServiceError(
                    "IMAGE_STRATEGY_CONFIRMATION_REQUIRED",
                    "请明确选择带图片生成，或只生成文字草稿",
                )
            parameters = {
                "project_id": project["project_id"],
                "expected_project_version": project["project_version"],
                "generation_mode": requested_mode,
                "idempotency_key": f"formal-plan-{project['project_id']}-v{project['project_version']}",
            }
            return "prepare_formal_plan", parameters, {
                "project_id": project["project_id"],
                "expected_project_version": project["project_version"],
                "generation_mode": requested_mode,
            }
        waiting = self.generation.list_waiting_plans(
            auth_token, workspace_id=project["workspace_id"],
            project_id=project["project_id"],
            expected_project_version=project["project_version"],
        ).get("items") or []
        if not waiting:
            raise WorkspaceServiceError("PLAN_CONFIRMATION_REQUIRED", "当前项目没有待确认规划")
        plan_id = waiting[0]["plan_id"]
        preview = self.generation.get_plan_preview(
            auth_token, workspace_id=project["workspace_id"],
            project_id=project["project_id"], plan_id=plan_id,
            expected_project_version=project["project_version"],
        )
        if preview.get("needs_clarification"):
            raise WorkspaceServiceError(
                "PLANNING_NEEDS_CLARIFICATION",
                "当前规划仍有待确认事项；智能体只解释冲突，不会提议生成脚本",
            )
        event = self.generation._plan_event(auth_token, plan_id)
        parameters = {
            "project_id": project["project_id"], "plan_id": plan_id,
            "plan_sha256": str(event["plan_sha256"]),
            "expected_project_version": project["project_version"],
            "idempotency_key": f"formal-generate-{plan_id}-v{project['project_version']}",
        }
        return "confirm_formal_plan_generation", parameters, {
            "project_id": project["project_id"], "plan_id": plan_id,
            "expected_project_version": project["project_version"],
        }


def _formal_confirmation_parameters(action: str, parameters: dict) -> dict:
    if action != "apply_formal_script_revision":
        return parameters
    sanitized = dict(parameters)
    # Selection reasons and stable identities remain server-owned and are bound
    # by the full proposal digest supplied to the confirmation service.
    sanitized.pop("assistant_selection", None)
    sanitized.pop("affected_shots", None)
    if isinstance(parameters.get("operation"), dict):
        sanitized["operation"] = {
            key: value for key, value in parameters["operation"].items()
            if key in {"operation", "shot_no", "field", "value"}
        }
    if isinstance(parameters.get("operations"), list):
        sanitized["operations"] = [
            {
                key: value for key, value in operation.items()
                if key in {"operation", "shot_no", "field", "value"}
            }
            for operation in parameters["operations"]
            if operation.get("operation") != "source_rebind"
        ]
    return sanitized


def _formal_proposal(events: list[dict]) -> tuple[dict | None, dict | None]:
    for event in reversed(events):
        response = event.get("details", {}).get("response")
        parameters = event.get("details", {}).get("internal_formal_parameters")
        if isinstance(response, dict) and response.get("action") in {
            "prepare_formal_plan", "confirm_formal_plan_generation",
            "apply_formal_script_revision", "apply_formal_image_revision",
            "record_formal_image_feedback", "apply_formal_fact_revision",
            "adopt_formal_default_images", "retry_formal_stage",
        } and isinstance(parameters, dict):
            return response, parameters
    return None, None


def _idempotency_record_id(events: list[dict]) -> str:
    for event in reversed(events):
        value = (event.get("details") or {}).get("idempotency_record_id")
        if value:
            return str(value)
    return ""


def _public_formal_response(response: dict | None, project_id: str | None) -> dict | None:
    if not isinstance(response, dict):
        return response
    result = dict(response)
    if result.get("type") == "admin_handoff":
        result.setdefault("next_href", "/admin")
        result.setdefault("next_label", "前往管理员诊断")
    legacy_prefix = f"/formal/{project_id}/"
    if str(result.get("next_href") or "").startswith(legacy_prefix):
        result["next_href"] = f"/projects/{project_id}/" + result["next_href"][len(legacy_prefix):]
    if result.get("analysis_task_id"):
        task_id = quote(str(result["analysis_task_id"]), safe="")
        result.setdefault("next_href", f"/projects/{project_id}/script?analysis_task_id={task_id}")
        result.setdefault("next_label", "继续修改脚本")
    if result.get("type") == "needs_clarification" and str(result.get("shot_no") or "").isdigit():
        shot = str(result["shot_no"])
        prompts = {
            "delete_shot": f"删除第 {shot} 镜",
            "clear_optional_field": f"清空第 {shot} 镜的[旁白/字幕/备注]",
            "replace_field": f"把第 {shot} 镜的[字段]修改为[新内容]",
        }
        result["choices"] = [
            {**choice, "instruction": prompts[choice["key"]]}
            if choice.get("key") in prompts else choice
            for choice in result.get("choices") or []
        ]
    action = str(result.get("action") or "")
    if action and result.get("type") == "action_proposal":
        result.setdefault("confirmation_stage", "confirmation_required")
        result.setdefault("next_href", FormalExecutableAgentService._next_href(str(project_id or ""), action))
    if result.get("status") == "waiting_human_review":
        result.setdefault("confirmation_stage", "waiting_human_review")
    elif result.get("status") == "completed" and result.get("type", "").startswith("formal_"):
        result.setdefault("confirmation_stage", "completed")
    if "version_id" in result and "result_version" not in result:
        result["result_version"] = result["version_id"]
    if result.get("type") == "formal_plan_prepared":
        result.setdefault("confirmation_stage", "plan_review")
        result.setdefault("draft_status", "waiting_user")
        result.setdefault("next_href", FormalExecutableAgentService._next_href(str(project_id or ""), "prepare_formal_plan"))
    result_type = str(result.get("type") or "")
    result_action = {
        "formal_generation_completed": "confirm_formal_plan_generation",
        "formal_script_revision_completed": "apply_formal_script_revision",
        "formal_script_draft_updated": "apply_formal_script_revision",
        "formal_image_revision_completed": "apply_formal_image_revision",
        "formal_image_feedback_completed": "record_formal_image_feedback",
        "formal_stage_retry_completed": "retry_formal_stage",
        "formal_fact_revision_completed": "apply_formal_fact_revision",
        "formal_default_images_adopted": "adopt_formal_default_images",
    }.get(result_type)
    if result_action:
        result.setdefault(
            "next_href",
            FormalExecutableAgentService._next_href(str(project_id or ""), result_action),
        )
        if result_type == "formal_script_draft_updated":
            result.setdefault("draft_status", "editing")
        elif result_type not in {"formal_generation_completed", "formal_image_feedback_completed"}:
            result.setdefault("draft_status", "saved")
    return result


def _sha256_json(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
