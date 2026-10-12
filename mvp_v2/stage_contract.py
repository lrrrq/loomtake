from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping


STAGE_MANIFEST_SCHEMA_VERSION = "1"


class StageId(str, Enum):
    INPUT_FREEZE = "input_freeze"
    SOURCE_CATALOG = "source_catalog"
    MEDIA_MANIFEST = "media_manifest"
    VISUAL_ANALYSIS = "visual_analysis"
    SCRIPT_PLAN = "script_plan"
    SCRIPT_DRAFT = "script_draft"
    VISUAL_BINDING = "visual_binding"
    WORKBOOK_RENDER = "workbook_render"
    MACHINE_AUDIT = "machine_audit"
    HUMAN_REVIEW = "human_review"
    PUBLISH_FEEDBACK = "publish_feedback"


class StageStatus(str, Enum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    BLOCKED = "blocked"
    FAILED = "failed"
    WAITING_HUMAN_REVIEW = "waiting_human_review"
    INVALIDATED = "invalidated"


class StageErrorCategory(str, Enum):
    INPUT = "input"
    DEPENDENCY = "dependency"
    CONTRACT = "contract"
    EXECUTION = "execution"
    SYSTEM = "system"


STAGE_ORDER = tuple(StageId)
STAGE_DEPENDENCIES: Mapping[StageId, tuple[StageId, ...]] = MappingProxyType({
    StageId.INPUT_FREEZE: (),
    StageId.SOURCE_CATALOG: (StageId.INPUT_FREEZE,),
    StageId.MEDIA_MANIFEST: (StageId.INPUT_FREEZE,),
    StageId.VISUAL_ANALYSIS: (StageId.MEDIA_MANIFEST,),
    StageId.SCRIPT_PLAN: (StageId.SOURCE_CATALOG,),
    StageId.SCRIPT_DRAFT: (StageId.SCRIPT_PLAN,),
    StageId.VISUAL_BINDING: (StageId.SCRIPT_DRAFT,),
    StageId.WORKBOOK_RENDER: (StageId.VISUAL_BINDING,),
    StageId.MACHINE_AUDIT: (StageId.WORKBOOK_RENDER,),
    StageId.HUMAN_REVIEW: (StageId.MACHINE_AUDIT,),
    StageId.PUBLISH_FEEDBACK: (StageId.HUMAN_REVIEW,),
})

STAGE_OPTIONAL_DEPENDENCIES: Mapping[StageId, tuple[StageId, ...]] = MappingProxyType({
    stage_id: (
        (StageId.VISUAL_ANALYSIS,)
        if stage_id in {StageId.SCRIPT_DRAFT, StageId.VISUAL_BINDING}
        else ()
    )
    for stage_id in STAGE_ORDER
})


@dataclass(frozen=True)
class StageError:
    category: StageErrorCategory
    code: str
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("stage error code must not be blank")
        if not self.message.strip():
            raise ValueError("stage error message must not be blank")

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


@dataclass(frozen=True)
class StageManifest:
    run_id: str
    category_id: str
    stage_id: StageId
    status: StageStatus
    attempt: int = 0
    input_sha256: str | None = None
    output_sha256: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    log_ref: str | None = None
    input_refs: tuple[str, ...] = ()
    output_refs: tuple[str, ...] = ()
    error: StageError | None = None

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise ValueError("run_id must not be blank")
        if not self.category_id.strip():
            raise ValueError("category_id must not be blank")
        if self.attempt < 0:
            raise ValueError("attempt must not be negative")
        if self.status in {StageStatus.BLOCKED, StageStatus.FAILED} and self.error is None:
            raise ValueError("blocked or failed stage must include an error")
        if self.error is not None and self.status not in {StageStatus.BLOCKED, StageStatus.FAILED}:
            raise ValueError("only blocked or failed stage may include an error")

    def to_dict(self) -> dict:
        return {
            "schema_version": STAGE_MANIFEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "category_id": self.category_id,
            "stage_id": self.stage_id.value,
            "status": self.status.value,
            "attempt": self.attempt,
            "depends_on": [stage_id.value for stage_id in STAGE_DEPENDENCIES[self.stage_id]],
            "optional_depends_on": [
                stage_id.value
                for stage_id in STAGE_OPTIONAL_DEPENDENCIES[self.stage_id]
            ],
            "input_sha256": self.input_sha256,
            "output_sha256": self.output_sha256,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "log_ref": self.log_ref,
            "input_refs": list(self.input_refs),
            "output_refs": list(self.output_refs),
            "error": self.error.to_dict() if self.error else None,
        }
