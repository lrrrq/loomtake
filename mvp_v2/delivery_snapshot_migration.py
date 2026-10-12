"""Migration 19: immutable Formal delivery snapshots and artifact bindings."""

from __future__ import annotations


DELIVERY_SNAPSHOT_MIGRATION_NAME = "formal-delivery-snapshots-v1"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE delivery_snapshots(
            delivery_snapshot_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL UNIQUE REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL UNIQUE REFERENCES project_runs(run_id) ON DELETE RESTRICT,
            brief_snapshot_id TEXT NOT NULL REFERENCES project_brief_snapshots(brief_snapshot_id) ON DELETE RESTRICT,
            plan_task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            image_snapshot_id TEXT NOT NULL REFERENCES version_image_decision_snapshots_v3(snapshot_id) ON DELETE RESTRICT,
            quality_snapshot_id TEXT NOT NULL REFERENCES version_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
            final_confirmation_id TEXT NOT NULL UNIQUE REFERENCES version_quality_final_confirmations(final_confirmation_id) ON DELETE RESTRICT,
            project_lock_version INTEGER NOT NULL CHECK(project_lock_version >= 0),
            shot_count INTEGER NOT NULL CHECK(shot_count > 0),
            script_plan_ref TEXT NOT NULL CHECK(script_plan_ref LIKE 'artifact://%'),
            script_plan_sha256 TEXT NOT NULL CHECK(length(script_plan_sha256)=64),
            script_draft_ref TEXT NOT NULL CHECK(script_draft_ref LIKE 'artifact://%'),
            script_draft_sha256 TEXT NOT NULL CHECK(length(script_draft_sha256)=64),
            visual_binding_ref TEXT NOT NULL CHECK(visual_binding_ref LIKE 'artifact://%'),
            visual_binding_sha256 TEXT NOT NULL CHECK(length(visual_binding_sha256)=64),
            image_decision_ref TEXT NOT NULL CHECK(image_decision_ref LIKE 'artifact://%'),
            image_decision_sha256 TEXT NOT NULL CHECK(length(image_decision_sha256)=64),
            workbook_ref TEXT NOT NULL CHECK(workbook_ref LIKE 'artifact://%'),
            workbook_sha256 TEXT NOT NULL CHECK(length(workbook_sha256)=64),
            source_trace_sha256 TEXT NOT NULL CHECK(length(source_trace_sha256)=64),
            snapshot_digest TEXT NOT NULL UNIQUE CHECK(length(snapshot_digest)=64),
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX delivery_snapshots_project_idx ON delivery_snapshots(project_id, created_at)",
        """
        CREATE TABLE delivery_artifact_bindings(
            delivery_artifact_binding_id TEXT PRIMARY KEY,
            delivery_snapshot_id TEXT NOT NULL REFERENCES delivery_snapshots(delivery_snapshot_id) ON DELETE RESTRICT,
            artifact_kind TEXT NOT NULL CHECK(artifact_kind IN ('excel','pdf')),
            availability TEXT NOT NULL CHECK(availability IN ('available','optional_unavailable')),
            artifact_ref TEXT UNIQUE,
            artifact_sha256 TEXT UNIQUE CHECK(artifact_sha256 IS NULL OR length(artifact_sha256)=64),
            size_bytes INTEGER CHECK(size_bytes IS NULL OR size_bytes >= 0),
            unavailable_reason TEXT,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            UNIQUE(delivery_snapshot_id, artifact_kind),
            CHECK((availability='available' AND artifact_ref LIKE 'artifact://%' AND artifact_sha256 IS NOT NULL
                   AND size_bytes IS NOT NULL AND unavailable_reason IS NULL)
               OR (availability='optional_unavailable' AND artifact_ref IS NULL AND artifact_sha256 IS NULL
                   AND size_bytes IS NULL AND length(trim(unavailable_reason)) > 0))
        )
        """,
        """
        CREATE TRIGGER delivery_snapshot_scope_match BEFORE INSERT ON delivery_snapshots
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM projects p
            JOIN project_versions v ON v.project_id=p.project_id
            JOIN project_runs r ON r.run_id=v.run_id AND r.project_id=p.project_id
            JOIN project_brief_snapshots b ON b.project_id=p.project_id
            JOIN tasks t ON t.project_id=p.project_id
            JOIN version_image_decision_snapshots_v3 i
              ON i.materialized_version_id=v.version_id AND i.run_id=v.run_id AND i.project_id=p.project_id
            JOIN version_quality_snapshots q
              ON q.version_id=v.version_id AND q.run_id=v.run_id AND q.project_id=p.project_id
            JOIN version_quality_final_confirmations f
              ON f.version_id=v.version_id AND f.run_id=v.run_id AND f.snapshot_id=q.snapshot_id
            WHERE p.workspace_id=NEW.workspace_id AND p.project_id=NEW.project_id
              AND v.version_id=NEW.version_id AND r.run_id=NEW.run_id
              AND b.brief_snapshot_id=NEW.brief_snapshot_id
              AND t.task_id=NEW.plan_task_id AND t.task_type='formal_generation_plan'
              AND i.snapshot_id=NEW.image_snapshot_id
              AND q.snapshot_id=NEW.quality_snapshot_id
              AND f.final_confirmation_id=NEW.final_confirmation_id
              AND f.decision='quality_pass'
        ) THEN RAISE(ABORT, 'DELIVERY_SNAPSHOT_SCOPE_MISMATCH') END; END
        """,
        "CREATE TRIGGER delivery_snapshots_no_update BEFORE UPDATE ON delivery_snapshots BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER delivery_snapshots_no_delete BEFORE DELETE ON delivery_snapshots BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER delivery_artifact_bindings_no_update BEFORE UPDATE ON delivery_artifact_bindings BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER delivery_artifact_bindings_no_delete BEFORE DELETE ON delivery_artifact_bindings BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )
