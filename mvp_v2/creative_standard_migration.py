"""T00-owned additive migration for immutable formal creative standards."""

from __future__ import annotations


CREATIVE_STANDARD_MIGRATION_NAME = "creative-standard-cards"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE creative_standard_versions(
            creative_standard_version_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            brief_version_id TEXT NOT NULL UNIQUE REFERENCES brief_versions(brief_version_id) ON DELETE RESTRICT,
            brief_payload_ref TEXT NOT NULL,
            brief_payload_sha256 TEXT NOT NULL,
            payload_ref TEXT NOT NULL UNIQUE,
            payload_sha256 TEXT NOT NULL UNIQUE,
            content_sha256 TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE creative_standard_confirmations(
            creative_standard_confirmation_id TEXT PRIMARY KEY,
            creative_standard_version_id TEXT NOT NULL UNIQUE REFERENCES creative_standard_versions(creative_standard_version_id) ON DELETE RESTRICT,
            brief_confirmation_id TEXT NOT NULL UNIQUE REFERENCES brief_confirmations(confirmation_id) ON DELETE RESTRICT,
            brief_snapshot_id TEXT NOT NULL UNIQUE REFERENCES project_brief_snapshots(brief_snapshot_id) ON DELETE RESTRICT,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            confirmed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            confirmed_at TEXT NOT NULL
        )
        """,
        "ALTER TABLE projects ADD COLUMN current_creative_standard_version_id TEXT REFERENCES creative_standard_versions(creative_standard_version_id) ON DELETE RESTRICT",
        "CREATE INDEX creative_standard_versions_workspace_project_idx ON creative_standard_versions(workspace_id, project_id, created_at)",
        "CREATE INDEX creative_standard_confirmations_workspace_project_idx ON creative_standard_confirmations(workspace_id, project_id, confirmed_at)",
        "CREATE INDEX projects_current_creative_standard_idx ON projects(current_creative_standard_version_id)",
        """
        CREATE TRIGGER creative_standard_versions_scope_match BEFORE INSERT ON creative_standard_versions
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM brief_versions v JOIN projects p ON p.project_id=v.project_id
            WHERE v.brief_version_id=NEW.brief_version_id
              AND v.workspace_id=NEW.workspace_id AND v.project_id=NEW.project_id
              AND p.workspace_id=NEW.workspace_id
              AND v.payload_ref=NEW.brief_payload_ref AND v.payload_sha256=NEW.brief_payload_sha256
        ) THEN RAISE(ABORT, 'CREATIVE_STANDARD_BRIEF_BINDING_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER creative_standard_confirmations_binding_match BEFORE INSERT ON creative_standard_confirmations
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM creative_standard_versions s
            JOIN brief_confirmations b ON b.confirmation_id=NEW.brief_confirmation_id
            WHERE s.creative_standard_version_id=NEW.creative_standard_version_id
              AND s.workspace_id=NEW.workspace_id AND s.project_id=NEW.project_id
              AND b.workspace_id=NEW.workspace_id AND b.project_id=NEW.project_id
              AND b.brief_version_id=s.brief_version_id AND b.brief_snapshot_id=NEW.brief_snapshot_id
        ) THEN RAISE(ABORT, 'CREATIVE_STANDARD_CONFIRMATION_BINDING_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER projects_current_creative_standard_match BEFORE UPDATE OF current_creative_standard_version_id, current_brief_snapshot_id ON projects
        WHEN NEW.current_creative_standard_version_id IS NOT NULL
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM creative_standard_confirmations c
            WHERE c.creative_standard_version_id=NEW.current_creative_standard_version_id
              AND c.project_id=NEW.project_id AND c.brief_snapshot_id=NEW.current_brief_snapshot_id
        ) THEN RAISE(ABORT, 'CREATIVE_STANDARD_CURRENT_BINDING_MISMATCH') END; END
        """,
        "CREATE TRIGGER creative_standard_versions_no_update BEFORE UPDATE ON creative_standard_versions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER creative_standard_versions_no_delete BEFORE DELETE ON creative_standard_versions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER creative_standard_confirmations_no_update BEFORE UPDATE ON creative_standard_confirmations BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER creative_standard_confirmations_no_delete BEFORE DELETE ON creative_standard_confirmations BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )
