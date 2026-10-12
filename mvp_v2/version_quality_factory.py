"""Explicit composition seam for a future human-only UI command root."""

from __future__ import annotations

from .artifact_service import RunArtifactResolver
from .version_delivery import VersionDeliveryService
from .version_quality import VersionDeliveryQualityAuthority, VersionQualityResolver
from .version_quality_artifacts import VersionQualityArtifactWriter
from .version_quality_repository import (
    WorkspaceFinalEvidenceLinkRepository,
    WorkspaceReviewerActorVerifier,
    WorkspaceVersionQualityRepository,
)
from .version_review_service import VersionReviewService
from .workspace_store import WorkspaceStore


def build_workspace_version_quality_resolver(
    *, store: WorkspaceStore, delivery_service: VersionDeliveryService,
    run_artifacts: RunArtifactResolver, reviews: VersionReviewService,
    evidence_artifacts: VersionQualityArtifactWriter,
) -> VersionQualityResolver:
    """Build the authoritative service; callers supply no arbitrary evidence Mapping."""
    links = WorkspaceFinalEvidenceLinkRepository(
        WorkspaceVersionQualityRepository(store)
    )
    return VersionQualityResolver(
        authority=VersionDeliveryQualityAuthority(delivery_service),
        run_artifacts=run_artifacts,
        reviews=reviews,
        evidence_artifacts=evidence_artifacts,
        final_links=links,
        actor_verifier=WorkspaceReviewerActorVerifier(store),
    )
