MUTATION_POLICY_MIGRATION_NAME = "central-mutation-policy"


def authoritative_migration_fragment() -> tuple[str, ...]:
    return (
        """
        CREATE TABLE mutation_events(
            mutation_event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            operation TEXT NOT NULL,
            mutation_kind TEXT NOT NULL CHECK(mutation_kind IN (
                'append_immutable','patch_field','advance_pointer',
                'rebuild_projection','cleanup_ephemeral','archive_project',
                'restore_project','purge_project'
            )),
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            request_sha256 TEXT NOT NULL CHECK(length(request_sha256) = 64),
            changed_fields_json TEXT NOT NULL,
            preserved_scope_json TEXT NOT NULL,
            recovery_mode TEXT NOT NULL,
            confirmation_id TEXT REFERENCES manual_command_confirmations(command_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX mutation_events_project_idx ON mutation_events(project_id, created_at)",
        """
        CREATE TRIGGER mutation_events_no_update
        BEFORE UPDATE ON mutation_events
        BEGIN SELECT RAISE(ABORT, 'mutation_events are immutable'); END
        """,
        """
        CREATE TRIGGER mutation_events_no_delete
        BEFORE DELETE ON mutation_events
        BEGIN SELECT RAISE(ABORT, 'mutation_events are immutable'); END
        """,
    )
