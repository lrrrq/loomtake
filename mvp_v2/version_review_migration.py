"""T07 migration 10: persistent draft and human-review state."""

from __future__ import annotations


VERSION_REVIEW_MIGRATION_NAME = "version-review-workbench-state"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE version_review_subjects(
            version_id TEXT PRIMARY KEY REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            run_id TEXT NOT NULL REFERENCES project_runs(run_id) ON DELETE RESTRICT,
            shots_json TEXT NOT NULL,
            shots_sha256 TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_drafts(
            draft_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            base_version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            status TEXT NOT NULL CHECK(status IN ('open')),
            current_revision INTEGER NOT NULL CHECK(current_revision >= 0),
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_draft_states(
            state_id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES version_review_drafts(draft_id) ON DELETE RESTRICT,
            parent_state_id TEXT REFERENCES version_review_draft_states(state_id) ON DELETE RESTRICT,
            shots_json TEXT NOT NULL,
            snapshot_sha256 TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_draft_heads(
            draft_id TEXT PRIMARY KEY REFERENCES version_review_drafts(draft_id) ON DELETE RESTRICT,
            current_state_id TEXT NOT NULL REFERENCES version_review_draft_states(state_id) ON DELETE RESTRICT,
            undo_state_ids_json TEXT NOT NULL,
            redo_state_ids_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_cell_patches(
            patch_id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES version_review_drafts(draft_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK(revision > 0),
            actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            origin TEXT NOT NULL CHECK(origin IN ('table','natural_language')),
            shot_no TEXT NOT NULL,
            field_name TEXT NOT NULL,
            old_value_json TEXT NOT NULL,
            new_value_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_undo_checkpoints(
            checkpoint_id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES version_review_drafts(draft_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK(revision > 0),
            patch_count INTEGER NOT NULL CHECK(patch_count >= 0),
            snapshot_sha256 TEXT NOT NULL,
            actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_lock_sets(
            lock_set_id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES version_review_drafts(draft_id) ON DELETE RESTRICT,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_lock_entries(
            lock_set_id TEXT NOT NULL REFERENCES version_review_lock_sets(lock_set_id) ON DELETE RESTRICT,
            shot_no TEXT NOT NULL,
            field_name TEXT NOT NULL,
            PRIMARY KEY(lock_set_id, shot_no, field_name)
        )
        """,
        """
        CREATE TABLE version_review_lock_links(
            draft_id TEXT PRIMARY KEY REFERENCES version_review_drafts(draft_id) ON DELETE RESTRICT,
            lock_set_id TEXT NOT NULL UNIQUE REFERENCES version_review_lock_sets(lock_set_id) ON DELETE RESTRICT,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_items(
            review_item_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            kind TEXT NOT NULL,
            shot_no TEXT,
            severity TEXT NOT NULL,
            evidence_ref TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_evidence(
            evidence_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            evidence_ref TEXT NOT NULL UNIQUE,
            evidence_sha256 TEXT NOT NULL UNIQUE,
            summary TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE version_review_human_decisions(
            decision_id TEXT PRIMARY KEY,
            review_item_id TEXT NOT NULL REFERENCES version_review_items(review_item_id) ON DELETE RESTRICT,
            version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            decision TEXT NOT NULL CHECK(decision IN ('approve','return','recheck')),
            evidence_ref TEXT NOT NULL,
            evidence_sha256 TEXT NOT NULL,
            reviewer_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            decided_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX version_review_drafts_project_idx ON version_review_drafts(project_id, created_at)",
        "CREATE INDEX version_review_states_draft_idx ON version_review_draft_states(draft_id, created_at)",
        "CREATE INDEX version_review_patches_draft_idx ON version_review_cell_patches(draft_id, revision)",
        "CREATE INDEX version_review_items_version_idx ON version_review_items(version_id, created_at)",
        "CREATE INDEX version_review_evidence_version_idx ON version_review_evidence(version_id, created_at)",
        "CREATE INDEX version_review_decisions_version_idx ON version_review_human_decisions(version_id, decided_at)",
        """
        CREATE TRIGGER version_review_subject_scope_match BEFORE INSERT ON version_review_subjects
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM projects p JOIN project_versions v ON v.project_id=p.project_id
            JOIN project_runs r ON r.run_id=v.run_id AND r.project_id=p.project_id
            WHERE p.workspace_id=NEW.workspace_id AND p.project_id=NEW.project_id
              AND v.version_id=NEW.version_id AND r.run_id=NEW.run_id
        ) THEN RAISE(ABORT, 'REVIEW_SUBJECT_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_review_draft_scope_match BEFORE INSERT ON version_review_drafts
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM projects p JOIN project_versions v ON v.project_id=p.project_id
            WHERE p.workspace_id=NEW.workspace_id AND p.project_id=NEW.project_id
              AND v.version_id=NEW.base_version_id
        ) THEN RAISE(ABORT, 'REVIEW_DRAFT_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_review_state_scope_match BEFORE INSERT ON version_review_draft_states
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_review_drafts d WHERE d.draft_id=NEW.draft_id
        ) THEN RAISE(ABORT, 'REVIEW_STATE_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_review_head_scope_insert BEFORE INSERT ON version_review_draft_heads
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_review_draft_states s
            WHERE s.state_id=NEW.current_state_id AND s.draft_id=NEW.draft_id
        ) THEN RAISE(ABORT, 'REVIEW_HEAD_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_review_head_scope_update BEFORE UPDATE OF current_state_id ON version_review_draft_heads
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_review_draft_states s
            WHERE s.state_id=NEW.current_state_id AND s.draft_id=NEW.draft_id
        ) THEN RAISE(ABORT, 'REVIEW_HEAD_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_review_item_scope_match BEFORE INSERT ON version_review_items
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_review_subjects s
            WHERE s.version_id=NEW.version_id AND s.workspace_id=NEW.workspace_id
              AND s.project_id=NEW.project_id
        ) THEN RAISE(ABORT, 'REVIEW_ITEM_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_review_evidence_scope_match BEFORE INSERT ON version_review_evidence
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_review_subjects s
            WHERE s.version_id=NEW.version_id AND s.workspace_id=NEW.workspace_id
              AND s.project_id=NEW.project_id
        ) THEN RAISE(ABORT, 'REVIEW_EVIDENCE_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER version_review_decision_scope_match BEFORE INSERT ON version_review_human_decisions
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM version_review_items i
            WHERE i.review_item_id=NEW.review_item_id AND i.version_id=NEW.version_id
        ) THEN RAISE(ABORT, 'REVIEW_DECISION_SCOPE_MISMATCH') END; END
        """,
        "CREATE TRIGGER version_review_subjects_no_update BEFORE UPDATE ON version_review_subjects BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_subjects_no_delete BEFORE DELETE ON version_review_subjects BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_states_no_update BEFORE UPDATE ON version_review_draft_states BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_states_no_delete BEFORE DELETE ON version_review_draft_states BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_patches_no_update BEFORE UPDATE ON version_review_cell_patches BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_patches_no_delete BEFORE DELETE ON version_review_cell_patches BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_checkpoints_no_update BEFORE UPDATE ON version_review_undo_checkpoints BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_checkpoints_no_delete BEFORE DELETE ON version_review_undo_checkpoints BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_lock_sets_no_update BEFORE UPDATE ON version_review_lock_sets BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_lock_sets_no_delete BEFORE DELETE ON version_review_lock_sets BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_items_no_update BEFORE UPDATE ON version_review_items BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_items_no_delete BEFORE DELETE ON version_review_items BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_evidence_no_update BEFORE UPDATE ON version_review_evidence BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_evidence_no_delete BEFORE DELETE ON version_review_evidence BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_decisions_no_update BEFORE UPDATE ON version_review_human_decisions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER version_review_decisions_no_delete BEFORE DELETE ON version_review_human_decisions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )
