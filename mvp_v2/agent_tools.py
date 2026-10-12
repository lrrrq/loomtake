from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


RiskLevel = Literal["R0", "R1", "R2", "R3"]
ToolStatus = Literal["completed", "accepted", "blocked", "failed"]
StableErrorCode = Literal[
    "INVALID_INPUT",
    "NOT_FOUND",
    "REVIEW_REQUIRED",
    "PERMISSION_DENIED",
    "WORKSPACE_VERSION_CONFLICT",
    "PROJECT_VERSION_CONFLICT",
    "PACKAGE_VERSION_CONFLICT",
    "KNOWLEDGE_VERSION_CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "BRIEF_ARTIFACT_UNAVAILABLE",
    "RECOVERY_RECONCILIATION_REQUIRED",
    "TOOL_TIMEOUT",
    "CANCELLED",
    "INTERNAL_ERROR",
]

_ENTITY_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_KNOWLEDGE_TARGET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,127}$"
_IDEMPOTENCY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$"
_ARTIFACT_REF_PATTERN = r"^artifact://[A-Za-z0-9._~!$&'()*+,;=:@%/-]+$"


class StrictToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectInput(StrictToolInput):
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)


class RunInput(StrictToolInput):
    run_id: str = Field(pattern=_ENTITY_ID_PATTERN)


class GetProjectStateInput(ProjectInput):
    pass


class GetRunDetailsInput(RunInput):
    pass


class ExplainBlockerInput(RunInput):
    pass


class PreflightBriefInput(StrictToolInput):
    project_id: Optional[str] = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    brief: Optional[dict[str, Any]] = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def exactly_one_source(self) -> "PreflightBriefInput":
        if (self.project_id is None) == (self.brief is None):
            raise ValueError("project_id 和 brief 必须且只能提供一个")
        return self


class WriteInput(StrictToolInput):
    idempotency_key: str = Field(pattern=_IDEMPOTENCY_PATTERN)


class WorkspaceWriteInput(WriteInput):
    expected_workspace_version: int = Field(ge=0)


class ProjectWriteInput(WriteInput):
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    expected_project_version: int = Field(ge=0)


class PackageWriteInput(WriteInput):
    package_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    expected_active_package_version: int = Field(ge=0)


class KnowledgeWriteInput(WriteInput):
    expected_knowledge_version: int = Field(ge=0)


class CreateProjectInput(WorkspaceWriteInput):
    workspace_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    category_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    name: str = Field(min_length=1, max_length=180)
    brief_snapshot: dict[str, Any] = Field(min_length=1)


class StartGenerationInput(ProjectWriteInput):
    mode: Literal["standard", "controlled", "exploration"] = "standard"
    asset_package_version: str = Field(default="", max_length=120)


class RetryStageInput(ProjectWriteInput):
    run_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")


class ForkRevisionInput(ProjectWriteInput):
    parent_run_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    modification_plan: dict[str, Any] = Field(min_length=1)


class CancelTaskInput(ProjectWriteInput):
    task_id: str = Field(pattern=r"^task_[A-Za-z0-9_-]{8,120}$")


class CompareVersionsInput(ProjectInput):
    left_version_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    right_version_id: str = Field(pattern=_ENTITY_ID_PATTERN)


class GetCreativeStrategiesInput(ProjectInput):
    pass


class GetFormalProjectContextInput(ProjectInput):
    pass


class GetRequirementCoverageInput(ProjectInput):
    pass


class ExplainFormalIssueInput(ProjectInput):
    pass


class PrepareFormalPlanInput(ProjectWriteInput):
    generation_mode: Literal["image_backed", "text_only_draft"]


class ConfirmFormalPlanGenerationInput(ProjectWriteInput):
    plan_id: str = Field(pattern=r"^task_[A-Za-z0-9_-]{8,120}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FormalScriptRevisionOperation(StrictToolInput):
    operation: Literal["replace_field", "clear_optional_field", "delete_shot"]
    shot_no: str = Field(pattern=r"^[1-9][0-9]*$")
    field: Optional[Literal[
        "structure", "camera_type", "shot_size", "camera_move",
        "duration_seconds", "visual", "sound",
        "voiceover", "subtitle", "notes",
    ]] = None
    value: Any = None

    @model_validator(mode="after")
    def operation_shape(self) -> "FormalScriptRevisionOperation":
        if self.operation == "delete_shot":
            if self.field is not None or self.value is not None:
                raise ValueError("delete_shot 不接受字段和值")
        elif self.operation == "clear_optional_field":
            if self.field not in {"voiceover", "subtitle", "notes"} or self.value != "":
                raise ValueError("clear_optional_field 只允许清空可选字段")
        elif self.field is None or self.value is None:
            raise ValueError("replace_field 必须提供字段和值")
        return self


class ApplyFormalScriptRevisionInput(ProjectWriteInput):
    expected_progress_revision: int | None = Field(default=None, ge=0)
    image_snapshot_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    base_version_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    expected_revision: int = Field(ge=0)
    feedback_kind: Literal["copy", "structure", "image", "fact"]
    operation: Optional[FormalScriptRevisionOperation] = None
    operations: Optional[list[FormalScriptRevisionOperation]] = Field(
        default=None, max_length=100,
    )
    entrypoint: Literal["direct", "table", "review", "assistant", "history"] = "direct"
    draft_only: bool = False
    materialize_existing_draft: bool = False
    reason: Optional[str] = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def exactly_one_operation_shape(self) -> "ApplyFormalScriptRevisionInput":
        if self.materialize_existing_draft:
            if self.draft_only or self.operation is not None or self.operations != [] or not self.reason:
                raise ValueError("草稿物化只接受空 operations 和明确原因")
            return self
        if (self.operation is None) == (self.operations is None):
            raise ValueError("operation 与 operations 必须且只能提供一个")
        if self.operations == []:
            raise ValueError("修订 operations 不能为空")
        return self


class ApplyFormalImageRevisionInput(ProjectWriteInput):
    base_version_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    image_proposal_id: str = Field(pattern=r"^task_[A-Za-z0-9_-]{8,120}$")
    image_proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_shot_no: str = Field(pattern=r"^[1-9][0-9]*$")
    target_image_id: Optional[str] = Field(default=None, pattern=_ENTITY_ID_PATTERN)
    candidate_task_id: str = Field(pattern=r"^task_[A-Za-z0-9_-]{8,120}$")


class RecordFormalImageFeedbackInput(ProjectWriteInput):
    workspace_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    base_version_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    shot_no: str = Field(pattern=r"^[1-9][0-9]*$")
    image_key: str = Field(pattern=_ENTITY_ID_PATTERN)
    reason: str = Field(min_length=1, max_length=1000)
    candidate_task_id: str = Field(pattern=r"^task_[A-Za-z0-9_-]{8,120}$")


class ApplyFormalFactRevisionInput(ProjectWriteInput):
    workspace_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    base_version_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    parent_brief_version_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    parent_brief_snapshot_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    field: Literal[
        "项目名称", "品牌或客户", "脚本类型", "行业或活动类别", "视频目标",
        "使用场景", "目标时长", "必须出现的内容", "禁止或不能出现的内容",
        "关键事件与流程", "特殊需求",
    ]
    value: str = Field(min_length=1, max_length=4000)
    evidence: str = Field(min_length=1, max_length=4000)
    entrypoint: Literal["history"] = "history"


class RetryFormalStageInput(ProjectWriteInput):
    workspace_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    retry_proposal_id: str = Field(pattern=r"^recovery_[A-Za-z0-9_-]{8,120}$")
    retry_proposal_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_stage_id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    target_task_id: str = Field(pattern=r"^task_[A-Za-z0-9_-]{8,120}$")


class FormalDefaultImageSelection(StrictToolInput):
    shot_no: str = Field(min_length=1, max_length=32)
    media_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    sources: list[dict[str, Any]] | None = None


class AdoptFormalDefaultImagesInput(ProjectWriteInput):
    expected_script_revision: int | None = Field(default=None, ge=0)
    package_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    package_version: int = Field(gt=0)
    package_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_scope_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    selections: list[FormalDefaultImageSelection] = Field(min_length=1, max_length=400)
    pending_shot_count: int = Field(default=0, ge=0, le=400)
    allowed_shot_ids: list[Annotated[str, Field(pattern=_ENTITY_ID_PATTERN)]] | None = Field(
        default=None, min_length=1, max_length=400,
    )


class SelectCreativeStrategyInput(ProjectWriteInput):
    strategy_portfolio_ref: str = Field(pattern=_ARTIFACT_REF_PATTERN)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strategy_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")


class SearchAssetsInput(StrictToolInput):
    category_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    query: str = Field(min_length=1, max_length=1000)
    filters: dict[str, Any] = Field(default_factory=dict)


class CreateAssetImportInput(PackageWriteInput):
    category_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    scope: Literal["project", "shared"]
    file_refs: list[str] = Field(min_length=1, max_length=500)


class AssetImportTransitionInput(PackageWriteInput):
    import_id: str = Field(pattern=_ENTITY_ID_PATTERN)
    expected_import_revision: int = Field(ge=1)


class GetReviewReadinessInput(RunInput):
    project_id: str = Field(pattern=_ENTITY_ID_PATTERN)


class PrepareHumanReviewInput(ProjectWriteInput):
    run_id: str = Field(pattern=_ENTITY_ID_PATTERN)


class CreateKnowledgeCandidateInput(KnowledgeWriteInput):
    scope: Literal["company", "category", "project", "personal"]
    # category ids such as ``launch`` are intentionally shorter than entity ids;
    # the knowledge service performs the scope-specific authority check.
    target_id: str = Field(pattern=_KNOWLEDGE_TARGET_PATTERN)
    statement: str = Field(min_length=1, max_length=4000)
    evidence_refs: list[str] = Field(min_length=1, max_length=100)


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: StableErrorCode
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(pattern=r"^task_[A-Za-z0-9_-]{8,120}$")
    status: ToolStatus
    artifact_refs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: Optional[ToolError] = None

    @model_validator(mode="after")
    def validate_artifact_refs(self) -> "ToolExecutionResult":
        import re

        invalid = [ref for ref in self.artifact_refs if not re.fullmatch(_ARTIFACT_REF_PATTERN, ref)]
        if invalid:
            raise ValueError("artifact_refs 必须使用 artifact:// 逻辑引用")
        return self


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    input_model: type[StrictToolInput]
    risk: RiskLevel
    permission: str
    idempotency_scope: str
    timeout_seconds: int
    cancellable: bool
    audit_event: str
    confirmation_required: bool = False
    availability: Literal["E02", "E03", "E04", "E05", "E07"] = "E02"


def _tool(
    name: str,
    input_model: type[StrictToolInput],
    risk: RiskLevel,
    permission: str,
    *,
    idempotency_scope: str = "read",
    timeout_seconds: int = 15,
    cancellable: bool = False,
    confirmation_required: bool = False,
    availability: Literal["E02", "E03", "E04", "E05", "E07"] = "E02",
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        input_model=input_model,
        risk=risk,
        permission=permission,
        idempotency_scope=idempotency_scope,
        timeout_seconds=timeout_seconds,
        cancellable=cancellable,
        audit_event=f"agent_tool.{name}",
        confirmation_required=confirmation_required,
        availability=availability,
    )


TOOL_REGISTRY = {
    item.name: item
    for item in (
        _tool("get_project_state", GetProjectStateInput, "R0", "project.read"),
        _tool("get_run_details", GetRunDetailsInput, "R0", "project.read"),
        _tool("explain_blocker", ExplainBlockerInput, "R0", "project.read"),
        _tool("preflight_brief", PreflightBriefInput, "R1", "project.diagnose", timeout_seconds=60),
        _tool("get_formal_project_context", GetFormalProjectContextInput, "R0", "project.read"),
        _tool("get_requirement_coverage", GetRequirementCoverageInput, "R0", "project.read"),
        _tool("explain_formal_issue", ExplainFormalIssueInput, "R0", "project.read"),
        _tool(
            "prepare_formal_plan", PrepareFormalPlanInput, "R2", "run.generate",
            idempotency_scope="project", timeout_seconds=300,
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "confirm_formal_plan_generation", ConfirmFormalPlanGenerationInput,
            "R2", "run.generate", idempotency_scope="project",
            timeout_seconds=600, confirmation_required=True, availability="E03",
        ),
        _tool(
            "apply_formal_script_revision", ApplyFormalScriptRevisionInput,
            "R2", "run.revise", idempotency_scope="project",
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "apply_formal_image_revision", ApplyFormalImageRevisionInput,
            "R2", "run.revise", idempotency_scope="project",
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "record_formal_image_feedback", RecordFormalImageFeedbackInput,
            "R2", "review.record", idempotency_scope="project",
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "apply_formal_fact_revision", ApplyFormalFactRevisionInput,
            "R2", "project.update", idempotency_scope="project",
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "retry_formal_stage", RetryFormalStageInput,
            "R2", "run.retry", idempotency_scope="project",
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "adopt_formal_default_images", AdoptFormalDefaultImagesInput,
            "R2", "review.record", idempotency_scope="project",
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "create_project", CreateProjectInput, "R2", "project.create",
            idempotency_scope="workspace", confirmation_required=True, availability="E03",
        ),
        _tool(
            "start_generation", StartGenerationInput, "R2", "run.generate",
            idempotency_scope="project", timeout_seconds=300, cancellable=True,
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "retry_stage", RetryStageInput, "R2", "run.retry",
            idempotency_scope="project", timeout_seconds=300, cancellable=True,
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "fork_revision", ForkRevisionInput, "R2", "run.revise",
            idempotency_scope="project", timeout_seconds=300, cancellable=True,
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "cancel_task", CancelTaskInput, "R2", "run.cancel",
            idempotency_scope="project", confirmation_required=True, availability="E03",
        ),
        _tool("compare_versions", CompareVersionsInput, "R0", "project.read", availability="E04"),
        _tool(
            "get_creative_strategies", GetCreativeStrategiesInput, "R1", "project.diagnose",
            timeout_seconds=60, availability="E06",
        ),
        _tool(
            "select_creative_strategy", SelectCreativeStrategyInput, "R2", "run.generate",
            idempotency_scope="project", confirmation_required=True, availability="E06",
        ),
        _tool("search_assets", SearchAssetsInput, "R0", "asset.read", availability="E05"),
        _tool(
            "create_asset_import", CreateAssetImportInput, "R2", "asset.import",
            idempotency_scope="package", timeout_seconds=300, cancellable=True,
            confirmation_required=True, availability="E05",
        ),
        _tool(
            "review_asset_import", AssetImportTransitionInput, "R2", "asset.review",
            idempotency_scope="package", confirmation_required=True, availability="E05",
        ),
        _tool(
            "get_review_readiness", GetReviewReadinessInput, "R1", "review.readiness",
            timeout_seconds=60, availability="E03",
        ),
        _tool(
            "prepare_human_review", PrepareHumanReviewInput, "R2", "review.prepare",
            idempotency_scope="project", timeout_seconds=120,
            confirmation_required=True, availability="E03",
        ),
        _tool(
            "create_knowledge_candidate", CreateKnowledgeCandidateInput, "R2", "knowledge.propose",
            idempotency_scope="knowledge", confirmation_required=True, availability="E07",
        ),
    )
}


def validate_tool_input(tool_name: str, payload: dict[str, Any]) -> StrictToolInput:
    try:
        definition = TOOL_REGISTRY[tool_name]
    except KeyError as exc:
        raise KeyError(f"unknown agent tool: {tool_name}") from exc
    return definition.input_model.model_validate(payload)


def export_tool_schemas(output_dir: str | Path) -> list[Path]:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    manifest = {"schema_version": 1, "tools": {}}
    output_schema = ToolExecutionResult.model_json_schema()
    for name in sorted(TOOL_REGISTRY):
        definition = TOOL_REGISTRY[name]
        input_name = f"{name}.input.schema.json"
        output_name = f"{name}.output.schema.json"
        input_path = root / input_name
        output_path = root / output_name
        _write_json(input_path, definition.input_model.model_json_schema())
        _write_json(output_path, output_schema)
        written.extend((input_path, output_path))
        manifest["tools"][name] = {
            "input_schema": input_name,
            "output_schema": output_name,
            "risk": definition.risk,
            "permission": definition.permission,
            "idempotency_scope": definition.idempotency_scope,
            "timeout_seconds": definition.timeout_seconds,
            "cancellable": definition.cancellable,
            "confirmation_required": definition.confirmation_required,
            "audit_event": definition.audit_event,
            "availability": definition.availability,
        }
    manifest_path = root / "manifest.json"
    _write_json(manifest_path, manifest)
    written.append(manifest_path)
    return written


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
