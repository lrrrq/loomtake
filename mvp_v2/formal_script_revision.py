"""Two-step, project-authoritative Formal script revision proposals."""

from __future__ import annotations

from io import BytesIO
import json
import re
from typing import Any

from .brief_artifact_store import BriefArtifactStore
from .formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from .formal_script_workspace import FormalScriptWorkspaceService, _EDITABLE_FIELDS
from .formal_script_assistant import (
    FormalScriptAssistantEditorPolicy,
    analyze_formal_script_instruction,
    required_wide_shot_ids,
)
from .formal_image_workspace import FormalImageWorkspaceService, ImageShotSelection
from .project_service import ProjectService
from .run_contract import sha256_json
from .task_service import TaskService
from .workspace_store import WorkspaceServiceError, new_id, utc_now


_REPLACE = re.compile(
    r"^(?:把|将)?第?\s*(\d+)\s*镜(?:的)?\s*"
    r"(画面描述|画面|声音|旁白|字幕|时长|备注|结构|类型|景别|运镜)\s*"
    r"(?:修改|改|调整|替换)(?:为|成)\s*[：:]?\s*(.+?)\s*$"
)
_CLEAR = re.compile(r"^(?:把|将)?第?\s*(\d+)\s*镜(?:的)?\s*(旁白|字幕|备注)\s*(?:删除|清空|去掉)\s*$")
_DELETE = re.compile(r"^(?:把|将)?第?\s*(\d+)\s*镜(?:整镜|镜头)?\s*(?:删除|删掉|移除)\s*$")
_CLEAR_PREFIX = re.compile(r"^(?:删除|清空|去掉)第?\s*(\d+)\s*镜(?:的)?\s*(旁白|字幕|备注)\s*$")
_DELETE_PREFIX = re.compile(r"^(?:删除|删掉|移除)第?\s*(\d+)\s*镜(?:整镜|镜头)?\s*$")
_AMBIGUOUS_DELETE = re.compile(r"^(?:把|将)?第?\s*(\d+)\s*镜(?:的)?\s*(?:内容)?\s*(?:删除|删掉|清空)\s*$")
_COMMAND_START = re.compile(
    r"^(?:(?:把|将)?第?\s*\d+\s*镜|(?:删除|删掉|移除|清空|去掉)\s*第?\s*\d+\s*镜)"
)


def _explicit_instruction_parts(text: str) -> list[str]:
    """Separate explicit commands without splitting quoted subtitle content."""
    pairs = {'"': '"', '“': '”', '「': '」', '『': '』'}
    closing = ""
    start = 0
    parts = []
    for index, char in enumerate(text):
        if closing:
            if char == closing and (index == 0 or text[index - 1] != "\\"):
                closing = ""
        elif char in pairs:
            closing = pairs[char]
        elif char in ";；\n" and _COMMAND_START.match(text[index + 1:].lstrip()):
            parts.append(text[start:index].strip())
            start = index + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


_FIELDS = {
    "结构": "structure", "时长": "duration_seconds", "画面": "visual",
    "类型": "camera_type", "景别": "shot_size", "运镜": "camera_move",
    "画面描述": "visual", "声音": "sound", "旁白": "voiceover",
    "字幕": "subtitle", "备注": "notes",
}
_FACT_FIELDS = frozenset({
    "项目名称", "品牌或客户", "脚本类型", "行业或活动类别", "视频目标",
    "使用场景", "目标时长", "必须出现的内容", "禁止或不能出现的内容",
    "关键事件与流程", "特殊需求",
})


class FormalRevisionService:
    def propose_image_batch(self, auth_token: str, **kwargs) -> dict:
        from .formal_image_batch import propose
        return propose(self, auth_token, **kwargs)

    def start_image_batch(self, auth_token: str, **kwargs) -> dict:
        from .formal_image_batch import start
        return start(self, auth_token, **kwargs)

    def execute_image_batch(self, auth_token: str, **kwargs) -> dict:
        from .formal_image_batch import execute
        return execute(self, auth_token, **kwargs)

    def confirm_image_batch(self, auth_token: str, **kwargs) -> dict:
        from .formal_image_batch import confirm
        return confirm(self, auth_token, **kwargs)

    def read_image_batch(self, auth_token: str, **kwargs) -> dict:
        from .formal_image_batch import read
        return read(self, auth_token, **kwargs)

    def revise_image_batch(self, auth_token: str, **kwargs) -> dict:
        from .formal_image_batch import revise
        return revise(self, auth_token, **kwargs)

    def cancel_image_batch(self, auth_token: str, **kwargs) -> dict:
        from .formal_image_batch import cancel
        return cancel(self, auth_token, **kwargs)

    def __init__(
        self,
        *,
        projects: ProjectService,
        tasks: TaskService,
        workspace: FormalScriptWorkspaceService,
        writes_enabled: bool,
        image_workspace: FormalImageWorkspaceService | None = None,
        brief_adapter: FormalBriefInputAdapter | None = None,
        brief_store: BriefArtifactStore | None = None,
        assistant_editor_policy: FormalScriptAssistantEditorPolicy | None = None,
    ) -> None:
        self.projects = projects
        self.tasks = tasks
        self.workspace = workspace
        self.writes_enabled = writes_enabled
        self.image_workspace = image_workspace
        self.brief_adapter = brief_adapter
        self.brief_store = brief_store
        self.assistant_editor_policy = assistant_editor_policy

    def apply_creative_draft_change(self, auth_token: str, **kwargs) -> None:
        from .creative_revision import apply
        apply(self, auth_token, **kwargs)

    def propose_creative_draft_change(self, auth_token: str, **kwargs) -> None:
        from .creative_revision import propose
        return propose(self, auth_token, **kwargs)

    def confirm_creative_draft_change(self, auth_token: str, **kwargs) -> None:
        from .creative_revision import confirm
        confirm(self, auth_token, **kwargs)

    def set_creative_protection(self, auth_token: str, **kwargs) -> None:
        from .creative_revision import protect
        protect(self, auth_token, **kwargs)

    def answer_creative_input(self, auth_token: str, **kwargs) -> None:
        from .creative_revision import answer
        answer(self, auth_token, **kwargs)

    def consume_creative_answers(self, auth_token: str, **kwargs) -> bool:
        from .creative_revision import consume_answers
        return consume_answers(self, auth_token, **kwargs)

    def read_task_context(self, auth_token: str, *, project_id: str, task_id: str) -> dict:
        project = self.projects.get_project(auth_token, project_id)
        task = self.tasks.get_task(auth_token, task_id)
        if task["project_id"] != project_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "任务不属于当前项目")
        _, parameters = _stored_proposal(self.tasks.list_task_events(auth_token, task_id))
        base_version_id = None
        shots = []
        current = False
        if parameters and parameters.get("project_id") == project_id:
            candidate_id = parameters.get("base_version_id")
            if candidate_id:
                version = self.projects.get_project_version(auth_token, candidate_id)
                if version["project_id"] != project_id:
                    raise WorkspaceServiceError("PERMISSION_DENIED", "任务基准版本不属于当前项目")
                base_version_id = candidate_id
                current = base_version_id == project.get("current_version_id")
            if current:
                model = self.workspace.read_current(auth_token, workspace_id=project["workspace_id"], project_id=project_id)
                requested = {str(item.get("shot_id") or "") for item in parameters.get("operations", [])}
                shots = [{"shot_id": shot["shot_id"], "shot_no": shot["shot_no"]}
                         for shot in model.get("shots", []) if shot["shot_id"] in requested]
        fields = TaskService.employee_task_fields(task, project_id)
        return {"task_id": task_id, "project_id": project_id, "base_version_id": base_version_id,
                "is_current_version": current, "shots": shots, "issue": fields.get("issue", ""),
                "title": fields.get("title", "项目任务"), "status": task["status"],
                "message": ("已定位本任务关联的当前版本；请核对问题后再修改。" if current else
                    "该任务关联历史版本，仅供查阅；当前稿不会自动重放历史操作。" if base_version_id else
                    "该历史任务没有可验证的版本和镜头关联，可查看当前脚本并人工核对问题。"),
                "automatic_replay": False}

    @staticmethod
    def parse_instruction(instruction: str) -> dict[str, Any]:
        text = str(instruction or "").strip()
        if not text or len(text) > 4000:
            raise WorkspaceServiceError("INVALID_INPUT", "修改要求不能为空或过长")
        if len(_explicit_instruction_parts(text)) > 1:
            raise WorkspaceServiceError("INSTRUCTION_AMBIGUOUS", "多条修改需要一起预览，请使用批量修改入口")
        ambiguous = _AMBIGUOUS_DELETE.fullmatch(text)
        if ambiguous:
            return {
                "state": "needs_clarification",
                "shot_no": ambiguous.group(1),
                "summary": "“删除内容”无法确定是删除整镜，还是只清空某个可选字段。",
                "choices": [
                    {"key": "delete_shot", "label": "删除整镜"},
                    {"key": "clear_optional_field", "label": "清空旁白、字幕或备注"},
                    {"key": "replace_field", "label": "替换指定字段"},
                ],
            }
        clear = _CLEAR.fullmatch(text) or _CLEAR_PREFIX.fullmatch(text)
        if clear:
            return {
                "state": "ready",
                "operation": "clear_optional_field",
                "shot_no": clear.group(1),
                "field": _FIELDS[clear.group(2)],
                "value": "",
            }
        deleted = _DELETE.fullmatch(text) or _DELETE_PREFIX.fullmatch(text)
        if deleted:
            return {
                "state": "ready", "operation": "delete_shot",
                "shot_no": deleted.group(1),
            }
        replaced = _REPLACE.fullmatch(text)
        if replaced:
            shot_no, label, raw = replaced.groups()
            field = _FIELDS[label]
            value: Any = raw.strip()
            if field == "duration_seconds":
                duration = re.fullmatch(r"(\d+)\s*(?:秒)?", value)
                if duration is None or int(duration.group(1)) <= 0:
                    raise WorkspaceServiceError("INVALID_INPUT", "镜头时长必须为正整数秒")
                value = int(duration.group(1))
            return {
                "state": "ready", "operation": "replace_field",
                "shot_no": shot_no, "field": field, "value": value,
            }
        raise WorkspaceServiceError(
            "INSTRUCTION_AMBIGUOUS",
            "请明确镜号、操作、字段和新内容；删除整镜请使用“删除第 N 镜”。",
        )

    @classmethod
    def parse_review_instructions(cls, note: str) -> list[dict[str, Any]]:
        parts = _explicit_instruction_parts(str(note or ""))
        if not parts:
            raise WorkspaceServiceError("INSTRUCTION_AMBIGUOUS", "审核意见未包含明确修改动作")
        operations = []
        for part in parts:
            parsed = cls.parse_instruction(part)
            if parsed.get("state") != "ready":
                raise WorkspaceServiceError("INSTRUCTION_AMBIGUOUS", parsed["summary"])
            operations.append({key: value for key, value in parsed.items() if key != "state"})
        if len(operations) > 1 and any(item["operation"] == "delete_shot" for item in operations):
            raise WorkspaceServiceError(
                "INSTRUCTION_AMBIGUOUS", "删除镜头会重排镜号，请单独创建结构修订提案",
            )
        targets = [
            (item["shot_no"], item.get("field") or "__whole_shot__")
            for item in operations
        ]
        if len(targets) != len(set(targets)):
            raise WorkspaceServiceError("INSTRUCTION_AMBIGUOUS", "同一镜头字段存在多个修改动作")
        return operations

    def propose(
        self,
        auth_token: str,
        *,
        agent_session_id: str,
        project_id: str,
        instruction: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        idempotency_key: str,
        entrypoint: str = "assistant",
        feedback_kind: str | None = None,
        draft_only: bool = False,
        analysis_task_id: str = "",
        selected_shot_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        if expected_project_version is None:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "缺少项目版本，请重新读取")
        if analysis_task_id:
            task = self.tasks.get_task(auth_token, analysis_task_id)
            if task["project_id"] != project_id or task["task_type"] != "formal_script_revision":
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "脚本分析任务不属于当前项目")
            if task["status"] != "needs_clarification":
                raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "脚本分析任务不能继续提交范围")
            project = self.projects.get_project(auth_token, project_id)
            self._check_expected_project_version(project, expected_project_version)
            task = self.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="understanding",
                details={"assistant_scope_continuation": True},
            )
            return self.propose_on_task(
                auth_token, task=task, project=project, instruction=instruction,
                expected_revision=expected_revision, idempotency_key=idempotency_key,
                entrypoint=entrypoint, feedback_kind=feedback_kind,
                draft_only=draft_only, selected_shot_ids=selected_shot_ids,
            )
        session = (
            self.tasks.get_agent_session(auth_token, agent_session_id)
            if agent_session_id else
            self.tasks.create_agent_session(auth_token, project_id=project_id)
        )
        agent_session_id = str(session["agent_session_id"])
        if session["project_id"] != project_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "Agent 会话不属于当前项目")
        project = self.projects.get_project(auth_token, session["project_id"])
        self._check_expected_project_version(project, expected_project_version)
        task = self.tasks.create_task(
            auth_token,
            agent_session_id=agent_session_id,
            task_type="formal_script_revision",
            payload_sha256=sha256_json({
                "instruction": instruction,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
                "draft_only": draft_only,
                "selected_shot_ids": list(selected_shot_ids or []),
            }),
            expected_project_version=project["project_version"],
        )
        task = self.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={"formal_context_version": project["project_version"]},
        )
        return self.propose_on_task(
            auth_token,
            task=task,
            project=project,
            instruction=instruction,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            entrypoint=entrypoint,
            feedback_kind=feedback_kind,
            draft_only=draft_only,
            selected_shot_ids=selected_shot_ids,
        )

    @staticmethod
    def _check_expected_project_version(project: dict, expected_project_version: int | None) -> None:
        if expected_project_version is None:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "缺少项目版本，请重新读取")
        if int(project["project_version"]) != expected_project_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目版本已变化，请刷新后重新修改")

    def propose_change(
        self,
        auth_token: str,
        *,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        operation: dict[str, Any],
        idempotency_key: str,
        entrypoint: str = "table",
        feedback_kind: str | None = None,
    ) -> dict:
        """Use the same proposal contract for table, assistant and review entry points."""
        project = self.projects.get_project(auth_token, project_id)
        self._check_expected_project_version(project, expected_project_version)
        session = self.tasks.create_agent_session(auth_token, project_id=project_id)
        task = self.tasks.create_task(
            auth_token,
            agent_session_id=session["agent_session_id"],
            task_type="formal_script_revision",
            payload_sha256=sha256_json({
                "operation": operation,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            }),
            expected_project_version=project["project_version"],
        )
        task = self.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={"formal_context_version": project["project_version"]},
        )
        return self._propose_operations_on_task(
            auth_token,
            task=task,
            project=project,
            operations=[operation],
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            entrypoint=entrypoint,
            feedback_kind=feedback_kind,
        )

    def apply_table_draft_change(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        shot_no: str,
        shot_id: str | None = None,
        field: str,
        value: Any,
        idempotency_key: str,
    ) -> dict:
        """Keep the established editable-draft journey behind the revision coordinator."""
        self._require_writes("FORMAL_SCRIPT_REVISION_DISABLED", "Formal 脚本修改能力当前关闭")
        return self.workspace.apply_table_change(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=expected_project_version,
            expected_revision=expected_revision,
            shot_no=shot_no,
            shot_id=shot_id,
            field=field,
            value=value,
            idempotency_key=idempotency_key,
        )

    def propose_draft_save(
        self,
        auth_token: str,
        *,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        reason: str,
        idempotency_key: str,
        expected_progress_revision: int | None = None,
    ) -> dict:
        """Preview and materialize an already edited draft through the same revision core."""
        self._require_writes("FORMAL_SCRIPT_REVISION_DISABLED", "Formal 脚本修改能力当前关闭")
        reason = str(reason or "").strip()
        key = self._idempotency_key(idempotency_key)
        if not reason or len(reason) > 500:
            raise WorkspaceServiceError("INVALID_INPUT", "保存原因不能为空或过长")
        project = self.projects.get_project(auth_token, project_id)
        self._check_expected_project_version(project, expected_project_version)
        current = self.workspace.read_current(
            auth_token, workspace_id=project["workspace_id"], project_id=project_id,
        )
        if current.get("state") not in {"editing", "ready"} or current.get("revision") != expected_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿已变化，请刷新后重新预览")
        _, context = self.workspace._current_context(
            auth_token, project["workspace_id"], project_id,
        )
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可保存脚本")
        images = None
        if self.workspace.services.image_workspace is not None:
            images = self.workspace.services.image_workspace.prepare_working_version(auth_token,
                workspace_id=project["workspace_id"], project_id=project_id,
                expected_project_version=project["project_version"], expected_script_revision=expected_revision,
                expected_progress_revision=expected_progress_revision)
        changes = self.workspace._draft_save_changes(
            auth_token, context, expected_revision=expected_revision,
        ) if current.get("can_save_version") else []
        if images:
            for item in images["comparisons"]:
                changes.append({"shot_id": item["shot_id"], "shot_no": item["shot_no"],
                    "field": "image", "label": f"第 {item['shot_no']} 镜 · 配图",
                    "before": item["before_caption"], "after": item["after_caption"]})
        if not changes:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "当前没有待保存的文字或配图修改")
        affected_shots = sorted({item["shot_no"] for item in changes}, key=int)
        affected_fields = list(dict.fromkeys(item["field"] for item in changes))
        parameters = {
            "project_id": project_id,
            "base_version_id": context.version_id,
            "expected_project_version": project["project_version"],
            "expected_revision": expected_revision,
            "feedback_kind": "copy",
            "operations": [],
            "entrypoint": "table",
            "materialize_existing_draft": True,
            "affected_shots": affected_shots,
            "reason": reason,
            "idempotency_key": key,
            **({"expected_progress_revision": images["progress_revision"],
                "image_snapshot_digest": images["snapshot_digest"]} if images else {}),
        }
        digest = sha256_json({"action": "apply_formal_script_revision", "parameters": parameters})
        replay = self._existing_payload_proposal(
            auth_token, project_id=project_id,
            task_type="formal_script_revision", payload_sha256=digest,
        )
        if replay is not None:
            return replay
        session = self.tasks.create_agent_session(auth_token, project_id=project_id)
        task = self.tasks.create_task(
            auth_token,
            agent_session_id=session["agent_session_id"],
            task_type="formal_script_revision",
            payload_sha256=digest,
            expected_project_version=project["project_version"],
            reuse_payload=True,
        )
        replay = self._replayed_proposal(auth_token, task)
        if replay is not None:
            return replay
        task = self.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="understanding",
            details={"formal_context_version": project["project_version"], "entrypoint": "table"},
        )
        response = {
            "type": "action_proposal",
            "title": "确认保存完整新版本",
            "summary": f"本次保存影响 {len({item['shot_id'] for item in changes})} 镜，共 {len(changes)} 项变化。",
            "action": "apply_formal_script_revision",
            "proposal_digest": digest,
            "risk": "R2",
            "before": {"version_id": context.version_id},
            "after": {"draft_revision": expected_revision, "reason": reason},
            "preview": {
                "before": {"version_label": current.get("version_label")},
                "after": {"draft_revision": expected_revision, "reason": reason},
                "affected_shots": affected_shots,
                "changes": changes,
                "warnings": [],
                "image_changes": images["comparisons"] if images else [],
            },
            "affected_shots": affected_shots,
            "affected_fields": affected_fields,
            "rerun_nodes": ["script_draft", "visual_binding", "workbook_render", "machine_audit"],
            "source_impact": "沿用草稿已有来源与修改历史",
            "image_impact": "文字与确认配图共同保存为一个版本；变化项与整片需重新审核",
            "confirmation_required": True,
            "will_change": "确认后创建不可变脚本子版本",
            "will_not_change": "不会修改父版本、Brief、发布状态或 CURRENT",
            "calls_models": False,
            "model_call_statement": "确认执行不调用模型",
            "impact_scope": "当前项目／当前草稿",
            "next_steps": ["核对影响后确认保存为新版本。"],
        }
        proposed = self.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="proposed",
            details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._result(proposed, response)

    def propose_review_feedback(
        self,
        auth_token: str,
        *,
        project_id: str,
        note: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict:
        operations = self.parse_review_instructions(note)
        project = self.projects.get_project(auth_token, project_id)
        session = self.tasks.create_agent_session(auth_token, project_id=project_id)
        task = self.tasks.create_task(
            auth_token,
            agent_session_id=session["agent_session_id"],
            task_type="formal_review_revision",
            payload_sha256=sha256_json({
                "operations": operations,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            }),
            expected_project_version=project["project_version"],
        )
        task = self.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={"formal_context_version": project["project_version"], "entrypoint": "review"},
        )
        return self._propose_operations_on_task(
            auth_token,
            task=task,
            project=project,
            operations=operations,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            entrypoint="review",
        )

    def latest_review_proposal(self, auth_token: str, *, project_id: str) -> dict | None:
        self.projects.get_project(auth_token, project_id)
        with self.workspace.services.store.connection() as connection:
            rows = connection.execute(
                """SELECT task_id FROM tasks
                WHERE project_id=? AND task_type='formal_review_revision'
                  AND status IN ('proposed','waiting_confirmation')
                ORDER BY created_at DESC, task_id DESC LIMIT 20""",
                (project_id,),
            ).fetchall()
        for row in rows:
            try:
                task = self.tasks.get_task(auth_token, row["task_id"])
                response, _parameters = _stored_proposal(
                    self.tasks.list_task_events(auth_token, row["task_id"]),
                )
            except WorkspaceServiceError as exc:
                if exc.code == "PERMISSION_DENIED":
                    continue
                raise
            if response is not None:
                return self._result(task, response)
        return None

    def propose_fact_revision(
        self,
        auth_token: str,
        *,
        project_id: str,
        expected_project_version: int,
        field: str,
        value: str,
        evidence: str,
        idempotency_key: str,
    ) -> dict:
        """Create a confirmed-action proposal for one pending Brief child."""
        self._require_writes("FORMAL_FACT_REVISION_DISABLED", "Formal 事实修订能力当前关闭")
        adapter, _brief_store = self._fact_dependencies()
        field = str(field or "").strip()
        value = str(value or "").strip()
        evidence = str(evidence or "").strip()
        key = self._idempotency_key(idempotency_key)
        if field not in _FACT_FIELDS:
            raise WorkspaceServiceError("INVALID_INPUT", "事实修订字段不受支持")
        if not value or len(value) > 4000 or not evidence or len(evidence) > 4000:
            raise WorkspaceServiceError("INVALID_INPUT", "事实新值与证据说明不能为空或过长")
        project = self.projects.get_project(auth_token, project_id)
        if project["project_version"] != expected_project_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目版本已变化")
        if not project.get("current_version_id") or not project.get("current_brief_snapshot_id"):
            raise WorkspaceServiceError("NOT_FOUND", "当前项目缺少可修订的 Brief 或脚本版本")
        current = adapter.read_current_formal(
            auth_token,
            workspace_id=project["workspace_id"],
            project_id=project_id,
        )
        if current.get("status") != "ready" or not current.get("brief_key"):
            raise WorkspaceServiceError("NOT_FOUND", "当前项目缺少可修订的 Brief")
        if current.get("confirmation_status") != "confirmed":
            raise WorkspaceServiceError(
                "BRIEF_CONFIRMATION_REQUIRED", "已有待确认 Brief；请先完成人工确认再继续事实修订",
            )
        with self.projects.store.connection() as connection:
            bound = connection.execute(
                """SELECT brief_version_id FROM brief_confirmations
                WHERE project_id=? AND brief_snapshot_id=?""",
                (project_id, project["current_brief_snapshot_id"]),
            ).fetchone()
        if bound is None or str(bound["brief_version_id"]) != str(current["brief_key"]):
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "当前 Brief 权威绑定已变化")
        before = self._display_fact_value(next((
            item.get("value") for item in (current.get("brief") or {}).get("facts") or []
            if item.get("label") == field
        ), None))
        parameters = {
            "project_id": project_id,
            "workspace_id": project["workspace_id"],
            "expected_project_version": project["project_version"],
            "base_version_id": project["current_version_id"],
            "parent_brief_version_id": current["brief_key"],
            "parent_brief_snapshot_id": project["current_brief_snapshot_id"],
            "field": field,
            "value": value,
            "evidence": evidence,
            "entrypoint": "history",
            "idempotency_key": key,
        }
        digest = sha256_json({"action": "apply_formal_fact_revision", "parameters": parameters})
        replay = self._existing_payload_proposal(
            auth_token, project_id=project_id,
            task_type="formal_fact_revision", payload_sha256=digest,
        )
        if replay is not None:
            return replay
        session = self.tasks.create_agent_session(auth_token, project_id=project_id)
        task = self.tasks.create_task(
            auth_token,
            agent_session_id=session["agent_session_id"],
            task_type="formal_fact_revision",
            payload_sha256=digest,
            expected_project_version=project["project_version"],
            reuse_payload=True,
        )
        replay = self._replayed_proposal(auth_token, task)
        if replay is not None:
            return replay
        task = self.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="understanding",
            details={"formal_context_version": project["project_version"], "entrypoint": "history"},
        )
        response = {
            "type": "action_proposal",
            "title": "确认事实修订",
            "action": "apply_formal_fact_revision",
            "proposal_digest": digest,
            "risk": "R2",
            "summary": f"预览事实字段“{field}”的新 Brief 子版本影响。",
            "before": before,
            "after": value,
            "affected_fields": [field],
            "rerun_nodes": ["brief_confirmation", "script_plan"],
            "brief_confirmation_required": True,
            "confirmation_required": True,
            "will_change": "仅创建一个待人工确认的 Brief 子版本和不可变事实反馈证据",
            "will_not_change": "原 Brief、脚本版本、审核、交付、发布状态和 CURRENT",
            "calls_models": adapter.understanding_policy is not None,
            "model_call_statement": (
                "导入新 Brief 子版本时会使用已配置的资料理解服务，并继续执行来源校验"
                if adapter.understanding_policy is not None
                else "确认执行不调用模型"
            ),
            "impact_scope": "当前项目／Brief 事实字段",
            "next_steps": ["确认后进入 Brief 权威页，重新人工确认并规划。"],
        }
        proposed = self.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="proposed",
            details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._result(proposed, response)

    def execute_fact_revision(self, auth_token: str, *, project: dict, parameters: dict) -> dict:
        adapter, brief_store = self._fact_dependencies()
        if (
            project["workspace_id"] != parameters["workspace_id"]
            or project["project_id"] != parameters["project_id"]
        ):
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "事实修订提案不属于当前项目")
        if (
            project["project_version"] != parameters["expected_project_version"]
            or project.get("current_version_id") != parameters["base_version_id"]
            or project.get("current_brief_snapshot_id") != parameters["parent_brief_snapshot_id"]
        ):
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "事实修订基线已变化")
        current = adapter.read_current_formal(
            auth_token,
            workspace_id=parameters["workspace_id"],
            project_id=parameters["project_id"],
        )
        if (
            current.get("status") != "ready"
            or current.get("confirmation_status") != "confirmed"
            or str(current.get("brief_key") or "") != parameters["parent_brief_version_id"]
        ):
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "事实修订确认前当前 Brief 已变化")
        fact_text = (
            f"{parameters['field']}: {parameters['value']}\n"
            f"特殊需求: 事实修订入口={parameters['entrypoint']}；"
            f"证据说明={parameters['evidence']}\n"
        ).encode("utf-8")
        feedback_checkpoint = brief_store.checkpoint()
        feedback_result: dict[str, str] = {}

        def persist_feedback(connection, version, actor) -> None:
            if version.parent_brief_version_id != parameters["parent_brief_version_id"]:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "新 Brief 子版本父子关系无效")
            frozen = brief_store.freeze({
                "schema_version": "mplus-formal-fact-feedback/v1",
                "entrypoint": parameters["entrypoint"],
                "feedback_kind": "fact",
                "base_version_id": parameters["base_version_id"],
                "parent_brief_version_id": parameters["parent_brief_version_id"],
                "parent_brief_snapshot_id": parameters["parent_brief_snapshot_id"],
                "child_brief_version_id": version.brief_version_id,
                "field": parameters["field"],
                "value": parameters["value"],
                "evidence": parameters["evidence"],
            })
            connection.execute(
                """INSERT INTO formal_version_feedback_records(
                feedback_id, workspace_id, project_id, base_version_id, child_version_id,
                feedback_kind, shot_no, payload_ref, payload_sha256,
                created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, NULL, 'fact', NULL, ?, ?, ?, ?)""",
                (
                    new_id("formal_feedback"),
                    parameters["workspace_id"],
                    parameters["project_id"],
                    parameters["base_version_id"],
                    frozen["brief_ref"],
                    frozen["brief_sha256"],
                    actor["actor_id"],
                    utc_now(),
                ),
            )
            feedback_result.update(frozen)

        try:
            imported = adapter.import_uploads(
                auth_token,
                workspace_id=parameters["workspace_id"],
                project_id=parameters["project_id"],
                uploads=[UploadedDocument(
                    filename=(
                        "事实修订-"
                        f"{sha256_json({'idempotency_key': parameters['idempotency_key']})[:16]}.txt"
                    ),
                    stream=BytesIO(fact_text),
                    critical=True,
                )],
                expected_project_version=parameters["expected_project_version"],
                transaction_hook=persist_feedback,
            )
        except Exception:
            brief_store.rollback_to(feedback_checkpoint)
            raise
        brief_version_id = str(imported.get("brief_key") or "")
        if (
            not brief_version_id
            or brief_version_id == parameters["parent_brief_version_id"]
            or not feedback_result
        ):
            raise WorkspaceServiceError("INTEGRITY_ERROR", "事实修订未创建新的 Brief 子版本")
        return {
            "type": "formal_fact_revision_completed",
            "status": "completed",
            "brief_version_id": brief_version_id,
            "brief_confirmation_required": True,
            "brief_url": f"/projects/{parameters['project_id']}/brief",
            "planning_started": False,
            "delivery_eligible": False,
        }

    def propose_history_image_feedback(
        self,
        auth_token: str,
        *,
        project_id: str,
        expected_project_version: int,
        shot_no: str,
        instruction: str,
        idempotency_key: str,
    ) -> dict:
        """Persist a history handoff that must end in the canonical image-version child."""
        self._require_writes("FORMAL_IMAGE_REVISION_DISABLED", "Formal 图片修订能力当前关闭")
        if self.image_workspace is None:
            raise WorkspaceServiceError("FORMAL_IMAGE_REVISION_UNAVAILABLE", "Formal 图片修订服务未安装")
        shot_no = str(shot_no or "").strip()
        instruction = str(instruction or "").strip()
        key = self._idempotency_key(idempotency_key)
        if not shot_no.isdigit() or int(shot_no) <= 0 or not instruction or len(instruction) > 4000:
            raise WorkspaceServiceError("INVALID_INPUT", "历史图片反馈必须包含有效镜号与明确要求")
        project = self.projects.get_project(auth_token, project_id)
        if project["project_version"] != expected_project_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目版本已变化")
        current = self.image_workspace.read_current(
            auth_token,
            workspace_id=project["workspace_id"],
            project_id=project_id,
            expected_project_version=project["project_version"],
        )
        target = next((
            item for item in current.get("shots") or [] if str(item.get("shot_no")) == shot_no
        ), None)
        image_key = str((target or {}).get("selected_image_key") or "")
        if target is None or target.get("decision_kind") != "image_confirmed" or not image_key:
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "目标镜头当前没有可替换的已确认图片")
        parameters = {
            "project_id": project_id,
            "workspace_id": project["workspace_id"],
            "base_version_id": current["base_version_id"],
            "expected_project_version": project["project_version"],
            "shot_no": shot_no,
            "image_key": image_key,
            "instruction": instruction,
            "entrypoint": "history",
            "idempotency_key": key,
        }
        digest = sha256_json({"action": "route_formal_image_feedback", "parameters": parameters})
        replay = self._existing_payload_proposal(
            auth_token, project_id=project_id,
            task_type="formal_image_feedback", payload_sha256=digest,
        )
        if replay is not None:
            return replay
        session = self.tasks.create_agent_session(auth_token, project_id=project_id)
        task = self.tasks.create_task(
            auth_token,
            agent_session_id=session["agent_session_id"],
            task_type="formal_image_feedback",
            payload_sha256=digest,
            expected_project_version=project["project_version"],
            reuse_payload=True,
        )
        replay = self._replayed_proposal(auth_token, task)
        if replay is not None:
            return replay
        task = self.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="understanding",
            details={"formal_context_version": project["project_version"], "entrypoint": "history"},
        )
        response = {
            "type": "image_feedback_proposal",
            "proposal_digest": digest,
            "summary": f"第 {shot_no} 镜图片反馈已持久化；请进入逐镜工作区重新检索并确认。",
            "affected_shots": [shot_no],
            "rerun_nodes": ["visual_binding", "workbook_render", "machine_audit"],
            "workspace_url": (
                f"/projects/{project_id}/images?revision_task_id={task['task_id']}"
                f"&revision_shot={shot_no}"
            ),
            "confirmation_required": False,
            "will_change": "只有逐镜工作区确认后才创建图片子版本并写入排除反馈",
            "will_not_change": "当前版本、当前图片、文案、时长、其他镜头和 CURRENT",
        }
        proposed = self.tasks.transition_task(
            auth_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="proposed",
            details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._result(proposed, response)

    def propose_image_change(
        self,
        auth_token: str,
        *,
        candidate_task_id: str,
        candidate_key: str,
        reuse_reason: str = "",
        idempotency_key: str,
    ) -> dict:
        if self.image_workspace is None:
            raise WorkspaceServiceError(
                "FORMAL_IMAGE_REVISION_UNAVAILABLE", "Formal 图片修订服务未安装",
            )
        if not self.writes_enabled:
            raise WorkspaceServiceError(
                "FORMAL_IMAGE_REVISION_DISABLED", "Formal 图片修订能力当前关闭",
            )
        candidate_task, candidate_context = self._candidate_task(
            auth_token, candidate_task_id,
        )
        project_id = str(candidate_task["project_id"])
        project = self.projects.get_project(auth_token, project_id)
        visible = {
            str(item["candidate_key"]): str(item["image_key"])
            for item in candidate_context.get("candidates") or []
        }
        selected_image = visible.get(str(candidate_key or ""))
        if not selected_image:
            raise WorkspaceServiceError("IMAGE_CANDIDATE_INVALID", "所选候选不属于当前项目候选 Task")
        replay = self._existing_image_action(
            auth_token, candidate_task["agent_session_id"], idempotency_key,
            task_type="formal_image_revision",
        )
        if replay is not None:
            return replay
        if project["project_version"] != candidate_context["expected_project_version"]:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目状态在候选检索后已变化")
        selected_public = next(
            dict(item.get("public") or {})
            for item in candidate_context.get("candidates") or []
            if item.get("candidate_key") == candidate_key
        )
        if int(selected_public.get("reuse_count") or 0) > 0 and not str(reuse_reason or "").strip():
            raise WorkspaceServiceError(
                "IMAGE_REUSE_REASON_REQUIRED", "该候选会形成感知近似复用，请填写人工复用理由",
            )
        workspace = self.image_workspace.read_current(
            auth_token,
            workspace_id=project["workspace_id"],
            project_id=project_id,
            expected_project_version=project["project_version"],
        )
        shot_no = str(candidate_context["shot_no"])
        target = next(
            (item for item in workspace.get("shots") or [] if item["shot_no"] == shot_no),
            None,
        )
        if target is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前版本不存在该镜头")
        if not all(item.get("confirmed") for item in workspace.get("shots") or []):
            raise WorkspaceServiceError(
                "IMAGE_DECISION_CONFIRMATION_REQUIRED",
                "请先完成当前版本全部逐镜图片确认，再单独替换目标镜头",
            )
        query = str(candidate_context["query"])
        searched = self.image_workspace.search_shot(
            auth_token, workspace_id=project["workspace_id"], project_id=project_id,
            shot_no=shot_no, query=query, base_version_id=workspace["base_version_id"],
            expected_project_version=project["project_version"],
        )
        if selected_image not in {candidate["image_key"] for candidate in searched["candidates"]}:
            raise WorkspaceServiceError("IMAGE_CANDIDATE_SNAPSHOT_DRIFT", "所选图片已不在当前权威安全候选快照中")
        selections = (ImageShotSelection(
            shot_no, selected_image, True, decision_kind="image_confirmed",
            candidate_snapshot_digest=searched["candidate_snapshot_digest"], query=query,
            reuse_reason=str(reuse_reason or ""),
        ),)
        image_proposal = self.image_workspace.preview_version(
            auth_token,
            workspace_id=project["workspace_id"],
            project_id=project_id,
            base_version_id=workspace["base_version_id"],
            expected_project_version=project["project_version"],
            selections=selections,
            allowed_shot_ids=[target["shot_id"]],
            idempotency_key=f"{idempotency_key}:image-version",
        )
        parameters = {
            "project_id": project_id,
            "base_version_id": workspace["base_version_id"],
            "expected_project_version": project["project_version"],
            "image_proposal_id": image_proposal["proposal_id"],
            "image_proposal_digest": image_proposal["proposal_digest"],
            "target_shot_no": str(shot_no),
            "target_image_id": selected_image,
            "candidate_task_id": candidate_task_id,
            "idempotency_key": idempotency_key,
        }
        digest = sha256_json({"action": "apply_formal_image_revision", "parameters": parameters})
        task = self.tasks.create_task(
            auth_token,
            agent_session_id=candidate_task["agent_session_id"],
            task_type="formal_image_revision",
            payload_sha256=digest,
            expected_project_version=project["project_version"],
        )
        task = self.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={"formal_context_version": project["project_version"]},
        )
        response = {
            "type": "action_proposal",
            "title": "确认单镜图片替换",
            "summary": f"仅将第 {shot_no} 镜替换为用户从可见权威候选中选择的图片。",
            "action": "apply_formal_image_revision",
            "proposal_digest": digest,
            "risk": "R2",
            "before": {"shot_no": str(shot_no), "image": candidate_context.get("current_image")},
            "after": {"shot_no": str(shot_no), "image": selected_public},
            "affected_shots": [str(shot_no)],
            "affected_fields": ["image_decision"],
            "rerun_nodes": ["visual_binding", "workbook_render", "machine_audit"],
            "source_impact": "保留目标图片内容 SHA、media ID 与来源三元组",
            "image_impact": "仅目标镜头图片决定变化",
            "feedback_impact": "父版本与其他镜头的图片及人工决定保持不变；复用理由只记录在本镜",
            "reuse_justification_shots": [],
            "confirmation_required": True,
            "will_change": f"第 {shot_no} 镜图片绑定",
            "will_not_change": "文案、时长、其他镜头图片身份、父版本、发布状态和 CURRENT",
            "calls_models": False,
            "model_call_statement": "确认执行不调用模型",
            "impact_scope": f"当前项目／镜头 {shot_no}",
            "next_steps": ["确认后创建不可变图片子版本。"],
        }
        proposed = self.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="proposed",
            details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._result(proposed, response)

    def propose_temporary_text_basis(
        self,
        auth_token: str,
        *,
        candidate_task_id: str,
        reason: str,
        idempotency_key: str,
    ) -> dict:
        reason = str(reason or "").strip()
        if not reason:
            raise WorkspaceServiceError("INVALID_INPUT", "暂准文字依据必须填写理由")
        candidate_task, context = self._candidate_task(auth_token, candidate_task_id)
        project = self.projects.get_project(auth_token, candidate_task["project_id"])
        replay = self._existing_image_action(
            auth_token, candidate_task["agent_session_id"], idempotency_key,
            task_type="formal_image_revision",
        )
        if replay is not None:
            return replay
        if project["project_version"] != context["expected_project_version"]:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目状态在候选检索后已变化")
        current = self.image_workspace.read_current(
            auth_token, workspace_id=project["workspace_id"], project_id=project["project_id"],
            expected_project_version=project["project_version"],
        )
        target_shot = str(context["shot_no"])
        target = next((item for item in current.get("shots") or [] if item["shot_no"] == target_shot), None)
        if target is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前版本不存在该镜头")
        selections = (ImageShotSelection(
            target_shot, "", True, decision_kind="temporary_text_basis", temporary_reason=reason,
        ),)
        image_proposal = self.image_workspace.preview_version(
            auth_token, workspace_id=project["workspace_id"], project_id=project["project_id"],
            base_version_id=current["base_version_id"],
            expected_project_version=project["project_version"], selections=selections,
            allowed_shot_ids=[target["shot_id"]],
            idempotency_key=f"{idempotency_key}:image-version",
        )
        return self._agent_image_proposal(
            auth_token, candidate_task=candidate_task, project=project,
            image_proposal=image_proposal, target_shot_no=target_shot,
            target_image_id="", idempotency_key=idempotency_key,
            summary=f"第 {target_shot} 镜将暂准文字依据；审核和交付继续阻断。",
        )

    def propose_image_feedback(
        self,
        auth_token: str,
        *,
        candidate_task_id: str,
        reason: str,
        idempotency_key: str,
    ) -> dict:
        reason = str(reason or "").strip()
        if not reason:
            raise WorkspaceServiceError("INVALID_INPUT", "图片不合适反馈必须填写原因")
        candidate_task, context = self._candidate_task(auth_token, candidate_task_id)
        current = context.get("current_image") or {}
        if not current.get("image_key"):
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "目标镜头当前没有已确认图片")
        project = self.projects.get_project(auth_token, candidate_task["project_id"])
        replay = self._existing_image_action(
            auth_token, candidate_task["agent_session_id"], idempotency_key,
            task_type="formal_image_feedback",
        )
        if replay is not None:
            return replay
        if project["project_version"] != context["expected_project_version"]:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目状态在候选检索后已变化")
        parameters = {
            "workspace_id": project["workspace_id"],
            "project_id": project["project_id"],
            "base_version_id": context["base_version_id"],
            "expected_project_version": context["expected_project_version"],
            "shot_no": context["shot_no"],
            "image_key": current["image_key"],
            "reason": reason,
            "candidate_task_id": candidate_task_id,
            "idempotency_key": idempotency_key,
        }
        digest = sha256_json({"action": "record_formal_image_feedback", "parameters": parameters})
        task = self.tasks.create_task(
            auth_token, agent_session_id=candidate_task["agent_session_id"],
            task_type="formal_image_feedback", payload_sha256=digest,
            expected_project_version=project["project_version"],
        )
        task = self.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="understanding", details={"formal_context_version": project["project_version"]},
        )
        response = {
            "type": "action_proposal", "title": "确认排除当前图片",
            "summary": f"将第 {context['shot_no']} 镜当前图片写入不可变排除反馈。",
            "action": "record_formal_image_feedback", "proposal_digest": digest,
            "before": {"shot_no": context["shot_no"], "image": current},
            "after": {"excluded": True}, "affected_shots": [context["shot_no"]],
            "affected_fields": ["image_feedback"], "rerun_nodes": ["visual_binding"],
            "confirmation_required": True, "will_change": "下一次权威图片检索的排除项",
            "will_not_change": "当前版本、当前图片、文案、时长、其他镜头和 CURRENT",
            "model_call_statement": "确认执行不调用模型", "impact_scope": f"当前项目／镜头 {context['shot_no']}",
            "next_steps": ["确认后重新检索，当前图片将不再进入安全候选。"],
        }
        proposed = self.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="proposed", details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._result(proposed, response)

    def execute_image_feedback(self, auth_token: str, *, project: dict, parameters: dict) -> dict:
        return self.image_workspace.record_image_feedback(
            auth_token, workspace_id=parameters["workspace_id"], project_id=parameters["project_id"],
            base_version_id=parameters["base_version_id"], shot_no=parameters["shot_no"],
            image_key=parameters["image_key"], reason=parameters["reason"],
            expected_project_version=parameters["expected_project_version"],
        )

    def _candidate_task(self, auth_token: str, candidate_task_id: str) -> tuple[dict, dict]:
        task = self.tasks.get_task(auth_token, candidate_task_id)
        if task["task_type"] != "formal_image_candidates" or task["status"] != "completed":
            raise WorkspaceServiceError("IMAGE_CANDIDATE_TASK_INVALID", "图片候选 Task 不可执行")
        details = next((
            event.get("details", {}).get("internal_formal_parameters")
            for event in reversed(self.tasks.list_task_events(auth_token, candidate_task_id))
            if event.get("details", {}).get("internal_formal_parameters")
        ), None)
        if not isinstance(details, dict):
            raise WorkspaceServiceError("IMAGE_CANDIDATE_TASK_INVALID", "图片候选 Task 缺少权威上下文")
        return task, details

    def _existing_image_action(
        self, auth_token: str, agent_session_id: str, idempotency_key: str,
        *, task_type: str,
    ) -> dict | None:
        for task in reversed(self.tasks.list_agent_session_tasks(auth_token, agent_session_id)):
            if task["task_type"] != task_type or task["status"] not in {
                "proposed", "waiting_confirmation", "executing", "completed",
            }:
                continue
            response = None
            parameters = None
            for event in reversed(self.tasks.list_task_events(auth_token, task["task_id"])):
                details = event.get("details") or {}
                if response is None and isinstance(details.get("response"), dict):
                    candidate_response = details["response"]
                    if candidate_response.get("type") == "action_proposal":
                        response = candidate_response
                if parameters is None and isinstance(details.get("internal_formal_parameters"), dict):
                    parameters = details["internal_formal_parameters"]
            if parameters and parameters.get("idempotency_key") == idempotency_key and response:
                return self._result(task, response)
        return None

    def _agent_image_proposal(
        self, auth_token: str, *, candidate_task: dict, project: dict,
        image_proposal: dict, target_shot_no: str, target_image_id: str,
        idempotency_key: str, summary: str,
    ) -> dict:
        parameters = {
            "project_id": project["project_id"], "base_version_id": image_proposal["before"]["base_version_id"],
            "expected_project_version": project["project_version"],
            "image_proposal_id": image_proposal["proposal_id"],
            "image_proposal_digest": image_proposal["proposal_digest"],
            "target_shot_no": target_shot_no, "target_image_id": target_image_id or None,
            "candidate_task_id": candidate_task["task_id"], "idempotency_key": idempotency_key,
        }
        digest = sha256_json({"action": "apply_formal_image_revision", "parameters": parameters})
        task = self.tasks.create_task(
            auth_token, agent_session_id=candidate_task["agent_session_id"],
            task_type="formal_image_revision", payload_sha256=digest,
            expected_project_version=project["project_version"],
        )
        task = self.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="understanding", details={"formal_context_version": project["project_version"]},
        )
        response = {
            "type": "action_proposal", "title": "确认单镜图片决定", "summary": summary,
            "action": "apply_formal_image_revision", "proposal_digest": digest,
            "before": {"shot_no": target_shot_no, "image": "当前图片决定"},
            "after": {"shot_no": target_shot_no, "image_key": target_image_id or "temporary_text_basis"},
            "affected_shots": [target_shot_no], "affected_fields": ["image_decision"],
            "rerun_nodes": ["visual_binding", "workbook_render", "machine_audit"],
            "confirmation_required": True, "will_change": f"第 {target_shot_no} 镜图片决定",
            "will_not_change": "文案、时长、其他镜头、父版本、发布状态和 CURRENT",
            "model_call_statement": "确认执行不调用模型", "impact_scope": f"当前项目／镜头 {target_shot_no}",
            "next_steps": ["确认后创建不可变图片子版本。"],
        }
        proposed = self.tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="proposed", details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._result(proposed, response)

    def execute_image(self, auth_token: str, *, project: dict, parameters: dict) -> dict:
        if self.image_workspace is None:
            raise WorkspaceServiceError(
                "FORMAL_IMAGE_REVISION_UNAVAILABLE", "Formal 图片修订服务未安装",
            )
        child_task = self.tasks.get_task(auth_token, parameters["image_proposal_id"])
        if child_task["project_id"] != project["project_id"]:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "图片修订提案不属于当前项目")
        result = self.image_workspace.confirm_version(
            auth_token,
            proposal_id=parameters["image_proposal_id"],
            proposal_digest=parameters["image_proposal_digest"],
        )
        temporary = not parameters.get("target_image_id")
        return {
            "type": "formal_image_revision_completed",
            "status": "completed",
            "summary": (
                f"第 {parameters['target_shot_no']} 镜已暂准文字依据；审核和交付继续阻断。"
                if temporary else
                f"第 {parameters['target_shot_no']} 镜图片已替换；父版本和其他业务字段保持不变。"
            ),
            "affected_shots": [parameters["target_shot_no"]],
            "affected_fields": ["image_decision"],
            "version_label": result.get("version_label", ""),
            "review_eligible": result.get("review_eligible", False),
            "delivery_eligible": result.get("delivery_eligible", False),
        }

    def propose_on_task(
        self,
        auth_token: str,
        *,
        task: dict,
        project: dict,
        instruction: str,
        expected_revision: int,
        idempotency_key: str,
        entrypoint: str = "assistant",
        feedback_kind: str | None = None,
        draft_only: bool = False,
        selected_shot_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        try:
            if len(_explicit_instruction_parts(str(instruction or ""))) > 1:
                operations = self.parse_review_instructions(instruction)
                return self._propose_operations_on_task(
                    auth_token, task=task, project=project, operations=operations,
                    expected_revision=expected_revision, idempotency_key=idempotency_key,
                    entrypoint=entrypoint, feedback_kind=feedback_kind,
                    draft_only=draft_only,
                    permitted_shot_ids=selected_shot_ids,
                )
            parsed = self.parse_instruction(instruction)
        except WorkspaceServiceError as exc:
            if exc.code != "INSTRUCTION_AMBIGUOUS":
                raise
            return self._propose_assistant_on_task(
                auth_token, task=task, project=project, instruction=instruction,
                expected_revision=expected_revision, idempotency_key=idempotency_key,
                entrypoint=entrypoint, feedback_kind=feedback_kind,
                draft_only=draft_only, selected_shot_ids=selected_shot_ids,
            )
        if parsed["state"] == "needs_clarification":
            response = {
                "type": "needs_clarification", "title": "需要确认删除范围",
                "summary": parsed["summary"], "shot_no": parsed["shot_no"],
                "choices": parsed["choices"],
                "next_steps": ["选择删除整镜、清空可选字段，或改为替换指定字段。"],
            }
            finished = self.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="needs_clarification", details={"response": response},
            )
            return self._result(finished, response)
        operation = {key: value for key, value in parsed.items() if key != "state"}
        return self._propose_operations_on_task(
            auth_token,
            task=task,
            project=project,
            operations=[operation],
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            entrypoint=entrypoint,
            feedback_kind=feedback_kind,
            draft_only=draft_only,
            permitted_shot_ids=selected_shot_ids,
        )

    def _propose_assistant_on_task(
        self,
        auth_token: str,
        *,
        task: dict,
        project: dict,
        instruction: str,
        expected_revision: int,
        idempotency_key: str,
        entrypoint: str,
        feedback_kind: str | None,
        draft_only: bool,
        selected_shot_ids: list[str] | tuple[str, ...] | None,
    ) -> dict:
        current = self.workspace.read_current(
            auth_token, workspace_id=project["workspace_id"],
            project_id=project["project_id"],
        )
        if current.get("state") == "empty":
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可修改脚本")
        if int(current.get("revision") or 0) != expected_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿已变化，请刷新后重新分析")
        analysis = analyze_formal_script_instruction(
            instruction=instruction,
            shots=list(current.get("shots") or []),
            assistant_locked_shot_ids=list(current.get("assistant_locked_shot_ids") or []),
            selected_shot_ids=list(selected_shot_ids or []),
            editor_policy=self.assistant_editor_policy,
            context=self.workspace.assistant_context(
                auth_token, workspace_id=project["workspace_id"], project_id=project["project_id"],
            ),
        )
        by_id = {str(item["shot_id"]): item for item in current.get("shots") or []}
        if analysis.get("analysis_code") == "script_no_changes":
            response = {**analysis, "type": "no_changes", "confirmation_required": False}
            task = self.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="executing",
                details={"assistant_analysis_only": True},
            )
            finished = self.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="completed",
                details={"response": response, "assistant_instruction": instruction},
            )
            return self._result(finished, response)
        if analysis["type"] in {"scope_required", "needs_clarification"}:
            suggestions = []
            for item in analysis.get("suggested_targets") or []:
                shot = by_id.get(str(item.get("shot_id") or ""), {})
                suggestions.append({
                    **item,
                    "shot_no": str(shot.get("shot_no") or ""),
                })
            response = {
                **analysis,
                "analysis_task_id": task["task_id"],
                "suggested_targets": suggestions,
                "next_steps": [
                    "选择要修改的镜头后继续。" if analysis["type"] == "scope_required"
                    else "检查编辑服务后重试。" if analysis.get("analysis_code") == "semantic_editor_unavailable"
                    else "重试预览；也可缩小范围或调整要求。"
                ],
            }
            finished = self.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="needs_clarification",
                details={"response": response, "assistant_instruction": instruction},
            )
            return self._result(finished, response)
        if analysis["type"] == "replan_required":
            protected_ids = set(analysis.get("assistant_locked_shot_ids") or [])
            protected_shots = [
                {
                    key: value
                    for key, value in shot.items()
                    if key in {
                        "shot_id", "shot_no", "structure", "camera_type", "shot_size",
                        "camera_move", "duration_seconds", "visual", "sound", "voiceover",
                        "subtitle", "notes", "source",
                    }
                }
                for shot in current.get("shots") or []
                if str(shot.get("shot_id") or "") in protected_ids
            ]
            response = {
                **analysis,
                "analysis_task_id": task["task_id"],
                "next_href": f"/formal/{project['project_id']}/plan?revision_task_id={task['task_id']}",
                "next_steps": ["进入 Formal 规划确认目标镜数；当前脚本不会直接插入或批量删除镜头。"],
            }
            finished = self.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="needs_clarification",
                details={
                    "response": response,
                    "assistant_instruction": instruction,
                    "internal_replan_intent": {
                        "shot_count_before": analysis.get("shot_count_before"),
                        "shot_count_after": analysis.get("shot_count_after"),
                        "protected_shots": protected_shots,
                    },
                },
            )
            return self._result(finished, response)
        if analysis["type"] != "rewrite_operations":
            raise WorkspaceServiceError("INTEGRITY_ERROR", "脚本助手返回未知分析类型")
        operations = []
        for item in analysis.get("operations") or []:
            shot = by_id.get(str(item.get("shot_id") or ""))
            if shot is None or shot.get("assistant_locked"):
                raise WorkspaceServiceError("SCRIPT_TARGET_LOCKED", "助手目标已被保护")
            operation_kind = str(item.get("operation") or "replace_field")
            if operation_kind not in {"replace_field", "clear_optional_field"}:
                raise WorkspaceServiceError("DRAFT_INVALID", "不支持的脚本修改方式")
            operations.append({
                "operation": operation_kind,
                "shot_no": str(shot["shot_no"]),
                "shot_id": str(shot["shot_id"]),
                "field": str(item["field"]),
                "value": item["value"],
            })
        return self._propose_operations_on_task(
            auth_token, task=task, project=project, operations=operations,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
            entrypoint=entrypoint, feedback_kind=feedback_kind, draft_only=draft_only,
            model_evidence=analysis.get("model_evidence"),
            assistant_selection={key: analysis[key] for key in (
                "selection_mode", "requested_count", "allowed_shot_ids", "actual_changed_shot_ids", "shot_reasons",
            ) if key in analysis},
        )

    def reselect_proposal(
        self, auth_token: str, *, project_id: str, task_id: str,
        expected_project_version: int, expected_revision: int,
        retained_shot_ids: list[str], idempotency_key: str,
    ) -> dict:
        """Rebuild complete shot suggestions through the same revision core."""
        source_task = self.tasks.get_task(auth_token, task_id)
        if source_task["project_id"] != project_id or source_task["task_type"] != "formal_script_revision":
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "修改提案不属于当前项目")
        from .workbench_task_recovery import require_standalone_child
        require_standalone_child(self.tasks, auth_token, task_id)
        events = self.tasks.list_task_events(auth_token, task_id)
        response, parameters = _stored_proposal(events)
        if not parameters or not response or response.get("action") != "apply_formal_script_revision":
            raise WorkspaceServiceError("INVALID_INPUT", "该提案不支持按镜头重新选择")
        retained = set(retained_shot_ids)
        proposed_ids = {str(op.get("shot_id") or "") for op in parameters["operations"]}
        if (len(retained) != len(retained_shot_ids) or "" in retained
                or not retained <= proposed_ids):
            raise WorkspaceServiceError("SCRIPT_SCOPE_MISMATCH", "只能保留本次已建议的完整镜头")
        payload_sha = sha256_json({
            "source_task_id": task_id, "retained_shot_ids": sorted(retained),
            "expected_project_version": expected_project_version, "expected_revision": expected_revision,
            "idempotency_key": idempotency_key,
        })
        if not retained and source_task["status"] == "cancelled":
            for event in reversed(events):
                details = event.get("details") or {}
                if details.get("selection_request_sha256") == payload_sha:
                    return self._result(source_task, details["response"])
        replay = self._existing_payload_proposal(auth_token, project_id=project_id,
            task_type="formal_script_revision", payload_sha256=payload_sha)
        if replay is not None:
            return replay
        if source_task["status"] != "proposed":
            raise WorkspaceServiceError("PROPOSAL_STATE_CHANGED", "原提案已执行或取消，请重新读取当前稿")
        project = self.projects.get_project(auth_token, project_id)
        self._check_expected_project_version(project, expected_project_version)
        current = self.workspace.read_current(auth_token, workspace_id=project["workspace_id"], project_id=project_id)
        if (current.get("revision") != expected_revision or parameters["expected_revision"] != expected_revision
                or parameters["base_version_id"] != current.get("base_version_id")
                or parameters["expected_project_version"] != expected_project_version):
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "当前稿或版本已变化，请重新提出修改")
        if not retained:
            result = {"type": "no_changes", "summary": "已取消本次全部建议，当前稿未改变。", "confirmation_required": False}
            cancelled = self.tasks.transition_task(auth_token, task_id=task_id,
                expected_task_version=source_task["task_version"], status="cancelled",
                details={"response": result, "selection_request_sha256": payload_sha})
            return self._result(cancelled, result)
        operations = [dict(op) for op in parameters["operations"]
                      if op.get("shot_id") in retained and op.get("operation") != "source_rebind"]
        task = self.tasks.create_task(auth_token, agent_session_id=source_task["agent_session_id"],
            task_type="formal_script_revision", payload_sha256=payload_sha,
            expected_project_version=expected_project_version)
        task = self.tasks.transition_task(auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="understanding",
            details={"source_task_id": task_id})
        metadata = dict(parameters.get("assistant_selection") or {})
        metadata["actual_changed_shot_ids"] = [s["shot_id"] for s in current["shots"] if s["shot_id"] in retained]
        metadata["shot_reasons"] = [r for r in metadata.get("shot_reasons") or [] if r["shot_id"] in retained]
        rebuilt = self._propose_operations_on_task(auth_token, task=task, project=project, operations=operations,
            expected_revision=expected_revision, idempotency_key=idempotency_key,
            entrypoint=parameters["entrypoint"], feedback_kind=parameters["feedback_kind"],
            draft_only=bool(parameters.get("draft_only")),
            model_evidence={**dict(response.get("model_evidence") or {}), "call_count": 0},
            assistant_selection=metadata)
        if rebuilt["response"].get("type") != "action_proposal":
            return rebuilt
        try:
            self.tasks.transition_task(auth_token, task_id=task_id,
                expected_task_version=source_task["task_version"], status="cancelled",
                details={"replacement_task_id": rebuilt["task_id"]})
        except WorkspaceServiceError:
            replacement = self.tasks.get_task(auth_token, rebuilt["task_id"])
            self.tasks.transition_task(auth_token, task_id=rebuilt["task_id"],
                expected_task_version=replacement["task_version"], status="cancelled",
                details={"reason": "原提案状态在重新选择时变化"})
            raise
        return rebuilt

    def _propose_operations_on_task(
        self,
        auth_token: str,
        *,
        task: dict,
        project: dict,
        operations: list[dict[str, Any]],
        expected_revision: int,
        idempotency_key: str,
        entrypoint: str = "direct",
        feedback_kind: str | None = None,
        draft_only: bool = False,
        model_evidence: dict[str, Any] | None = None,
        assistant_selection: dict[str, Any] | None = None,
        permitted_shot_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        if not operations:
            raise WorkspaceServiceError("INVALID_INPUT", "修订提案至少需要一项修改")
        current = self.workspace.read_current(
            auth_token, workspace_id=project["workspace_id"],
            project_id=project["project_id"],
        )
        if current.get("state") == "empty":
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可修改脚本")
        if current["revision"] != expected_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿已变化，请刷新后重新提案")
        current_by_no = {str(item["shot_no"]): item for item in current.get("shots") or []}
        if permitted_shot_ids:
            allowed = set(permitted_shot_ids)
            known = {str(item["shot_id"]) for item in current_by_no.values()}
            if len(allowed) != len(permitted_shot_ids) or not allowed <= known:
                raise WorkspaceServiceError("SCRIPT_SCOPE_MISMATCH", "所选镜头已变化，请重新选择")
            if any(str(current_by_no.get(str(op.get("shot_no") or ""), {}).get("shot_id") or "") not in allowed for op in operations):
                response = {"type": "needs_clarification", "analysis_code": "scope_conflict",
                    "summary": "文字指定的镜头超出当前选择，请明确采用哪个范围。", "confirmation_required": False}
                finished = self.tasks.transition_task(auth_token, task_id=task["task_id"],
                    expected_task_version=task["task_version"], status="needs_clarification", details={"response": response})
                return self._result(finished, response)
            assistant_selection = {**dict(assistant_selection or {}), "allowed_shot_ids": list(permitted_shot_ids)}
        normalized_operations = []
        for operation in operations:
            shot = current_by_no.get(str(operation.get("shot_no") or ""))
            if shot is None:
                raise WorkspaceServiceError("DRAFT_INVALID", "目标镜头不存在")
            supplied_shot_id = str(operation.get("shot_id") or "").strip()
            if supplied_shot_id and supplied_shot_id != str(shot.get("shot_id") or ""):
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "镜头身份已变化，请重新分析")
            if shot.get("assistant_locked"):
                raise WorkspaceServiceError("SCRIPT_TARGET_LOCKED", "助手不能修改受保护镜头")
            normalized = dict(operation)
            normalized["shot_id"] = str(shot["shot_id"])
            normalized_operations.append(normalized)
        operations = normalized_operations
        if entrypoint == "assistant" and any(op.get("field") == "shot_size"
                and str(op.get("value") or "") not in {"全景", "远景", "大远景"} for op in operations):
            context = self.workspace.assistant_context(auth_token,
                workspace_id=project["workspace_id"], project_id=project["project_id"])
            required_wide = required_wide_shot_ids(list(current_by_no.values()), context)
            if any(op["shot_id"] in required_wide and op.get("field") == "shot_size"
                   and str(op.get("value") or "") not in {"全景", "远景", "大远景"} for op in operations):
                response = {"type": "needs_clarification", "analysis_code": "scope_conflict",
                    "summary": "本次修改与已确认的必拍全景要求冲突，请保留该镜景别或先修订需求。",
                    "confirmation_required": False}
                finished = self.tasks.transition_task(auth_token, task_id=task["task_id"],
                    expected_task_version=task["task_version"], status="needs_clarification", details={"response": response})
                return self._result(finished, response)
        try:
            rematch = self.workspace.preflight_source_rematches(
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                expected_project_version=project["project_version"],
                expected_revision=expected_revision,
                operations=operations,
                allow_original_adaptation=True,
            )
        except WorkspaceServiceError as exc:
            self.tasks.transition_task(auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="blocked",
                details={"response": {"type": "blocked", "summary": exc.message, "error_code": exc.code}})
            raise

        if not rematch.get("ready"):
            response = {
                "type": "source_match_required",
                "title": "当前模板缺少适合的镜头",
                "summary": "当前版本的来源库不支持这次机位、景别或运镜调整。请调整要求，或更换模板后重新规划。",
                "analysis_task_id": task["task_id"],
                "issues": rematch.get("issues") or [],
                "confirmation_required": False,
                "next_steps": ["调整要求后重试；更换模板需要重新规划。"],
                "next_href": f"/projects/{project['project_id']}/brief?panel=templates",
                "next_label": "查看模板",
            }
            finished = self.tasks.transition_task(
                auth_token,
                task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="needs_clarification",
                details={"response": response},
            )
            return self._result(finished, response)
        operations = list(rematch.get("operations") or operations)
        source_matches = list(rematch.get("matches") or [])
        visible_operations = [
            operation for operation in operations
            if operation.get("operation") != "source_rebind"
        ]
        try:
            previews = [self._preview(current, operation) for operation in visible_operations]
        except WorkspaceServiceError as exc:
            # Failed/no-op previews have no pending work for the user to confirm.
            # Close the existing task before preserving the original API error.
            self.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="blocked",
                details={"response": {
                    "type": "blocked", "summary": exc.message, "error_code": exc.code,
                    "next_steps": ["调整修改要求后重新提交。"],
                }},
            )
            raise
        affected_shots = sorted(
            {shot for preview in previews for shot in preview.get("affected_shots") or []},
            key=lambda value: int(value),
        )
        preview = {
            "summary": "；".join(item["summary"] for item in previews),
            "before": [item.get("before") for item in previews],
            "after": [item.get("after") for item in previews],
            "affected_shots": affected_shots,
            "fields": [item.get("field") or "整镜" for item in previews],
            "changes": previews,
            "source_change": (
                "；".join(dict.fromkeys(
                    "原创拍摄建议，保留原参考来源" if item.get("origin") == "original_suggestion"
                    else "采用重新匹配的创意库来源" for item in source_matches))
                if source_matches else
                "；".join(dict.fromkeys(
                    item.get("source_change") or "来源不变" for item in previews
                ))
            ),
            "source_matches": source_matches,
        }
        if not self.writes_enabled:
            response = {
                "type": "read_only_diagnostic", "title": "只读诊断",
                "summary": "已识别修改范围，但脚本写能力当前关闭。",
                "error_code": "FORMAL_SCRIPT_REVISION_DISABLED",
                "preview": preview,
                "next_steps": ["启用 Formal 脚本修改能力后重新提交。"],
            }
            finished = self.tasks.transition_task(
                auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="blocked",
                details={"response": response},
            )
            return self._result(finished, response)
        _, context = self.workspace._current_context(
            auth_token, project["workspace_id"], project["project_id"],
        )
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可修改脚本")
        image_sensitive_fields = {"camera_type", "shot_size", "camera_move", "visual"}
        kind = "structure" if any(
            operation["operation"] == "delete_shot"
            or operation.get("field") in {"structure", "camera_type", "shot_size", "camera_move"}
            for operation in operations
        ) else "copy"
        if feedback_kind is not None and feedback_kind != kind and entrypoint != "assistant":
            raise WorkspaceServiceError(
                "FEEDBACK_KIND_MISMATCH", "反馈类型与规范化修改影响不一致",
            )
        rerun_nodes = (
            ["script_plan", "script_draft", "visual_binding", "workbook_render", "machine_audit"]
            if kind == "structure" else
            ["script_draft", "visual_binding", "workbook_render", "machine_audit"]
        )
        parameters = {
            "project_id": project["project_id"],
            "base_version_id": context.version_id,
            "expected_project_version": project["project_version"],
            "expected_revision": expected_revision,
            "feedback_kind": kind,
            "operations": operations,
            "entrypoint": entrypoint,
            "draft_only": draft_only,
            "idempotency_key": str(idempotency_key or "").strip(),
        }
        if assistant_selection is not None:
            parameters["assistant_selection"] = assistant_selection
        digest = sha256_json({"action": "apply_formal_script_revision", "parameters": parameters})
        response = {
            "type": "action_proposal", "title": "确认脚本修改",
            "summary": preview["summary"], "action": "apply_formal_script_revision",
            "proposal_digest": digest, "risk": "R2", "preview": preview,
            "draft_only": draft_only,
            "before": preview.get("before"), "after": preview.get("after"),
            "affected_shots": preview.get("affected_shots") or [],
            "affected_fields": preview["fields"],
            "rerun_nodes": [] if draft_only else rerun_nodes,
            "source_impact": preview.get("source_change") or "来源不变",
            "image_impact": (
                "应用草稿时不重算图片；保存新版本时统一处理"
                if draft_only and not any(
                    operation.get("field") in image_sensitive_fields for operation in operations
                )
                else "受影响镜头需重新匹配并确认图片；未受影响镜头保持原决定"
            ),
            "feedback_impact": (
                "修改进入当前草稿；保存新版本时统一记录"
                if draft_only else f"记录 {kind} 反馈并由子版本消费"
            ),
            "confirmation_required": True,
            "shot_reasons": list((assistant_selection or {}).get("shot_reasons") or []),
            "selection_mode": (assistant_selection or {}).get("selection_mode", "all"),
            "allowed_shot_ids": list((assistant_selection or {}).get("allowed_shot_ids") or dict.fromkeys(op["shot_id"] for op in operations)),
            "actual_changed_shot_ids": list(dict.fromkeys(op["shot_id"] for op in operations)),
            "suggestions": [{
                "shot_id": shot_id, "shot_no": next(op["shot_no"] for op in operations if op["shot_id"] == shot_id),
                "fields": list(dict.fromkeys(op.get("field", "整镜") for op in visible_operations if op["shot_id"] == shot_id)),
                "reason": next((r["reason"] for r in (assistant_selection or {}).get("shot_reasons") or [] if r["shot_id"] == shot_id), ""),
            } for shot_id in dict.fromkeys(op["shot_id"] for op in visible_operations)],
            "parameters": {
                "changes": [
                    {
                        "shot_no": operation["shot_no"],
                        "operation": operation["operation"],
                        **({"field": operation["field"]} if operation.get("field") else {}),
                    }
                    for operation in visible_operations
                ],
            },
            "will_change": "仅修改当前可恢复草稿中的已展示范围",
            "will_not_change": "不会修改 Brief、历史版本、发布状态或 CURRENT",
            "calls_models": bool(
                model_evidence and int(model_evidence.get("call_count") or 0) > 0
            ),
            "model_evidence": model_evidence or {
                "provider": "deterministic", "model": "none",
                "purpose": "literal_script_edit", "call_count": 0,
            },
            "model_call_statement": (
                "语义范围已由服务端 DeepSeek editor 分析；确认执行只写入已展示差异"
                if model_evidence and int(model_evidence.get("call_count") or 0) > 0
                else "本次为确定性修改，未调用模型"
            ),
            "impact_scope": "当前项目／镜头 " + "、".join(affected_shots),
            "next_steps": [
                "核对差异后应用到草稿；确认保存新版本前不会改动历史版本。"
                if draft_only else
                "核对前后差异后确认执行；版本或草稿变化会使提案失效。"
            ],
        }
        proposed = self.tasks.transition_task(
            auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="proposed",
            details={"response": response, "internal_formal_parameters": parameters},
        )
        return self._result(proposed, response)

    def execute(self, auth_token: str, *, project: dict, parameters: dict) -> dict:
        _, base_context = self.workspace._current_context(
            auth_token, project["workspace_id"], project["project_id"],
        )
        if base_context is None or base_context.version_id != parameters.get("base_version_id"):
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "修订基线版本已变化")
        current = self.workspace.read_current(
            auth_token, workspace_id=project["workspace_id"], project_id=project["project_id"],
        )
        if current.get("revision") != parameters["expected_revision"]:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿在提案后已变化；旧提案未执行")
        operations = parameters.get("operations") or (
            [] if parameters.get("materialize_existing_draft") else [parameters["operation"]]
        )
        if not operations:
            changed = current
        elif len(operations) > 1:
            changed = self.workspace.apply_revision_operations(
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                expected_revision=parameters["expected_revision"],
                expected_project_version=parameters["expected_project_version"],
                operations=operations,
                idempotency_key=f"{parameters['idempotency_key']}:change-set",
            )
        else:
            changed = self.workspace.apply_revision_operation(
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                expected_revision=parameters["expected_revision"],
                expected_project_version=parameters["expected_project_version"],
                operation=operations[0],
                idempotency_key=f"{parameters['idempotency_key']}:change:1",
            )
        if parameters.get("draft_only"):
            return {
                "type": "formal_script_draft_updated",
                "status": "completed",
                "revision": changed["revision"],
                "summary": "修改已应用到草稿；尚未创建新版本。",
                "affected_shots": sorted(
                    {str(item["shot_no"]) for item in operations}, key=int,
                ),
                "shots": changed["shots"],
            }
        def persist_feedback(connection, child_version_id, materialized, actor) -> None:
            for operation in operations:
                feedback_kind = (
                    "structure"
                    if operation["operation"] == "delete_shot" or operation.get("field") == "structure"
                    else parameters.get("feedback_kind", "copy")
                )
                connection.execute(
                    """INSERT INTO formal_version_feedback_records(
                    feedback_id, workspace_id, project_id, base_version_id, child_version_id,
                    feedback_kind, shot_no, payload_ref, payload_sha256,
                    created_by_actor_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("formal_feedback"), project["workspace_id"], project["project_id"],
                        base_context.version_id, child_version_id,
                        feedback_kind, str(operation.get("shot_no") or "") or None,
                        materialized["revision_report_ref"], materialized["revision_report_sha256"],
                        actor["actor_id"], utc_now(),
                    ),
                )
        saved = self.workspace.save_version(
            auth_token,
            workspace_id=project["workspace_id"],
            project_id=project["project_id"],
            expected_revision=changed["revision"],
            expected_project_version=project["project_version"],
            reason=(
                parameters.get("reason")
                if parameters.get("materialize_existing_draft")
                else f"已确认的{parameters.get('feedback_kind', 'copy')}反馈"
            ),
            idempotency_key=f"{parameters['idempotency_key']}:version",
            transaction_hook=persist_feedback,
            expected_progress_revision=parameters.get("expected_progress_revision"),
            image_snapshot_digest=parameters.get("image_snapshot_digest"),
        )
        return {
            "type": "formal_script_revision_completed",
            "status": "completed", "revision": saved["revision"],
            "summary": "已按确认差异创建不可变子版本；父版本保持不变。",
            "affected_shots": (
                list(parameters.get("affected_shots") or [])
                if parameters.get("materialize_existing_draft")
                else sorted({str(item["shot_no"]) for item in operations}, key=int)
            ),
            "shots": saved["shots"],
            "version_label": saved["version_label"],
        }

    @staticmethod
    def _preview(current: dict, operation: dict) -> dict:
        shot_no = str(operation["shot_no"])
        shot = next((item for item in current["shots"] if item["shot_no"] == shot_no), None)
        if shot is None:
            raise WorkspaceServiceError("DRAFT_INVALID", "目标镜头不存在")
        kind = operation["operation"]
        before_duration = sum(int(item["duration_seconds"]) for item in current["shots"])
        if kind == "delete_shot":
            if len(current["shots"]) <= 1:
                raise WorkspaceServiceError("DRAFT_INVALID", "脚本至少必须保留一镜")
            after_duration = before_duration - int(shot["duration_seconds"])
            return {
                "summary": f"删除第 {shot_no} 镜并连续重排后续镜号",
                "shot_no": shot_no, "field": "整镜", "before": shot, "after": None,
                "affected_shots": [item["shot_no"] for item in current["shots"] if int(item["shot_no"]) >= int(shot_no)],
                "duration_change_seconds": after_duration - before_duration,
                "source_change": "删除目标镜来源；其余镜头保留原始来源",
                "warnings": ["镜头数量将减少 1；相邻复核将按 N-1 重算。"],
            }
        field = operation["field"]
        employee_field = next(key for key, value in _EDITABLE_FIELDS.items() if key == field)
        before = shot[employee_field]
        after = operation.get("value", "")
        if before == after:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "修改内容与当前草稿相同")
        delta = int(after) - int(before) if field == "duration_seconds" else 0
        return {
            "summary": f"修改第 {shot_no} 镜的{_EDITABLE_FIELDS[field]}",
            "shot_no": shot_no, "field": _EDITABLE_FIELDS[field],
            "before": before, "after": after, "affected_shots": [shot_no],
            "duration_change_seconds": delta, "source_change": "来源不变", "warnings": [],
        }

    def _fact_dependencies(self) -> tuple[FormalBriefInputAdapter, BriefArtifactStore]:
        if self.brief_adapter is None or self.brief_store is None:
            raise WorkspaceServiceError(
                "FORMAL_FACT_REVISION_UNAVAILABLE", "Formal 事实修订服务依赖尚未完整接入",
            )
        return self.brief_adapter, self.brief_store

    @staticmethod
    def _display_fact_value(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value

    def _require_writes(self, code: str, message: str) -> None:
        if not self.writes_enabled:
            raise WorkspaceServiceError(code, message)

    @staticmethod
    def _idempotency_key(value: str) -> str:
        key = str(value or "").strip()
        if not key or len(key) > 160:
            raise WorkspaceServiceError("INVALID_INPUT", "幂等键无效")
        return key

    def _replayed_proposal(self, auth_token: str, task: dict) -> dict | None:
        if not task.get("replayed"):
            return None
        response, parameters = _stored_proposal(
            self.tasks.list_task_events(auth_token, task["task_id"]),
        )
        if response is None or parameters is None:
            raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "同一请求正在建立提案，请稍后重试")
        return self._result(task, response)

    def _existing_payload_proposal(
        self,
        auth_token: str,
        *,
        project_id: str,
        task_type: str,
        payload_sha256: str,
    ) -> dict | None:
        with self.workspace.services.store.connection() as connection:
            rows = connection.execute(
                """SELECT task_id FROM tasks
                WHERE project_id=? AND task_type=? AND payload_sha256=?
                ORDER BY created_at, task_id""",
                (project_id, task_type, payload_sha256),
            ).fetchall()
        for row in rows:
            try:
                task = self.tasks.get_task(auth_token, row["task_id"])
            except WorkspaceServiceError as exc:
                if exc.code == "PERMISSION_DENIED":
                    continue
                raise
            response, parameters = _stored_proposal(
                self.tasks.list_task_events(auth_token, task["task_id"]),
            )
            if response is not None and parameters is not None:
                return self._result(task, response)
        return None

    @staticmethod
    def _result(task: dict, response: dict) -> dict:
        return {"task_id": task["task_id"], "status": task["status"], "response": response}


# Compatibility import name; both assistant and direct Formal routes receive this one authority.
FormalScriptRevisionService = FormalRevisionService


def _stored_proposal(events: list[dict]) -> tuple[dict | None, dict | None]:
    for event in reversed(events):
        details = event.get("details") or {}
        response = details.get("response")
        parameters = details.get("internal_formal_parameters")
        if isinstance(response, dict) and isinstance(parameters, dict):
            return response, parameters
    return None, None
