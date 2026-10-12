"""Migration 18: immutable task attempts, checkpoints and controlled recovery."""

from __future__ import annotations


FORMAL_OPERATIONS_MIGRATION_NAME = "formal-operations-attempts-v1"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE task_attempts(
            attempt_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            attempt_no INTEGER NOT NULL CHECK(attempt_no > 0),
            origin TEXT NOT NULL CHECK(origin IN ('initial','retry','checkpoint')),
            parent_attempt_id TEXT REFERENCES task_attempts(attempt_id) ON DELETE RESTRICT,
            requested_stage_id TEXT,
            reason TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL,
            UNIQUE(task_id, attempt_no)
        )
        """,
        "CREATE INDEX task_attempts_project_idx ON task_attempts(project_id, created_at)",
        """
        CREATE TABLE stage_checkpoints(
            checkpoint_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL REFERENCES task_attempts(attempt_id) ON DELETE RESTRICT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            stage_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('completed','failed','blocked')),
            input_ref TEXT CHECK(input_ref IS NULL OR input_ref LIKE 'artifact://%'),
            input_sha256 TEXT CHECK(input_sha256 IS NULL OR length(input_sha256)=64),
            output_ref TEXT CHECK(output_ref IS NULL OR output_ref LIKE 'artifact://%'),
            output_sha256 TEXT CHECK(output_sha256 IS NULL OR length(output_sha256)=64),
            error_code TEXT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX stage_checkpoints_task_idx ON stage_checkpoints(task_id, created_at)",
        """
        CREATE TABLE provider_call_diagnostics(
            provider_call_id TEXT PRIMARY KEY,
            attempt_id TEXT NOT NULL REFERENCES task_attempts(attempt_id) ON DELETE RESTRICT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            stage_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('started','completed','failed')),
            duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms >= 0),
            error_code TEXT,
            input_summary TEXT NOT NULL,
            error_summary TEXT NOT NULL,
            output_ref TEXT CHECK(output_ref IS NULL OR output_ref LIKE 'artifact://%'),
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX provider_call_diagnostics_task_idx ON provider_call_diagnostics(task_id, created_at)",
        """
        CREATE TABLE dead_letters(
            dead_letter_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            attempt_id TEXT REFERENCES task_attempts(attempt_id) ON DELETE RESTRICT,
            reason_code TEXT NOT NULL,
            payload_ref TEXT NOT NULL CHECK(payload_ref LIKE 'artifact://%'),
            payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64),
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX dead_letters_task_idx ON dead_letters(task_id, created_at)",
        """
        CREATE TABLE recovery_proposals(
            recovery_proposal_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
            base_attempt_id TEXT NOT NULL REFERENCES task_attempts(attempt_id) ON DELETE RESTRICT,
            checkpoint_id TEXT REFERENCES stage_checkpoints(checkpoint_id) ON DELETE RESTRICT,
            action TEXT NOT NULL CHECK(action IN ('retry_current_stage','resume_checkpoint','safe_reset')),
            requested_stage_id TEXT,
            reason TEXT NOT NULL,
            expected_project_version INTEGER NOT NULL CHECK(expected_project_version >= 0),
            proposal_digest TEXT NOT NULL UNIQUE CHECK(length(proposal_digest)=64),
            idempotency_key TEXT NOT NULL,
            proposed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            proposed_at TEXT NOT NULL,
            UNIQUE(proposed_by_actor_id, project_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE recovery_executions(
            recovery_execution_id TEXT PRIMARY KEY,
            recovery_proposal_id TEXT NOT NULL UNIQUE REFERENCES recovery_proposals(recovery_proposal_id) ON DELETE RESTRICT,
            new_attempt_id TEXT NOT NULL UNIQUE REFERENCES task_attempts(attempt_id) ON DELETE RESTRICT,
            confirmed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            confirmed_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE operations_audit_events(
            operations_event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            event_kind TEXT NOT NULL CHECK(event_kind IN ('worker','backup','restore','deployment','rollback','alert')),
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            evidence_ref TEXT CHECK(evidence_ref IS NULL OR evidence_ref LIKE 'artifact://%'),
            evidence_sha256 TEXT CHECK(evidence_sha256 IS NULL OR length(evidence_sha256)=64),
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX operations_audit_workspace_idx ON operations_audit_events(workspace_id, created_at)",
        """
        CREATE TRIGGER task_attempt_scope_match BEFORE INSERT ON task_attempts
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM tasks t WHERE t.task_id=NEW.task_id AND t.project_id=NEW.project_id
        ) THEN RAISE(ABORT, 'TASK_ATTEMPT_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER recovery_proposal_scope_match BEFORE INSERT ON recovery_proposals
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM projects p
            JOIN tasks t ON t.project_id=p.project_id
            JOIN task_attempts a ON a.task_id=t.task_id
            WHERE p.workspace_id=NEW.workspace_id AND p.project_id=NEW.project_id
              AND t.task_id=NEW.task_id AND a.attempt_id=NEW.base_attempt_id
              AND (NEW.checkpoint_id IS NULL OR EXISTS(
                  SELECT 1 FROM stage_checkpoints c
                  WHERE c.checkpoint_id=NEW.checkpoint_id AND c.task_id=NEW.task_id
                    AND c.attempt_id=NEW.base_attempt_id
              ))
        ) THEN RAISE(ABORT, 'RECOVERY_PROPOSAL_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER stage_checkpoint_scope_match BEFORE INSERT ON stage_checkpoints
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM task_attempts a
            WHERE a.attempt_id=NEW.attempt_id AND a.task_id=NEW.task_id
        ) THEN RAISE(ABORT, 'STAGE_CHECKPOINT_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER provider_call_scope_match BEFORE INSERT ON provider_call_diagnostics
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM task_attempts a
            WHERE a.attempt_id=NEW.attempt_id AND a.task_id=NEW.task_id
        ) THEN RAISE(ABORT, 'PROVIDER_CALL_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER dead_letter_scope_match BEFORE INSERT ON dead_letters
        BEGIN SELECT CASE WHEN NEW.attempt_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM task_attempts a
            WHERE a.attempt_id=NEW.attempt_id AND a.task_id=NEW.task_id
        ) THEN RAISE(ABORT, 'DEAD_LETTER_SCOPE_MISMATCH') END; END
        """,
        """
        CREATE TRIGGER recovery_execution_scope_match BEFORE INSERT ON recovery_executions
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM recovery_proposals p
            JOIN task_attempts a ON a.attempt_id=NEW.new_attempt_id
            WHERE p.recovery_proposal_id=NEW.recovery_proposal_id
              AND a.task_id=p.task_id AND a.project_id=p.project_id
        ) THEN RAISE(ABORT, 'RECOVERY_EXECUTION_SCOPE_MISMATCH') END; END
        """,
        *tuple(
            f"CREATE TRIGGER {table}_no_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END"
            for table in (
                "task_attempts", "stage_checkpoints", "provider_call_diagnostics",
                "dead_letters", "recovery_proposals", "recovery_executions",
                "operations_audit_events",
            )
        ),
        *tuple(
            f"CREATE TRIGGER {table}_no_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END"
            for table in (
                "task_attempts", "stage_checkpoints", "provider_call_diagnostics",
                "dead_letters", "recovery_proposals", "recovery_executions",
                "operations_audit_events",
            )
        ),
    )
