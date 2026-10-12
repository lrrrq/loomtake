"""Migration 13: immutable mapping from a persisted draft to its materialized version."""

from __future__ import annotations


VERSION_REVIEW_SUBMISSION_MIGRATION_NAME = "version-review-draft-submissions"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE version_review_draft_submissions(
            submission_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            draft_id TEXT NOT NULL UNIQUE REFERENCES version_review_drafts(draft_id) ON DELETE RESTRICT,
            base_version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            materialized_version_id TEXT NOT NULL UNIQUE REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK(revision > 0),
            reason TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            revision_report_ref TEXT NOT NULL CHECK(revision_report_ref LIKE 'artifact://%'),
            revision_report_sha256 TEXT NOT NULL CHECK(length(revision_report_sha256)=64),
            submitted_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            submitted_at TEXT NOT NULL,
            UNIQUE(submitted_by_actor_id, project_id, idempotency_key)
        )
        """,
        "CREATE INDEX version_review_submissions_project_idx ON version_review_draft_submissions(project_id, submitted_at)",
        "CREATE INDEX version_review_submissions_parent_idx ON version_review_draft_submissions(base_version_id, submitted_at)",
        """
        CREATE TRIGGER version_review_submission_scope_match
        BEFORE INSERT ON version_review_draft_submissions
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1
            FROM version_review_drafts d
            JOIN project_versions parent ON parent.version_id=d.base_version_id
            JOIN project_versions child ON child.version_id=NEW.materialized_version_id
            JOIN projects p ON p.project_id=d.project_id
            WHERE d.draft_id=NEW.draft_id
              AND d.base_version_id=NEW.base_version_id
              AND d.workspace_id=NEW.workspace_id
              AND d.project_id=NEW.project_id
              AND parent.project_id=NEW.project_id
              AND child.project_id=NEW.project_id
              AND child.parent_version_id=NEW.base_version_id
              AND p.workspace_id=NEW.workspace_id
        ) THEN RAISE(ABORT, 'REVIEW_SUBMISSION_SCOPE_MISMATCH') END; END
        """,
        "CREATE TRIGGER version_review_submissions_no_update BEFORE UPDATE ON version_review_draft_submissions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_submissions_no_delete BEFORE DELETE ON version_review_draft_submissions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )
