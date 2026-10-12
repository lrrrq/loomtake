"""Migration 17: immutable Formal completion, image decisions and feedback evidence."""

from __future__ import annotations


FORMAL_CAPABILITY_MIGRATION_NAME = "formal-capability-regression-v3"


def authoritative_migration_fragment() -> tuple[str, ...]:
    statements = (
        """
        CREATE TABLE version_completion_states(
            completion_state_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL UNIQUE REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            generation_mode TEXT NOT NULL CHECK(generation_mode IN ('image_backed','text_only_draft')),
            image_state TEXT NOT NULL CHECK(image_state IN ('images_pending','temporary_text_basis','images_confirmed')),
            review_eligible INTEGER NOT NULL CHECK(review_eligible IN (0,1)),
            delivery_eligible INTEGER NOT NULL CHECK(delivery_eligible IN (0,1)),
            evidence_ref TEXT NOT NULL CHECK(evidence_ref LIKE 'artifact://%'),
            evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            CHECK((image_state='images_confirmed' AND review_eligible=1 AND delivery_eligible=1)
               OR (image_state!='images_confirmed' AND review_eligible=0 AND delivery_eligible=0))
        )
        """,
        """
        CREATE TABLE version_image_decision_snapshots_v3(
            snapshot_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            base_version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            materialized_version_id TEXT NOT NULL UNIQUE REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL UNIQUE REFERENCES project_runs(run_id) ON DELETE RESTRICT,
            shot_count INTEGER NOT NULL CHECK(shot_count > 0),
            image_confirmed_count INTEGER NOT NULL CHECK(image_confirmed_count >= 0),
            temporary_text_basis_count INTEGER NOT NULL CHECK(temporary_text_basis_count >= 0),
            snapshot_ref TEXT NOT NULL UNIQUE CHECK(snapshot_ref LIKE 'artifact://%'),
            snapshot_sha256 TEXT NOT NULL UNIQUE CHECK(length(snapshot_sha256)=64),
            request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
            candidate_snapshot_sha256 TEXT NOT NULL CHECK(length(candidate_snapshot_sha256)=64),
            idempotency_key TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            UNIQUE(created_by_actor_id, project_id, idempotency_key),
            CHECK(image_confirmed_count + temporary_text_basis_count = shot_count)
        )
        """,
        """
        CREATE TABLE version_image_decisions_v3(
            decision_id TEXT PRIMARY KEY,
            snapshot_id TEXT NOT NULL REFERENCES version_image_decision_snapshots_v3(snapshot_id) ON DELETE RESTRICT,
            shot_no TEXT NOT NULL,
            decision_kind TEXT NOT NULL CHECK(decision_kind IN ('image_confirmed','temporary_text_basis')),
            project_image_id TEXT REFERENCES project_images(project_image_id) ON DELETE RESTRICT,
            image_sha256 TEXT CHECK(image_sha256 IS NULL OR length(image_sha256)=64),
            analysis_version_id TEXT REFERENCES project_image_analysis_versions(project_image_analysis_version_id) ON DELETE RESTRICT,
            source_kind TEXT,
            source_key TEXT,
            source_location_json TEXT,
            temporary_reason TEXT,
            decided_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            decided_at TEXT NOT NULL,
            UNIQUE(snapshot_id, shot_no),
            CHECK((decision_kind='image_confirmed' AND project_image_id IS NOT NULL AND image_sha256 IS NOT NULL
                   AND analysis_version_id IS NOT NULL AND temporary_reason IS NULL)
               OR (decision_kind='temporary_text_basis' AND project_image_id IS NULL AND image_sha256 IS NULL
                   AND analysis_version_id IS NULL AND length(trim(temporary_reason)) > 0))
        )
        """,
        """
        CREATE TABLE formal_version_feedback_records(
            feedback_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            base_version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            child_version_id TEXT REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            feedback_kind TEXT NOT NULL CHECK(feedback_kind IN ('copy','structure','image','fact')),
            shot_no TEXT,
            payload_ref TEXT NOT NULL CHECK(payload_ref LIKE 'artifact://%'),
            payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE formal_visual_identity_mappings(
            identity_mapping_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            legacy_binding_key TEXT NOT NULL,
            resolution_kind TEXT NOT NULL CHECK(resolution_kind IN ('media_id','content_sha256','manifest_exact','ambiguous')),
            media_id TEXT,
            content_sha256 TEXT CHECK(content_sha256 IS NULL OR length(content_sha256)=64),
            evidence_ref TEXT NOT NULL CHECK(evidence_ref LIKE 'artifact://%'),
            evidence_sha256 TEXT NOT NULL CHECK(length(evidence_sha256)=64),
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            UNIQUE(version_id, legacy_binding_key)
        )
        """,
    )
    indexes = (
        "CREATE INDEX version_completion_states_project_idx ON version_completion_states(project_id, created_at)",
        "CREATE INDEX version_image_snapshots_v3_project_idx ON version_image_decision_snapshots_v3(project_id, created_at)",
        "CREATE INDEX version_image_decisions_v3_snapshot_idx ON version_image_decisions_v3(snapshot_id, shot_no)",
        "CREATE INDEX formal_feedback_project_idx ON formal_version_feedback_records(project_id, base_version_id, created_at)",
        "CREATE INDEX formal_identity_project_idx ON formal_visual_identity_mappings(project_id, version_id)",
    )
    immutable = tuple(
        f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END"
        for table in (
            "version_completion_states", "version_image_decision_snapshots_v3",
            "version_image_decisions_v3", "formal_version_feedback_records",
            "formal_visual_identity_mappings",
        )
    ) + tuple(
        f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END"
        for table in (
            "version_completion_states", "version_image_decision_snapshots_v3",
            "version_image_decisions_v3", "formal_version_feedback_records",
            "formal_visual_identity_mappings",
        )
    )
    return statements + indexes + immutable
