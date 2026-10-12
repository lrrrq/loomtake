from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Protocol

from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .project_service import ProjectService
from .version_delivery import VersionDeliveryContext, VersionDeliveryService
from .version_review_service import (
    FinalConfirmation,
    ShotReviewSnapshot,
    VersionReviewService,
)
from .workspace_store import WorkspaceServiceError
from .version_quality_artifacts import (
    VersionQualityArtifact,
    VersionQualityArtifactWriter,
)


SCHEMA_VERSION = "mplus-version-quality-evidence/v1"


@dataclass(frozen=True)
class VersionSubject:
    workspace_id: str
    project_id: str
    version_id: str
    run_id: str
    manifest_sha256: str


@dataclass(frozen=True)
class MachineAuditEvidence:
    ref: str
    sha256: str
    status: str


@dataclass(frozen=True)
class FinalConfirmationEvidence:
    confirmation_id: str
    decision: str
    evidence_ref: str
    evidence_sha256: str
    reviewer_actor_id: str
    confirmed_at: str


@dataclass(frozen=True)
class VersionQualityEvidence:
    """Canonical, resolver-owned quality evidence consumed by delivery stages."""

    workspace_id: str
    project_id: str
    version_id: str
    run_id: str
    machine_audit: MachineAuditEvidence
    shot_review: ShotReviewSnapshot
    final_confirmation: FinalConfirmationEvidence | None
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        final = self.final_confirmation
        return {
            "schema_version": self.schema_version,
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "version_id": self.version_id,
            "run_id": self.run_id,
            "machine_audit": self.machine_audit.__dict__.copy(),
            "shot_review": {
                "snapshot_ref": self.shot_review.snapshot_ref,
                "snapshot_sha256": self.shot_review.snapshot_sha256,
                "total_count": self.shot_review.total_count,
                "resolved_count": self.shot_review.resolved_count,
                "unresolved_count": self.shot_review.unresolved_count,
                "last_reviewer_actor_id": self.shot_review.last_reviewer_actor_id,
                "last_reviewed_at": self.shot_review.last_reviewed_at,
            },
            "final_confirmation": final.__dict__.copy() if final else None,
        }


@dataclass(frozen=True)
class VersionQualityAccess:
    """Subset read from frozen VersionDeliveryContext through an authority port."""

    workspace_id: str
    project_id: str
    project_lock_version: int
    version_id: str
    run_id: str
    manifest_ref: str
    manifest_sha256: str


