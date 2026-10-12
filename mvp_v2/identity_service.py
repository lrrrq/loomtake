from __future__ import annotations

import secrets
import sqlite3

from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    check_version,
    enqueue_outbox,
    new_id,
    require_nonempty,
    row_dict,
    sha256_text,
    utc_now,
)


def _authenticated_actor(connection, token: str) -> dict:
    normalized_token = str(token or "").strip()
    if not normalized_token:
        raise WorkspaceServiceError("AUTH_SESSION_INVALID", "认证会话无效或操作者已禁用")
    token_hash = sha256_text(normalized_token)
    row = connection.execute(
        """
        SELECT s.auth_session_id, s.actor_id, s.expires_at,
               a.display_name, a.auth_source, a.status AS actor_status
        FROM auth_sessions s
        JOIN actors a ON a.actor_id = s.actor_id
        WHERE s.token_hash = ? AND s.revoked_at IS NULL
        """,
        (token_hash,),
    ).fetchone()
    if row is None or row["actor_status"] != "active":
        raise WorkspaceServiceError("AUTH_SESSION_INVALID", "认证会话无效或操作者已禁用")
    if row["expires_at"] and row["expires_at"] <= utc_now():
        raise WorkspaceServiceError("AUTH_SESSION_INVALID", "认证会话已过期")
    return row_dict(row)


def require_permission(
    connection,
    token: str,
    *,
    workspace_id: str,
    permission: str,
    project_id: str | None = None,
) -> dict:
    actor = _authenticated_actor(connection, token)
    parameters = [actor["actor_id"], workspace_id, permission]
    scope_sql = "ra.project_id IS NULL"
    if project_id is not None:
        scope_sql = "(ra.project_id IS NULL OR ra.project_id = ?)"
        parameters.append(project_id)
    allowed = connection.execute(
        f"""
        SELECT 1
        FROM role_assignments ra
        JOIN role_permissions rp ON rp.role_id = ra.role_id
        WHERE ra.actor_id = ? AND ra.workspace_id = ?
          AND rp.permission_id = ? AND {scope_sql}
        LIMIT 1
        """,
        parameters,
    ).fetchone()
    if allowed is None:
        raise WorkspaceServiceError(
            "PERMISSION_DENIED",
            f"操作者无权限执行 {permission}",
        )
    return actor


def require_collection_read(connection, token: str, *, workspace_id: str, permission: str):
    """Return the actor and allowed project IDs for a workspace collection.

    None means an existing workspace-wide grant. Scoped members still pass
    the canonical permission check for each assigned project; this does not
    confer workspace permissions or authorize mutations.
    """
    if permission not in {"project.read", "task.read"}:
        raise ValueError("Collection scope is only defined for project/task reads")
    try:
        return require_permission(connection, token, workspace_id=workspace_id,
            permission=permission), None
    except WorkspaceServiceError as denied:
        if denied.code != "PERMISSION_DENIED":
            raise
        actor = _authenticated_actor(connection, token)
        candidates = connection.execute(
            """SELECT DISTINCT ra.project_id FROM role_assignments ra
               JOIN projects p ON p.project_id=ra.project_id AND p.workspace_id=ra.workspace_id
               WHERE ra.actor_id=? AND ra.workspace_id=? ORDER BY ra.project_id""",
            (actor["actor_id"], workspace_id),
        ).fetchall()
        allowed = []
        for row in candidates:
            try:
                require_permission(connection, token, workspace_id=workspace_id,
                    project_id=row["project_id"], permission=permission)
            except WorkspaceServiceError as exc:
                if exc.code != "PERMISSION_DENIED":
                    raise
            else:
                allowed.append(row["project_id"])
        if not allowed:
            raise denied
        return actor, tuple(allowed)


