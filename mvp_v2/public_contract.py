from __future__ import annotations

import re
from enum import Enum
from types import MappingProxyType


PUBLIC_CONTRACT_VERSION = "mplus-public-contract/v1"
ARTIFACT_REF_PATTERN = re.compile(r"^artifact://[A-Za-z0-9._~!$&'()*+,;=:@%/-]+$")


class PublicLifecycleStatus(str, Enum):
    RUNNING = "running"
    BLOCKED = "blocked"
    FAILED = "failed"
    WAITING_HUMAN_REVIEW = "waiting_human_review"
    PENDING_REVIEW = "pending_review"
    COMPLETED = "completed"
    PUBLISHED = "published"


# Names and relationships are public, storage-neutral contracts. Existing tables
# may use the listed physical key names until a separately approved DDL migration.
STABLE_ENTITY_CONTRACTS = MappingProxyType({
    "brief": {"id_field": "brief_snapshot_id", "parent": "project_id", "artifact_ref_field": "brief_ref"},
    "plan": {"id_field": "plan_id", "parent": "brief_snapshot_id", "artifact_ref_field": "plan_ref"},
    "script": {"id_field": "version_id", "parent": "project_id", "artifact_ref_field": "artifact_ref"},
    "image": {"id_field": "asset_id", "parent": "project_id", "artifact_ref_field": "artifact_ref"},
    "task": {"id_field": "task_id", "parent": "project_id", "artifact_ref_field": "result_ref"},
    "review": {"id_field": "review_id", "parent": "version_id", "artifact_ref_field": "evidence_ref"},
    "publication": {"id_field": "publication_id", "parent": "version_id", "artifact_ref_field": "outbox_event_ref"},
})


def require_artifact_ref(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not ARTIFACT_REF_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field} 必须是 artifact:// 稳定逻辑引用")
    return normalized
