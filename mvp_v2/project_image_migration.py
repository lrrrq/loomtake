"""Migration 12: project-scoped image authority and manual analysis evidence."""

from __future__ import annotations


PROJECT_IMAGE_MIGRATION_NAME = "project-image-analysis-authority"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE project_images(
            project_image_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            image_sha256 TEXT NOT NULL,
            artifact_ref TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, image_sha256),
            UNIQUE(project_id, artifact_ref)
        )
        """,
        """
        CREATE TABLE project_image_source_links(
            project_image_source_link_id TEXT PRIMARY KEY,
            project_image_id TEXT NOT NULL REFERENCES project_images(project_image_id) ON DELETE RESTRICT,
            document_image_id TEXT NOT NULL UNIQUE REFERENCES document_images(document_image_id) ON DELETE RESTRICT,
            source_document_id TEXT NOT NULL,
            source_location_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE project_image_analysis_versions(
            project_image_analysis_version_id TEXT PRIMARY KEY,
            project_image_id TEXT NOT NULL REFERENCES project_images(project_image_id) ON DELETE RESTRICT,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            brief_version_id TEXT NOT NULL REFERENCES brief_versions(brief_version_id) ON DELETE RESTRICT,
            brief_snapshot_id TEXT NOT NULL REFERENCES project_brief_snapshots(brief_snapshot_id) ON DELETE RESTRICT,
            image_sha256 TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pass','low_confidence','blocked','failed')),
            artifact_ref TEXT NOT NULL UNIQUE,
            artifact_sha256 TEXT NOT NULL UNIQUE,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE project_image_analysis_current_links(
            project_image_id TEXT PRIMARY KEY REFERENCES project_images(project_image_id) ON DELETE RESTRICT,
            project_image_analysis_version_id TEXT NOT NULL UNIQUE REFERENCES project_image_analysis_versions(project_image_analysis_version_id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX project_images_scope_idx ON project_images(workspace_id, project_id, created_at)",
        "CREATE INDEX project_image_links_image_idx ON project_image_source_links(project_image_id)",
        "CREATE INDEX project_image_analysis_scope_idx ON project_image_analysis_versions(workspace_id, project_id, brief_version_id, created_at)",
        """
        CREATE TRIGGER project_images_scope_match BEFORE INSERT ON project_images
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM projects WHERE project_id=NEW.project_id AND workspace_id=NEW.workspace_id
        ) THEN RAISE(ABORT, 'PROJECT_IMAGE_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER project_image_source_scope_match BEFORE INSERT ON project_image_source_links
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM project_images i
            JOIN document_images d ON d.document_image_id=NEW.document_image_id
            JOIN project_files f ON f.file_id=d.file_id
            WHERE i.project_image_id=NEW.project_image_id
              AND f.project_id=i.project_id AND d.source_document_id=NEW.source_document_id
        ) THEN RAISE(ABORT, 'PROJECT_IMAGE_SOURCE_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER project_image_analysis_scope_match BEFORE INSERT ON project_image_analysis_versions
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM project_images i
            JOIN brief_versions v ON v.brief_version_id=NEW.brief_version_id
            JOIN project_brief_snapshots s ON s.brief_snapshot_id=NEW.brief_snapshot_id
            JOIN brief_confirmations c ON c.brief_version_id=v.brief_version_id AND c.brief_snapshot_id=s.brief_snapshot_id
            WHERE i.project_image_id=NEW.project_image_id
              AND i.workspace_id=NEW.workspace_id AND i.project_id=NEW.project_id
              AND i.image_sha256=NEW.image_sha256
              AND v.workspace_id=NEW.workspace_id AND v.project_id=NEW.project_id
              AND s.project_id=NEW.project_id AND c.workspace_id=NEW.workspace_id AND c.project_id=NEW.project_id
              AND c.confirmation_status='confirmed' AND c.unresolved_conflict_count=0
        ) THEN RAISE(ABORT, 'PROJECT_IMAGE_ANALYSIS_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER project_image_analysis_current_scope_match BEFORE INSERT ON project_image_analysis_current_links
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM project_image_analysis_versions v
            WHERE v.project_image_analysis_version_id=NEW.project_image_analysis_version_id
              AND v.project_image_id=NEW.project_image_id
        ) THEN RAISE(ABORT, 'PROJECT_IMAGE_CURRENT_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER project_image_analysis_current_scope_update BEFORE UPDATE OF project_image_analysis_version_id ON project_image_analysis_current_links
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM project_image_analysis_versions v
            WHERE v.project_image_analysis_version_id=NEW.project_image_analysis_version_id
              AND v.project_image_id=NEW.project_image_id
        ) THEN RAISE(ABORT, 'PROJECT_IMAGE_CURRENT_SCOPE_MISMATCH') END; END
        """,
        "CREATE TRIGGER project_images_no_update BEFORE UPDATE ON project_images BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER project_images_no_delete BEFORE DELETE ON project_images BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER project_image_source_links_no_update BEFORE UPDATE ON project_image_source_links BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER project_image_source_links_no_delete BEFORE DELETE ON project_image_source_links BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER project_image_analysis_versions_no_update BEFORE UPDATE ON project_image_analysis_versions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER project_image_analysis_versions_no_delete BEFORE DELETE ON project_image_analysis_versions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )
