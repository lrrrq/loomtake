from __future__ import annotations

from .generated_image_migration import decision_origin_extension

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .formal_brief_migration import FORMAL_BRIEF_MIGRATION_NAME, authoritative_migration_fragment
from .version_quality_migration import VERSION_QUALITY_MIGRATION_NAME, authoritative_migration_fragment as version_quality_migration_fragment
from .version_review_migration import VERSION_REVIEW_MIGRATION_NAME, authoritative_migration_fragment as version_review_migration_fragment
from .version_review_submission_migration import VERSION_REVIEW_SUBMISSION_MIGRATION_NAME, authoritative_migration_fragment as version_review_submission_migration_fragment
from .version_image_decision_migration import VERSION_IMAGE_DECISION_MIGRATION_NAME, authoritative_migration_fragment as version_image_decision_migration_fragment
from .creative_standard_migration import CREATIVE_STANDARD_MIGRATION_NAME, authoritative_migration_fragment as creative_standard_migration_fragment
from .project_image_migration import PROJECT_IMAGE_MIGRATION_NAME, authoritative_migration_fragment as project_image_migration_fragment
from .mutation_policy_migration import MUTATION_POLICY_MIGRATION_NAME, authoritative_migration_fragment as mutation_policy_migration_fragment
from .formal_recovery_migration import FORMAL_RECOVERY_MIGRATION_NAME, authoritative_migration_fragment as formal_recovery_migration_fragment
from .formal_capability_migration import FORMAL_CAPABILITY_MIGRATION_NAME, authoritative_migration_fragment as formal_capability_migration_fragment
from .formal_operations_migration import FORMAL_OPERATIONS_MIGRATION_NAME, authoritative_migration_fragment as formal_operations_migration_fragment
from .delivery_snapshot_migration import DELIVERY_SNAPSHOT_MIGRATION_NAME, authoritative_migration_fragment as delivery_snapshot_migration_fragment
from .runtime_config import load_runtime_config


class WorkspaceServiceError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


ROLE_PERMISSIONS = {
    "viewer": {
        "agent_session.create",
        "asset.read",
        "project.read",
        "task.create",
        "task.read",
        "task.update_own",
    },
    "editor": {
        "asset.import",
        "knowledge.propose",
        "project.create",
        "project.diagnose",
        "project.update",
        "review.prepare",
        "review.readiness",
        "run.bind",
        "run.cancel",
        "run.generate",
        "run.retry",
        "run.revise",
        "version.create",
    },
    "reviewer": {
        "asset.publish",
        "asset.review",
        "candidate.select",
        "publication.create",
        "review.record",
        "version.publish",
        "version.select_candidate",
    },
    "administrator": {
        "identity.manage",
        "knowledge.approve",
        "project.archive",
        "project.restore",
        "system.read",
        "system.recover",
        "system.verify",
    },
}

