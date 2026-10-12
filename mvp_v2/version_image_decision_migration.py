"""Migration 14: immutable per-shot image decisions for one script version."""

from __future__ import annotations


VERSION_IMAGE_DECISION_MIGRATION_NAME = "version-image-decision-snapshots"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE version_image_decision_snapshots(
            snapshot_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            base_version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            materialized_version_id TEXT NOT NULL UNIQUE REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL UNIQUE REFERENCES project_runs(run_id) ON DELETE RESTRICT,
            shot_count INTEGER NOT NULL CHECK(shot_count > 0),
            confirmed_count INTEGER NOT NULL CHECK(confirmed_count = shot_count),
            snapshot_ref TEXT NOT NULL UNIQUE CHECK(snapshot_ref LIKE 'artifact://%'),
            snapshot_sha256 TEXT NOT NULL UNIQUE CHECK(length(snapshot_sha256)=64),
            request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
            idempotency_key TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            UNIQUE(created_by_actor_id, project_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE version_image_decisions(
            decision_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL REFERENCES version_image_decision_snapshots(snapshot_id) ON DELETE RESTRICT,
            shot_no TEXT NOT NULL,
            project_image_id TEXT NOT NULL REFERENCES project_images(project_image_id) ON DELETE RESTRICT,
            image_sha256 TEXT NOT NULL CHECK(length(image_sha256)=64),
            analysis_version_id TEXT NOT NULL REFERENCES project_image_analysis_versions(project_image_analysis_version_id) ON DELETE RESTRICT,
            analysis_ref TEXT NOT NULL CHECK(analysis_ref LIKE 'artifact://%'),
            analysis_sha256 TEXT NOT NULL CHECK(length(analysis_sha256)=64),
            source_document_id TEXT NOT NULL,
            source_location_json TEXT NOT NULL,
            decided_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            decided_at TEXT NOT NULL,
            UNIQUE(snapshot_id, shot_no),
            UNIQUE(snapshot_id, project_image_id),
            UNIQUE(snapshot_id, image_sha256)
        )
        """,
        "CREATE INDEX version_image_snapshots_project_idx ON version_image_decision_snapshots(project_id, created_at)",
        "CREATE INDEX version_image_decisions_snapshot_idx ON version_image_decisions(snapshot_id, shot_no)",
        """
        CREATE TRIGGER version_image_snapshot_scope_match
        BEFORE INSERT ON version_image_decision_snapshots
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1
            FROM projects p
            JOIN project_versions parent ON parent.version_id=NEW.base_version_id
            JOIN project_versions child ON child.version_id=NEW.materialized_version_id
            JOIN project_runs r ON r.run_id=NEW.run_id
            WHERE p.project_id=NEW.project_id AND p.workspace_id=NEW.workspace_id
              AND parent.project_id=NEW.project_id
              AND child.project_id=NEW.project_id
              AND child.parent_version_id=NEW.base_version_id
              AND child.run_id=NEW.run_id
              AND r.project_id=NEW.project_id
        ) THEN RAISE(ABORT, 'VERSION_IMAGE_SNAPSHOT_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_image_decision_scope_match
        BEFORE INSERT ON version_image_decisions
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1
            FROM version_image_decision_snapshots s
            JOIN project_images i ON i.project_image_id=NEW.project_image_id
            JOIN project_image_analysis_versions a
              ON a.project_image_analysis_version_id=NEW.analysis_version_id
            WHERE s.snapshot_id=NEW.snapshot_id
              AND i.workspace_id=s.workspace_id AND i.project_id=s.project_id
              AND i.image_sha256=NEW.image_sha256
              AND a.project_image_id=i.project_image_id
              AND a.workspace_id=s.workspace_id AND a.project_id=s.project_id
              AND a.image_sha256=NEW.image_sha256 AND a.status='pass'
              AND a.artifact_ref=NEW.analysis_ref
              AND a.artifact_sha256=NEW.analysis_sha256
        ) THEN RAISE(ABORT, 'VERSION_IMAGE_DECISION_SCOPE_MISMATCH') END; END
        """,
        "CREATE TRIGGER version_image_snapshots_no_update BEFORE UPDATE ON version_image_decision_snapshots BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_image_snapshots_no_delete BEFORE DELETE ON version_image_decision_snapshots BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_image_decisions_no_update BEFORE UPDATE ON version_image_decisions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_image_decisions_no_delete BEFORE DELETE ON version_image_decisions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )
