from __future__ import annotations

"""正式项目旅程的窄 UI/API 边界。

这里不解析 Brief、不调用生成器，也不回退到 Alpha 输入。正式 Brief 的文件、事实、
冲突和确认只能穿过注入的 T01 ``FormalBriefInputAdapter``。
"""

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable, Literal, Optional, Union
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .executable_agent import ExecutableAgentService
from .formal_brief_domain import FormalBriefDomainError
from .formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument, _employee_document_key
from .formal_generation_service import FormalGenerationService
from .formal_image_workspace import (
    FormalImageWorkspaceService,
    ImageShotSelection,
)
from .formal_review_workspace import FormalReviewWorkspaceService
from .formal_script_workspace import FormalScriptWorkspaceService
from .manual_project_commands import ManualProjectCommandService, ManualProjectCommandServices
from .mutation_policy import require_active_project
from .readonly_agent import ReadonlyAgentService
from .task_service import TaskService
from .workspace_store import WorkspaceServiceError
from .script_type_adapter import metadata_for_category_id
from .input_limits import FORMAL_PASTED_TEXT_BYTES


FormalBriefUploadFactory = Callable[[str, BytesIO, bool], UploadedDocument]
MAX_DIRECT_BRIEF_TEXT_BYTES = FORMAL_PASTED_TEXT_BYTES


class StrictWritePayload(BaseModel):
    """Pydantic v2 request boundary: never silently discard caller fields."""

    model_config = ConfigDict(extra="forbid")


class OwnerPayload(StrictWritePayload):
    workspace_name: str = Field(min_length=1, max_length=120)
    display_name: str = Field(min_length=1, max_length=120)
    confirmed: bool = False


class ProjectPayload(StrictWritePayload):
    name: str = Field(min_length=1, max_length=120)
    category_id: str | None = Field(default=None, min_length=1, max_length=80)
    expected_workspace_version: int = Field(ge=0)

    @field_validator("category_id")
    @classmethod
    def known_category(cls, value: str | None) -> str | None:
        return metadata_for_category_id(value).category_id if value is not None else None


class TemplateActionPayload(StrictWritePayload):
    workspace_id: str = Field(min_length=1, max_length=128)
    expected_project_version: int = Field(ge=0)
    template_ref: str = Field(min_length=1, max_length=200)


class TemplateSelectPayload(TemplateActionPayload):
    confirmed: bool = False


class TemplateTrialPayload(TemplateActionPayload):
    generation_mode: Literal["text_only_draft", "image_backed"] = "text_only_draft"


class BriefActionPayload(StrictWritePayload):
    brief_version_id: str = Field(min_length=1, max_length=128)
    expected_project_version: int = Field(ge=0)


class ConflictResolutionPayload(BriefActionPayload):
    selected_fact_id: str = Field(min_length=1, max_length=128)


class BriefRetryPayload(BriefActionPayload):
    document_keys: list[StrictStr] = Field(min_length=1, max_length=20)


class BriefFieldUpdatePayload(BriefActionPayload):
    field_label: StrictStr = Field(min_length=1, max_length=64)
    value: StrictStr = Field(min_length=1, max_length=4000)


class PlanActionPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    # 业务服务在完成 Brief、创作标准、项目锁和图片理解检查后强制要求该值。
    # 这里允许缺省仅为避免 FastAPI 422 提前遮蔽更高优先级的权威门禁。
    generation_mode: Optional[Literal["image_backed", "text_only_draft"]] = None
    revision_task_id: Optional[str] = Field(default=None, min_length=8, max_length=160)
    expected_creative_revision: StrictInt | None = Field(default=None, ge=0)


class ImageCreationPayload(StrictWritePayload):
    expected_project_version: StrictInt = Field(ge=0)
    expected_creative_revision: StrictInt | None = Field(default=None, ge=0)
    expected_script_revision: StrictInt | None = Field(default=None, ge=0)
    expected_target_basis: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    purpose: Literal["storyboard", "complex", "rough"] | None = None
    target_kind: Literal["creative_block", "formal_shot"]
    target_id: StrictStr = Field(min_length=1, max_length=160)
    prompt: StrictStr = Field(min_length=1, max_length=3600)
    operation_key: StrictStr = Field(min_length=8, max_length=160)
    reference_image_ids: list[StrictStr] = Field(default_factory=list, max_length=14)
    reference_task_ids: list[StrictStr] = Field(default_factory=list, max_length=14)
    expected_prompt_sha256: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class ImagePromptPayload(StrictWritePayload):
    target_kind: Literal["creative_block", "formal_shot"]
    target_id: StrictStr = Field(min_length=1, max_length=160)
    additional_instruction: StrictStr = Field(default="", max_length=4000)
    edited_prompt: StrictStr | None = Field(default=None, min_length=1, max_length=3600)
    expected_prompt_sha256: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    regenerate: bool = False
    use_saved_prompt: bool = False


class ImageReferenceTransferPayload(StrictWritePayload):
    version_id: StrictStr = Field(min_length=1, max_length=160)


class ImageAdoptionPayload(StrictWritePayload):
    defer_selection: StrictBool = False
    expected_project_version: StrictInt = Field(ge=0)
    expected_creative_revision: StrictInt | None = Field(default=None, ge=0)
    expected_progress_revision: StrictInt | None = Field(default=None, ge=0)
    expected_script_revision: StrictInt | None = Field(default=None, ge=0)
    acknowledged_target_basis: StrictStr | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    target_kind: Literal["creative_block", "formal_shot"]
    target_id: StrictStr = Field(min_length=1, max_length=160)
    operation_key: StrictStr = Field(min_length=8, max_length=160)


class CreativeStartPayload(StrictWritePayload):
    expected_project_version: StrictInt = Field(ge=0)
    expected_revision: StrictInt = Field(ge=0)
    instruction: StrictStr = Field(min_length=1, max_length=32000)
    operation_key: StrictStr = Field(min_length=8, max_length=160)


class CreativeEditPayload(StrictWritePayload):
    expected_project_version: StrictInt = Field(ge=0)
    expected_revision: StrictInt = Field(ge=0)
    expected_draft_revision: StrictInt = Field(ge=0)
    action: Literal["patch", "undo", "redo", "delete"]
    operation_key: StrictStr = Field(min_length=8, max_length=160)
    block_id: StrictStr | None = Field(default=None, max_length=160)
    field: Literal["段落", "画面描述", "声音", "时长"] | None = None
    value: StrictStr | None = Field(default=None, max_length=6000)


class CreativeGenerationPayload(CreativeStartPayload):
    use_confirmed_brief: StrictBool = False


class CreativeRevisionPayload(CreativeStartPayload):
    selected_block_ids: list[StrictStr] = Field(min_length=1, max_length=80)


class CreativeRevisionConfirmPayload(StrictWritePayload):
    task_id: StrictStr = Field(min_length=8, max_length=160)
    proposal_digest: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    retained_block_ids: list[StrictStr] | None = Field(default=None, max_length=80)


class CreativeProtectionPayload(StrictWritePayload):
    expected_project_version: StrictInt = Field(ge=0)
    expected_revision: StrictInt = Field(ge=0)
    protected_block_ids: list[StrictStr] = Field(max_length=80)
    operation_key: StrictStr = Field(min_length=8, max_length=160)


class CreativeImageReferenceItem(StrictWritePayload):
    block_id: StrictStr = Field(min_length=1, max_length=160)
    library_candidate_key: StrictStr = Field(default="", max_length=160)
    image_key: StrictStr = Field(default="", max_length=160)
    task_id: StrictStr = Field(default="", max_length=160)
    content_sha256: StrictStr = Field(default="", max_length=64)
    primary: StrictBool = False


class CreativeImageReferencesPayload(StrictWritePayload):
    expected_project_version: StrictInt = Field(ge=0)
    expected_revision: StrictInt = Field(ge=0)
    operation_key: StrictStr = Field(min_length=8, max_length=160)
    selections: list[CreativeImageReferenceItem] = Field(max_length=240)
    replace_block_ids: list[StrictStr] = Field(default_factory=list, max_length=80)


class CreativeAnswerPayload(StrictWritePayload):
    expected_project_version: StrictInt = Field(ge=0)
    expected_revision: StrictInt = Field(ge=0)
    field: Literal["脚本类型", "目标时长", "风格调性", "视频目标"]
    value: StrictStr = Field(min_length=1, max_length=4000)
    operation_key: StrictStr = Field(min_length=8, max_length=160)


class PlanConflictChoicePayload(StrictWritePayload):
    conflict_id: str = Field(min_length=1, max_length=256)
    choice: Literal["brief_wins", "template_wins"]


class PlanConflictResolutionPayload(StrictWritePayload):
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_project_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    resolutions: list[PlanConflictChoicePayload] = Field(min_length=1, max_length=100)


class PlanConflictResolutionExecutePayload(PlanConflictResolutionPayload):
    command_token: str = Field(min_length=24, max_length=2048)


class ImageAnalysisPayload(StrictWritePayload):
    expected_analysis_key: str | None = Field(default=None, max_length=160)
    expected_project_version: int = Field(ge=0)
    status: str = Field(min_length=1, max_length=32)
    caption: str = Field(min_length=1, max_length=500)
    tags: list[str] = Field(min_length=1, max_length=32)
    subjects: list[str] = Field(min_length=1, max_length=32)
    scene: str = Field(min_length=1, max_length=500)
    purpose: str = Field(min_length=1, max_length=500)
    quality: str = Field(min_length=1, max_length=500)
    brand: str = Field(min_length=1, max_length=500)
    evidence: list[str] = Field(min_length=1, max_length=32)
    quality_flags: list[Literal[
        "slight_blur", "severe_blur", "low_resolution", "overexposed",
        "underexposed", "bad_crop", "watermark", "unreadable_subject",
    ]] = Field(default_factory=list, max_length=8)
    negative_flags: list[Literal[
        "external_brand", "forbidden_content", "sensitive_content",
    ]] = Field(default_factory=list, max_length=3)


class ImageAnalysisSelectionPayload(StrictWritePayload):
    image_key: str = Field(min_length=1, max_length=160)
    analysis_key: str = Field(min_length=1, max_length=160)


class ImageAnalysisConfirmationPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    selections: list[ImageAnalysisSelectionPayload] = Field(min_length=1, max_length=400)
    note: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ImageProgressSelectionPayload(StrictWritePayload):
    shot_id: str = Field(min_length=1, max_length=160)
    selection_active: bool = True
    image_key: str = Field(default="", max_length=160)
    library_candidate_key: str = Field(default="", max_length=160)
    checked: bool = False
    remove: bool = False
    decision_kind: Literal["image_confirmed", "temporary_text_basis"] = "image_confirmed"
    temporary_reason: str = Field(default="", max_length=500)
    reuse_reason: str = Field(default="", max_length=500)
    query: str = Field(default="", max_length=300)


class ImageProgressPayload(StrictWritePayload):
    base_version_id: str = Field(min_length=8, max_length=160)
    expected_project_version: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    expected_script_revision: int = Field(ge=0)
    selections: list[ImageProgressSelectionPayload] = Field(max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ImageConfirmationPreviewPayload(ImageProgressPayload):
    pass


class ImageConfirmationPayload(ImageConfirmationPreviewPayload):
    preview_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_shot_ids: list[str] = Field(min_length=1, max_length=200)


class ImageShotSelectionPayload(StrictWritePayload):
    shot_no: str = Field(min_length=1, max_length=32)
    image_key: str = Field(default="", max_length=160)
    confirmed: Literal[True]
    decision_kind: Literal["image_confirmed", "temporary_text_basis"] = "image_confirmed"
    temporary_reason: str = Field(default="", max_length=500)
    candidate_snapshot_digest: str = Field(default="", pattern=r"^$|^[0-9a-f]{64}$")
    query: str = Field(default="", max_length=300)
    reuse_reason: str = Field(default="", max_length=500)


class ImageVersionSavePayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    selections: list[ImageShotSelectionPayload] = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ImageVersionPreviewPayload(ImageVersionSavePayload):
    base_version_id: str = Field(min_length=8, max_length=160)
    source_revision_task_id: Optional[str] = Field(default=None, min_length=8, max_length=160)
    allowed_shot_ids: Optional[list[str]] = Field(default=None, min_length=1, max_length=200)


class ImageFeedbackProposalPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    shot_no: str = Field(min_length=1, max_length=32)
    instruction: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ImageVersionConfirmPayload(StrictWritePayload):
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DefaultImageSelectionPayload(StrictWritePayload):
    shot_no: str = Field(min_length=1, max_length=32)
    candidate_key: str = Field(min_length=1, max_length=160)


class DefaultImageProposalPayload(StrictWritePayload):
    expected_script_revision: int | None = Field(default=None, ge=0)
    expected_project_version: int = Field(ge=0)
    selections: list[DefaultImageSelectionPayload] = Field(min_length=1, max_length=400)
    idempotency_key: str = Field(min_length=1, max_length=160)
    allowed_shot_ids: Optional[list[str]] = Field(default=None, min_length=1, max_length=200)


class ScriptTableChangePayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    shot_no: str = Field(min_length=1, max_length=32)
    shot_id: str = Field(min_length=1, max_length=160)
    field: Literal[
        "structure", "camera_type", "shot_size", "camera_move",
        "duration_seconds", "visual", "sound",
        "voiceover", "subtitle", "notes",
    ]
    value: Union[StrictStr, StrictInt]
    idempotency_key: str = Field(min_length=1, max_length=160)


class ImageBatchProposalPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    instruction: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=160)
    selected_shot_ids: list[str] = Field(default_factory=list, max_length=200)
    save_eligible: StrictBool = False
    mode: Literal['', 'fill_missing', 'replace_selected'] = ''
    target_kind: Literal['creative_block', 'formal_shot'] = 'formal_shot'
    selected_block_ids: list[str] = Field(default_factory=list, max_length=240)
    expected_creative_revision: int | None = Field(default=None, ge=0)


class ImageBatchConfirmPayload(StrictWritePayload):
    proposal_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    selected_candidate_keys: list[str] = Field(min_length=1, max_length=200)


class ImageBatchChoicePayload(StrictWritePayload):
    shot_id: str | None = Field(default=None, min_length=1, max_length=160)
    block_id: str | None = Field(default=None, min_length=1, max_length=160)
    candidate_key: str | None = Field(default=None, max_length=160)
    image_key: str | None = Field(default=None, max_length=160)
    library_candidate_key: str | None = Field(default=None, max_length=160)
    included: StrictBool | None = None
    prepare_generation: StrictBool | None = None
    query: str = Field(default='', max_length=300)


class ImageBatchRevisePayload(StrictWritePayload):
    proposal_digest: str = Field(pattern=r'^[0-9a-f]{64}$')
    updates: list[ImageBatchChoicePayload] = Field(min_length=1, max_length=200)


class ImageBatchCancelPayload(StrictWritePayload):
    proposal_digest: str = Field(pattern=r'^[0-9a-f]{64}$')


class ScriptInstructionPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    instruction: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ScriptRevisionProposalPayload(ScriptInstructionPayload):
    entrypoint: Literal["table", "assistant", "review", "history"]
    feedback_kind: Literal["copy", "structure"]
    analysis_task_id: str = Field(default="", max_length=160)
    selected_shot_ids: list[str] = Field(default_factory=list, max_length=400)


class ScriptAssistantLocksPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    locked_shot_ids: list[str] = Field(default_factory=list, max_length=400)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ScriptSuggestionSelectionPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    retained_shot_ids: list[str] = Field(max_length=400)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ScriptHistoryPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    expected_revision: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ScriptSavePayload(ScriptHistoryPayload):
    reason: str = Field(min_length=1, max_length=500)
    expected_progress_revision: int | None = Field(default=None, ge=0)


class FactRevisionProposalPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    field: Literal[
        "项目名称", "品牌或客户", "脚本类型", "行业或活动类别", "视频目标",
        "使用场景", "目标时长", "必须出现的内容", "禁止或不能出现的内容",
        "关键事件与流程", "特殊需求",
    ]
    value: str = Field(min_length=1, max_length=4000)
    evidence: str = Field(min_length=1, max_length=4000)
    idempotency_key: str = Field(min_length=1, max_length=160)


class FactRevisionConfirmPayload(StrictWritePayload):
    proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ReviewStartPayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=1, max_length=160)


class ReviewDecisionPayload(ReviewStartPayload):
    issue_keys: list[str] = Field(min_length=1, max_length=400)
    decision: Literal["approve", "return"]
    note: str = Field(default="", max_length=4000)


class ReviewFinalPayload(ReviewStartPayload):
    confirmed: Literal[True]
    note: str = Field(default="", max_length=4000)


class ProjectLifecyclePayload(StrictWritePayload):
    expected_project_version: int = Field(ge=0)
    idempotency_key: str = Field(min_length=8, max_length=160)
    reason: str = Field(min_length=1, max_length=500)


class ProjectLifecycleExecutePayload(ProjectLifecyclePayload):
    command_token: str = Field(min_length=24, max_length=2048)


def _readonly(api: ReadonlyAgentService | ExecutableAgentService) -> ReadonlyAgentService:
    return api.services.readonly if isinstance(api, ExecutableAgentService) else api


def formal_setup_status(
    agent_api: ReadonlyAgentService | ExecutableAgentService,
    *,
    brief_adapter: FormalBriefInputAdapter | None,
    brief_upload_factory: FormalBriefUploadFactory | None,
    formal_generation_service: FormalGenerationService | None,
    formal_script_service: FormalScriptWorkspaceService | None,
    formal_review_service: FormalReviewWorkspaceService | None = None,
    formal_admin_service=None,
) -> dict:
    """Return the formal employee setup DTO without consulting local runtime state."""
    owner = agent_api.setup_status()
    enabled = owner.get("enabled") is True
    brief_available = brief_adapter is not None and brief_upload_factory is not None
    plan_dependencies = brief_available and formal_generation_service is not None
    generation_available = (
        plan_dependencies
        and formal_generation_service.provider_policy is not None
    )
    plan_write_available = (
        enabled and generation_available
        and owner.get("formal_agent_writes") == "available"
    )
    script_revision_available = (
        enabled and owner.get("formal_script_revision") == "available"
    )
    default_image_available = (
        enabled and owner.get("formal_default_image_adoption") == "available"
    )
    return {
        "setup_state": "owner_ready" if enabled else "owner_confirmation_required",
        "workspace_capability": "authenticated_access" if enabled else "owner_creation_available",
        "project_capability": "available" if enabled else "workspace_required",
        "brief_availability": "available" if brief_available else "blocked",
        "plan_availability": "available" if plan_dependencies else "blocked",
        "generation_availability": "available" if generation_available else "blocked",
        "script_availability": "available" if formal_script_service is not None else "blocked",
        "review_availability": "available" if formal_review_service is not None else "blocked",
        "admin_diagnostics_availability": "available" if formal_admin_service is not None else "blocked",
        "project_archival_availability": "available" if enabled else "blocked",
        "project_purge_availability": "blocked",
        "capabilities": {
            "read": {
                "state": "available" if enabled else "blocked",
                "code": "" if enabled else "FORMAL_READ_UNAVAILABLE",
            },
            "plan_write": {
                "state": "available" if plan_write_available else "blocked",
                "code": "" if plan_write_available else "FORMAL_PLAN_WRITES_DISABLED",
            },
            "script_revision": {
                "state": "available" if script_revision_available else "blocked",
                "code": "" if script_revision_available else "FORMAL_SCRIPT_REVISION_DISABLED",
            },
            "default_image_adoption": {
                "state": "available" if default_image_available else "blocked",
                "code": "" if default_image_available else "FORMAL_DEFAULT_IMAGE_ADOPTION_DISABLED",
            },
        },
        # Existing formal.html consumes these two values.  They remain business
        # states, not a compatibility bridge to the legacy bootstrap payload.
        "enabled": enabled,
        "brief_adapter": "ready" if brief_available else "BLOCK",
        "formal_plan": "ready" if plan_dependencies else "BLOCK",
        "formal_plan_code": (
            "FORMAL_GENERATION_PROVIDER_UNAVAILABLE"
            if plan_dependencies and not generation_available
            else "" if plan_dependencies else "FORMAL_PLAN_UNAVAILABLE"
        ),
    }


def install_formal_routes(
    app: FastAPI,
    *,
    agent_api: ReadonlyAgentService | ExecutableAgentService,
    brief_adapter: FormalBriefInputAdapter | None = None,
    brief_upload_factory: FormalBriefUploadFactory | None = None,
    formal_generation_service: FormalGenerationService | None = None,
    formal_script_service: FormalScriptWorkspaceService | None = None,
    formal_image_service: FormalImageWorkspaceService | None = None,
    formal_review_service: FormalReviewWorkspaceService | None = None,
    formal_admin_service=None,
    include_legacy_root: bool = False,
) -> None:
    """Install the formal Brief API on an existing application.

    The adapter is deliberately optional only so an unintegrated deployment can
    render its blocked state.  It never falls back to legacy/Alpha Brief APIs.
    """
    app.state.agent_api = agent_api
    app.state.brief_adapter = brief_adapter
    app.state.formal_generation_service = formal_generation_service
    app.state.formal_script_service = formal_script_service
    app.state.formal_image_service = formal_image_service
    app.state.formal_review_service = formal_review_service
    app.state.formal_admin_service = formal_admin_service
    projects = _readonly(agent_api).services.projects
    project_commands = ManualProjectCommandService(
        ManualProjectCommandServices(store=projects.store)
    )

    def failure(exc: Exception) -> HTTPException:
        code = "FORMAL_BRIEF_INVALID" if isinstance(exc, FormalBriefDomainError) else str(getattr(exc, "code", "INTERNAL_ERROR"))
        status = 401 if code == "AUTH_SESSION_INVALID" else 403 if code in {"PERMISSION_DENIED", "CROSS_PROJECT_DENIED"} else 404 if code == "NOT_FOUND" else 409
        detail = {"code": code, "message": str(getattr(exc, "message", "") or exc)}
        if getattr(exc, "issues", None):
            detail["issues"] = exc.issues
        return HTTPException(status_code=status, detail=detail)

    def require_adapter() -> tuple[FormalBriefInputAdapter, FormalBriefUploadFactory]:
        adapter = app.state.brief_adapter
        if adapter is None or brief_upload_factory is None:
            raise WorkspaceServiceError(
                "FORMAL_BRIEF_ADAPTER_UNAVAILABLE",
                "正式 Brief 输入适配器尚未接入；项目保持不可规划。",
            )
        return adapter, brief_upload_factory

    def require_generation() -> FormalGenerationService:
        service = app.state.formal_generation_service
        if (
            service is None
            or app.state.brief_adapter is None
            or brief_upload_factory is None
        ):
            raise WorkspaceServiceError(
                "FORMAL_PLAN_UNAVAILABLE",
                "正式 Brief、上传边界或规划服务尚未完整接入；项目保持不可规划。",
            )
        return service

    def require_script() -> FormalScriptWorkspaceService:
        service = app.state.formal_script_service
        if service is None:
            raise WorkspaceServiceError(
                "FORMAL_SCRIPT_UNAVAILABLE",
                "脚本编辑与版本服务尚未接入；当前版本不会被修改。",
            )
        return service

    def require_revision():
        service = getattr(agent_api, "script_revision", None)
        if service is None:
            raise WorkspaceServiceError(
                "FORMAL_SCRIPT_REVISION_UNAVAILABLE",
                "Formal 统一修订服务尚未接入；当前项目不会被修改。",
            )
        return service

    def require_image_workspace() -> FormalImageWorkspaceService:
        service = app.state.formal_image_service
        if service is None:
            raise WorkspaceServiceError(
                "FORMAL_IMAGE_WORKSPACE_UNAVAILABLE",
                "逐镜图片确认服务尚未接入；当前版本不会被修改。",
            )
        return service

    def require_review_workspace() -> FormalReviewWorkspaceService:
        service = app.state.formal_review_service
        if service is None:
            raise WorkspaceServiceError(
                "FORMAL_REVIEW_UNAVAILABLE",
                "项目经理审核服务尚未接入；当前版本不会写入人工结论。",
            )
        return service

    def project_overview(auth_token: str, project: dict) -> dict:
        """Compose the employee overview from existing authorities; no state is persisted here."""
        project_id = project["project_id"]
        workspace_id = project["workspace_id"]
        stages = [
            {"id": "brief", "label": "Brief 与资料", "state": "未开始", "detail": "导入并确认项目资料。"},
            {"id": "materials", "label": "项目素材", "state": "未开始", "detail": "准备规划前可用的项目图片。"},
            {"id": "planning", "label": "规划与生成", "state": "未开始", "detail": "确认事件和镜头安排。"},
            {"id": "script", "label": "脚本与版本", "state": "未开始", "detail": "创建并保存脚本版本。"},
            {"id": "shot_images", "label": "逐镜配图", "state": "未开始", "detail": "为成稿镜头逐镜确认图片。"},
            {"id": "review", "label": "审核与交付", "state": "未开始", "detail": "逐项审核并确认整片。"},
        ]
        blockers: list[str] = []
        warnings: list[str] = []
        next_action = None
        brief_confirmed = False
        try:
            if app.state.brief_adapter is not None:
                brief = app.state.brief_adapter.read_current_formal(
                    auth_token, workspace_id=workspace_id, project_id=project_id,
                )
                if brief.get("status") != "empty":
                    brief_confirmed = brief.get("confirmation_status") == "confirmed" and not brief.get("unresolved_conflict_count")
                    stages[0]["state"] = "已完成" if brief_confirmed else "待确认"
                    stages[0]["detail"] = "资料已确认，可继续处理项目。" if brief_confirmed else "请核对资料和需要确认的内容。"
                    if not brief_confirmed:
                        blockers.append("Brief 仍有待确认内容。")
                else:
                    blockers.append("尚未导入项目 Brief。")
            else:
                blockers.append("Brief 服务暂不可用。")
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            blockers.append(str(getattr(exc, "message", "Brief 当前不可读取") or exc))

        activity = None
        if brief_confirmed and formal_generation_service is not None:
            try:
                readiness = formal_generation_service.get_image_readiness(
                    auth_token, workspace_id=workspace_id, project_id=project_id,
                    expected_project_version=project["project_version"],
                )
                total = int(readiness.get("total") or 0)
                attention = sum(int(readiness.get(key) or 0) for key in ("pending", "low_confidence", "blocked", "failed"))
                if total and not attention:
                    stages[1]["state"] = "已完成"
                    stages[1]["detail"] = f"已有 {total} 张图片可用于规划。"
                else:
                    stages[1]["state"] = "待确认" if not total else "需处理"
                    stages[1]["detail"] = "可补充图片，或继续生成文字脚本。" if not total else "仍有项目图片需要核对。"
                    if attention:
                        warnings.append(stages[1]["detail"])
            except (FormalBriefDomainError, WorkspaceServiceError) as exc:
                stages[1]["state"] = "需处理"
                blockers.append(str(getattr(exc, "message", "项目素材当前不可读取") or exc))
            try:
                activity = formal_generation_service.list_project_activity(
                    auth_token, workspace_id=workspace_id, project_id=project_id,
                )
            except WorkspaceServiceError as exc:
                blockers.append(str(getattr(exc, "message", "任务状态当前不可读取") or exc))

        latest = next(iter((activity or {}).get("items") or []), None)
        if latest:
            task_state = latest.get("state")
            if task_state == "completed":
                stages[2].update(state="已完成", detail=latest.get("next_action") or "规划任务已完成，请核对实际结果。")
            elif task_state == "waiting_user":
                stages[2]["state"] = "待确认"
            elif task_state in {"blocked", "failed", "partially_completed"}:
                stages[2]["state"] = "需处理"
                if latest.get("issue"):
                    blockers.append(latest["issue"])
            else:
                stages[2]["state"] = "进行中"
            next_action = {
                "label": latest.get("action_label"),
                "href": latest.get("action_href"),
                "reason": latest.get("next_action"),
            }
        if brief_confirmed and not project.get("current_version_id") and formal_generation_service is not None:
            try:
                planning = formal_generation_service.get_planning_readiness(
                    auth_token, workspace_id=workspace_id, project_id=project_id,
                    expected_project_version=project["project_version"],
                )
                if not planning["ready"]:
                    stages[2].update(state="需处理", detail=planning["message"])
                    blockers.append(planning["message"])
                    next_action = {"label": "补充规划资料", "href": f"/projects/{project_id}/brief", "reason": planning["message"]}
                elif latest and latest.get("issue_code") == "confirmed_brief_needs_clarification":
                    if latest.get("issue") in blockers:
                        blockers.remove(latest["issue"])
                    stages[2].update(state="未开始", detail="规划字段已核对，可以重新生成规划。")
                    next_action = {"label": "重新生成规划", "href": f"/projects/{project_id}/plan", "reason": stages[2]["detail"]}
            except (FormalBriefDomainError, WorkspaceServiceError) as exc:
                stages[2].update(state="需处理", detail=str(getattr(exc, "message", "") or exc))
                blockers.append(stages[2]["detail"])
                next_action = {"label": "核对规划资料", "href": f"/projects/{project_id}/brief", "reason": stages[2]["detail"]}
        if not project.get("current_version_id") and formal_generation_service is not None:
            try:
                creative = formal_generation_service.read_creative_draft(auth_token, workspace_id=workspace_id, project_id=project_id)
                if creative.get("blocks"):
                    stages[3].update(state="初稿已保存", detail=f"已有 {len(creative['blocks'])} 段初稿，尚未创建正式版本。")
            except WorkspaceServiceError as exc:
                stages[3].update(state="读取失败", detail=exc.message)
        if project.get("current_version_id"):
            stages[2]["state"] = "已完成"
            stages[3]["state"] = "已完成"
            stages[4]["state"] = "待确认"
            stages[5]["state"] = "未开始"
            next_action = {
                "label": "进入逐镜配图",
                "href": f"/projects/{project_id}/shot-images",
                "reason": "请为每个成稿镜头确认配图。",
            }
            image_model = None
            review_model = None
            if app.state.formal_image_service is not None:
                try:
                    image_model = app.state.formal_image_service.read_current(
                        auth_token,
                        workspace_id=workspace_id,
                        project_id=project_id,
                        expected_project_version=project["project_version"],
                    )
                except WorkspaceServiceError as exc:
                    stages[4]["state"] = "需处理"
                    blockers.append(str(getattr(exc, "message", "逐镜配图当前不可读取") or exc))
            if (image_model or {}).get("state") == "confirmed":
                shot_count = int((image_model or {}).get("shot_count") or 0)
                stages[4]["state"] = "已完成"
                stages[4]["detail"] = (
                    f"已确认 {shot_count} 个镜头的图片。"
                    if shot_count else "逐镜图片已确认。"
                )
                stages[5]["state"] = "待确认"
                next_action = {
                    "label": "进入审核与交付",
                    "href": f"/projects/{project_id}/review",
                    "reason": "逐镜图片已确认，请完成项目经理审核。",
                }
                if app.state.formal_review_service is not None:
                    try:
                        review_model = app.state.formal_review_service.read_current(
                            auth_token,
                            workspace_id=workspace_id,
                            project_id=project_id,
                            expected_project_version=project["project_version"],
                        )
                        review_state = str(review_model.get("state") or "")
                        if review_state == "confirmed":
                            stages[5]["state"] = "已完成"
                            stages[5]["detail"] = "整片确认已完成，可下载正式交付。"
                            next_action = {
                                "label": "下载正式交付",
                                "href": f"/projects/{project_id}/review",
                                "reason": "整片确认已完成，可下载正式 Excel；PDF 按需生成。",
                            }
                        elif review_state == "ready_final":
                            stages[5]["state"] = "待确认"
                            stages[5]["detail"] = "全部审核项已处理，等待整片最终确认。"
                            next_action = {
                                "label": "完成整片确认",
                                "href": f"/projects/{project_id}/review",
                                "reason": "全部审核项已处理，请完成整片最终确认。",
                            }
                        elif review_state == "needs_change":
                            stages[5]["state"] = "需处理"
                            stages[5]["detail"] = "当前版本已退回，请修改并保存新版本。"
                            edit_action = next(iter(review_model.get("edit_actions") or []), None)
                            next_action = edit_action or {
                                "label": "查看退回意见",
                                "href": f"/projects/{project_id}/review",
                                "reason": "当前版本存在退回项。",
                            }
                        elif review_state == "reviewing":
                            stages[5]["state"] = "进行中"
                            stages[5]["detail"] = "项目经理审核进行中。"
                    except WorkspaceServiceError as exc:
                        stages[5]["state"] = "需处理"
                        blockers.append(str(getattr(exc, "message", "审核状态当前不可读取") or exc))
            elif (image_model or {}).get("state") == "temporary_text_basis":
                stages[4]["state"] = "需处理"
                stages[4]["detail"] = "当前版本含暂准文字依据，不能审核或交付。"
                blockers.append(stages[4]["detail"])
        elif brief_confirmed and not next_action:
            next_action = {"label": "进入规划与生成", "href": f"/projects/{project_id}/plan", "reason": "先查看事件和镜头安排。"}
        elif not brief_confirmed:
            next_action = {"label": "进入 Brief 与资料", "href": f"/projects/{project_id}/brief", "reason": "先导入并确认项目资料。"}
        draft = {"state": "empty", "can_save_version": False, "revision": 0}
        saved_delivery = None
        if project.get("current_version_id") and app.state.formal_script_service is not None:
            try:
                script_model = app.state.formal_script_service.read_current(
                    auth_token, workspace_id=workspace_id, project_id=project_id,
                )
                draft = {key: script_model[key] for key in (
                    "state", "can_save_version", "revision", "base_version_id", "version_label",
                ) if key in script_model}
                version_label = str(script_model.get("version_label") or "已保存版本")
                # Working image progress must not revoke frozen-version delivery.
                if review_model is None and app.state.formal_review_service is not None:
                    try:
                        review_model = app.state.formal_review_service.read_current(
                            auth_token, workspace_id=workspace_id, project_id=project_id,
                            expected_project_version=project["project_version"],
                        )
                    except WorkspaceServiceError:
                        warnings.append("已保存版本的交付状态暂不可读取，请在审核与交付页重新核对。")
                if (review_model or {}).get("state") == "confirmed":
                    saved_delivery = {
                        "label": f"下载{version_label}正式交付",
                        "href": f"/projects/{project_id}/review",
                        "version_label": version_label,
                    }
                if script_model.get("can_save_version"):
                    stages[3].update(state="待保存", detail="当前稿已暂存，尚未保存为新版本。")
                    for stage in stages[4:]:
                        stage["detail"] = f"{version_label}：{stage['detail']}"
                    next_action = {
                        "label": "保存当前稿", "href": f"/projects/{project_id}/script?panel=script&action=save",
                        "reason": "当前稿有待保存修改；配图、审核与交付仍属于已保存版本。",
                    }
            except WorkspaceServiceError as exc:
                draft = {"state": "unknown", "can_save_version": False, "revision": None}
                stages[3].update(state="需处理", detail="当前稿状态暂不可读取，请重试。")
                blockers.append(str(getattr(exc, "message", stages[3]["detail"]) or exc))
                next_action = {
                    "label": "重新读取当前稿", "href": f"/projects/{project_id}/script",
                    "reason": stages[3]["detail"],
                }
        if project.get("status") == "archived":
            next_action = {"label": "查看归档设置", "href": f"/projects/{project_id}/overview", "reason": "项目已归档。内容仍可查看；恢复后才能继续修改。"}
        active_stage = next((stage for stage in stages if stage["state"] not in {"已完成", "未开始"}), None)
        progress = agent_api.services.projects.progress_projection(project,
            stage=f"{active_stage['label']} · {active_stage['state']}" if active_stage else "查看项目进度",
            next_action=next_action)
        return {
            "summary": progress,
            "project_id": project_id,
            "project_status": project.get("status"),
            "stages": stages,
            "blockers": list(dict.fromkeys(blockers)),
            "warnings": list(dict.fromkeys(warnings)),
            "next_action": progress["next_action"],
            "draft": draft,
            "saved_delivery": saved_delivery,
            "related_tasks": (activity or {}).get("items", []) if activity else [],
        }

    def employee_plan(value: dict) -> dict:
        return {
            key: value[key]
            for key in (
                "plan_id", "plan_sha256", "status", "events", "shot_count",
                "total_duration_seconds", "shots", "requirements", "coverage",
                "capacity_report", "traceability", "audit", "needs_clarification",
                "template_rule_contract", "conflicts", "decision_counts",
                "template_summary",
                "creative_conversion",
            )
            if key in value
        }

    def employee_completion(value: dict) -> dict:
        result = {
            key: value[key]
            for key in (
                "plan_id", "status", "version_id", "script_rows",
                "pending_confirmations", "traceability",
            )
            if key in value
        }
        if "script_rows" in result:
            result["script_rows"] = [
                {
                    key: row[key]
                    for key in ("镜号", "画面描述", "声音", "时长(秒)")
                    if key in row
                }
                for row in result["script_rows"]
            ]
        return result

    def require_project_scope(
        auth_token: str, *, workspace_id: str, project_id: str, write: bool = False,
    ) -> dict:
        project = projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
        if write:
            require_active_project(project)
        return project

    def token(value: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> str:
        if not value.strip():
            raise HTTPException(status_code=401, detail={"code": "AUTH_SESSION_INVALID"})
        return value

    @app.get("/api/formal/setup")
    def setup() -> dict:
        return formal_setup_status(
            agent_api,
            brief_adapter=brief_adapter,
            brief_upload_factory=brief_upload_factory,
            formal_generation_service=formal_generation_service,
            formal_script_service=formal_script_service,
            formal_review_service=formal_review_service,
            formal_admin_service=formal_admin_service,
        )

    @app.post("/api/formal/owner")
    def enable_owner(payload: OwnerPayload) -> dict:
        try:
            return agent_api.enable_local_owner(**payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/workspaces/{workspace_id}")
    def workspace(workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return projects.get_workspace(token(auth_token), workspace_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/workspaces/{workspace_id}/projects")
    def list_projects(workspace_id: str, summaries: bool = False, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            items = agent_api.list_projects(verified, workspace_id)
            if summaries:
                progress = agent_api.services.projects.list_progress_summaries(verified, workspace_id)
                items = [{**item, "summary": progress.get(item["project_id"])} for item in items]
            return {"items": items}
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/tasks/{task_id}/context")
    def task_action_context(project_id: str, task_id: str,
                            auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return require_revision().read_task_context(token(auth_token), project_id=project_id, task_id=task_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/workspaces/{workspace_id}/tasks")
    def workspace_tasks(
        workspace_id: str,
        project_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
        page_size: Optional[int] = None,
        cursor: Optional[str] = None,
        query: str = "",
        created_from: str = "",
        created_to: str = "",
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            if page_size is not None:
                return agent_api.services.tasks.query_workspace_tasks(
                    verified, workspace_id, project_id=project_id, status=status,
                    page_size=page_size, cursor=cursor, query=query,
                    created_from=created_from, created_to=created_to,
                )
            items = agent_api.services.tasks.list_workspace_tasks(
                verified,
                workspace_id,
                project_id=project_id,
                status=status,
                limit=limit,
            )
            return {"items": items, "count": len(items)}
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/workspaces/{workspace_id}/projects")
    def create_project(workspace_id: str, payload: ProjectPayload, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return projects.create_project(
                token(auth_token), workspace_id=workspace_id, name=payload.name,
                category_id=payload.category_id,
                expected_workspace_version=payload.expected_workspace_version,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}")
    def project(project_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return projects.get_project(token(auth_token), project_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/overview")
    def project_overview_state(project_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            project = projects.get_project(verified, project_id)
            return project_overview(verified, project)
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/lifecycle")
    def project_lifecycle(project_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            project = projects.get_project(token(auth_token), project_id)
            recovery = (
                project_commands.recovery_status(token(auth_token), project_id)
                if project["status"] == "archived"
                else {"available": False, "reason": "PROJECT_NOT_ARCHIVED", "remaining_days": 0}
            )
            return {
                "project_id": project_id,
                "status": project["status"],
                "project_version": project["project_version"],
                "archive_available": project["status"] == "active",
                "restore": recovery,
                "purge_available": False,
            }
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    def prepare_lifecycle(
        project_id: str, action: str, payload: ProjectLifecyclePayload, auth_token: str,
    ) -> dict:
        try:
            tool_name = f"{action}_project"
            return project_commands.prepare(
                token(auth_token), tool_name=tool_name,
                parameters={"project_id": project_id, **payload.model_dump()},
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    def execute_lifecycle(
        project_id: str, action: str, payload: ProjectLifecycleExecutePayload, auth_token: str,
    ) -> dict:
        try:
            parameters = payload.model_dump()
            command_token = parameters.pop("command_token")
            return project_commands.execute(
                token(auth_token), tool_name=f"{action}_project",
                parameters={"project_id": project_id, **parameters},
                command_token=command_token,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/lifecycle/archive/prepare")
    def prepare_project_archive(project_id: str, payload: ProjectLifecyclePayload, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        return prepare_lifecycle(project_id, "archive", payload, auth_token)

    @app.post("/api/formal/projects/{project_id}/lifecycle/archive/execute")
    def execute_project_archive(project_id: str, payload: ProjectLifecycleExecutePayload, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        return execute_lifecycle(project_id, "archive", payload, auth_token)

    @app.post("/api/formal/projects/{project_id}/lifecycle/restore/prepare")
    def prepare_project_restore(project_id: str, payload: ProjectLifecyclePayload, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        return prepare_lifecycle(project_id, "restore", payload, auth_token)

    @app.post("/api/formal/projects/{project_id}/lifecycle/restore/execute")
    def execute_project_restore(project_id: str, payload: ProjectLifecycleExecutePayload, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        return execute_lifecycle(project_id, "restore", payload, auth_token)

    @app.get("/api/formal/projects/{project_id}/brief")
    def brief_state(project_id: str, workspace_id: str, brief_version_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            adapter, _ = require_adapter()
            structured = adapter.read_structured(
                verified, workspace_id=workspace_id, project_id=project_id,
                brief_version_id=brief_version_id,
            )
            return structured
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/brief/current")
    def current_brief_state(project_id: str, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            adapter, _ = require_adapter()
            return adapter.read_current_formal(
                verified, workspace_id=workspace_id, project_id=project_id,
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/brief/field")
    def update_brief_field(project_id: str, payload: BriefFieldUpdatePayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            adapter, _ = require_adapter()
            return adapter.update_field(verified, workspace_id=workspace_id, project_id=project_id,
                brief_version_id=payload.brief_version_id, field_label=payload.field_label,
                value=payload.value, expected_project_version=payload.expected_project_version)
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/brief/files")
    async def upload_brief_files(project_id: str, request: Request, workspace_id: str, expected_project_version: int, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            adapter, factory = require_adapter()
            adapter.service.preflight_import(
                verified, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=expected_project_version,
            )
            limits = adapter.upload_limits
            # FormData serializes textarea LF as CRLF. Bound transport memory
            # separately, then apply the unchanged logical UTF-8 text limit.
            async with request.form(max_files=limits["max_documents"] + 1, max_fields=3, max_part_size=2 * (limits["max_text_bytes"] + 1)) as form:
                unknown_fields = set(form.keys()) - {"files", "critical", "text"}
                if unknown_fields:
                    raise WorkspaceServiceError("INVALID_INPUT", f"不接受未声明上传字段：{sorted(unknown_fields)[0]}")
                uploads = form.getlist("files")
                raw_critical = str(form.get("critical") or "true").casefold()
                if raw_critical not in {"true", "false"}:
                    raise WorkspaceServiceError("INVALID_INPUT", "critical 必须为布尔值")
                raw_text = form.get("text") or ""
                if not isinstance(raw_text, str):
                    raise WorkspaceServiceError("INVALID_INPUT", "直接输入只接受文字")
                direct_text = raw_text.replace("\r\n", "\n").replace("\r", "\n").strip()
                encoded = direct_text.encode("utf-8")
                if len(encoded) > min(limits["max_text_bytes"], limits["max_file_bytes"]):
                    raise WorkspaceServiceError("FILE_TOO_LARGE", f"直接输入文字不得超过 {min(limits['max_text_bytes'], limits['max_file_bytes'])} UTF-8 bytes")
                if len(uploads) + bool(direct_text) > limits["max_documents"]:
                    raise WorkspaceServiceError("UPLOAD_LIMIT_EXCEEDED", f"单批资料不得超过 {limits['max_documents']} 份，粘贴文字计一份")
                if not uploads and not direct_text:
                    raise WorkspaceServiceError("INVALID_INPUT", "请上传资料或直接输入 Brief 文字")
                # Inspect the entire batch before reading any file into memory.
                for item in uploads:
                    if not callable(getattr(item, "read", None)) or not getattr(item, "filename", None):
                        raise WorkspaceServiceError("INVALID_INPUT", "上传文件须有文件名和二进制内容")
                    if isinstance(getattr(item, "size", None), int) and item.size > limits["max_file_bytes"]:
                        raise WorkspaceServiceError("FILE_TOO_LARGE", f"单文件不得超过 {limits['max_file_bytes']} bytes")
                files = []
                for item in uploads:
                    content = await item.read(limits["max_file_bytes"] + 1)
                    if len(content) > limits["max_file_bytes"]:
                        raise WorkspaceServiceError("FILE_TOO_LARGE", f"单文件不得超过 {limits['max_file_bytes']} bytes")
                    files.append((item.filename, BytesIO(content), raw_critical == "true"))
                if direct_text:
                    files.append(("直接输入.txt", BytesIO(encoded), raw_critical == "true"))
            imported = await run_in_threadpool(adapter.import_uploads, verified,
                workspace_id=workspace_id, project_id=project_id,
                uploads=[factory(*item) for item in files], expected_project_version=expected_project_version)
            return imported
        except StarletteHTTPException as exc:
            if exc.status_code == 400 and str(exc.detail).startswith("Part exceeded maximum size"):
                raise failure(WorkspaceServiceError("FILE_TOO_LARGE", "粘贴文字超过上传容量，请拆分后导入；已保存的资料不受影响。")) from exc
            raise
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/brief/files/retry")
    def retry_brief_files(project_id: str, payload: BriefRetryPayload, workspace_id: str,
                          auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            adapter, _ = require_adapter()
            raw = adapter.service.read_structured(verified, workspace_id=workspace_id,
                project_id=project_id, brief_version_id=payload.brief_version_id)
            identities = {_employee_document_key(item["source_document_id"]): item["source_document_id"]
                for item in raw["source_manifest"]["documents"]}
            if len(set(payload.document_keys)) != len(payload.document_keys) or any(key not in identities for key in payload.document_keys):
                raise WorkspaceServiceError("INVALID_INPUT", "请选择当前资料中的失败文件")
            result = adapter.retry_failed_documents(verified, workspace_id=workspace_id,
                project_id=project_id, brief_version_id=payload.brief_version_id,
                document_ids=[identities[key] for key in payload.document_keys],
                expected_project_version=payload.expected_project_version)
            result.pop("retried_document_ids", None)
            result["retried_count"] = len(payload.document_keys)
            return result
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/brief/files/{document_key}/replace")
    async def replace_brief_file(project_id: str, document_key: str, request: Request,
            workspace_id: str, brief_version_id: str, expected_project_version: int,
            auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            adapter, factory = require_adapter()
            adapter.service.preflight_import(verified, workspace_id=workspace_id,
                project_id=project_id, expected_project_version=expected_project_version)
            raw = adapter.service.read_structured(verified, workspace_id=workspace_id,
                project_id=project_id, brief_version_id=brief_version_id)
            identity = next((item["source_document_id"] for item in raw["source_manifest"]["documents"]
                if _employee_document_key(item["source_document_id"]) == document_key), None)
            if identity is None:
                raise WorkspaceServiceError("INVALID_INPUT", "请选择当前资料中的失败文件")
            limits = adapter.upload_limits
            async with request.form(max_files=1, max_fields=0, max_part_size=limits["max_file_bytes"]+1) as form:
                item = form.get("file")
                if set(form.keys()) != {"file"} or not callable(getattr(item, "read", None)) or not getattr(item, "filename", None):
                    raise WorkspaceServiceError("INVALID_INPUT", "请选择一份替换文件")
                if isinstance(getattr(item, "size", None), int) and item.size > limits["max_file_bytes"]:
                    raise WorkspaceServiceError("FILE_TOO_LARGE", "替换文件超过当前单文件限制")
                content = await item.read(limits["max_file_bytes"]+1)
                if len(content) > limits["max_file_bytes"]:
                    raise WorkspaceServiceError("FILE_TOO_LARGE", "替换文件超过当前单文件限制")
            result = await run_in_threadpool(adapter.replace_failed_document, verified,
                workspace_id=workspace_id, project_id=project_id, brief_version_id=brief_version_id,
                document_id=identity, upload=factory(item.filename, BytesIO(content), True),
                expected_project_version=expected_project_version)
            for key in ("retried_document_ids", "replaced_document_id", "replacement_document_id"):
                result.pop(key, None)
            return result
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/brief/conflicts/{conflict_id}/resolve")
    def resolve_conflict(project_id: str, conflict_id: str, payload: ConflictResolutionPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            adapter, _ = require_adapter()
            resolved = adapter.resolve_conflict(
                verified, workspace_id=workspace_id, project_id=project_id,
                brief_version_id=payload.brief_version_id, conflict_id=conflict_id,
                selected_fact_id=payload.selected_fact_id,
                expected_project_version=payload.expected_project_version,
            )
            return resolved
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/brief/confirm")
    def confirm_brief(project_id: str, payload: BriefActionPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            adapter, _ = require_adapter()
            confirmed = adapter.confirm(verified, workspace_id=workspace_id, project_id=project_id, brief_version_id=payload.brief_version_id, confirmed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), expected_project_version=payload.expected_project_version)
            return confirmed
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/categories")
    def categories() -> dict:
        from .script_type_adapter import SCRIPT_TYPE_METADATA
        return {"items": [{"category_id": item.category_id, "label": item.display_name} for item in SCRIPT_TYPE_METADATA]}

    @app.get("/api/formal/projects/{project_id}/templates")
    def templates(project_id: str, workspace_id: str, target_category_id: str | None = None,
                  auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return require_generation().template_catalog(token(auth_token), workspace_id=workspace_id,
                project_id=project_id, target_category_id=target_category_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/templates/import")
    async def import_template(project_id: str, request: Request, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            form = await request.form()
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read"):
                raise WorkspaceServiceError("TEMPLATE_FILE_INVALID", "请选择Excel模板")
            try:
                expected = int(str(form.get("expected_project_version", "")))
            except ValueError:
                raise WorkspaceServiceError("INVALID_INPUT", "请刷新项目信息") from None
            return require_generation().import_template(verified, workspace_id=str(form.get("workspace_id") or ""),
                project_id=project_id, expected_project_version=expected,
                filename=upload.filename or "", content=await upload.read(50 * 1024 * 1024 + 1),
                display_name=str(form.get("display_name") or ""))
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/templates/trial")
    def trial_template(project_id: str, payload: TemplateTrialPayload, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            result = require_generation().trial_template(token(auth_token), project_id=project_id, **payload.model_dump())
            return {"template": result["template"], "plan": employee_plan(result["plan"])}
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/templates/select")
    def select_template(project_id: str, payload: TemplateSelectPayload, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            return require_generation().select_template(token(auth_token), project_id=project_id, **payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/image-creations/context")
    def image_creation_context(project_id: str, workspace_id: str, target_kind: str, target_id: str,
                               auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_generation().image_creation_context(verified, workspace_id=workspace_id,
                project_id=project_id, target_kind=target_kind, target_id=target_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-creations/prepare")
    def prepare_image_prompt(project_id: str, workspace_id: str, payload: ImagePromptPayload,
                             auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_generation().prepare_image_prompt(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-creations")
    async def start_image_creation(project_id: str, workspace_id: str, request: Request,
                                   auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        from .image_providers import MAX_IMAGE_BYTES
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            uploads = []
            if request.headers.get("content-type", "").startswith("multipart/form-data"):
                async with request.form(max_files=14, max_fields=1) as form:
                    if set(form) - {"payload", "reference_files"} or len(form.getlist("payload")) != 1:
                        raise WorkspaceServiceError("IMAGE_INPUT_INVALID", "图片上传字段无效")
                    payload = ImageCreationPayload.model_validate_json(str(form.get("payload", "")))
                    total = 0
                    for upload in form.getlist("reference_files"):
                        if not hasattr(upload, "read"):
                            raise WorkspaceServiceError("IMAGE_INPUT_INVALID", "参考图片无效")
                        content = await upload.read(MAX_IMAGE_BYTES - total + 1)
                        total += len(content)
                        if total > MAX_IMAGE_BYTES:
                            raise WorkspaceServiceError("IMAGE_INPUT_INVALID", "参考图片合计超过本地请求限制")
                        uploads.append((upload.filename or "参考图", content))
            else:
                payload = ImageCreationPayload.model_validate_json(await request.body())
            return require_generation().start_image_creation(verified, workspace_id=workspace_id,
                project_id=project_id, reference_uploads=uploads, **payload.model_dump())
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="图片请求格式无效，请核对目标和创作要求") from exc
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/image-creations/{task_id}")
    def read_image_creation(project_id: str, workspace_id: str, task_id: str,
                            auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_generation().read_image_creation(verified, workspace_id=workspace_id,
                project_id=project_id, task_id=task_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/image-creations/{task_id}/preview")
    @app.get("/api/formal/projects/{project_id}/image-creations/{task_id}/references/{reference_index}")
    def image_creation_preview(project_id: str, workspace_id: str, task_id: str, reference_index: int | None = None,
                               auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> Response:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            content, mime = require_generation().preview_image_creation(verified, workspace_id=workspace_id,
                project_id=project_id, task_id=task_id, reference_index=reference_index)
            return Response(content, media_type=mime, headers={"Cache-Control":"private, no-store", "X-Content-Type-Options":"nosniff"})
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-creations/{task_id}/adopt")
    def adopt_image_creation(project_id: str, workspace_id: str, task_id: str, payload: ImageAdoptionPayload,
                             auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_generation().adopt_image_creation(verified, workspace_id=workspace_id,
                project_id=project_id, task_id=task_id, **payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/creative-draft")
    def creative_draft(project_id: str, workspace_id: str,
                       since_task_id: str | None = None, after_sequence: int = 0,
                       known_revision: int | None = None, known_brief_marker: str | None = None,
                       auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_generation().read_creative_draft(verified, workspace_id=workspace_id, project_id=project_id,
                since_task_id=since_task_id, after_sequence=after_sequence,
                known_revision=known_revision, known_brief_marker=known_brief_marker)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/creative-draft/image-references")
    def save_creative_image_references(project_id: str, workspace_id: str, payload: CreativeImageReferencesPayload,
                                       auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            require_generation().save_creative_image_references(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump(), library_service=getattr(agent_api, 'default_images', None))
            return require_generation().read_creative_draft(verified, workspace_id=workspace_id, project_id=project_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/creative-draft/image-references/{reference_id}/preview")
    def preview_creative_reference(project_id: str, workspace_id: str, reference_id: str,
                                   version_id: str | None = None,
                                   auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> Response:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            content, mime = require_generation().preview_creative_image_reference(verified,
                workspace_id=workspace_id, project_id=project_id, reference_id=reference_id, version_id=version_id)
            return Response(content=content, media_type=mime, headers={"Cache-Control": "no-store"})
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/creative-draft/generate")
    def generate_creative_draft(project_id: str, workspace_id: str, payload: CreativeGenerationPayload,
                                auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_generation().start_creative_draft(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/creative-draft/changes")
    def change_creative_draft(project_id: str, workspace_id: str, payload: CreativeEditPayload,
                              auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            require_revision().apply_creative_draft_change(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump())
            return require_generation().read_creative_draft(verified, workspace_id=workspace_id, project_id=project_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/creative-draft/revisions")
    def revise_creative_draft(project_id: str, workspace_id: str, payload: CreativeRevisionPayload,
                              auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            require_revision().propose_creative_draft_change(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump())
            return require_generation().read_creative_draft(verified, workspace_id=workspace_id, project_id=project_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/creative-draft/revisions/confirm")
    def confirm_creative_revision(project_id: str, workspace_id: str, payload: CreativeRevisionConfirmPayload,
                                  auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            from .workbench_task_recovery import require_standalone_child
            require_standalone_child(require_revision().tasks, verified, payload.task_id)
            require_revision().confirm_creative_draft_change(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump())
            return require_generation().read_creative_draft(verified, workspace_id=workspace_id, project_id=project_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/creative-draft/protection")
    def protect_creative_draft(project_id: str, workspace_id: str, payload: CreativeProtectionPayload,
                               auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            require_revision().set_creative_protection(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump())
            return require_generation().read_creative_draft(verified, workspace_id=workspace_id, project_id=project_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/creative-draft/answers")
    def answer_creative_question(project_id: str, workspace_id: str, payload: CreativeAnswerPayload,
                                 auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            require_revision().answer_creative_input(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump())
            return require_generation().read_creative_draft(verified, workspace_id=workspace_id, project_id=project_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/creative-draft/answers/continue")
    def continue_creative_answers(project_id: str, workspace_id: str, payload: StrictWritePayload,
                                  auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            more = require_revision().consume_creative_answers(verified, workspace_id=workspace_id,
                project_id=project_id, force_preview=True)
            while more:
                more = require_revision().consume_creative_answers(verified, workspace_id=workspace_id, project_id=project_id)
            return require_generation().read_creative_draft(verified, workspace_id=workspace_id, project_id=project_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/plans")
    def prepare_plan(project_id: str, payload: PlanActionPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            service = require_generation()
            return employee_plan(service.prepare_plan(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=payload.expected_project_version,
                generation_mode=payload.generation_mode,
                revision_task_id=payload.revision_task_id,
                expected_creative_revision=payload.expected_creative_revision,
            ))
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/images")
    def image_readiness(project_id: str, workspace_id: str, expected_project_version: Optional[int] = None, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_generation().get_image_readiness(
                verified, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=expected_project_version,
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/images/synchronize")
    def synchronize_image_sources(project_id: str, payload: PlanActionPayload, workspace_id: str,
                                  auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_generation().synchronize_image_sources(verified, workspace_id=workspace_id,
                project_id=project_id, expected_project_version=payload.expected_project_version)
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/images/understand")
    def understand_images(project_id: str, payload: PlanActionPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            return require_generation().analyze_pending_images(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=payload.expected_project_version,
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/images/analysis-confirmations")
    def confirm_image_analyses(project_id: str, payload: ImageAnalysisConfirmationPayload,
                              workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_generation().confirm_image_analyses(verified, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=payload.expected_project_version,
                selections=[item.model_dump() for item in payload.selections], note=payload.note,
                idempotency_key=payload.idempotency_key)
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/images/analysis-confirmations/{idempotency_key}")
    def read_image_analysis_confirmation(project_id: str, idempotency_key: str, workspace_id: str,
                                         auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_generation().read_image_analysis_confirmation(verified,
                workspace_id=workspace_id, project_id=project_id, idempotency_key=idempotency_key)
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/images/{image_id}/analysis")
    def record_image_analysis(project_id: str, image_id: str, payload: ImageAnalysisPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_generation().record_image_analysis(
                verified, workspace_id=workspace_id, project_id=project_id, image_id=image_id,
                expected_project_version=payload.expected_project_version, status=payload.status,
                caption=payload.caption, tags=tuple(payload.tags), subjects=tuple(payload.subjects),
                scene=payload.scene, purpose=payload.purpose, quality=payload.quality,
                brand=payload.brand, evidence=tuple(payload.evidence),
                quality_flags=tuple(payload.quality_flags),
                negative_flags=tuple(payload.negative_flags),
                expected_analysis_key=payload.expected_analysis_key,
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/images/{image_id}/preview")
    def image_preview(project_id: str, image_id: str, workspace_id: str, expected_project_version: int, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"), variant: str = "original", version_id: str = "") -> Response:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            if variant not in {"original", "thumbnail"}:
                raise WorkspaceServiceError("INVALID_INPUT", "图片预览规格无效")
            content, media_type = require_generation().read_image_preview(
                verified, workspace_id=workspace_id, project_id=project_id, image_id=image_id,
                expected_project_version=expected_project_version,
                variant=variant, version_id=version_id,
            )
            return Response(content=content, media_type=media_type, headers={"Cache-Control": "no-store"})
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post('/api/formal/projects/{project_id}/image-workspace/assistant')
    def propose_image_batch(project_id: str, workspace_id: str, payload: ImageBatchProposalPayload,
                            background_tasks: BackgroundTasks,
                            auth_token: str = Header(default='', alias='X-MPlus-Agent-Auth')) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            revision = require_revision()
            result = revision.start_image_batch(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump())
            if result['task']['status'] == 'understanding':
                background_tasks.add_task(revision.execute_image_batch, verified, task_id=result['task']['task_id'])
            return result
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get('/api/formal/projects/{project_id}/image-workspace/assistant')
    def read_image_batch(project_id: str, workspace_id: str, task_id: str | None = None,
                         auth_token: str = Header(default='', alias='X-MPlus-Agent-Auth')) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_revision().read_image_batch(verified, workspace_id=workspace_id,
                project_id=project_id, task_id=task_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.patch('/api/formal/projects/{project_id}/image-workspace/assistant/{task_id}')
    def revise_image_batch(project_id: str, workspace_id: str, task_id: str, payload: ImageBatchRevisePayload,
                           auth_token: str = Header(default='', alias='X-MPlus-Agent-Auth')) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            revision = require_revision()
            if revision.tasks.get_task(verified, task_id)['project_id'] != project_id:
                raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '方案不属于当前项目')
            from .workbench_task_recovery import require_standalone_child
            require_standalone_child(revision.tasks, verified, task_id)
            return revision.revise_image_batch(verified, task_id=task_id, **payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post('/api/formal/projects/{project_id}/image-workspace/assistant/{task_id}/cancel')
    def cancel_image_batch(project_id: str, workspace_id: str, task_id: str, payload: ImageBatchCancelPayload,
                           auth_token: str = Header(default='', alias='X-MPlus-Agent-Auth')) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            revision = require_revision()
            if revision.tasks.get_task(verified, task_id)['project_id'] != project_id:
                raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '方案不属于当前项目')
            from .workbench_task_recovery import require_standalone_child
            require_standalone_child(revision.tasks, verified, task_id)
            return revision.cancel_image_batch(verified, task_id=task_id, **payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post('/api/formal/projects/{project_id}/image-workspace/assistant/{task_id}/confirm')
    def confirm_image_batch(project_id: str, workspace_id: str, task_id: str, payload: ImageBatchConfirmPayload,
                            auth_token: str = Header(default='', alias='X-MPlus-Agent-Auth')) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            revision = require_revision()
            task = revision.tasks.get_task(verified, task_id)
            if task['project_id'] != project_id:
                raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '方案不属于当前项目')
            from .workbench_task_recovery import require_standalone_child
            require_standalone_child(revision.tasks, verified, task_id)
            return revision.confirm_image_batch(verified, task_id=task_id, **payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/image-workspace")
    def image_workspace(
        project_id: str,
        workspace_id: str,
        expected_project_version: Optional[int] = None,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_image_workspace().read_current(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=expected_project_version,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-workspace/creative-references")
    def inherit_creative_images(project_id: str, workspace_id: str, payload: ImageReferenceTransferPayload,
                                 auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_generation().inherit_creative_images(verified, workspace_id=workspace_id,
                project_id=project_id, version_id=payload.version_id)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/image-workspace/progress")
    def image_progress(project_id: str, workspace_id: str, expected_project_version: Optional[int] = None,
                       auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_image_workspace().read_progress(
                verified, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=expected_project_version,
                library_service=getattr(agent_api, "default_images", None),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.patch("/api/formal/projects/{project_id}/image-workspace/progress")
    def save_image_progress(project_id: str, payload: ImageProgressPayload, workspace_id: str,
                            auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_image_workspace().save_progress(
                verified, workspace_id=workspace_id, project_id=project_id,
                **payload.model_dump(), library_service=getattr(agent_api, "default_images", None),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc


    @app.post("/api/formal/projects/{project_id}/image-workspace/progress/discard")
    def discard_image_progress(project_id: str, workspace_id: str, payload: ImageProgressPayload,
            auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            if payload.selections:
                raise WorkspaceServiceError('INVALID_INPUT', '放弃全部配图草稿不接受替换选项')
            return require_image_workspace().save_progress(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump(), discard_all=True)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-workspace/confirmation-preview")
    def preview_progress_confirmation(project_id: str, workspace_id: str, payload: ImageConfirmationPreviewPayload,
            auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            values = payload.model_dump(exclude={'idempotency_key'})
            return require_image_workspace().preview_progress_confirmation(verified, workspace_id=workspace_id,
                project_id=project_id, **values, library_service=getattr(agent_api, 'default_images', None))
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-workspace/confirm-progress")
    def confirm_image_progress(project_id: str, workspace_id: str, payload: ImageConfirmationPayload,
            auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_image_workspace().confirm_progress(verified, workspace_id=workspace_id,
                project_id=project_id, **payload.model_dump(), library_service=getattr(agent_api, 'default_images', None),
                confirm_action=agent_api.confirm_action)
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/images/default-candidates")
    def default_image_candidates(project_id: str, workspace_id: str, expected_project_version: int, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"), view: str = "recommended", query: str = "", page: int = 1, page_size: int = 24, shot_no: str = "", match_scope: str = "", source: str = "", scene: str = "", subject: str = "", brand: str = "", framing: str = "", analysis_status: str = "", block_id: str = "") -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            service = getattr(agent_api, "default_images", None)
            if service is None:
                raise WorkspaceServiceError("ASSET_PACKAGE_UNAVAILABLE", "默认图片服务未安装")
            return service.candidates(
                verified, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=expected_project_version,
                view=view, query=query, page=page, page_size=page_size, shot_no=shot_no,
                match_scope=match_scope, source=source, scene=scene, subject=subject,
                brand=brand, framing=framing, analysis_status=analysis_status,
                **({"block_id": block_id} if block_id else {}),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/images/default-candidates/{media_id}/preview")
    def default_image_preview(project_id: str, media_id: str, workspace_id: str, expected_project_version: int, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"), variant: str = "original", version_id: str = "") -> Response:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            service = getattr(agent_api, "default_images", None)
            if service is None:
                raise WorkspaceServiceError("ASSET_PACKAGE_UNAVAILABLE", "默认图片服务未安装")
            content, media_type = service.preview(
                verified, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=expected_project_version, media_id=media_id,
                variant=variant, version_id=version_id,
            )
            return Response(content=content, media_type=media_type, headers={"Cache-Control": "no-store"})
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/images/default-proposals")
    def propose_default_images(project_id: str, payload: DefaultImageProposalPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            service = getattr(agent_api, "default_images", None)
            if service is None:
                raise WorkspaceServiceError("ASSET_PACKAGE_UNAVAILABLE", "默认图片服务未安装")
            return service.propose(
                verified, agent_session_id="",
                workspace_id=workspace_id, project_id=project_id,
                expected_project_version=payload.expected_project_version,
                expected_script_revision=payload.expected_script_revision,
                selections=[item.model_dump() for item in payload.selections],
                idempotency_key=payload.idempotency_key,
                allowed_shot_ids=payload.allowed_shot_ids,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-workspace/proposals")
    def preview_image_workspace_version(
        project_id: str,
        payload: ImageVersionPreviewPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            return require_image_workspace().preview_version(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                base_version_id=payload.base_version_id,
                expected_project_version=payload.expected_project_version,
                selections=tuple(
                    ImageShotSelection(
                        item.shot_no, item.image_key, item.confirmed,
                        decision_kind=item.decision_kind,
                        temporary_reason=item.temporary_reason,
                        candidate_snapshot_digest=item.candidate_snapshot_digest,
                        query=item.query,
                        reuse_reason=item.reuse_reason,
                    )
                    for item in payload.selections
                ),
                idempotency_key=payload.idempotency_key,
                source_revision_task_id=payload.source_revision_task_id,
                allowed_shot_ids=payload.allowed_shot_ids,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-workspace/feedback-proposals")
    def propose_image_workspace_feedback(
        project_id: str,
        payload: ImageFeedbackProposalPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            return require_revision().propose_history_image_feedback(
                verified,
                project_id=project_id,
                **payload.model_dump(),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-workspace/proposals/{proposal_id}/confirm")
    def confirm_image_workspace_version(
        project_id: str,
        proposal_id: str,
        payload: ImageVersionConfirmPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            task = TaskService(require_image_workspace().services.store).get_task(
                verified, proposal_id,
            )
            if task["project_id"] != project_id:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "图片版本提案不属于当前项目")
            return require_image_workspace().confirm_version(
                verified, proposal_id=proposal_id,
                proposal_digest=payload.proposal_digest,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-workspace/save")
    def save_image_workspace(
        project_id: str,
        payload: ImageVersionSavePayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            raise WorkspaceServiceError(
                "TWO_STEP_CONFIRMATION_REQUIRED",
                "图片版本必须先预览影响范围，再确认同一提案摘要",
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/image-workspace/shots/{shot_no}/candidates")
    def search_image_workspace_shot(
        project_id: str,
        shot_no: str,
        workspace_id: str,
        base_version_id: str,
        expected_project_version: int,
        query: str = "",
        source_revision_task_id: Optional[str] = None,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id,
            )
            return require_image_workspace().search_shot(
                verified, workspace_id=workspace_id, project_id=project_id,
                shot_no=shot_no, query=query, base_version_id=base_version_id,
                expected_project_version=expected_project_version,
                source_revision_task_id=source_revision_task_id,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/image-workspace/supplements")
    async def supplement_image_workspace(
        project_id: str,
        request: Request,
        workspace_id: str,
        expected_project_version: int,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            form = await request.form()
            if set(form.keys()) != {"file"}:
                raise WorkspaceServiceError("INVALID_INPUT", "本地补图只接受一个 file 字段")
            upload = form.get("file")
            if upload is None or not hasattr(upload, "read") or not upload.filename:
                raise WorkspaceServiceError("INVALID_INPUT", "请选择一张本地图片")
            return require_image_workspace().supplement_local_image(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=expected_project_version,
                filename=upload.filename,
                content=await upload.read(),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    def selected_review_version(auth_token: str, project_id: str,
                                version_id: str | None, version_no: int | None) -> str | None:
        if version_no is None:
            return version_id
        if version_id is not None or version_no < 1:
            raise WorkspaceServiceError("INVALID_INPUT", "请指定一个有效的版本")
        versions = require_review_workspace().services.projects.list_project_versions(auth_token, project_id)
        if version_no > len(versions):
            raise WorkspaceServiceError("NOT_FOUND", "脚本版本不存在")
        return str(versions[version_no - 1]["version_id"])

    @app.get("/api/formal/projects/{project_id}/review")
    def review_workspace(
        project_id: str,
        workspace_id: str,
        expected_project_version: Optional[int] = None,
        version_id: Optional[str] = None,
        version_no: Optional[int] = None,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            model = require_review_workspace().read_current(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=expected_project_version,
                version_id=selected_review_version(verified, project_id, version_id, version_no),
            )
            return model
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/review/start")
    def start_review(
        project_id: str,
        payload: ReviewStartPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_review_workspace().start_review(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=payload.expected_project_version,
                idempotency_key=payload.idempotency_key,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/review/decisions")
    def decide_review_items(
        project_id: str,
        payload: ReviewDecisionPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            model = require_review_workspace().decide(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=payload.expected_project_version,
                issue_keys=tuple(payload.issue_keys),
                decision=payload.decision,
                note=payload.note,
                idempotency_key=payload.idempotency_key,
            )
            return model
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/review/final")
    def confirm_final_review(
        project_id: str,
        payload: ReviewFinalPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_review_workspace().confirm_final(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=payload.expected_project_version,
                note=payload.note,
                idempotency_key=payload.idempotency_key,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/review/excel")
    def download_reviewed_excel(
        project_id: str,
        workspace_id: str,
        expected_project_version: int,
        version_id: Optional[str] = None,
        version_no: Optional[int] = None,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> FileResponse:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id,
            )
            delivery = require_review_workspace().prepare_excel_delivery(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=expected_project_version,
                version_id=selected_review_version(verified, project_id, version_id, version_no),
            )
            return FileResponse(
                delivery.path,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                filename=delivery.filename,
                headers={"Cache-Control": "no-store"},
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/review/pdf")
    def download_reviewed_pdf(
        project_id: str,
        workspace_id: str,
        expected_project_version: int,
        version_id: Optional[str] = None,
        version_no: Optional[int] = None,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> Response:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id,
            )
            delivery = require_review_workspace().prepare_pdf_delivery(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=expected_project_version,
                version_id=selected_review_version(verified, project_id, version_id, version_no),
            )
            if delivery.status == "optional_unavailable" or delivery.path is None:
                return JSONResponse({
                    "state": "optional_unavailable",
                    "message": delivery.unavailable_reason,
                    "excel_blocked": False,
                    "production_certification": "BLOCK",
                })
            return FileResponse(
                delivery.path,
                media_type="application/pdf",
                filename=delivery.filename,
                headers={"Cache-Control": "no-store"},
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/plans")
    def waiting_plans(project_id: str, workspace_id: str, expected_project_version: Optional[int] = None, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_generation().list_waiting_plans(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=expected_project_version,
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/tasks")
    def project_tasks(
        project_id: str,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id,
            )
            return require_generation().list_project_activity(
                verified, workspace_id=workspace_id, project_id=project_id,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/planning-readiness")
    def planning_readiness(
        project_id: str, workspace_id: str, expected_project_version: Optional[int] = None,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_generation().get_planning_readiness(
                verified, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=expected_project_version,
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/plans/{plan_id}")
    def plan_preview(project_id: str, plan_id: str, workspace_id: str, expected_project_version: Optional[int] = None, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return employee_plan(require_generation().get_plan_preview(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                plan_id=plan_id,
                expected_project_version=expected_project_version,
            ))
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/plans/{plan_id}/conflict-resolutions/prepare")
    def prepare_plan_conflict_resolutions(
        project_id: str,
        plan_id: str,
        payload: PlanConflictResolutionPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            return require_generation().prepare_conflict_resolutions(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                plan_id=plan_id,
                plan_sha256=payload.plan_sha256,
                expected_project_version=payload.expected_project_version,
                idempotency_key=payload.idempotency_key,
                resolutions=[item.model_dump(mode="json") for item in payload.resolutions],
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/plans/{plan_id}/conflict-resolutions/execute")
    def execute_plan_conflict_resolutions(
        project_id: str,
        plan_id: str,
        payload: PlanConflictResolutionExecutePayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            return require_generation().execute_conflict_resolutions(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                plan_id=plan_id,
                plan_sha256=payload.plan_sha256,
                expected_project_version=payload.expected_project_version,
                idempotency_key=payload.idempotency_key,
                resolutions=[item.model_dump(mode="json") for item in payload.resolutions],
                command_token=payload.command_token,
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/plans/{plan_id}/confirm")
    def confirm_plan(project_id: str, plan_id: str, payload: PlanActionPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            service = require_generation()
            return employee_completion(service.confirm_plan_and_generate(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                plan_id=plan_id,
                expected_project_version=payload.expected_project_version,
            ))
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/script")
    def current_script(project_id: str, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_script().read_current(
                verified, workspace_id=workspace_id, project_id=project_id,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/script/versions/{version_no}/diff")
    def script_version_diff(project_id: str, version_no: int, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            return require_script().read_version_diff(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                version_no=version_no,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.get("/api/formal/projects/{project_id}/script/versions/{version_no}/review-workbook")
    def script_review_workbook(project_id: str, version_no: int, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> Response:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id)
            result = require_script().build_review_workbook(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                version_no=version_no,
            )
            return Response(
                content=result["content"],
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={
                    "Cache-Control": "no-store",
                    "Content-Disposition": (
                        "attachment; filename*=UTF-8''" + quote(result["filename"])
                    ),
                },
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.put("/api/formal/projects/{project_id}/script/assistant-locks")
    def set_script_assistant_locks(project_id: str, payload: ScriptAssistantLocksPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            return require_script().set_assistant_locks(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=payload.expected_project_version,
                locked_shot_ids=payload.locked_shot_ids,
                idempotency_key=payload.idempotency_key,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/table")
    def change_script_cell(project_id: str, payload: ScriptTableChangePayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_revision().apply_table_draft_change(
                verified,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=payload.expected_project_version,
                expected_revision=payload.expected_revision,
                shot_no=payload.shot_no,
                shot_id=payload.shot_id,
                field=payload.field,
                value=payload.value,
                idempotency_key=payload.idempotency_key,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/instruction")
    def change_script_with_instruction(project_id: str, payload: ScriptInstructionPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            revision = getattr(agent_api, "script_revision", None)
            if revision is None:
                raise WorkspaceServiceError(
                    "FORMAL_SCRIPT_REVISION_UNAVAILABLE", "Formal 统一修订服务未安装",
                )
            instruction = (
                f"把第 {payload.shot_no} 镜的{payload.field}修改为{payload.value}"
                if hasattr(payload, "shot_no") else payload.instruction
            )
            return revision.propose(
                verified,
                agent_session_id="",
                project_id=project_id,
                instruction=instruction,
                expected_revision=payload.expected_revision,
                expected_project_version=payload.expected_project_version,
                idempotency_key=payload.idempotency_key,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/revision-proposals")
    def propose_script_revision(project_id: str, payload: ScriptRevisionProposalPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            revision = getattr(agent_api, "script_revision", None)
            if revision is None:
                raise WorkspaceServiceError(
                    "FORMAL_SCRIPT_REVISION_UNAVAILABLE", "Formal 脚本修改服务未安装",
                )
            return revision.propose(
                verified,
                agent_session_id="",
                project_id=project_id,
                instruction=payload.instruction,
                expected_revision=payload.expected_revision,
                expected_project_version=payload.expected_project_version,
                idempotency_key=payload.idempotency_key,
                entrypoint=payload.entrypoint,
                feedback_kind=payload.feedback_kind,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/draft-proposals")
    def propose_script_draft_change(project_id: str, payload: ScriptRevisionProposalPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            return require_revision().propose(
                verified,
                agent_session_id="",
                project_id=project_id,
                instruction=payload.instruction,
                expected_revision=payload.expected_revision,
                expected_project_version=payload.expected_project_version,
                idempotency_key=payload.idempotency_key,
                entrypoint=payload.entrypoint,
                feedback_kind=payload.feedback_kind,
                draft_only=True,
                analysis_task_id=payload.analysis_task_id,
                selected_shot_ids=payload.selected_shot_ids,
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/revisions/{task_id}/selection")
    def select_script_suggestions(project_id: str, task_id: str, payload: ScriptSuggestionSelectionPayload,
                                  workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_revision().reselect_proposal(verified, project_id=project_id, task_id=task_id,
                                                       **payload.model_dump())
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/undo")
    def undo_script_change(project_id: str, payload: ScriptHistoryPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_script().undo(
                verified, workspace_id=workspace_id, project_id=project_id,
                **payload.model_dump(),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/redo")
    def redo_script_change(project_id: str, payload: ScriptHistoryPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_script().redo(
                verified, workspace_id=workspace_id, project_id=project_id,
                **payload.model_dump(),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/discard")
    def discard_script_changes(project_id: str, payload: ScriptHistoryPayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_script().discard_changes(
                verified, workspace_id=workspace_id, project_id=project_id,
                **payload.model_dump(),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/script/save")
    def save_script_version(project_id: str, payload: ScriptSavePayload, workspace_id: str, auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth")) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(verified, workspace_id=workspace_id, project_id=project_id, write=True)
            return require_revision().propose_draft_save(
                verified, project_id=project_id,
                **payload.model_dump(),
            )
        except WorkspaceServiceError as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/fact-revision-proposals")
    def propose_fact_revision(
        project_id: str,
        payload: FactRevisionProposalPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            return require_revision().propose_fact_revision(
                verified, project_id=project_id, **payload.model_dump(),
            )
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    @app.post("/api/formal/projects/{project_id}/fact-revision-proposals/{task_id}/confirm")
    def confirm_fact_revision(
        project_id: str,
        task_id: str,
        payload: FactRevisionConfirmPayload,
        workspace_id: str,
        auth_token: str = Header(default="", alias="X-MPlus-Agent-Auth"),
    ) -> dict:
        try:
            verified = token(auth_token)
            require_project_scope(
                verified, workspace_id=workspace_id, project_id=project_id, write=True,
            )
            if not isinstance(agent_api, ExecutableAgentService):
                raise WorkspaceServiceError(
                    "FORMAL_FACT_REVISION_UNAVAILABLE", "Formal 事实修订确认服务尚未接入",
                )
            task = _readonly(agent_api).services.tasks.get_task(verified, task_id)
            if task["project_id"] != project_id or task["task_type"] != "formal_fact_revision":
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "事实修订提案不属于当前项目")
            completed = agent_api.confirm_action(
                verified, task_id, proposal_digest=payload.proposal_digest,
            )
            response = completed.get("response") or {}
            if response.get("type") == "action_blocked":
                raise WorkspaceServiceError(
                    str(response.get("error_code") or "PROJECT_STATE_CHANGED"),
                    str(response.get("summary") or "事实修订未执行"),
                )
            return response
        except (FormalBriefDomainError, WorkspaceServiceError) as exc:
            raise failure(exc) from exc

    if include_legacy_root:
        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(Path(__file__).with_name("ui") / "formal.html")


def create_formal_console_app(
    *,
    agent_api: ReadonlyAgentService | ExecutableAgentService,
    brief_adapter: FormalBriefInputAdapter | None = None,
    brief_upload_factory: FormalBriefUploadFactory | None = None,
    formal_generation_service: FormalGenerationService | None = None,
    formal_script_service: FormalScriptWorkspaceService | None = None,
    formal_image_service: FormalImageWorkspaceService | None = None,
) -> FastAPI:
    """Create the standalone formal compatibility application."""
    app = FastAPI(title="Loomtake 正式项目工作台", docs_url=None, redoc_url=None)
    install_formal_routes(
        app,
        agent_api=agent_api,
        brief_adapter=brief_adapter,
        brief_upload_factory=brief_upload_factory,
        formal_generation_service=formal_generation_service,
        formal_script_service=formal_script_service,
        formal_image_service=formal_image_service,
        include_legacy_root=True,
    )
    return app
