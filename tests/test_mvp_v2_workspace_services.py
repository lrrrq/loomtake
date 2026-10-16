import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mvp_v2.identity_service import LocalIdentityAdapter, require_permission
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import (
    ROLE_INHERITANCE,
    ROLE_PERMISSIONS,
    WorkspaceServiceError,
    WorkspaceStore,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class WorkspaceServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database_path = self.root / "runtime" / "workspace.sqlite3"
        self.store = WorkspaceStore(self.database_path)
        self.store.initialize()
        self.identity = LocalIdentityAdapter(self.store)
        bootstrap = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="本机管理员",
            confirmed=True,
            token="owner-session-secret",
        )
        self.workspace_id = bootstrap["workspace_id"]
        self.owner_id = bootstrap["actor_id"]
        self.owner_token = bootstrap["auth_token"]
        self.projects = ProjectService(self.store)
        self.versions = VersionService(self.store)
        self.tasks = TaskService(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def _create_project(self, name="同名项目"):
        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        return self.projects.create_project(
            self.owner_token,
            workspace_id=self.workspace_id,
            name=name,
            category_id="launch",
            expected_workspace_version=workspace["workspace_version"],
        )

    def _create_version(self):
        project = self._create_project()
        run = self.projects.bind_run(
            self.owner_token,
            project_id=project["project_id"],
            run_id="run_anon_001",
            source_kind="native",
            manifest_ref="artifact://run/run_anon_001/manifest",
            manifest_sha256=_sha("manifest"),
            expected_project_version=project["project_version"],
        )
        version = self.projects.create_project_version(
            self.owner_token,
            project_id=project["project_id"],
            run_id=run["run_id"],
            content_sha256=_sha("version-content"),
            artifact_ref="artifact://run/run_anon_001/reviewable",
            expected_project_version=run["project_version"],
        )
        return project, run, version

    def test_package_source_migration_preserves_old_row_and_allows_second_location(self):
        from unittest.mock import patch
        from mvp_v2 import workspace_store as schema
        old = WorkspaceStore(self.root / "old-v26.sqlite3")
        with patch.object(schema, "_MIGRATIONS", tuple(item for item in schema._MIGRATIONS if item[0] <= 26)):
            old.initialize()
            owner = LocalIdentityAdapter(old).bootstrap_owner(workspace_name="匿名迁移",
                display_name="匿名管理员", confirmed=True, token="migration-owner")
            project = ProjectService(old).create_project(owner["auth_token"],
                workspace_id=owner["workspace_id"], name="匿名来源项目", category_id="launch",
                expected_workspace_version=0)
        original = ("source-old", "image-old", "package-old", 1, "media-old", "a"*64,
                    "anonymous.xlsx", "模版", 7, owner["actor_id"], "2026-01-01T00:00:00Z")
        with old.transaction() as c:
            c.execute("INSERT INTO project_images VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("image-old", owner["workspace_id"], project["project_id"], "a"*64,
                 "artifact://anonymous/image", owner["actor_id"], original[-1]))
            c.execute("INSERT INTO project_image_package_source_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", original)
        old.initialize()
        old.initialize()
        with old.transaction() as c:
            self.assertEqual(tuple(c.execute("SELECT * FROM project_image_package_source_links").fetchone()), original)
            second = (*original[:8], 8, *original[9:])
            second = ("source-new", *second[1:])
            c.execute("INSERT INTO project_image_package_source_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", second)
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("INSERT INTO project_image_package_source_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          ("source-duplicate", *second[1:]))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                c.execute("UPDATE project_image_package_source_links SET source_row=9 WHERE source_row=7")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "PROJECT_IMAGE_PACKAGE_SOURCE_SCOPE_MISMATCH"):
                c.execute("INSERT INTO project_image_package_source_links VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          ("source-wrong-sha", *original[1:5], "b"*64, *original[6:]))
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_schema_migration_is_idempotent_and_enables_wal_and_foreign_keys(self):
        self.store.initialize()

        with self.store.connection() as connection:
            migrations = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }

        self.assertEqual([row[0] for row in migrations], list(range(1, 29)))
        self.assertEqual(journal_mode.lower(), "wal")
        self.assertEqual(foreign_keys, 1)
        self.assertTrue(
            {
                "workspaces",
                "projects",
                "project_runs",
                "project_brief_snapshots",
                "project_versions",
                "actors",
                "auth_sessions",
                "agent_sessions",
                "tasks",
                "task_events",
                "version_reviews",
                "candidate_selections",
                "version_publications",
                "action_confirmations",
                "tool_idempotency_records",
                "outbox",
                "mutation_events",
            }.issubset(tables)
        )

    def test_bootstrap_requires_confirmation_and_auth_token_is_only_stored_as_hash(self):
        other_store = WorkspaceStore(self.root / "other" / "workspace.sqlite3")
        other_store.initialize()
        other_identity = LocalIdentityAdapter(other_store)

        with self.assertRaisesRegex(WorkspaceServiceError, "BOOTSTRAP_CONFIRMATION_REQUIRED"):
            other_identity.bootstrap_owner(
                workspace_name="未确认工作区",
                display_name="未确认用户",
                confirmed=False,
            )

        with self.store.connection() as connection:
            stored = connection.execute(
                "SELECT token_hash FROM auth_sessions WHERE actor_id = ?",
                (self.owner_id,),
            ).fetchone()[0]

        self.assertNotEqual(stored, self.owner_token)
        self.assertEqual(stored, _sha(self.owner_token))
        resolved = self.identity.resolve_auth_session(self.owner_token)
        self.assertEqual(resolved["actor_id"], self.owner_id)
        self.assertEqual(resolved["roles"], ["administrator"])

        for missing_token in ("", "   ", None):
            with self.assertRaisesRegex(WorkspaceServiceError, "AUTH_SESSION_INVALID"):
                self.projects.get_workspace(missing_token, self.workspace_id)

    def test_same_name_projects_stay_distinct_and_workspace_lock_prevents_overwrite(self):
        first = self.projects.create_project(
            self.owner_token,
            workspace_id=self.workspace_id,
            name="同名项目",
            category_id="launch",
            expected_workspace_version=0,
        )
        second = self.projects.create_project(
            self.owner_token,
            workspace_id=self.workspace_id,
            name="同名项目",
            category_id="exhibition",
            expected_workspace_version=1,
        )

        self.assertNotEqual(first["project_id"], second["project_id"])
        renamed = self.projects.rename_project(
            self.owner_token,
            project_id=first["project_id"],
            name="项目已改名",
            expected_project_version=0,
        )
        self.assertEqual(renamed["project_id"], first["project_id"])
        self.assertEqual(renamed["project_version"], 1)

        with self.assertRaisesRegex(WorkspaceServiceError, "WORKSPACE_VERSION_CONFLICT"):
            self.projects.create_project(
                self.owner_token,
                workspace_id=self.workspace_id,
                name="冲突项目",
                category_id="launch",
                expected_workspace_version=1,
            )

        self.assertEqual(len(self.projects.list_projects(self.owner_token, self.workspace_id)), 2)

    def test_permissions_are_resolved_from_auth_session_and_disabled_actor_is_rejected(self):
        project = self._create_project()
        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        viewer = self.identity.create_actor(
            self.owner_token,
            workspace_id=self.workspace_id,
            display_name="匿名查看者",
            auth_source="local",
            role="viewer",
            expected_workspace_version=workspace["workspace_version"],
        )
        viewer_session = self.identity.issue_auth_session(
            actor_id=viewer["actor_id"], token="viewer-session-secret"
        )

        visible = self.projects.get_project(viewer_session["auth_token"], project["project_id"])
        self.assertEqual(visible["project_id"], project["project_id"])
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.projects.rename_project(
                viewer_session["auth_token"],
                project_id=project["project_id"],
                name="越权改名",
                expected_project_version=project["project_version"],
            )

        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        self.identity.disable_actor(
            self.owner_token,
            workspace_id=self.workspace_id,
            actor_id=viewer["actor_id"],
            expected_workspace_version=workspace["workspace_version"],
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "AUTH_SESSION_INVALID"):
            self.projects.get_project(viewer_session["auth_token"], project["project_id"])

    def test_project_scoped_role_cannot_read_another_project(self):
        allowed_project = self._create_project("允许访问")
        other_project = self._create_project("禁止访问")
        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        viewer = self.identity.create_actor(
            self.owner_token,
            workspace_id=self.workspace_id,
            project_id=allowed_project["project_id"],
            display_name="项目限定查看者",
            auth_source="local",
            role="viewer",
            expected_workspace_version=workspace["workspace_version"],
        )
        viewer_token = self.identity.issue_auth_session(
            actor_id=viewer["actor_id"], token="scoped-viewer-token"
        )["auth_token"]

        self.assertEqual(
            self.projects.get_project(viewer_token, allowed_project["project_id"])["name"],
            "允许访问",
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.projects.get_project(viewer_token, other_project["project_id"])

    def test_permission_matrix_has_positive_and_negative_cases_for_every_role(self):
        role_tokens = {"administrator": self.owner_token}
        workspace_version = self.projects.get_workspace(
            self.owner_token, self.workspace_id
        )["workspace_version"]
        for role in ("viewer", "editor", "reviewer"):
            actor = self.identity.create_actor(
                self.owner_token,
                workspace_id=self.workspace_id,
                display_name=f"匿名{role}",
                auth_source="local",
                role=role,
                expected_workspace_version=workspace_version,
            )
            workspace_version = actor["workspace_version"]
            role_tokens[role] = self.identity.issue_auth_session(
                actor_id=actor["actor_id"], token=f"{role}-matrix-token"
            )["auth_token"]

        all_permissions = {
            permission
            for permissions in ROLE_PERMISSIONS.values()
            for permission in permissions
        }
        with self.store.connection() as connection:
            for role, token in role_tokens.items():
                expected = {
                    permission
                    for inherited in ROLE_INHERITANCE[role]
                    for permission in ROLE_PERMISSIONS[inherited]
                }
                for permission in all_permissions:
                    if permission in expected:
                        resolved = require_permission(
                            connection,
                            token,
                            workspace_id=self.workspace_id,
                            permission=permission,
                        )
                        self.assertTrue(resolved["actor_id"])
                    else:
                        with self.assertRaisesRegex(
                            WorkspaceServiceError, "PERMISSION_DENIED"
                        ):
                            require_permission(
                                connection,
                                token,
                                workspace_id=self.workspace_id,
                                permission=permission,
                            )

    def test_run_binding_and_project_versions_are_immutable(self):
        _, run, version = self._create_version()

        with self.store.connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE project_versions SET artifact_ref = ? WHERE version_id = ?",
                    ("artifact://tampered", version["version_id"]),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM project_runs WHERE run_id = ?", (run["run_id"],)
                )

        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.projects.bind_run(
                self.owner_token,
                project_id=version["project_id"],
                run_id="run_anon_conflict",
                source_kind="legacy",
                manifest_ref="artifact://legacy-run/run_anon_conflict/manifest",
                manifest_sha256=_sha("legacy-manifest"),
                expected_project_version=1,
            )

        current = self.projects.get_project(self.owner_token, version["project_id"])
        self.assertEqual(current["project_version"], 2)
        self.assertEqual(current["current_version_id"], version["version_id"])

    def test_brief_snapshots_are_logical_immutable_and_available_before_first_run(self):
        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        project = self.projects.create_project(
            self.owner_token,
            workspace_id=self.workspace_id,
            name="Brief 预检项目",
            category_id="launch",
            brief_ref="artifact://brief/anonymous/initial",
            brief_sha256=_sha("initial-brief"),
            expected_workspace_version=workspace["workspace_version"],
        )
        initial = self.projects.get_current_brief_snapshot(
            self.owner_token, project["project_id"]
        )
        self.assertEqual(initial["brief_ref"], "artifact://brief/anonymous/initial")
        self.assertEqual(self.projects.list_project_runs(
            self.owner_token, project["project_id"]
        ), [])

        updated = self.projects.add_brief_snapshot(
            self.owner_token,
            project_id=project["project_id"],
            brief_ref="artifact://brief/anonymous/revised",
            brief_sha256=_sha("revised-brief"),
            expected_project_version=project["project_version"],
        )
        current = self.projects.get_current_brief_snapshot(
            self.owner_token, project["project_id"]
        )
        stored_project = self.projects.get_project(self.owner_token, project["project_id"])
        self.assertEqual(current["brief_snapshot_id"], updated["brief_snapshot_id"])
        self.assertEqual(
            stored_project["initial_brief_snapshot_id"], initial["brief_snapshot_id"]
        )
        with self.store.connection() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE project_brief_snapshots SET brief_ref = ? "
                    "WHERE brief_snapshot_id = ?",
                    ("artifact://brief/tampered", initial["brief_snapshot_id"]),
                )

    def test_review_candidate_and_publication_are_separate_and_current_is_not_written(self):
        _, _, version = self._create_version()
        candidate = self.versions.select_candidate(
            self.owner_token,
            version_id=version["version_id"],
            expected_project_version=version["project_version"],
            reason="提交人工复核",
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "REVIEW_REQUIRED"):
            self.versions.publish_version(
                self.owner_token,
                version_id=version["version_id"],
                review_id="review_missing",
                expected_project_version=candidate["project_version"],
            )

        review = self.versions.record_review(
            self.owner_token,
            version_id=version["version_id"],
            decision="quality_pass",
            evidence_ref="artifact://review/anonymous-pass",
            expected_project_version=candidate["project_version"],
        )
        publication = self.versions.publish_version(
            self.owner_token,
            version_id=version["version_id"],
            review_id=review["review_id"],
            expected_project_version=review["project_version"],
        )

        with self.store.connection() as connection:
            counts = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "candidate_selections",
                    "version_reviews",
                    "version_publications",
                )
            }
            event = connection.execute(
                "SELECT event_type, payload_json, status FROM outbox "
                "WHERE aggregate_id = ? ORDER BY sequence_id DESC LIMIT 1",
                (version["project_id"],),
            ).fetchone()

        self.assertEqual(counts, {
            "candidate_selections": 1,
            "version_reviews": 1,
            "version_publications": 1,
        })
        self.assertEqual(publication["status"], "published")
        self.assertEqual(event["event_type"], "project.version_published")
        self.assertEqual(event["status"], "pending")
        self.assertIn(version["content_sha256"], event["payload_json"])
        self.assertFalse((self.root / "CURRENT").exists())
        self.assertFalse((self.database_path.parent / "CURRENT").exists())

    def test_auth_session_agent_session_and_task_state_are_independent_and_recoverable(self):
        project = self._create_project()
        agent_session = self.tasks.create_agent_session(
            self.owner_token, project_id=project["project_id"]
        )
        task = self.tasks.create_task(
            self.owner_token,
            agent_session_id=agent_session["agent_session_id"],
            task_type="read_project_state",
            payload_sha256=_sha("anonymous-task"),
            expected_project_version=project["project_version"],
        )
        executing = self.tasks.transition_task(
            self.owner_token,
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="executing",
        )
        completed = self.tasks.transition_task(
            self.owner_token,
            task_id=task["task_id"],
            expected_task_version=executing["task_version"],
            status="completed",
            result_ref="artifact://task/anonymous/result",
        )

        restarted_service = TaskService(WorkspaceStore(self.database_path))
        restored = restarted_service.get_task(self.owner_token, completed["task_id"])
        session_tasks = restarted_service.list_agent_session_tasks(
            self.owner_token, agent_session["agent_session_id"]
        )
        events = restarted_service.list_task_events(self.owner_token, completed["task_id"])

        self.assertNotEqual(agent_session["agent_session_id"], agent_session["auth_session_id"])
        self.assertEqual(restored["status"], "completed")
        self.assertEqual([item["task_id"] for item in session_tasks], [completed["task_id"]])
        self.assertEqual(restored["result_ref"], "artifact://task/anonymous/result")
        self.assertEqual([event["status"] for event in events], [
            "received",
            "executing",
            "completed",
        ])

    def test_task_and_project_conflicts_have_stable_codes(self):
        project = self._create_project()
        agent_session = self.tasks.create_agent_session(
            self.owner_token, project_id=project["project_id"]
        )
        task = self.tasks.create_task(
            self.owner_token,
            agent_session_id=agent_session["agent_session_id"],
            task_type="read_project_state",
            payload_sha256=_sha("task-conflict"),
            expected_project_version=project["project_version"],
        )
        self.tasks.transition_task(
            self.owner_token,
            task_id=task["task_id"],
            expected_task_version=0,
            status="executing",
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "TASK_VERSION_CONFLICT"):
            self.tasks.transition_task(
                self.owner_token,
                task_id=task["task_id"],
                expected_task_version=0,
                status="cancelled",
            )


if __name__ == "__main__":
    unittest.main()