ROLE_INHERITANCE = {
    "viewer": ("viewer",),
    "editor": ("viewer", "editor"),
    "reviewer": ("viewer", "editor", "reviewer"),
    "administrator": ("viewer", "editor", "reviewer", "administrator"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def require_nonempty(value: str, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise WorkspaceServiceError("INVALID_INPUT", f"{field} 不能为空")
    return normalized


def require_sha256(value: str, field: str) -> str:
    normalized = require_nonempty(value, field).lower()
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        raise WorkspaceServiceError("INVALID_INPUT", f"{field} 必须是 SHA-256")
    return normalized


def require_artifact_ref(value: str, field: str) -> str:
    normalized = require_nonempty(value, field)
    if not normalized.startswith("artifact://"):
        raise WorkspaceServiceError("INVALID_INPUT", f"{field} 必须是 artifact:// 逻辑引用")
    return normalized


def check_version(actual: int, expected: int, code: str) -> None:
    if type(expected) is not int or expected < 0 or actual != expected:
        raise WorkspaceServiceError(
            code,
            f"版本冲突：expected={expected!r}, actual={actual}",
        )


def enqueue_outbox(
    connection: sqlite3.Connection,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    payload: dict,
) -> str:
    event_id = new_id("event")
    connection.execute(
        """
        INSERT INTO outbox(
            event_id, aggregate_type, aggregate_id, event_type,
            payload_json, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            event_id,
            require_nonempty(aggregate_type, "aggregate_type"),
            require_nonempty(aggregate_id, "aggregate_id"),
            require_nonempty(event_type, "event_type"),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            utc_now(),
        ),
    )
    return event_id


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS workspaces(
        workspace_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        lock_version INTEGER NOT NULL DEFAULT 0 CHECK(lock_version >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS actors(
        actor_id TEXT PRIMARY KEY,
        display_name TEXT NOT NULL,
        auth_source TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active', 'disabled')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS roles(
        role_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS permissions(
        permission_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS role_permissions(
        role_id TEXT NOT NULL REFERENCES roles(role_id) ON DELETE RESTRICT,
        permission_id TEXT NOT NULL REFERENCES permissions(permission_id) ON DELETE RESTRICT,
        PRIMARY KEY(role_id, permission_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS auth_sessions(
        auth_session_id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        token_hash TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        expires_at TEXT,
        revoked_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects(
        project_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
        name TEXT NOT NULL,
        category_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'archived')),
        lock_version INTEGER NOT NULL DEFAULT 0 CHECK(lock_version >= 0),
        initial_brief_snapshot_id TEXT REFERENCES project_brief_snapshots(brief_snapshot_id) ON DELETE RESTRICT,
        current_brief_snapshot_id TEXT REFERENCES project_brief_snapshots(brief_snapshot_id) ON DELETE RESTRICT,
        created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS projects_workspace_idx ON projects(workspace_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS role_assignments(
        assignment_id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
        project_id TEXT REFERENCES projects(project_id) ON DELETE RESTRICT,
        role_id TEXT NOT NULL REFERENCES roles(role_id) ON DELETE RESTRICT,
        created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS role_assignment_scope_unique
    ON role_assignments(actor_id, workspace_id, COALESCE(project_id, ''), role_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_sessions(
        agent_session_id TEXT PRIMARY KEY,
        auth_session_id TEXT NOT NULL REFERENCES auth_sessions(auth_session_id) ON DELETE RESTRICT,
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        status TEXT NOT NULL CHECK(status IN ('active', 'closed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS project_runs(
        run_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        parent_run_id TEXT REFERENCES project_runs(run_id) ON DELETE RESTRICT,
        source_kind TEXT NOT NULL CHECK(source_kind IN ('native', 'legacy')),
        manifest_ref TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        bound_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        bound_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS project_runs_project_idx ON project_runs(project_id, bound_at)",
    """
    CREATE TABLE IF NOT EXISTS project_brief_snapshots(
        brief_snapshot_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        brief_ref TEXT NOT NULL,
        brief_sha256 TEXT NOT NULL,
        created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS project_brief_snapshots_project_idx
    ON project_brief_snapshots(project_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS project_versions(
        version_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        run_id TEXT NOT NULL UNIQUE REFERENCES project_runs(run_id) ON DELETE RESTRICT,
        parent_version_id TEXT REFERENCES project_versions(version_id) ON DELETE RESTRICT,
        content_sha256 TEXT NOT NULL,
        artifact_ref TEXT NOT NULL,
        created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS project_versions_project_idx ON project_versions(project_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS version_reviews(
        review_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
        reviewer_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        decision TEXT NOT NULL CHECK(decision IN ('quality_pass', 'quality_fail')),
        evidence_ref TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS version_reviews_version_idx ON version_reviews(version_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS candidate_selections(
        candidate_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
        selected_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS candidate_project_idx ON candidate_selections(project_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS version_publications(
        publication_id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
        review_id TEXT NOT NULL REFERENCES version_reviews(review_id) ON DELETE RESTRICT,
        published_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        status TEXT NOT NULL CHECK(status IN ('published', 'retired')),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS publications_project_idx ON version_publications(project_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS tasks(
        task_id TEXT PRIMARY KEY,
        agent_session_id TEXT NOT NULL REFERENCES agent_sessions(agent_session_id) ON DELETE RESTRICT,
        auth_session_id TEXT NOT NULL REFERENCES auth_sessions(auth_session_id) ON DELETE RESTRICT,
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        task_type TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'received', 'understanding', 'needs_clarification', 'proposed',
            'waiting_confirmation', 'executing', 'waiting_human_review',
            'waiting_user', 'completed', 'partially_completed', 'blocked',
            'failed', 'cancelled'
        )),
        result_ref TEXT,
        error_code TEXT,
        lock_version INTEGER NOT NULL DEFAULT 0 CHECK(lock_version >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS tasks_project_idx ON tasks(project_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS task_events(
        task_event_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE RESTRICT,
        sequence_no INTEGER NOT NULL CHECK(sequence_no > 0),
        status TEXT NOT NULL,
        details_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(task_id, sequence_no)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS outbox(
        sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id TEXT NOT NULL UNIQUE,
        aggregate_type TEXT NOT NULL,
        aggregate_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'published', 'failed')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        created_at TEXT NOT NULL,
        published_at TEXT,
        last_error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS outbox_pending_idx ON outbox(status, sequence_id)",
    """
    CREATE TRIGGER IF NOT EXISTS project_runs_no_update
    BEFORE UPDATE ON project_runs BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS project_runs_no_delete
    BEFORE DELETE ON project_runs BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS project_versions_no_update
    BEFORE UPDATE ON project_versions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS project_versions_no_delete
    BEFORE DELETE ON project_versions BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS project_brief_snapshots_no_update
    BEFORE UPDATE ON project_brief_snapshots BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS project_brief_snapshots_no_delete
    BEFORE DELETE ON project_brief_snapshots BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS version_reviews_no_update
    BEFORE UPDATE ON version_reviews BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS version_reviews_no_delete
    BEFORE DELETE ON version_reviews BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS candidate_selections_no_update
    BEFORE UPDATE ON candidate_selections BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS candidate_selections_no_delete
    BEFORE DELETE ON candidate_selections BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS version_publications_no_update
    BEFORE UPDATE ON version_publications BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS version_publications_no_delete
    BEFORE DELETE ON version_publications BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS task_events_no_update
    BEFORE UPDATE ON task_events BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS task_events_no_delete
    BEFORE DELETE ON task_events BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
)

_CONFIRMATION_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS action_confirmations(
        confirmation_id TEXT PRIMARY KEY,
        agent_session_id TEXT NOT NULL REFERENCES agent_sessions(agent_session_id) ON DELETE RESTRICT,
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        tool_name TEXT NOT NULL,
        parameters_sha256 TEXT NOT NULL,
        nonce_hash TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending', 'consumed', 'revoked')),
        created_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS action_confirmations_pending_idx
    ON action_confirmations(status, expires_at)
    """,
)

_IDEMPOTENCY_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS tool_idempotency_records(
        idempotency_record_id TEXT PRIMARY KEY,
        agent_session_id TEXT NOT NULL REFERENCES agent_sessions(agent_session_id) ON DELETE RESTRICT,
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        scope_type TEXT NOT NULL CHECK(scope_type IN ('workspace', 'project', 'package', 'knowledge')),
        scope_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('in_progress', 'accepted', 'completed', 'blocked', 'failed')),
        response_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(actor_id, scope_type, scope_id, tool_name, idempotency_key)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS tool_idempotency_status_idx
    ON tool_idempotency_records(status, updated_at)
    """,
)

_KNOWLEDGE_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_states(
        workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
        lock_version INTEGER NOT NULL DEFAULT 0 CHECK(lock_version >= 0),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_candidates(
        candidate_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
        scope TEXT NOT NULL CHECK(scope IN ('company', 'category', 'project', 'personal')),
        target_id TEXT NOT NULL,
        statement TEXT NOT NULL,
        statement_sha256 TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        proposed_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        knowledge_version INTEGER NOT NULL CHECK(knowledge_version > 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_candidates_workspace_idx
    ON knowledge_candidates(workspace_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_approvals(
        approval_id TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL UNIQUE REFERENCES knowledge_candidates(candidate_id) ON DELETE RESTRICT,
        approved_scope TEXT NOT NULL CHECK(approved_scope IN ('company', 'category', 'project', 'personal')),
        review_evidence TEXT NOT NULL,
        approved_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS knowledge_rules(
        rule_id TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
        candidate_id TEXT NOT NULL UNIQUE REFERENCES knowledge_candidates(candidate_id) ON DELETE RESTRICT,
        scope TEXT NOT NULL CHECK(scope IN ('company', 'category', 'project', 'personal')),
        target_id TEXT NOT NULL,
        statement TEXT NOT NULL,
        statement_sha256 TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        approved_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        active_version INTEGER NOT NULL CHECK(active_version > 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS knowledge_rules_lookup_idx
    ON knowledge_rules(workspace_id, scope, target_id, active_version)
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_candidates_no_update
    BEFORE UPDATE ON knowledge_candidates BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_candidates_no_delete
    BEFORE DELETE ON knowledge_candidates BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_approvals_no_update
    BEFORE UPDATE ON knowledge_approvals BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_approvals_no_delete
    BEFORE DELETE ON knowledge_approvals BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_rules_no_update
    BEFORE UPDATE ON knowledge_rules BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS knowledge_rules_no_delete
    BEFORE DELETE ON knowledge_rules BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END
    """,
)

_MANUAL_COMMAND_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS manual_command_confirmations(
        command_id TEXT PRIMARY KEY,
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        tool_name TEXT NOT NULL,
        parameters_sha256 TEXT NOT NULL,
        nonce_hash TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','consumed')),
        created_at TEXT NOT NULL,
        consumed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS manual_command_audits(
        command_audit_id TEXT PRIMARY KEY,
        command_id TEXT NOT NULL REFERENCES manual_command_confirmations(command_id) ON DELETE RESTRICT,
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        tool_name TEXT NOT NULL,
        status TEXT NOT NULL,
        parameters_sha256 TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
)

_MANUAL_COMMAND_IDEMPOTENCY_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS manual_command_idempotency(
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        tool_name TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL,
        command_id TEXT NOT NULL REFERENCES manual_command_confirmations(command_id) ON DELETE RESTRICT,
        status TEXT NOT NULL CHECK(status IN ('prepared','consumed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(actor_id, project_id, tool_name, idempotency_key)
    )
    """,
)

_PROJECT_CURRENT_VERSION_SCHEMA_STATEMENTS = (
    "ALTER TABLE projects ADD COLUMN current_version_id TEXT REFERENCES project_versions(version_id) ON DELETE RESTRICT",
    "CREATE INDEX IF NOT EXISTS projects_current_version_idx ON projects(current_version_id)",
)

_FORMAL_SCRIPT_ASSISTANT_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE formal_script_assistant_locks(
        workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        shot_id TEXT NOT NULL,
        created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(workspace_id, project_id, shot_id)
    )
    """,
    "CREATE INDEX formal_script_assistant_locks_project_idx ON formal_script_assistant_locks(project_id, shot_id)",
    """
    CREATE TABLE formal_script_assistant_lock_requests(
        actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
        project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
        idempotency_key TEXT NOT NULL,
        request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),
        response_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY(actor_id, project_id, idempotency_key)
    )
    """,
)

_MIGRATIONS = (
    (1, "workspace-foundation", _SCHEMA_STATEMENTS),
    (2, "action-confirmations", _CONFIRMATION_SCHEMA_STATEMENTS),
    (3, "tool-idempotency", _IDEMPOTENCY_SCHEMA_STATEMENTS),
    (4, "knowledge-governance", _KNOWLEDGE_SCHEMA_STATEMENTS),
    (5, "manual-r3-commands", _MANUAL_COMMAND_SCHEMA_STATEMENTS),
    (6, "manual-r3-command-idempotency", _MANUAL_COMMAND_IDEMPOTENCY_SCHEMA_STATEMENTS),
    (7, "project-current-version", _PROJECT_CURRENT_VERSION_SCHEMA_STATEMENTS),
    (8, FORMAL_BRIEF_MIGRATION_NAME, authoritative_migration_fragment()),
    (9, VERSION_QUALITY_MIGRATION_NAME, version_quality_migration_fragment()),
    (10, VERSION_REVIEW_MIGRATION_NAME, version_review_migration_fragment()),
    (11, CREATIVE_STANDARD_MIGRATION_NAME, creative_standard_migration_fragment()),
    (12, PROJECT_IMAGE_MIGRATION_NAME, project_image_migration_fragment()),
    (13, VERSION_REVIEW_SUBMISSION_MIGRATION_NAME, version_review_submission_migration_fragment()),
    (14, VERSION_IMAGE_DECISION_MIGRATION_NAME, version_image_decision_migration_fragment()),
    (15, MUTATION_POLICY_MIGRATION_NAME, mutation_policy_migration_fragment()),
    (16, FORMAL_RECOVERY_MIGRATION_NAME, formal_recovery_migration_fragment()),
    (17, FORMAL_CAPABILITY_MIGRATION_NAME, formal_capability_migration_fragment()),
    (18, FORMAL_OPERATIONS_MIGRATION_NAME, formal_operations_migration_fragment()),
    (19, DELIVERY_SNAPSHOT_MIGRATION_NAME, delivery_snapshot_migration_fragment()),
    (20, "formal-script-assistant-locks", _FORMAL_SCRIPT_ASSISTANT_SCHEMA_STATEMENTS),
    (21, "project-template-binding", (
        "ALTER TABLE projects ADD COLUMN template_package_ref TEXT",
        "ALTER TABLE projects ADD COLUMN template_package_sha256 TEXT",
    )),
    (22, "project-category-at-brief", ()),
    (23, "formal-image-progress", (
        """CREATE TABLE formal_image_progress(
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            base_version_id TEXT NOT NULL REFERENCES project_versions(version_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK(revision >= 0),
            selections_json TEXT NOT NULL,
            saved_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            saved_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, project_id, base_version_id)
        )""",
        """CREATE TABLE formal_image_progress_requests(
            actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            idempotency_key TEXT NOT NULL,
            request_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(actor_id, project_id, idempotency_key)
        )""",
    )),
    (24, "creative-draft-head", (
        """CREATE TABLE creative_draft_heads(
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT PRIMARY KEY REFERENCES projects(project_id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK(revision > 0),
            payload_ref TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    )),
    (25, 'generated-image-sources', (
        """CREATE TABLE project_image_generated_sources(
            task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE RESTRICT,
            project_image_id TEXT NOT NULL REFERENCES project_images(project_image_id) ON DELETE RESTRICT,
            origin TEXT NOT NULL CHECK(origin IN ('generated', 'edited')),
            result_ref TEXT NOT NULL CHECK(result_ref LIKE 'artifact://creative-image/%'),
            result_sha256 TEXT NOT NULL CHECK(length(result_sha256)=64),
            parent_images_json TEXT NOT NULL,
            created_by_actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX generated_image_sources_image_idx ON project_image_generated_sources(project_image_id)",
        """CREATE TRIGGER generated_image_sources_scope BEFORE INSERT ON project_image_generated_sources
        BEGIN SELECT CASE WHEN NOT EXISTS(
            SELECT 1 FROM tasks t JOIN project_images i ON i.project_image_id=NEW.project_image_id
            WHERE t.task_id=NEW.task_id AND t.project_id=i.project_id
              AND t.actor_id=NEW.created_by_actor_id AND t.task_type='formal_image_creation'
              AND t.status='completed' AND t.result_ref=NEW.result_ref
              AND NEW.result_ref='artifact://creative-image/' || NEW.result_sha256
        ) THEN RAISE(ABORT, 'GENERATED_IMAGE_SOURCE_SCOPE_MISMATCH') END; END""",
        "CREATE TRIGGER generated_image_sources_no_update BEFORE UPDATE ON project_image_generated_sources BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER generated_image_sources_no_delete BEFORE DELETE ON project_image_generated_sources BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    ) + decision_origin_extension()),
    (26, "manual-confirmation-results", (
        """CREATE TABLE manual_command_results(
            command_id TEXT PRIMARY KEY REFERENCES manual_command_confirmations(command_id) ON DELETE RESTRICT,
            response_json TEXT NOT NULL
        )""",
        "CREATE TRIGGER manual_command_results_no_update BEFORE UPDATE ON manual_command_results BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER manual_command_results_no_delete BEFORE DELETE ON manual_command_results BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )),
    (27, "package-image-multiple-source-rows", (
        "DROP TRIGGER project_image_package_source_scope_match",
        "DROP TRIGGER project_image_package_source_links_no_update",
        "DROP TRIGGER project_image_package_source_links_no_delete",
        "DROP INDEX project_image_package_links_image_idx",
        "ALTER TABLE project_image_package_source_links RENAME TO project_image_package_source_links_v26",
        formal_recovery_migration_fragment()[0].replace(
            "UNIQUE(project_image_id, package_id, package_version, media_id)",
            "UNIQUE(project_image_id, package_id, package_version, media_id, source_file, source_sheet, source_row)"),
        "INSERT INTO project_image_package_source_links SELECT * FROM project_image_package_source_links_v26",
        "DROP TABLE project_image_package_source_links_v26",
    ) + formal_recovery_migration_fragment()[1:5]),
    (28, "project-director-conversation", (
        "ALTER TABLE agent_sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE agent_sessions ADD COLUMN archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0,1))",
        "ALTER TABLE agent_sessions ADD COLUMN draft_content TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE agent_sessions ADD COLUMN selected_source_ids_json TEXT",
        "ALTER TABLE agent_sessions ADD COLUMN metadata_revision INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE agent_sessions ADD COLUMN summary_json TEXT NOT NULL DEFAULT '{}'",
        "CREATE INDEX agent_sessions_owner_recent ON agent_sessions(project_id, actor_id, archived, updated_at)",
        """CREATE TABLE knowledge_project_convention_events(
            event_id TEXT PRIMARY KEY,
            convention_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id) ON DELETE RESTRICT,
            project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
            rule_id TEXT NOT NULL REFERENCES knowledge_rules(rule_id) ON DELETE RESTRICT,
            event_type TEXT NOT NULL CHECK(event_type IN ('created','corrected','disabled','deleted')),
            actor_id TEXT NOT NULL REFERENCES actors(actor_id) ON DELETE RESTRICT,
            knowledge_version INTEGER NOT NULL CHECK(knowledge_version > 0),
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX knowledge_project_convention_scope ON knowledge_project_convention_events(workspace_id,project_id,knowledge_version)",
        "CREATE INDEX knowledge_project_convention_version ON knowledge_project_convention_events(convention_id,knowledge_version)",
        "CREATE INDEX knowledge_project_convention_rule ON knowledge_project_convention_events(rule_id,event_type)",
        "CREATE TRIGGER knowledge_project_convention_no_update BEFORE UPDATE ON knowledge_project_convention_events BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
        "CREATE TRIGGER knowledge_project_convention_no_delete BEFORE DELETE ON knowledge_project_convention_events BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_RECORD'); END",
    )),
)


_COMPOSED_TRANSACTIONS = ContextVar("mplus_composed_transactions", default={})


class WorkspaceStore:
    """SQLite repository boundary for project and task authority records."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser()

    @classmethod
    def from_runtime_config(cls, config: dict | None = None) -> "WorkspaceStore":
        runtime = config or load_runtime_config()
        return cls(Path(runtime["data_root"]) / "workspace.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def connection(self):
        active = _COMPOSED_TRANSACTIONS.get().get(str(self.database_path.resolve()))
        if active is not None:
            yield active
            return
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        active = _COMPOSED_TRANSACTIONS.get().get(str(self.database_path.resolve()))
        if active is not None:
            savepoint = "composed_" + uuid.uuid4().hex
            active.execute("SAVEPOINT " + savepoint)
            try:
                yield active
            except BaseException:
                active.execute("ROLLBACK TO " + savepoint)
                active.execute("RELEASE " + savepoint)
                raise
            else:
                active.execute("RELEASE " + savepoint)
            return
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def atomic(self):
        """Compose existing authoritative writes without publishing a partial result.

        Opt-in only: unrelated requests retain their own connections. Immutable
        artifact blobs may be prepared, but no head or receipt survives rollback.
        Model calls must complete before entering this boundary.
        """
        key = str(self.database_path.resolve())
        if key in _COMPOSED_TRANSACTIONS.get():
            with self.transaction() as connection:
                yield connection
            return
        self.initialize()
        connection = self._connect()
        token = None
        try:
            connection.execute("BEGIN IMMEDIATE")
            token = _COMPOSED_TRANSACTIONS.set({**_COMPOSED_TRANSACTIONS.get(), key: connection})
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            if token is not None:
                _COMPOSED_TRANSACTIONS.reset(token)
            connection.close()

    def initialize(self) -> None:
        if str(self.database_path.resolve()) in _COMPOSED_TRANSACTIONS.get():
            return
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # SQLite's table rebuild must happen without cascading into child
            # tables. Check all foreign keys before committing the migration.
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations(
                        version INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        applied_at TEXT NOT NULL
                    )
                    """
                )
                for version, name, statements in _MIGRATIONS:
                    applied = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?",
                        (version,),
                    ).fetchone()
                    if applied is not None:
                        continue
                    for statement in statements:
                        connection.execute(statement)
                    if version == 22:
                        self._allow_unclassified_projects(connection)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, utc_now()),
                    )
                self._seed_permissions(connection)
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise WorkspaceServiceError("INTEGRITY_ERROR", "数据库迁移后关联校验失败")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _allow_unclassified_projects(connection: sqlite3.Connection) -> None:
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='projects'"
        ).fetchone()[0]
        indexes = [row[0] for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='projects' AND type IN ('index','trigger') AND sql IS NOT NULL"
        )]
        columns = ", ".join('"' + row[1].replace('"', '""') + '"'
                            for row in connection.execute("PRAGMA table_info(projects)"))
        schema = schema.replace("CREATE TABLE projects", "CREATE TABLE projects__category_optional", 1)
        schema = schema.replace("category_id TEXT NOT NULL", "category_id TEXT", 1)
        connection.execute(schema)
        connection.execute(f"INSERT INTO projects__category_optional ({columns}) SELECT {columns} FROM projects")
        connection.execute("DROP TABLE projects")
        connection.execute("PRAGMA legacy_alter_table = ON")
        try:
            connection.execute("ALTER TABLE projects__category_optional RENAME TO projects")
        finally:
            connection.execute("PRAGMA legacy_alter_table = OFF")
        for statement in indexes:
            connection.execute(statement)

    @staticmethod
    def _seed_permissions(connection: sqlite3.Connection) -> None:
        timestamp = utc_now()
        all_permissions = sorted(
            permission
            for permissions in ROLE_PERMISSIONS.values()
            for permission in permissions
        )
        for role_id in ROLE_PERMISSIONS:
            connection.execute(
                "INSERT OR IGNORE INTO roles(role_id, created_at) VALUES (?, ?)",
                (role_id, timestamp),
            )
        for permission_id in all_permissions:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(permission_id, created_at) VALUES (?, ?)",
                (permission_id, timestamp),
            )
        for role_id, inherited_roles in ROLE_INHERITANCE.items():
            permissions = set()
            for inherited_role in inherited_roles:
                permissions.update(ROLE_PERMISSIONS[inherited_role])
            for permission_id in permissions:
                connection.execute(
                    "INSERT OR IGNORE INTO role_permissions(role_id, permission_id) VALUES (?, ?)",
                    (role_id, permission_id),
                )
