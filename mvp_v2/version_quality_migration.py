"""T07-authorized WorkspaceStore migration 9 for version-quality evidence."""

from __future__ import annotations


VERSION_QUALITY_MIGRATION_NAME = "version-quality-evidence"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE version_quality_snapshots(
            snapshot_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL REFERENCES project_runs(run_id) ON DELETE RESTRICT,
            snapshot_ref TEXT NOT NULL UNIQUE,
            snapshot_sha256 TEXT NOT NULL UNIQUE,
            review_content_sha256 TEXT NOT NULL,
            total_count INTEGER NOT NULL CHECK(total_count >= 0),
            resolved_count INTEGER NOT NULL CHECK(resolved_count >= 0),
            unresolved_count INTEGER NOT NULL CHECK(unresolved_count >= 0),
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            last_reviewer_actor_id TEXT REFERENCES actors(actor_id) ON DELETE RESTRICT,
            last_reviewed_at TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_quality_snapshot_links(
            version_id TEXT PRIMARY KEY REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL UNIQUE REFERENCES version_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_quality_final_confirmations(
            final_confirmation_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL UNIQUE REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL REFERENCES project_runs(run_id) ON DELETE RESTRICT,
            snapshot_id TEXT NOT NULL REFERENCES version_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
            confirmation_ref TEXT NOT NULL UNIQUE,
            confirmation_sha256 TEXT NOT NULL UNIQUE,
            decision TEXT NOT NULL CHECK(decision IN ('quality_pass','quality_fail')),
            reviewer_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            confirmed_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX version_quality_snapshots_project_version_idx ON version_quality_snapshots(project_id, version_id, created_at)",
        "CREATE INDEX version_quality_final_project_version_idx ON version_quality_final_confirmations(project_id, version_id, created_at)",
        """
        CREATE TRIGGER version_quality_snapshot_scope_match BEFORE INSERT ON version_quality_snapshots
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM projects p JOIN project_versions v ON v.project_id=p.project_id
            JOIN project_runs r ON r.run_id=v.run_id AND r.project_id=p.project_id
            WHERE p.workspace_id=NEW.workspace_id AND p.project_id=NEW.project_id
              AND v.version_id=NEW.version_id AND r.run_id=NEW.run_id
        ) THEN RAISE(ABORT, 'QUALITY_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_quality_snapshot_link_scope_update BEFORE UPDATE OF snapshot_id ON version_quality_snapshot_links
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_quality_snapshots s
            WHERE s.snapshot_id=NEW.snapshot_id AND s.version_id=NEW.version_id
        ) THEN RAISE(ABORT, 'QUALITY_SNAPSHOT_LINK_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_quality_snapshot_link_scope_match BEFORE INSERT ON version_quality_snapshot_links
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_quality_snapshots s
            WHERE s.snapshot_id=NEW.snapshot_id AND s.version_id=NEW.version_id
        ) THEN RAISE(ABORT, 'QUALITY_SNAPSHOT_LINK_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_quality_final_current_snapshot BEFORE INSERT ON version_quality_final_confirmations
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_quality_snapshot_links l
            WHERE l.version_id=NEW.version_id AND l.snapshot_id=NEW.snapshot_id
        ) THEN RAISE(ABORT, 'QUALITY_FINAL_NOT_CURRENT_SNAPSHOT') END; END
        """,
        """
        CREATE TRIGGER version_quality_final_scope_match BEFORE INSERT ON version_quality_final_confirmations
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_quality_snapshots s JOIN projects p ON p.project_id=s.project_id
            WHERE s.snapshot_id=NEW.snapshot_id AND s.workspace_id=NEW.workspace_id
              AND s.project_id=NEW.project_id AND s.version_id=NEW.version_id AND s.run_id=NEW.run_id
              AND p.workspace_id=NEW.workspace_id
        ) THEN RAISE(ABORT, 'QUALITY_FINAL_SCOPE_MISMATCH') END; END
        """,
        "CREATE TRIGGER version_quality_snapshots_no_update BEFORE UPDATE ON version_quality_snapshots BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_quality_snapshots_no_delete BEFORE DELETE ON version_quality_snapshots BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_quality_final_no_update BEFORE UPDATE ON version_quality_final_confirmations BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_quality_final_no_delete BEFORE DELETE ON version_quality_final_confirmations BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )
