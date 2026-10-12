from __future__ import annotations

from types import MappingProxyType
from typing import Any

from pydantic import Field

from .agent_tools import (
    AssetImportTransitionInput,
    KnowledgeWriteInput,
    ProjectWriteInput,
    StrictToolInput,
    ToolDefinition,
)


class PublishAssetImportPageInput(AssetImportTransitionInput):
    confirmation_token: str = Field(min_length=24, max_length=2048)


class RecordVersionReviewPageInput(ProjectWriteInput):
    version_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    confirmation_token: str = Field(min_length=24, max_length=2048)


class SelectCandidateVersionPageInput(ProjectWriteInput):
    version_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    confirmation_token: str = Field(min_length=24, max_length=2048)


class PublishVersionPageInput(ProjectWriteInput):
    version_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    review_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    confirmation_token: str = Field(min_length=24, max_length=2048)


class ApproveKnowledgePageInput(KnowledgeWriteInput):
    candidate_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    level: str = Field(pattern=r"^(company|category|project)$")
    review_evidence: str = Field(min_length=1, max_length=4000)
    confirmation_token: str = Field(min_length=24, max_length=2048)


class ProjectLifecyclePageInput(ProjectWriteInput):
    reason: str = Field(min_length=1, max_length=500)
    confirmation_token: str = Field(min_length=24, max_length=2048)


def _page_action(
    name: str,
    input_model: type[StrictToolInput],
    permission: str,
    idempotency_scope: str,
    availability: str,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        input_model=input_model,
        risk="R3",
        permission=permission,
        idempotency_scope=idempotency_scope,
        timeout_seconds=120,
        cancellable=False,
        audit_event=f"manual_page.{name}",
        confirmation_required=True,
        availability=availability,
    )


# These actions are intentionally absent from the Agent registry and its exported
# schemas. They are only accepted by authenticated manual page application
# services, where role, project, target version and confirmation are rechecked.
MANUAL_ACTION_REGISTRY = MappingProxyType({
    "archive_project": _page_action(
        "archive_project", ProjectLifecyclePageInput, "project.archive", "project", "M01"
    ),
    "restore_project": _page_action(
        "restore_project", ProjectLifecyclePageInput, "project.restore", "project", "M01"
    ),
    "record_version_review": _page_action(
        "record_version_review", RecordVersionReviewPageInput, "review.record", "project", "E04"
    ),
    "select_candidate_version": _page_action(
        "select_candidate_version", SelectCandidateVersionPageInput, "version.select_candidate", "project", "E04"
    ),
    "publish_asset_import": _page_action(
        "publish_asset_import", PublishAssetImportPageInput, "asset.publish", "package", "E05"
    ),
    "publish_version": _page_action(
        "publish_version", PublishVersionPageInput, "version.publish", "project", "E04"
    ),
    "approve_knowledge": _page_action(
        "approve_knowledge", ApproveKnowledgePageInput, "knowledge.approve", "knowledge", "E07"
    ),
})


def validate_manual_action_input(tool_name: str, payload: dict[str, Any]) -> StrictToolInput:
    try:
        definition = MANUAL_ACTION_REGISTRY[tool_name]
    except KeyError as exc:
        raise KeyError(f"unknown manual page action: {tool_name}") from exc
    return definition.input_model.model_validate(payload)
