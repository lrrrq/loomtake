from __future__ import annotations

from dataclasses import dataclass

from .identity_service import require_permission
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    new_id,
    require_artifact_ref,
    require_sha256,
    utc_now,
)
from .version_quality_artifacts import VersionQualityArtifact


@dataclass(frozen=True)
class PersistedQualitySnapshot:
    snapshot_id: str
    artifact: VersionQualityArtifact


@dataclass(frozen=True)
class PersistedFinalEvidence:
    final_confirmation_id: str
    artifact: VersionQualityArtifact


class WorkspaceVersionQualityRepository:
    """Migration-9 link repository; only immutable facts live in WorkspaceStore."""

    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.store.initialize()

    def save_snapshot(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        version_id: str, run_id: str, artifact: VersionQualityArtifact,
        review_content_sha256: str, total_count: int, resolved_count: int,
        unresolved_count: int, last_reviewer_actor_id: str | None,
        last_reviewed_at: str | None, expected_project_lock_version: int,
    ) -> PersistedQualitySnapshot:
        artifact = _artifact(artifact, "snapshot")
        review_content_sha256 = require_sha256(review_content_sha256, "review_content_sha256")
        _counts(total_count, resolved_count, unresolved_count)
        with self.store.transaction() as connection:
            actor = require_permission(
                connection, auth_token, workspace_id=workspace_id, project_id=project_id,
                permission="review.record",
            )
            _require_context(
                connection, workspace_id, project_id, version_id, run_id,
                expected_project_lock_version,
            )
            if last_reviewer_actor_id is not None:
                _require_project_actor(connection, last_reviewer_actor_id, workspace_id, project_id)
            if connection.execute(
                "SELECT 1 FROM version_quality_final_confirmations WHERE version_id=?",
                (version_id,),
            ).fetchone() is not None:
                raise WorkspaceServiceError("FINAL_CONFIRMATION_EXISTS", "final confirmation 后不可覆盖 snapshot")
            existing = connection.execute(
                """SELECT snapshot_id FROM version_quality_snapshots
                WHERE version_id=? AND snapshot_ref=? AND snapshot_sha256=?""",
                (version_id, artifact.ref, artifact.sha256),
            ).fetchone()
            if existing is not None:
                return PersistedQualitySnapshot(str(existing["snapshot_id"]), artifact)
            snapshot_id = new_id("quality_snapshot")
            timestamp = utc_now()
            connection.execute(
                """INSERT INTO version_quality_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (snapshot_id, workspace_id, project_id, version_id, run_id, artifact.ref,
                 artifact.sha256, review_content_sha256, total_count, resolved_count,
                 unresolved_count, actor["actor_id"], last_reviewer_actor_id, last_reviewed_at, timestamp),
            )
            row = connection.execute(
                "SELECT snapshot_id FROM version_quality_snapshot_links WHERE version_id=?", (version_id,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO version_quality_snapshot_links VALUES (?, ?, ?)",
                    (version_id, snapshot_id, timestamp),
                )
            else:
                connection.execute(
                    "UPDATE version_quality_snapshot_links SET snapshot_id=?, updated_at=? WHERE version_id=?",
                    (snapshot_id, timestamp, version_id),
                )
        return PersistedQualitySnapshot(snapshot_id, artifact)

    def current_snapshot(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        version_id: str, run_id: str, expected_project_lock_version: int,
    ) -> PersistedQualitySnapshot | None:
        with self.store.connection() as connection:
            require_permission(connection, auth_token, workspace_id=workspace_id, project_id=project_id, permission="project.read")
            _require_context(
                connection, workspace_id, project_id, version_id, run_id,
                expected_project_lock_version,
            )
            row = connection.execute(
                """SELECT s.snapshot_id, s.snapshot_ref, s.snapshot_sha256
                FROM version_quality_snapshot_links l JOIN version_quality_snapshots s ON s.snapshot_id=l.snapshot_id
                WHERE l.version_id=?""", (version_id,)
            ).fetchone()
        return None if row is None else PersistedQualitySnapshot(
            str(row["snapshot_id"]), VersionQualityArtifact(str(row["snapshot_ref"]), str(row["snapshot_sha256"]))
        )

    def save_final(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        version_id: str, run_id: str, snapshot_id: str, artifact: VersionQualityArtifact,
        decision: str, reviewer_actor_id: str, confirmed_at: str,
        expected_project_lock_version: int, final_confirmation_id: str | None = None,
    ) -> PersistedFinalEvidence:
        artifact = _artifact(artifact, "confirmation")
        if decision not in {"quality_pass", "quality_fail"}:
            raise WorkspaceServiceError("INVALID_INPUT", "final decision 无效")
        if not str(confirmed_at).strip():
            raise WorkspaceServiceError("INVALID_INPUT", "confirmed_at 不能为空")
        with self.store.transaction() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id, project_id=project_id, permission="review.record")
            if actor["actor_id"] != reviewer_actor_id:
                raise WorkspaceServiceError("PERMISSION_DENIED", "final reviewer 必须来自认证 actor")
            _require_context(
                connection, workspace_id, project_id, version_id, run_id,
                expected_project_lock_version,
            )
            current = connection.execute(
                "SELECT snapshot_id FROM version_quality_snapshot_links WHERE version_id=?", (version_id,)
            ).fetchone()
            if current is None or current["snapshot_id"] != snapshot_id:
                raise WorkspaceServiceError("QUALITY_SNAPSHOT_STALE", "final evidence 必须绑定当前 snapshot")
            if connection.execute(
                "SELECT 1 FROM version_quality_final_confirmations WHERE version_id=?",
                (version_id,),
            ).fetchone() is not None:
                raise WorkspaceServiceError("FINAL_CONFIRMATION_EXISTS", "final evidence 不可覆盖")
            final_id = str(final_confirmation_id or new_id("quality_final")).strip()
            if not final_id:
                raise WorkspaceServiceError("INVALID_INPUT", "final confirmation id 不能为空")
            connection.execute(
                "INSERT INTO version_quality_final_confirmations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (final_id, workspace_id, project_id, version_id, run_id, snapshot_id,
                 artifact.ref, artifact.sha256, decision, reviewer_actor_id, confirmed_at, utc_now()),
            )
        return PersistedFinalEvidence(final_id, artifact)

    def final(
        self, auth_token: str, *, workspace_id: str, project_id: str,
        version_id: str, run_id: str, expected_project_lock_version: int,
    ) -> PersistedFinalEvidence | None:
        with self.store.connection() as connection:
            require_permission(connection, auth_token, workspace_id=workspace_id, project_id=project_id, permission="project.read")
            _require_context(
                connection, workspace_id, project_id, version_id, run_id,
                expected_project_lock_version,
            )
            row = connection.execute(
                "SELECT final_confirmation_id, confirmation_ref, confirmation_sha256 FROM version_quality_final_confirmations WHERE version_id=?",
                (version_id,),
            ).fetchone()
        return None if row is None else PersistedFinalEvidence(
            str(row["final_confirmation_id"]), VersionQualityArtifact(str(row["confirmation_ref"]), str(row["confirmation_sha256"]))
        )


def _artifact(value: VersionQualityArtifact, label: str) -> VersionQualityArtifact:
    return VersionQualityArtifact(
        require_artifact_ref(value.ref, f"{label}_ref"),
        require_sha256(value.sha256, f"{label}_sha256"),
    )


def _counts(total: int, resolved: int, unresolved: int) -> None:
    if any(type(value) is not int or value < 0 for value in (total, resolved, unresolved)):
        raise WorkspaceServiceError("INVALID_INPUT", "quality counts 无效")


def _require_context(
    connection, workspace_id: str, project_id: str, version_id: str, run_id: str,
    expected_project_lock_version: int,
) -> None:
    row = connection.execute(
        """SELECT 1 FROM projects p JOIN project_versions v ON v.project_id=p.project_id
        JOIN project_runs r ON r.run_id=v.run_id AND r.project_id=p.project_id
        WHERE p.workspace_id=? AND p.project_id=? AND v.version_id=? AND r.run_id=?""",
        (workspace_id, project_id, version_id, run_id),
    ).fetchone()
    if row is None:
        raise WorkspaceServiceError("INTEGRITY_ERROR", "workspace/project/version/run 绑定不一致")
    current = connection.execute(
        "SELECT lock_version, current_version_id FROM projects WHERE project_id=?",
        (project_id,),
    ).fetchone()
    if current is None or current["current_version_id"] != version_id:
        raise WorkspaceServiceError("VERSION_NOT_CURRENT", "version 不是 project 当前版本")
    if int(current["lock_version"]) != expected_project_lock_version:
        raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project lock version 不匹配")


def _require_project_actor(connection, actor_id: str, workspace_id: str, project_id: str) -> None:
    row = connection.execute(
        "SELECT 1 FROM role_assignments WHERE actor_id=? AND workspace_id=? AND (project_id IS NULL OR project_id=?)",
        (actor_id, workspace_id, project_id),
    ).fetchone()
    if row is None:
        raise WorkspaceServiceError("PERMISSION_DENIED", "reviewer actor 不属于当前 project")


class WorkspaceFinalEvidenceLinkRepository:
    """Production FinalEvidenceLinkRepository backed by migration 9."""

    def __init__(self, repository: WorkspaceVersionQualityRepository):
        self.repository = repository

    def save_snapshot(self, auth_token, access, artifact, **metadata):
        persisted = self.repository.save_snapshot(
            auth_token,
            workspace_id=access.workspace_id,
            project_id=access.project_id,
            version_id=access.version_id,
            run_id=access.run_id,
            artifact=artifact,
            expected_project_lock_version=access.project_lock_version,
            **metadata,
        )
        return persisted.artifact

    def snapshot(self, auth_token, access):
        persisted = self.repository.current_snapshot(
            auth_token,
            workspace_id=access.workspace_id,
            project_id=access.project_id,
            version_id=access.version_id,
            run_id=access.run_id,
            expected_project_lock_version=access.project_lock_version,
        )
        return None if persisted is None else persisted.artifact

    def save_final(self, auth_token, access, *, snapshot_ref, artifact, **metadata):
        snapshot = self.repository.current_snapshot(
            auth_token,
            workspace_id=access.workspace_id,
            project_id=access.project_id,
            version_id=access.version_id,
            run_id=access.run_id,
            expected_project_lock_version=access.project_lock_version,
        )
        if snapshot is None or snapshot.artifact.ref != snapshot_ref:
            raise WorkspaceServiceError("QUALITY_SNAPSHOT_STALE", "final evidence 必须绑定当前 snapshot")
        persisted = self.repository.save_final(
            auth_token,
            workspace_id=access.workspace_id,
            project_id=access.project_id,
            version_id=access.version_id,
            run_id=access.run_id,
            snapshot_id=snapshot.snapshot_id,
            artifact=artifact,
            expected_project_lock_version=access.project_lock_version,
            **metadata,
        )
        return persisted.artifact

    def final(self, auth_token, access):
        persisted = self.repository.final(
            auth_token,
            workspace_id=access.workspace_id,
            project_id=access.project_id,
            version_id=access.version_id,
            run_id=access.run_id,
            expected_project_lock_version=access.project_lock_version,
        )
        return None if persisted is None else persisted.artifact


class WorkspaceReviewerActorVerifier:
    """Checks reviewer capability without trusting an actor id supplied by a caller."""

    def __init__(self, store: WorkspaceStore):
        self.store = store

    def is_reviewer(self, actor_id: str, *, workspace_id: str, project_id: str) -> bool:
        with self.store.connection() as connection:
            row = connection.execute(
                """SELECT 1
                FROM actors a
                JOIN role_assignments ra ON ra.actor_id=a.actor_id
                JOIN role_permissions rp ON rp.role_id=ra.role_id
                WHERE a.actor_id=? AND a.status='active' AND ra.workspace_id=?
                  AND (ra.project_id IS NULL OR ra.project_id=?)
                  AND rp.permission_id='review.record'
                LIMIT 1""",
                (actor_id, workspace_id, project_id),
            ).fetchone()
        return row is not None