class VersionQualityAuthority(Protocol):
    def resolve(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> VersionQualityAccess: ...


class ReviewerActorResolver(Protocol):
    def resolve_reviewer(
        self, auth_token: str, *, workspace_id: str, project_id: str
    ) -> str: ...


class ReviewerActorVerifier(Protocol):
    def is_reviewer(
        self, actor_id: str, *, workspace_id: str, project_id: str
    ) -> bool: ...


class FinalEvidenceLinkRepository(Protocol):
    """Quality-link port; production adapter must persist links in WorkspaceStore."""

    def save_snapshot(
        self, auth_token: str, access: VersionQualityAccess,
        artifact: VersionQualityArtifact, *, review_content_sha256: str,
        total_count: int, resolved_count: int, unresolved_count: int,
        last_reviewer_actor_id: str | None, last_reviewed_at: str | None,
    ) -> VersionQualityArtifact: ...

    def snapshot(self, auth_token: str, access: VersionQualityAccess) -> VersionQualityArtifact | None: ...

    def save_final(
        self, auth_token: str, access: VersionQualityAccess, *, snapshot_ref: str,
        artifact: VersionQualityArtifact, decision: str, reviewer_actor_id: str,
        confirmed_at: str,
    ) -> VersionQualityArtifact: ...

    def final(self, auth_token: str, access: VersionQualityAccess) -> VersionQualityArtifact | None: ...


class InMemoryFinalEvidenceLinkRepository:
    """Stage-two fixture seam, not R03 persistence or restart recovery by itself."""

    def __init__(self) -> None:
        self._finals: dict[str, VersionQualityArtifact] = {}
        self._snapshots: dict[str, VersionQualityArtifact] = {}

    def save_snapshot(self, auth_token: str, access: VersionQualityAccess, artifact: VersionQualityArtifact, **kwargs) -> VersionQualityArtifact:
        self._snapshots[access.version_id] = artifact
        return artifact

    def snapshot(self, auth_token: str, access: VersionQualityAccess) -> VersionQualityArtifact | None:
        return self._snapshots.get(access.version_id)

    def save_final(self, auth_token: str, access: VersionQualityAccess, *, artifact: VersionQualityArtifact, **kwargs) -> VersionQualityArtifact:
        if access.version_id in self._finals:
            raise WorkspaceServiceError("FINAL_CONFIRMATION_EXISTS", "final evidence 不可覆盖")
        self._finals[access.version_id] = artifact
        return artifact

    def final(self, auth_token: str, access: VersionQualityAccess) -> VersionQualityArtifact | None:
        return self._finals.get(access.version_id)


class VersionDeliveryQualityAuthority:
    """Thin adapter over C0 VersionDeliveryService; no context extension or Mapping input."""

    def __init__(self, delivery_service: VersionDeliveryService):
        self.delivery_service = delivery_service

    def resolve(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> VersionQualityAccess:
        context: VersionDeliveryContext = self.delivery_service.resolve(
            auth_token, project_id, version_id,
            expected_project_version=expected_project_lock_version,
        )
        if context.schema_version != "mplus-version-delivery-context/v1":
            raise WorkspaceServiceError("INTEGRITY_ERROR", "VersionDeliveryContext schema 不受支持")
        return VersionQualityAccess(
            workspace_id=str(context.workspace_id), project_id=str(context.project_id),
            project_lock_version=int(context.project_lock_version),
            version_id=str(context.version_id), run_id=str(context.run_id),
            manifest_ref=str(context.manifest.ref),
            manifest_sha256=str(context.manifest.sha256),
        )


class VersionQualityResolver:
    """Formal evidence resolver: validates auth/current version via authority and bytes via artifact store."""

    def __init__(
        self,
        *,
        authority: VersionQualityAuthority,
        run_artifacts: RunArtifactResolver,
        reviews: VersionReviewService,
        evidence_artifacts: VersionQualityArtifactWriter,
        final_links: FinalEvidenceLinkRepository,
        actor_verifier: ReviewerActorVerifier,
    ):
        self.authority = authority
        self.run_artifacts = run_artifacts
        self.reviews = reviews
        self.evidence_artifacts = evidence_artifacts
        self.final_links = final_links
        self.actor_verifier = actor_verifier

    def resolve_authoritative_evidence(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> VersionQualityEvidence:
        """唯一稳定交付接口：不接收 DTO/Mapping，只重读权威状态与字节证据。"""
        return self._resolve_authoritative(
            auth_token, project_id, version_id, expected_project_lock_version
        )

    def materialize_shot_snapshot(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> ShotReviewSnapshot:
        access = self.authority.resolve(
            auth_token, project_id=project_id, version_id=version_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        self._require_access(access, project_id, version_id)
        self._require_lock(access, expected_project_lock_version)
        snapshot, payload = self._snapshot_payload(access)
        artifact = self.evidence_artifacts.write_json("shot-review", payload)
        verified = self.evidence_artifacts.read_json(artifact.ref, artifact.sha256)
        if verified != payload:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "shot review snapshot 解析结果不一致")
        artifact = self.final_links.save_snapshot(
            auth_token, access, artifact, review_content_sha256=snapshot.snapshot_sha256,
            total_count=snapshot.total_count, resolved_count=snapshot.resolved_count,
            unresolved_count=snapshot.unresolved_count,
            last_reviewer_actor_id=snapshot.last_reviewer_actor_id,
            last_reviewed_at=snapshot.last_reviewed_at,
        )
        return self._snapshot_from_artifact(snapshot, artifact)

    def resolve_image_candidate_readiness(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> bool:
        evidence = self.resolve_authoritative_evidence(
            auth_token, project_id=project_id, version_id=version_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        return evidence.machine_audit.status == "pass"

    def resolve_image_binding_readiness(
        self, auth_token: str, *, project_id: str, version_id: str, shot_no: str,
        expected_project_lock_version: int,
    ) -> bool:
        evidence = self.resolve_authoritative_evidence(
            auth_token, project_id=project_id, version_id=version_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        return evidence.machine_audit.status == "pass" and str(shot_no) in evidence.shot_review.image_confirmed_shot_nos

    def resolve_export_readiness(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> bool:
        evidence = self.resolve_authoritative_evidence(
            auth_token, project_id=project_id, version_id=version_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        confirmation = evidence.final_confirmation
        if (
            evidence.machine_audit.status != "pass"
            or evidence.shot_review.unresolved_count
            or confirmation is None
            or confirmation.decision != "quality_pass"
        ):
            return False
        version = self.reviews.repository.formal_version(evidence.version_id)
        return set(evidence.shot_review.image_confirmed_shot_nos) == {
            str(item["shot_no"]) for item in version.shots
        }

    def _resolve_authoritative(
        self, auth_token: str, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> VersionQualityEvidence:
        access = self.authority.resolve(
            auth_token, project_id=project_id, version_id=version_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        self._require_access(access, project_id, version_id)
        self._require_lock(access, expected_project_lock_version)
        machine = self._machine_audit(access)
        snapshot = self._read_latest_snapshot(auth_token, access)
        linked = self.final_links.final(auth_token, access)
        final = self._read_final_evidence(access, snapshot, linked) if linked else None
        return VersionQualityEvidence(
            workspace_id=access.workspace_id, project_id=access.project_id,
            version_id=access.version_id, run_id=access.run_id,
            machine_audit=machine, shot_review=snapshot, final_confirmation=final,
        )

    def _snapshot_payload(self, access: VersionQualityAccess) -> tuple[ShotReviewSnapshot, dict]:
        snapshot = self.reviews.shot_review_snapshot(
            access.workspace_id, access.project_id, access.version_id
        )
        payload = {
            "schema_version": "mplus-shot-review-snapshot/v1",
            "workspace_id": access.workspace_id, "project_id": access.project_id,
            "version_id": access.version_id, "run_id": access.run_id,
            "total_count": snapshot.total_count, "resolved_count": snapshot.resolved_count,
            "unresolved_count": snapshot.unresolved_count,
            "image_confirmed_shot_nos": list(snapshot.image_confirmed_shot_nos),
            "last_reviewer_actor_id": snapshot.last_reviewer_actor_id,
            "last_reviewed_at": snapshot.last_reviewed_at,
            "review_items": [item.__dict__ for item in self.reviews.repository.review_items(access.version_id)],
            "human_decisions": [item.__dict__ for item in self.reviews.repository.decisions(access.version_id)],
        }
        return snapshot, payload

    @staticmethod
    def _snapshot_from_artifact(
        snapshot: ShotReviewSnapshot, artifact: VersionQualityArtifact,
    ) -> ShotReviewSnapshot:
        return ShotReviewSnapshot(
            snapshot_ref=artifact.ref, snapshot_sha256=artifact.sha256,
            total_count=snapshot.total_count, resolved_count=snapshot.resolved_count,
            unresolved_count=snapshot.unresolved_count,
            last_reviewer_actor_id=snapshot.last_reviewer_actor_id,
            last_reviewed_at=snapshot.last_reviewed_at,
            image_confirmed_shot_nos=snapshot.image_confirmed_shot_nos,
        )

    def _read_latest_snapshot(self, auth_token: str, access: VersionQualityAccess) -> ShotReviewSnapshot:
        artifact = self.final_links.snapshot(auth_token, access)
        if artifact is None:
            raise WorkspaceServiceError("QUALITY_SNAPSHOT_REQUIRED", "当前 version 尚未物化审核快照")
        payload = self.evidence_artifacts.read_json(artifact.ref, artifact.sha256)
        snapshot, expected = self._snapshot_payload(access)
        if payload != expected:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "shot review snapshot 不是当前不可变审核状态")
        return self._snapshot_from_artifact(snapshot, artifact)

    def _machine_audit(self, access: VersionQualityAccess) -> MachineAuditEvidence:
        expected_ref = f"artifact://run/{access.run_id}/manifest"
        if access.manifest_ref != expected_ref:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "run manifest 引用不匹配")
        manifest = self.run_artifacts.read_manifest(
            access.run_id, expected_sha256=access.manifest_sha256
        )
        record = (manifest.get("artifacts") or {}).get("run_report")
        expected_report_sha256 = str((record or {}).get("sha256") or "").lower()
        if not isinstance(record, dict) or len(expected_report_sha256) != 64:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "绑定 manifest 缺少 run_report SHA-256")
        try:
            resolved = self.run_artifacts.resolve_artifact(access.run_id, "run_report")
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError(exc.code, exc.message) from exc
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "run_report 不可读取") from exc
        if resolved.sha256 != expected_report_sha256:
            raise WorkspaceServiceError("ARTIFACT_HASH_MISMATCH", "run_report 已脱离绑定 manifest")
        if not isinstance(payload, dict) or payload.get("run_id") != access.run_id:
            raise WorkspaceServiceError("ARTIFACT_ID_MISMATCH", "run_report 与 version run 不一致")
        return MachineAuditEvidence(
            f"artifact://run/{access.run_id}/run_report", resolved.sha256,
            str(payload.get("machine_audit_status") or "unknown"),
        )

    def _read_final_evidence(
        self, access: VersionQualityAccess, snapshot: ShotReviewSnapshot,
        artifact: VersionQualityArtifact,
    ) -> FinalConfirmationEvidence:
        payload = self.evidence_artifacts.read_json(artifact.ref, artifact.sha256)
        confirmation = payload.get("confirmation") if isinstance(payload, dict) else None
        if (
            not isinstance(confirmation, dict)
            or payload.get("schema_version") != "mplus-final-confirmation-evidence/v1"
            or tuple(payload.get(key) for key in ("workspace_id", "project_id", "version_id", "run_id"))
            != (access.workspace_id, access.project_id, access.version_id, access.run_id)
            or payload.get("total_count") != snapshot.total_count
            or payload.get("snapshot_ref") != snapshot.snapshot_ref
            or payload.get("snapshot_sha256") != snapshot.snapshot_sha256
        ):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "final evidence 与最新快照或版本上下文不匹配")
        stored = self.reviews.repository.final_confirmation(access.version_id)
        if stored is None or confirmation != stored.__dict__:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "final evidence 不是当前不可变确认")
        if not self.actor_verifier.is_reviewer(
            stored.reviewer_actor_id,
            workspace_id=access.workspace_id,
            project_id=access.project_id,
        ):
            raise WorkspaceServiceError("PERMISSION_DENIED", "final confirmation actor 不具备 reviewer 权限")
        return FinalConfirmationEvidence(
            confirmation_id=stored.confirmation_id, decision=stored.decision,
            evidence_ref=artifact.ref, evidence_sha256=artifact.sha256,
            reviewer_actor_id=stored.reviewer_actor_id, confirmed_at=stored.confirmed_at,
        )

    @staticmethod
    def _require_access(access: VersionQualityAccess, project_id: str, version_id: str) -> None:
        if access.project_id != project_id or access.version_id != version_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "authority 返回跨项目或跨版本上下文")

    @staticmethod
    def _require_lock(access: VersionQualityAccess, expected_project_lock_version: int) -> None:
        if access.project_lock_version != expected_project_lock_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project lock version 不匹配")


class AuthenticatedFinalConfirmationService:
    """Non-UI adapter: derives actor from auth and binds final evidence to the latest artifact snapshot."""

    def __init__(
        self, *, resolver: VersionQualityResolver,
        actor_resolver: ReviewerActorResolver,
    ):
        self.resolver = resolver
        self.actor_resolver = actor_resolver

    def confirm(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
        decision: str, evidence_ref: str, evidence_sha256: str,
    ) -> FinalConfirmationEvidence:
        access = self.resolver.authority.resolve(
            auth_token, project_id=project_id, version_id=version_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        self.resolver._require_access(access, project_id, version_id)
        self.resolver._require_lock(access, expected_project_lock_version)
        actor_id = str(self.actor_resolver.resolve_reviewer(
            auth_token, workspace_id=access.workspace_id, project_id=project_id
        ) or "").strip()
        if not actor_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "无法从认证上下文派生 reviewer actor")
        snapshot = self.resolver.materialize_shot_snapshot(
            auth_token, project_id=project_id, version_id=version_id,
            expected_project_lock_version=expected_project_lock_version,
        )
        confirmation = self.resolver.reviews.build_final_confirmation(
            version_id, decision=decision, evidence_ref=evidence_ref,
            evidence_sha256=evidence_sha256, reviewer_actor_id=actor_id,
        )
        self.resolver.reviews.repository.save_final_confirmation(confirmation)
        payload = {
            "schema_version": "mplus-final-confirmation-evidence/v1",
            "workspace_id": access.workspace_id, "project_id": access.project_id,
            "version_id": access.version_id, "run_id": access.run_id,
            "total_count": snapshot.total_count, "snapshot_ref": snapshot.snapshot_ref,
            "snapshot_sha256": snapshot.snapshot_sha256,
            "confirmation": confirmation.__dict__.copy(),
        }
        artifact = self.resolver.evidence_artifacts.write_json("final-confirmation", payload)
        self.resolver.evidence_artifacts.read_json(artifact.ref, artifact.sha256)
        artifact = self.resolver.final_links.save_final(
            auth_token, access, snapshot_ref=snapshot.snapshot_ref, artifact=artifact,
            decision=confirmation.decision, reviewer_actor_id=confirmation.reviewer_actor_id,
            confirmed_at=confirmation.confirmed_at, final_confirmation_id=confirmation.confirmation_id,
        )
        return FinalConfirmationEvidence(
            confirmation_id=confirmation.confirmation_id, decision=confirmation.decision,
            evidence_ref=artifact.ref, evidence_sha256=artifact.sha256,
            reviewer_actor_id=confirmation.reviewer_actor_id,
            confirmed_at=confirmation.confirmed_at,
        )


class VersionQualityService:
    """Legacy stage-one DTO builder; its private predicates are display-only.

    Formal delivery decisions must use VersionQualityResolver's auth-bound
    resolve_*_readiness methods. This legacy service neither publishes nor
    writes CURRENT.
    """

    def __init__(self, *, projects: ProjectService, artifacts: RunArtifactResolver, reviews: VersionReviewService):
        self.projects = projects
        self.artifacts = artifacts
        self.reviews = reviews

    def resolve(self, auth_token: str, *, project_id: str, version_id: str) -> VersionQualityEvidence:
        subject = self._subject(auth_token, project_id=project_id, version_id=version_id)
        machine = self._machine_audit(subject)
        snapshot = self.reviews.shot_review_snapshot(subject.workspace_id, subject.project_id, subject.version_id)
        confirmation = self.reviews.repository.final_confirmation(subject.version_id)
        return VersionQualityEvidence(
            workspace_id=subject.workspace_id, project_id=subject.project_id,
            version_id=subject.version_id, run_id=subject.run_id,
            machine_audit=machine, shot_review=snapshot,
            final_confirmation=_final_evidence(confirmation),
        )

    def _display_can_prepare_image_candidates(self, evidence: VersionQualityEvidence) -> bool:
        return evidence.machine_audit.status == "pass"

    def _display_can_bind_image(self, evidence: VersionQualityEvidence, *, shot_no: str) -> bool:
        return self._display_can_prepare_image_candidates(evidence) and str(shot_no) in evidence.shot_review.image_confirmed_shot_nos

    def _display_can_export_delivery(self, evidence: VersionQualityEvidence) -> bool:
        confirmation = evidence.final_confirmation
        if evidence.machine_audit.status != "pass" or evidence.shot_review.unresolved_count:
            return False
        if confirmation is None or confirmation.decision != "quality_pass":
            return False
        version = self.reviews.repository.formal_version(evidence.version_id)
        expected_shots = {str(item["shot_no"]) for item in version.shots}
        return set(evidence.shot_review.image_confirmed_shot_nos) == expected_shots

    def _subject(self, auth_token: str, *, project_id: str, version_id: str) -> VersionSubject:
        project = self.projects.get_project(auth_token, project_id)
        version = self.projects.get_project_version(auth_token, version_id)
        if version["project_id"] != project_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "version 不属于当前 project")
        binding = self.projects.get_run_binding(auth_token, version["run_id"])
        if binding["project_id"] != project_id:
            raise WorkspaceServiceError("PERMISSION_DENIED", "version run 不属于当前 project")
        return VersionSubject(project["workspace_id"], project_id, version_id, version["run_id"], binding["manifest_sha256"])

    def _machine_audit(self, subject: VersionSubject) -> MachineAuditEvidence:
        manifest = self.artifacts.read_manifest(
            subject.run_id, expected_sha256=subject.manifest_sha256
        )
        record = (manifest.get("artifacts") or {}).get("run_report")
        expected_report_sha256 = str((record or {}).get("sha256") or "").lower()
        if not isinstance(record, dict) or len(expected_report_sha256) != 64:
            raise ArtifactResolutionError("ARTIFACT_INVALID", "绑定 manifest 缺少 run_report SHA-256")
        resolved = self.artifacts.resolve_artifact(subject.run_id, "run_report")
        if resolved.sha256 != expected_report_sha256:
            raise ArtifactResolutionError(
                "ARTIFACT_HASH_MISMATCH", "run_report 已脱离绑定 manifest"
            )
        try:
            payload = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactResolutionError("ARTIFACT_INVALID", "run_report 不可读取") from exc
        if not isinstance(payload, dict) or payload.get("run_id") != subject.run_id:
            raise ArtifactResolutionError("ARTIFACT_ID_MISMATCH", "run_report 与 version run 不一致")
        status = str(payload.get("machine_audit_status") or "unknown")
        return MachineAuditEvidence(f"artifact://run/{subject.run_id}/run_report", resolved.sha256, status)


class DomainFinalConfirmationCommand:
    """阶段一领域命令，不可作为 UI/生产 Command 使用。

    它刻意不接收 auth token、project 或乐观锁版本；最终同库适配必须由
    独立人工 Command 服务从 auth token 派生 actor，并校验 reviewer 权限、
    project 与 current version。
    """

    def __init__(self, reviews: VersionReviewService):
        self.reviews = reviews

    def execute(self, *, version_id: str, decision: str, evidence_ref: str, evidence_sha256: str, reviewer_actor_id: str) -> FinalConfirmation:
        return self.reviews.final_confirm(
            version_id,
            decision=decision,
            evidence_ref=evidence_ref,
            evidence_sha256=evidence_sha256,
            reviewer_actor_id=reviewer_actor_id,
        )


def _final_evidence(value: FinalConfirmation | None) -> FinalConfirmationEvidence | None:
    if value is None:
        return None
    return FinalConfirmationEvidence(
        confirmation_id=value.confirmation_id, decision=value.decision,
        evidence_ref=value.evidence_ref, evidence_sha256=value.evidence_sha256,
        reviewer_actor_id=value.reviewer_actor_id, confirmed_at=value.confirmed_at,
    )
