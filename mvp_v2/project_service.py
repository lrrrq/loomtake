from __future__ import annotations

from .identity_service import require_collection_read, require_permission
from .mutation_policy import require_active_project
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    check_version,
    enqueue_outbox,
    new_id,
    require_artifact_ref,
    require_nonempty,
    require_sha256,
    row_dict,
    utc_now,
)
from .version_review_repository import require_no_unsaved_script_draft


def _project(connection, project_id: str):
    row = connection.execute(
        "SELECT * FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
    return row


def _increment_project(
    connection,
    *,
    project_id: str,
    expected_project_version: int,
    timestamp: str,
) -> int:
    updated = connection.execute(
        """
        UPDATE projects SET lock_version = lock_version + 1, updated_at = ?
        WHERE project_id = ? AND lock_version = ?
        """,
        (timestamp, project_id, expected_project_version),
    )
    if updated.rowcount != 1:
        raise WorkspaceServiceError(
            "PROJECT_VERSION_CONFLICT", "project 在写入期间已变化"
        )
    return expected_project_version + 1


class ProjectService:
    def __init__(self, store: WorkspaceStore):
        self.store = store
        self.store.initialize()

    def get_workspace(self, auth_token: str, workspace_id: str) -> dict:
        with self.store.connection() as connection:
            require_collection_read(
                connection,
                auth_token,
                workspace_id=workspace_id,
                permission="project.read",
            )
            row = connection.execute(
                "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("NOT_FOUND", "workspace 不存在")
            result = row_dict(row)
        result["workspace_version"] = result.pop("lock_version")
        return result

    def create_project(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        name: str,
        category_id: str | None = None,
        expected_workspace_version: int,
        brief_ref: str | None = None,
        brief_sha256: str | None = None,
        _project_id: str | None = None,
    ) -> dict:
        name = require_nonempty(name, "name")
        category_id = require_nonempty(category_id, "category_id") if category_id is not None else None
        if (brief_ref is None) != (brief_sha256 is None):
            raise WorkspaceServiceError(
                "INVALID_INPUT", "brief_ref 与 brief_sha256 必须同时提供"
            )
        if brief_ref is not None:
            brief_ref = require_artifact_ref(brief_ref, "brief_ref")
            brief_sha256 = require_sha256(brief_sha256, "brief_sha256")
        project_id = require_nonempty(_project_id, "project_id") if _project_id else new_id("project")
        brief_snapshot_id = new_id("brief_snapshot") if brief_ref is not None else None
        timestamp = utc_now()
        with self.store.transaction() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                permission="project.create",
            )
            workspace = connection.execute(
                "SELECT lock_version FROM workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise WorkspaceServiceError("NOT_FOUND", "workspace 不存在")
            existing = connection.execute(
                "SELECT * FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if existing is not None:
                if (
                    not _project_id
                    or existing["workspace_id"] != workspace_id
                    or existing["name"] != name
                    or existing["category_id"] != category_id
                ):
                    raise WorkspaceServiceError(
                        "IDEMPOTENCY_CONFLICT", "项目身份已绑定不同创建请求",
                    )
                return {
                    "project_id": project_id,
                    "workspace_id": workspace_id,
                    "name": existing["name"],
                    "category_id": existing["category_id"],
                    "status": existing["status"],
                    "initial_brief_snapshot_id": existing["initial_brief_snapshot_id"],
                    "current_brief_snapshot_id": existing["current_brief_snapshot_id"],
                    "project_version": int(existing["lock_version"]),
                    "workspace_version": int(workspace["lock_version"]),
                }
            check_version(
                workspace["lock_version"],
                expected_workspace_version,
                "WORKSPACE_VERSION_CONFLICT",
            )
            connection.execute(
                """
                INSERT INTO projects(
                    project_id, workspace_id, name, category_id, status,
                    lock_version, initial_brief_snapshot_id, current_brief_snapshot_id,
                    created_by_actor_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', 0, NULL, NULL, ?, ?, ?)
                """,
                (
                    project_id,
                    workspace_id,
                    name,
                    category_id,
                    actor["actor_id"],
                    timestamp,
                    timestamp,
                ),
            )
            if brief_snapshot_id is not None:
                connection.execute(
                    """
                    INSERT INTO project_brief_snapshots(
                        brief_snapshot_id, project_id, brief_ref, brief_sha256,
                        created_by_actor_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        brief_snapshot_id,
                        project_id,
                        brief_ref,
                        brief_sha256,
                        actor["actor_id"],
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE projects
                    SET initial_brief_snapshot_id = ?, current_brief_snapshot_id = ?
                    WHERE project_id = ?
                    """,
                    (brief_snapshot_id, brief_snapshot_id, project_id),
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
                event_type="workspace.project_created",
                payload={
                    "category_id": category_id,
                    "brief_snapshot_id": brief_snapshot_id,
                    "project_id": project_id,
                },
            )
        return {
            "project_id": project_id,
            "workspace_id": workspace_id,
            "name": name,
            "category_id": category_id,
            "status": "active",
            "initial_brief_snapshot_id": brief_snapshot_id,
            "current_brief_snapshot_id": brief_snapshot_id,
            "project_version": 0,
            "workspace_version": expected_workspace_version + 1,
        }

    def rename_project(
        self,
        auth_token: str,
        *,
        project_id: str,
        name: str,
        expected_project_version: int,
    ) -> dict:
        name = require_nonempty(name, "name")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            project = _project(connection, project_id)
            require_active_project(project)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.update",
            )
            check_version(
                project["lock_version"],
                expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            connection.execute(
                """
                UPDATE projects SET name = ?, lock_version = lock_version + 1, updated_at = ?
                WHERE project_id = ? AND lock_version = ?
                """,
                (name, timestamp, project_id, expected_project_version),
            )
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project_id,
                event_type="project.renamed",
                payload={"actor_id": actor["actor_id"], "name": name},
            )
        return {
            "project_id": project_id,
            "workspace_id": project["workspace_id"],
            "name": name,
            "category_id": project["category_id"],
            "status": project["status"],
            "project_version": expected_project_version + 1,
        }

    def select_template(
        self, auth_token: str, *, project_id: str, package_ref: str,
        package_sha256: str, expected_project_version: int,
    ) -> dict:
        package_ref = require_nonempty(package_ref, "package_ref")
        package_sha256 = require_sha256(package_sha256, "package_sha256")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            project = _project(connection, project_id)
            require_active_project(project)
            actor = require_permission(connection, auth_token,
                workspace_id=project["workspace_id"], project_id=project_id,
                permission="project.update")
            check_version(project["lock_version"], expected_project_version, "PROJECT_VERSION_CONFLICT")
            # This selects the template for a future plan. Existing versions and
            # the current edit draft retain their frozen template; planning and
            # generation still enforce their own unsaved-draft protection.
            connection.execute(
                "UPDATE projects SET template_package_ref=?, template_package_sha256=? WHERE project_id=?",
                (package_ref, package_sha256, project_id),
            )
            _increment_project(connection, project_id=project_id,
                expected_project_version=expected_project_version, timestamp=timestamp)
            enqueue_outbox(connection, aggregate_type="project", aggregate_id=project_id,
                event_type="project.template_selected", payload={
                    "actor_id": actor["actor_id"], "package_ref": package_ref,
                    "package_sha256": package_sha256,
                })
        return self.get_project(auth_token, project_id)

    def get_project(self, auth_token: str, project_id: str) -> dict:
        with self.store.connection() as connection:
            project = _project(connection, project_id)
            require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.read",
            )
            result = row_dict(project)
        result["project_version"] = result.pop("lock_version")
        return result

    def list_projects(self, auth_token: str, workspace_id: str) -> list[dict]:
        with self.store.connection() as connection:
            _, scope = require_collection_read(
                connection,
                auth_token,
                workspace_id=workspace_id,
                permission="project.read",
            )
            scoped = "" if scope is None else f" AND project_id IN ({','.join('?' for _ in scope)})"
            rows = connection.execute(
                f"""
                SELECT * FROM projects WHERE workspace_id = ?{scoped}
                ORDER BY created_at, project_id
                """,
                (workspace_id, *(scope or ())),
            ).fetchall()
        result = []
        for row in rows:
            item = row_dict(row)
            item["project_version"] = item.pop("lock_version")
            result.append(item)
        return result

    @staticmethod
    def progress_projection(project: dict, *, stage: str = "状态待核对", next_action: dict | None = None) -> dict:
        """Shared, conservative display projection; never persists project progress."""
        from urllib.parse import quote
        project_id = quote(project["project_id"], safe="")
        if project.get("status") == "archived":
            stage = "已归档 · 内容只读"
            next_action = {"label": "查看／恢复", "href": f"/projects/{project_id}/overview", "reason": "恢复后才能继续修改。"}
        return {"stage": stage, "next_action": next_action or {
            "label": "打开项目", "href": f"/projects/{project_id}/overview", "reason": "进入项目后核对当前内容。"}}

    def list_progress_summaries(self, auth_token: str, workspace_id: str) -> dict[str, dict]:
        """Batch only registered metadata; does not load scripts, workbooks or providers."""
        with self.store.connection() as connection:
            actor, scope = require_collection_read(connection, auth_token, workspace_id=workspace_id, permission="project.read")
            scoped = "" if scope is None else f" AND p.project_id IN ({','.join('?' for _ in scope)})"
            rows = connection.execute(
                f"""SELECT p.*, EXISTS(SELECT 1 FROM project_files f WHERE f.project_id=p.project_id) AS has_files,
                EXISTS(SELECT 1 FROM brief_confirmations c WHERE c.project_id=p.project_id
                  AND c.brief_snapshot_id=p.current_brief_snapshot_id) AS brief_confirmed,
                (SELECT COUNT(*) FROM project_versions v WHERE v.project_id=p.project_id) AS version_count
                FROM projects p WHERE workspace_id=?{scoped}""", (workspace_id, *(scope or ())),
            ).fetchall()
            task_rows = connection.execute(
                """SELECT t.* FROM tasks t JOIN projects p ON p.project_id=t.project_id
                WHERE p.workspace_id=? AND t.actor_id=? AND t.task_id=(
                  SELECT t2.task_id FROM tasks t2 WHERE t2.project_id=t.project_id AND t2.actor_id=t.actor_id
                  ORDER BY t2.created_at DESC, t2.task_id DESC LIMIT 1)""", (workspace_id, actor["actor_id"]),
            ).fetchall()
        from .task_service import TaskService
        tasks = {row["project_id"]: dict(row) for row in task_rows}
        result = {}
        for row in rows:
            project = dict(row)
            project_id = project["project_id"]
            task = tasks.get(project_id)
            action = None
            if project.get("current_version_id"):
                stage = f"已有 {project['version_count']} 个版本 · 后续状态待核对"
            elif project["brief_confirmed"]:
                stage = "需求已确认 · 待创作"
                action = {"label": "进入创作", "href": f"/projects/{project_id}/plan", "reason": "核对事件与镜头安排。"}
            elif project["has_files"] or project.get("current_brief_snapshot_id"):
                stage = "已有需求资料 · 待核对"
                action = {"label": "核对需求", "href": f"/projects/{project_id}/brief", "reason": "核对资料和确认状态。"}
            else:
                stage = "尚未导入需求"
                action = {"label": "导入需求", "href": f"/projects/{project_id}/brief", "reason": "导入项目资料后开始制作。"}
            if task and task["status"] not in {"completed", "cancelled"}:
                fields = TaskService.employee_task_fields(task, project_id)
                stage = fields.get("state_label") or stage
                action = {"label": fields.get("action_label"), "href": fields.get("action_href"), "reason": fields.get("next_action")}
            result[project_id] = {**self.progress_projection(project, stage=stage, next_action=action),
                "updated_at": max(project["updated_at"], (task or {}).get("updated_at", "")),
                "evidence_scope": "已登记的需求、版本和当前账号最近任务；配图与审核以项目页回读为准"}
        return result

    def bind_run(
        self,
        auth_token: str,
        *,
        project_id: str,
        run_id: str,
        source_kind: str,
        manifest_ref: str,
        manifest_sha256: str,
        expected_project_version: int,
        parent_run_id: str | None = None,
    ) -> dict:
        run_id = require_nonempty(run_id, "run_id")
        if source_kind not in {"native", "legacy"}:
            raise WorkspaceServiceError("INVALID_INPUT", "source_kind 必须是 native 或 legacy")
        manifest_ref = require_artifact_ref(manifest_ref, "manifest_ref")
        manifest_sha256 = require_sha256(manifest_sha256, "manifest_sha256")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            project = _project(connection, project_id)
            require_active_project(project)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="run.bind",
            )
            check_version(
                project["lock_version"],
                expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            existing = connection.execute(
                "SELECT project_id FROM project_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing is not None:
                raise WorkspaceServiceError("RUN_ALREADY_BOUND", "run 已绑定到 project")
            if parent_run_id is not None:
                parent = connection.execute(
                    "SELECT project_id FROM project_runs WHERE run_id = ?",
                    (parent_run_id,),
                ).fetchone()
                if parent is None or parent["project_id"] != project_id:
                    raise WorkspaceServiceError(
                        "INVALID_PARENT_RUN", "parent run 不属于同一 project"
                    )
            connection.execute(
                """
                INSERT INTO project_runs(
                    run_id, project_id, parent_run_id, source_kind,
                    manifest_ref, manifest_sha256, bound_by_actor_id, bound_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    parent_run_id,
                    source_kind,
                    manifest_ref,
                    manifest_sha256,
                    actor["actor_id"],
                    timestamp,
                ),
            )
            project_version = _increment_project(
                connection,
                project_id=project_id,
                expected_project_version=expected_project_version,
                timestamp=timestamp,
            )
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project_id,
                event_type="project.run_bound",
                payload={
                    "manifest_ref": manifest_ref,
                    "manifest_sha256": manifest_sha256,
                    "run_id": run_id,
                    "source_kind": source_kind,
                },
            )
        return {
            "run_id": run_id,
            "project_id": project_id,
            "parent_run_id": parent_run_id,
            "source_kind": source_kind,
            "manifest_ref": manifest_ref,
            "manifest_sha256": manifest_sha256,
            "project_version": project_version,
        }

    def get_run_binding(self, auth_token: str, run_id: str) -> dict:
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT r.*, p.workspace_id
                FROM project_runs r JOIN projects p ON p.project_id = r.project_id
                WHERE r.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("NOT_FOUND", "run binding 不存在")
            require_permission(
                connection,
                auth_token,
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                permission="project.read",
            )
            result = row_dict(row)
        result.pop("workspace_id", None)
        return result

    def list_project_runs(self, auth_token: str, project_id: str) -> list[dict]:
        with self.store.connection() as connection:
            project = _project(connection, project_id)
            require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.read",
            )
            rows = connection.execute(
                """
                SELECT * FROM project_runs WHERE project_id = ?
                ORDER BY bound_at, run_id
                """,
                (project["project_id"],),
            ).fetchall()
        return [row_dict(row) for row in rows]

    def add_brief_snapshot(
        self,
        auth_token: str,
        *,
        project_id: str,
        brief_ref: str,
        brief_sha256: str,
        expected_project_version: int,
    ) -> dict:
        brief_ref = require_artifact_ref(brief_ref, "brief_ref")
        brief_sha256 = require_sha256(brief_sha256, "brief_sha256")
        brief_snapshot_id = new_id("brief_snapshot")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            project = _project(connection, project_id)
            require_active_project(project)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.update",
            )
            check_version(
                project["lock_version"],
                expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            connection.execute(
                """
                INSERT INTO project_brief_snapshots(
                    brief_snapshot_id, project_id, brief_ref, brief_sha256,
                    created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    brief_snapshot_id,
                    project_id,
                    brief_ref,
                    brief_sha256,
                    actor["actor_id"],
                    timestamp,
                ),
            )
            connection.execute(
                """
                UPDATE projects
                SET initial_brief_snapshot_id = COALESCE(initial_brief_snapshot_id, ?),
                    current_brief_snapshot_id = ?
                WHERE project_id = ?
                """,
                (brief_snapshot_id, brief_snapshot_id, project_id),
            )
            project_version = _increment_project(
                connection,
                project_id=project_id,
                expected_project_version=expected_project_version,
                timestamp=timestamp,
            )
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project_id,
                event_type="project.brief_snapshot_added",
                payload={
                    "brief_ref": brief_ref,
                    "brief_sha256": brief_sha256,
                    "brief_snapshot_id": brief_snapshot_id,
                },
            )
        return {
            "brief_snapshot_id": brief_snapshot_id,
            "project_id": project_id,
            "brief_ref": brief_ref,
            "brief_sha256": brief_sha256,
            "project_version": project_version,
        }

    def get_current_brief_snapshot(self, auth_token: str, project_id: str) -> dict | None:
        with self.store.connection() as connection:
            project = _project(connection, project_id)
            require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.read",
            )
            snapshot_id = project["current_brief_snapshot_id"]
            if snapshot_id is None:
                return None
            row = connection.execute(
                """
                SELECT * FROM project_brief_snapshots
                WHERE brief_snapshot_id = ? AND project_id = ?
                """,
                (snapshot_id, project_id),
            ).fetchone()
        if row is None:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "current Brief snapshot 缺失")
        return row_dict(row)

    def create_project_version(
        self,
        auth_token: str,
        *,
        project_id: str,
        run_id: str,
        content_sha256: str,
        artifact_ref: str,
        expected_project_version: int,
        parent_version_id: str | None = None,
    ) -> dict:
        content_sha256 = require_sha256(content_sha256, "content_sha256")
        artifact_ref = require_artifact_ref(artifact_ref, "artifact_ref")
        version_id = new_id("version")
        timestamp = utc_now()
        with self.store.transaction() as connection:
            project = _project(connection, project_id)
            require_active_project(project)
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="version.create",
            )
            check_version(
                project["lock_version"],
                expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            run = connection.execute(
                "SELECT project_id FROM project_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None or run["project_id"] != project_id:
                raise WorkspaceServiceError("INVALID_RUN_BINDING", "run 不属于同一 project")
            if connection.execute(
                "SELECT 1 FROM project_versions WHERE run_id = ?", (run_id,)
            ).fetchone() is not None:
                raise WorkspaceServiceError("VERSION_ALREADY_EXISTS", "run 已形成不可变 version")
            if parent_version_id is not None:
                parent = connection.execute(
                    "SELECT project_id FROM project_versions WHERE version_id = ?",
                    (parent_version_id,),
                ).fetchone()
                if parent is None or parent["project_id"] != project_id:
                    raise WorkspaceServiceError(
                        "INVALID_PARENT_VERSION", "parent version 不属于同一 project"
                    )
                require_no_unsaved_script_draft(
                    connection,
                    workspace_id=str(project["workspace_id"]),
                    project_id=project_id,
                    base_version_id=parent_version_id,
                )
            connection.execute(
                """
                INSERT INTO project_versions(
                    version_id, project_id, run_id, parent_version_id,
                    content_sha256, artifact_ref, created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    version_id,
                    project_id,
                    run_id,
                    parent_version_id,
                    content_sha256,
                    artifact_ref,
                    actor["actor_id"],
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE projects SET current_version_id = ? WHERE project_id = ?",
                (version_id, project_id),
            )
            project_version = _increment_project(
                connection,
                project_id=project_id,
                expected_project_version=expected_project_version,
                timestamp=timestamp,
            )
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project_id,
                event_type="project.version_created",
                payload={
                    "artifact_ref": artifact_ref,
                    "content_sha256": content_sha256,
                    "run_id": run_id,
                    "version_id": version_id,
                },
            )
        return {
            "version_id": version_id,
            "project_id": project_id,
            "run_id": run_id,
            "parent_version_id": parent_version_id,
            "content_sha256": content_sha256,
            "artifact_ref": artifact_ref,
            "current_version_id": version_id,
            "project_version": project_version,
        }

    def get_project_version(self, auth_token: str, version_id: str) -> dict:
        with self.store.connection() as connection:
            row = connection.execute(
                """
                SELECT v.*, p.workspace_id
                FROM project_versions v JOIN projects p ON p.project_id = v.project_id
                WHERE v.version_id = ?
                """,
                (version_id,),
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("NOT_FOUND", "version 不存在")
            require_permission(
                connection,
                auth_token,
                workspace_id=row["workspace_id"],
                project_id=row["project_id"],
                permission="project.read",
            )
            result = row_dict(row)
        result.pop("workspace_id", None)
        return result

    def list_project_versions(self, auth_token: str, project_id: str) -> list[dict]:
        with self.store.connection() as connection:
            project = _project(connection, project_id)
            require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.read",
            )
            rows = connection.execute(
                """
                SELECT * FROM project_versions WHERE project_id = ?
                ORDER BY created_at, version_id
                """,
                (project_id,),
            ).fetchall()
        return [row_dict(row) for row in rows]
