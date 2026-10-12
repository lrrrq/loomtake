from __future__ import annotations

from contextlib import nullcontext

import json

from .identity_service import require_permission
from .mutation_policy import require_active_project
from .project_service import _increment_project, _project
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    check_version,
    enqueue_outbox,
    new_id,
    require_artifact_ref,
    require_nonempty,
    row_dict,
    utc_now,
)


def _version(connection, version_id: str):
    row = connection.execute(
        "SELECT * FROM project_versions WHERE version_id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise WorkspaceServiceError("NOT_FOUND", "version 不存在")
    return row


class VersionService:
    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.store.initialize()

    def record_review(
        self,
        auth_token: str,
        *,
        version_id: str,
        decision: str,
        evidence_ref: str,
        expected_project_version: int,
        _connection=None,
    ) -> dict:
        if decision not in {"quality_pass", "quality_fail"}:
            raise WorkspaceServiceError("INVALID_INPUT", "未知人工审核结论")
        evidence_ref = require_artifact_ref(evidence_ref, "evidence_ref")
        review_id = new_id("review")
        timestamp = utc_now()
        with (nullcontext(_connection) if _connection is not None else self.store.transaction()) as connection:
            version = _version(connection, version_id)
            project = _project(connection, version["project_id"])
            require_active_project(project)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                permission="review.record",
            )
            existing = connection.execute(
                """
                SELECT * FROM version_reviews
                WHERE project_id = ? AND version_id = ? AND evidence_ref = ?
                ORDER BY created_at, rowid LIMIT 1
                """,
                (project["project_id"], version_id, evidence_ref),
            ).fetchone()
            if existing is not None:
                if existing["decision"] != decision:
                    raise WorkspaceServiceError(
                        "REVIEW_EVIDENCE_CONFLICT",
                        "同一审核证据不能登记不同结论",
                    )
                return {
                    "review_id": existing["review_id"],
                    "project_id": project["project_id"],
                    "version_id": version_id,
                    "decision": decision,
                    "evidence_ref": evidence_ref,
                    "project_version": int(project["lock_version"]),
                }
            check_version(
                project["lock_version"],
                expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            connection.execute(
                """
                INSERT INTO version_reviews(
                    review_id, project_id, version_id, reviewer_actor_id,
                    decision, evidence_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    project["project_id"],
                    version_id,
                    actor["actor_id"],
                    decision,
                    evidence_ref,
                    timestamp,
                ),
            )
            project_version = _increment_project(
                connection,
                project_id=project["project_id"],
                expected_project_version=expected_project_version,
                timestamp=timestamp,
            )
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project["project_id"],
                event_type="project.version_reviewed",
                payload={
                    "decision": decision,
                    "evidence_ref": evidence_ref,
                    "review_id": review_id,
                    "version_id": version_id,
                },
            )
        return {
            "review_id": review_id,
            "project_id": project["project_id"],
            "version_id": version_id,
            "decision": decision,
            "evidence_ref": evidence_ref,
            "project_version": project_version,
        }

    def select_candidate(
        self,
        auth_token: str,
        *,
        version_id: str,
        expected_project_version: int,
        reason: str,
        _connection=None,
    ) -> dict:
        reason = require_nonempty(reason, "reason")
        candidate_id = new_id("candidate")
        timestamp = utc_now()
        with (nullcontext(_connection) if _connection is not None else self.store.transaction()) as connection:
            version = _version(connection, version_id)
            project = _project(connection, version["project_id"])
            require_active_project(project)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                permission="version.select_candidate",
            )
            check_version(
                project["lock_version"],
                expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            connection.execute(
                """
                INSERT INTO candidate_selections(
                    candidate_id, project_id, version_id,
                    selected_by_actor_id, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    project["project_id"],
                    version_id,
                    actor["actor_id"],
                    reason,
                    timestamp,
                ),
            )
            project_version = _increment_project(
                connection,
                project_id=project["project_id"],
                expected_project_version=expected_project_version,
                timestamp=timestamp,
            )
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project["project_id"],
                event_type="project.candidate_selected",
                payload={"candidate_id": candidate_id, "version_id": version_id},
            )
        return {
            "candidate_id": candidate_id,
            "project_id": project["project_id"],
            "version_id": version_id,
            "reason": reason,
            "project_version": project_version,
        }

    def publish_version(
        self,
        auth_token: str,
        *,
        version_id: str,
        review_id: str,
        expected_project_version: int,
        _connection=None,
    ) -> dict:
        review_id = require_nonempty(review_id, "review_id")
        publication_id = new_id("publication")
        replayed = False
        timestamp = utc_now()
        with (nullcontext(_connection) if _connection is not None else self.store.transaction()) as connection:
            version = _version(connection, version_id)
            project = _project(connection, version["project_id"])
            require_active_project(project)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                permission="version.publish",
            )
            check_version(
                project["lock_version"],
                expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            candidate = connection.execute(
                """
                SELECT version_id FROM candidate_selections
                WHERE project_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (project["project_id"],),
            ).fetchone()
            if candidate is None or candidate["version_id"] != version_id:
                raise WorkspaceServiceError(
                    "CANDIDATE_REQUIRED", "只有当前候选 version 可以发布"
                )
            review = connection.execute(
                """
                SELECT * FROM version_reviews
                WHERE review_id = ? AND project_id = ? AND version_id = ?
                """,
                (review_id, project["project_id"], version_id),
            ).fetchone()
            if review is None or review["decision"] != "quality_pass":
                raise WorkspaceServiceError(
                    "REVIEW_REQUIRED", "发布前必须存在目标 version 的人工 quality_pass"
                )
            latest_review = connection.execute(
                """
                SELECT review_id FROM version_reviews
                WHERE project_id = ? AND version_id = ?
                ORDER BY created_at DESC, rowid DESC LIMIT 1
                """,
                (project["project_id"], version_id),
            ).fetchone()
            if latest_review["review_id"] != review_id:
                raise WorkspaceServiceError(
                    "REVIEW_REQUIRED", "只能使用目标 version 的最新人工审核结论发布"
                )
            existing = connection.execute(
                """SELECT publication_id FROM version_publications
                WHERE project_id=? AND version_id=? AND review_id=?
                ORDER BY created_at DESC, publication_id DESC LIMIT 1""",
                (project["project_id"], version_id, review_id),
            ).fetchone()
            if existing is not None:
                publication_id = str(existing["publication_id"])
                project_version = int(project["lock_version"])
                replayed = True
            else:
                connection.execute(
                    """
                    INSERT INTO version_publications(
                        publication_id, project_id, version_id, review_id,
                        published_by_actor_id, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, 'published', ?)
                    """,
                    (
                        publication_id,
                        project["project_id"],
                        version_id,
                        review_id,
                        actor["actor_id"],
                        timestamp,
                    ),
                )
                project_version = _increment_project(
                    connection,
                    project_id=project["project_id"],
                    expected_project_version=expected_project_version,
                    timestamp=timestamp,
                )
                enqueue_outbox(
                    connection,
                    aggregate_type="project",
                    aggregate_id=project["project_id"],
                    event_type="project.version_published",
                    payload={
                        "artifact_ref": version["artifact_ref"],
                        "content_sha256": version["content_sha256"],
                        "publication_id": publication_id,
                        "review_id": review_id,
                        "version_id": version_id,
                    },
                )
        return {
            "publication_id": publication_id,
            "project_id": project["project_id"],
            "version_id": version_id,
            "review_id": review_id,
            "status": "published",
            "projection_status": "pending",
            "project_version": project_version,
            "replayed": replayed,
        }

    def list_reviews(self, auth_token: str, version_id: str) -> list[dict]:
        return self._read_version_records(
            auth_token,
            version_id,
            "SELECT * FROM version_reviews WHERE version_id = ? ORDER BY created_at, review_id",
        )

    def list_candidates(self, auth_token: str, version_id: str) -> list[dict]:
        return self._read_version_records(
            auth_token,
            version_id,
            "SELECT * FROM candidate_selections WHERE version_id = ? ORDER BY created_at, candidate_id",
        )

    def list_publications(self, auth_token: str, version_id: str) -> list[dict]:
        records = self._read_version_records(
            auth_token,
            version_id,
            "SELECT * FROM version_publications WHERE version_id = ? ORDER BY created_at, publication_id",
        )
        if not records:
            return records
        project_id = records[0]["project_id"]
        with self.store.connection() as connection:
            events = connection.execute(
                """SELECT payload_json, status, published_at, last_error
                FROM outbox WHERE aggregate_type='project' AND aggregate_id=?
                  AND event_type='project.version_published'
                ORDER BY sequence_id""",
                (project_id,),
            ).fetchall()
        projections = {}
        for event in events:
            try:
                publication_id = str(json.loads(event["payload_json"])["publication_id"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            projections[publication_id] = event
        for record in records:
            projection = projections.get(record["publication_id"])
            record["projection_status"] = (
                str(projection["status"]) if projection is not None else "failed"
            )
            record["projection_error"] = (
                str(projection["last_error"] or "") if projection is not None else "OUTBOX_MISSING"
            )
            record["projected_at"] = (
                projection["published_at"] if projection is not None else None
            )
        return records

    def _read_version_records(
        self,
        auth_token: str,
        version_id: str,
        statement: str,
    ) -> list[dict]:
        with self.store.connection() as connection:
            version = _version(connection, version_id)
            project = _project(connection, version["project_id"])
            require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project["project_id"],
                permission="project.read",
            )
            rows = connection.execute(statement, (version_id,)).fetchall()
        return [row_dict(row) for row in rows]
