from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from mvp_v2.agent_tools import TOOL_REGISTRY
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.manual_actions import MANUAL_ACTION_REGISTRY
from mvp_v2.manual_project_commands import (
    ManualProjectCommandService,
    ManualProjectCommandServices,
)
from mvp_v2.mutation_policy import MUTATION_RULES, require_mutation_rule
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class MutationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temporary.name) / "workspace.sqlite3")
        self.store.initialize()
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区", display_name="管理员", confirmed=True,
            token="owner-secret",
        )
        self.workspace_id = owner["workspace_id"]
        self.token = owner["auth_token"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.commands = ManualProjectCommandService(
            ManualProjectCommandServices(store=self.store)
        )
        self.project = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名归档测试",
            category_id="launch", expected_workspace_version=0,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _parameters(self, action: str, version: int) -> dict:
        return {
            "project_id": self.project["project_id"],
            "expected_project_version": version,
            "idempotency_key": f"{action}-idempotency-0001",
            "reason": "用户明确要求变更项目状态",
        }

    def test_archive_and_restore_are_confirmed_atomic_and_preserve_business_records(self):
        agent_session = self.tasks.create_agent_session(
            self.token, project_id=self.project["project_id"],
        )
        snapshot = self.projects.add_brief_snapshot(
            self.token, project_id=self.project["project_id"],
            brief_ref="artifact://brief/anonymous", brief_sha256="a" * 64,
            expected_project_version=0,
        )
        parameters = self._parameters("archive", snapshot["project_version"])
        prepared = self.commands.prepare(
            self.token, tool_name="archive_project", parameters=parameters,
        )
        self.assertEqual(self.projects.get_project(self.token, self.project["project_id"])["status"], "active")
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM mutation_events").fetchone()[0], 0)

        archived = self.commands.execute(
            self.token, tool_name="archive_project", parameters=parameters,
            command_token=prepared["command_token"],
        )["result"]
        self.assertEqual(archived["project_status"], "archived")
        self.assertIn("brief_versions", archived["preserved_scope"])
        self.assertEqual(self.projects.get_current_brief_snapshot(self.token, self.project["project_id"])["brief_snapshot_id"], snapshot["brief_snapshot_id"])
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_ARCHIVED"):
            self.projects.rename_project(
                self.token, project_id=self.project["project_id"], name="不应写入",
                expected_project_version=archived["project_version"],
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_ARCHIVED"):
            self.tasks.create_agent_session(self.token, project_id=self.project["project_id"])
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_ARCHIVED"):
            self.tasks.create_task(
                self.token, agent_session_id=agent_session["agent_session_id"],
                task_type="archived-write", payload_sha256="c" * 64,
                expected_project_version=archived["project_version"],
            )

        restore_parameters = self._parameters("restore", archived["project_version"])
        restore_parameters["idempotency_key"] = "restore-idempotency-0001"
        restore = self.commands.prepare(
            self.token, tool_name="restore_project", parameters=restore_parameters,
        )
        restored = self.commands.execute(
            self.token, tool_name="restore_project", parameters=restore_parameters,
            command_token=restore["command_token"],
        )["result"]
        self.assertEqual(restored["project_status"], "active")
        self.assertEqual(self.projects.get_current_brief_snapshot(self.token, self.project["project_id"])["brief_snapshot_id"], snapshot["brief_snapshot_id"])

        with self.store.connection() as connection:
            events = connection.execute(
                "SELECT operation, changed_fields_json, preserved_scope_json FROM mutation_events ORDER BY created_at"
            ).fetchall()
            self.assertEqual([row["operation"] for row in events], ["project.archive", "project.restore"])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE mutation_events SET operation='tampered'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM mutation_events")

    def test_drift_replay_expiry_and_purge_fail_closed(self):
        parameters = self._parameters("archive", 0)
        prepared = self.commands.prepare(self.token, tool_name="archive_project", parameters=parameters)
        self.projects.rename_project(
            self.token, project_id=self.project["project_id"], name="发生漂移",
            expected_project_version=0,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.commands.execute(
                self.token, tool_name="archive_project", parameters=parameters,
                command_token=prepared["command_token"],
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.commands.prepare(
                self.token, tool_name="archive_project",
                parameters={**parameters, "expected_project_version": 1},
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "MUTATION_PURGE_UNAVAILABLE"):
            require_mutation_rule("project.purge")
        self.assertNotIn("purge_project", MANUAL_ACTION_REGISTRY)
        self.assertNotIn("purge_project", TOOL_REGISTRY)

    def test_restore_window_expiry_is_readable_but_not_writable(self):
        old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat().replace("+00:00", "Z")
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE projects SET status='archived', lock_version=1 WHERE project_id=?",
                (self.project["project_id"],),
            )
            connection.execute(
                """
                INSERT INTO mutation_events VALUES (
                    'mutation_expired', ?, ?, (SELECT created_by_actor_id FROM projects WHERE project_id=?),
                    'project.archive', 'archive_project', 'project', ?, ?, '[\"status\"]',
                    '[\"brief_versions\"]', 'restore_30_days', NULL, ?
                )
                """,
                (
                    self.workspace_id, self.project["project_id"], self.project["project_id"],
                    self.project["project_id"], "b" * 64, old,
                ),
            )
        status = self.commands.recovery_status(self.token, self.project["project_id"])
        self.assertFalse(status["available"])
        self.assertEqual(status["reason"], "PROJECT_ARCHIVE_EXPIRED")
        self.assertEqual(self.projects.get_project(self.token, self.project["project_id"])["status"], "archived")
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_ARCHIVE_EXPIRED"):
            self.commands.prepare(
                self.token, tool_name="restore_project",
                parameters={
                    **self._parameters("restore-expired", 1),
                    "idempotency_key": "restore-expired-0001",
                },
            )

    def test_registry_has_explicit_granularity_and_only_administrator_permissions(self):
        self.assertEqual(MUTATION_RULES["script.patch"].mutation_kind, "patch_field")
        self.assertEqual(MUTATION_RULES["script.version"].mutation_kind, "append_immutable")
        with self.store.connection() as connection:
            roles = {
                row[0] for row in connection.execute(
                    """
                    SELECT rp.role_id FROM role_permissions rp
                    WHERE rp.permission_id IN ('project.archive','project.restore')
                    """
                )
            }
        self.assertEqual(roles, {"administrator"})

    def test_archive_is_blocked_while_project_has_live_task(self):
        session = self.tasks.create_agent_session(self.token, project_id=self.project["project_id"])
        self.tasks.create_task(
            self.token, agent_session_id=session["agent_session_id"],
            task_type="active-work", payload_sha256="d" * 64,
            expected_project_version=0,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_BUSY"):
            self.commands.prepare(
                self.token, tool_name="archive_project",
                parameters=self._parameters("archive-busy", 0),
            )


if __name__ == "__main__":
    unittest.main()