class LocalIdentityAdapter:
    """Local identity boundary. Raw session tokens are returned once and never stored."""

    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.store.initialize()

    def validate_session(self, auth_token: str) -> dict:
        """Validate an existing local session without granting a permission."""
        with self.store.connection() as connection:
            actor = _authenticated_actor(connection, auth_token)
        return {
            "auth_session_id": actor["auth_session_id"],
            "actor_id": actor["actor_id"],
            "auth_source": actor["auth_source"],
        }

    def bootstrap_owner(
        self,
        *,
        workspace_name: str,
        display_name: str,
        confirmed: bool,
        token: str | None = None,
    ) -> dict:
        if confirmed is not True:
            raise WorkspaceServiceError(
                "BOOTSTRAP_CONFIRMATION_REQUIRED",
                "首次本机 owner 创建必须由用户明确确认",
            )
        workspace_name = require_nonempty(workspace_name, "workspace_name")
        display_name = require_nonempty(display_name, "display_name")
        auth_token = token or secrets.token_urlsafe(32)
        timestamp = utc_now()
        workspace_id = new_id("workspace")
        actor_id = new_id("actor")
        auth_session_id = new_id("auth")
        with self.store.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM workspaces LIMIT 1"
            ).fetchone()
            if exists is not None:
                raise WorkspaceServiceError(
                    "BOOTSTRAP_ALREADY_COMPLETED",
                    "本地身份初始化已经完成",
                )
            connection.execute(
                """
                INSERT INTO workspaces(
                    workspace_id, name, lock_version, created_at, updated_at
                ) VALUES (?, ?, 0, ?, ?)
                """,
                (workspace_id, workspace_name, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO actors(
                    actor_id, display_name, auth_source, status, created_at, updated_at
                ) VALUES (?, ?, 'local', 'active', ?, ?)
                """,
                (actor_id, display_name, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO role_assignments(
                    assignment_id, actor_id, workspace_id, project_id, role_id,
                    created_by_actor_id, created_at
                ) VALUES (?, ?, ?, NULL, 'administrator', ?, ?)
                """,
                (new_id("role_assignment"), actor_id, workspace_id, actor_id, timestamp),
            )
            connection.execute(
                """
                INSERT INTO auth_sessions(
                    auth_session_id, actor_id, token_hash, created_at, expires_at, revoked_at
                ) VALUES (?, ?, ?, ?, NULL, NULL)
                """,
                (auth_session_id, actor_id, sha256_text(auth_token), timestamp),
            )
            enqueue_outbox(
                connection,
                aggregate_type="workspace",
                aggregate_id=workspace_id,
                event_type="workspace.bootstrapped",
                payload={"actor_id": actor_id, "workspace_id": workspace_id},
            )
        return {
            "workspace_id": workspace_id,
            "workspace_version": 0,
            "actor_id": actor_id,
            "auth_session_id": auth_session_id,
            "auth_token": auth_token,
        }

    def create_actor(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        display_name: str,
        auth_source: str,
        role: str,
        expected_workspace_version: int,
        project_id: str | None = None,
    ) -> dict:
        display_name = require_nonempty(display_name, "display_name")
        auth_source = require_nonempty(auth_source, "auth_source")
        role = require_nonempty(role, "role")
        actor_id = new_id("actor")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                permission="identity.manage",
            )
            workspace = connection.execute(
                "SELECT lock_version FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise WorkspaceServiceError("NOT_FOUND", "workspace 不存在")
            check_version(
                workspace["lock_version"],
                expected_workspace_version,
                "WORKSPACE_VERSION_CONFLICT",
            )
            if connection.execute(
                "SELECT 1 FROM roles WHERE role_id = ?", (role,)
            ).fetchone() is None:
                raise WorkspaceServiceError("INVALID_INPUT", f"未知角色: {role}")
            if project_id is not None:
                project = connection.execute(
                    "SELECT workspace_id FROM projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if project is None or project["workspace_id"] != workspace_id:
                    raise WorkspaceServiceError(
                        "INVALID_INPUT", "project role 必须属于目标 workspace"
                    )
            connection.execute(
                """
                INSERT INTO actors(
                    actor_id, display_name, auth_source, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                """,
                (actor_id, display_name, auth_source, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO role_assignments(
                    assignment_id, actor_id, workspace_id, project_id, role_id,
                    created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("role_assignment"),
                    actor_id,
                    workspace_id,
                    project_id,
                    role,
                    actor["actor_id"],
                    timestamp,
                ),
            )
            updated = connection.execute(
                """
                UPDATE workspaces SET lock_version = lock_version + 1, updated_at = ?
                WHERE workspace_id = ? AND lock_version = ?
                """,
                (timestamp, workspace_id, expected_workspace_version),
            )
            if updated.rowcount != 1:
                raise WorkspaceServiceError(
                    "WORKSPACE_VERSION_CONFLICT", "workspace 在写入期间已变化"
                )
            enqueue_outbox(
                connection,
                aggregate_type="workspace",
                aggregate_id=workspace_id,
                event_type="workspace.actor_created",
                payload={"actor_id": actor_id, "project_id": project_id, "role": role},
            )
        return {
            "actor_id": actor_id,
            "display_name": display_name,
            "auth_source": auth_source,
            "role": role,
            "project_id": project_id,
            "status": "active",
            "workspace_version": expected_workspace_version + 1,
        }

    def issue_auth_session(
        self,
        *,
        actor_id: str,
        token: str | None = None,
        expires_at: str | None = None,
    ) -> dict:
        auth_token = token or secrets.token_urlsafe(32)
        auth_session_id = new_id("auth")
        timestamp = utc_now()
        try:
            with self.store.transaction() as connection:
                actor = connection.execute(
                    "SELECT status FROM actors WHERE actor_id = ?", (actor_id,)
                ).fetchone()
                if actor is None or actor["status"] != "active":
                    raise WorkspaceServiceError("AUTH_SESSION_INVALID", "操作者不存在或已禁用")
                connection.execute(
                    """
                    INSERT INTO auth_sessions(
                        auth_session_id, actor_id, token_hash, created_at, expires_at, revoked_at
                    ) VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        auth_session_id,
                        actor_id,
                        sha256_text(auth_token),
                        timestamp,
                        expires_at,
                    ),
                )
                enqueue_outbox(
                    connection,
                    aggregate_type="actor",
                    aggregate_id=actor_id,
                    event_type="actor.auth_session_issued",
                    payload={"auth_session_id": auth_session_id},
                )
        except sqlite3.IntegrityError as exc:
            raise WorkspaceServiceError("AUTH_TOKEN_CONFLICT", "认证 token 已存在") from exc
        return {
            "auth_session_id": auth_session_id,
            "actor_id": actor_id,
            "auth_token": auth_token,
            "expires_at": expires_at,
        }

    def reissue_local_owner_session(self, *, confirmed: bool) -> dict:
        if confirmed is not True:
            raise WorkspaceServiceError(
                "OWNER_SESSION_CONFIRMATION_REQUIRED",
                "重新进入本机工作区必须由用户明确确认",
            )
        with self.store.connection() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT a.actor_id, ra.workspace_id
                FROM actors a
                JOIN role_assignments ra ON ra.actor_id = a.actor_id
                WHERE a.auth_source = 'local'
                  AND a.status = 'active'
                  AND ra.project_id IS NULL
                  AND ra.role_id = 'administrator'
                ORDER BY a.actor_id, ra.workspace_id
                """
            ).fetchall()
        if len(rows) != 1:
            raise WorkspaceServiceError(
                "LOCAL_OWNER_RECOVERY_AMBIGUOUS",
                "本机管理员身份不唯一，不能自动重新签发会话",
            )
        session = self.issue_auth_session(actor_id=str(rows[0]["actor_id"]))
        return {**session, "workspace_id": str(rows[0]["workspace_id"])}

    def disable_actor(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        actor_id: str,
        expected_workspace_version: int,
    ) -> dict:
        timestamp = utc_now()
        with self.store.transaction() as connection:
            operator = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                permission="identity.manage",
            )
            workspace = connection.execute(
                "SELECT lock_version FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise WorkspaceServiceError("NOT_FOUND", "workspace 不存在")
            check_version(
                workspace["lock_version"],
                expected_workspace_version,
                "WORKSPACE_VERSION_CONFLICT",
            )
            belongs = connection.execute(
                """
                SELECT 1 FROM role_assignments
                WHERE actor_id = ? AND workspace_id = ? LIMIT 1
                """,
                (actor_id, workspace_id),
            ).fetchone()
            if belongs is None:
                raise WorkspaceServiceError("NOT_FOUND", "actor 不属于 workspace")
            connection.execute(
                "UPDATE actors SET status = 'disabled', updated_at = ? WHERE actor_id = ?",
                (timestamp, actor_id),
            )
            connection.execute(
                "UPDATE auth_sessions SET revoked_at = ? WHERE actor_id = ? AND revoked_at IS NULL",
                (timestamp, actor_id),
            )
            connection.execute(
                """
                UPDATE workspaces SET lock_version = lock_version + 1, updated_at = ?
                WHERE workspace_id = ? AND lock_version = ?
                """,
                (timestamp, workspace_id, expected_workspace_version),
            )
            enqueue_outbox(
                connection,
                aggregate_type="workspace",
                aggregate_id=workspace_id,
                event_type="workspace.actor_disabled",
                payload={
                    "actor_id": actor_id,
                    "disabled_by_actor_id": operator["actor_id"],
                },
            )
        return {
            "actor_id": actor_id,
            "status": "disabled",
            "workspace_version": expected_workspace_version + 1,
        }

    def resolve_auth_session(self, auth_token: str) -> dict:
        with self.store.connection() as connection:
            actor = _authenticated_actor(connection, auth_token)
            roles = connection.execute(
                """
                SELECT DISTINCT role_id FROM role_assignments
                WHERE actor_id = ? ORDER BY role_id
                """,
                (actor["actor_id"],),
            ).fetchall()
        return {
            "auth_session_id": actor["auth_session_id"],
            "actor_id": actor["actor_id"],
            "display_name": actor["display_name"],
            "auth_source": actor["auth_source"],
            "roles": [row["role_id"] for row in roles],
        }
