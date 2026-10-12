import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.action_confirmation import ActionConfirmationService, ManualPageConfirmationService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class ActionConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temporary.name) / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="本机 owner",
            confirmed=True,
            token="owner-auth-token",
        )
        self.auth_token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.owner_id = owner["actor_id"]
        projects = ProjectService(self.store)
        project = projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="匿名项目",
            category_id="launch",
            expected_workspace_version=0,
        )
        self.project_id = project["project_id"]
        self.session = TaskService(self.store).create_agent_session(
            self.auth_token,
            project_id=self.project_id,
        )
        self.service = ActionConfirmationService(self.store, b"x" * 32)
        self.parameters = {
            "project_id": self.project_id,
            "mode": "standard",
            "expected_project_version": 0,
            "idempotency_key": "request-00000001",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_confirmation_binds_actor_session_tool_and_parameters_and_consumes_once(self):
        issued = self.service.issue(
            self.auth_token,
            agent_session_id=self.session["agent_session_id"],
            tool_name="start_generation",
            parameters=self.parameters,
        )
        consumed = self.service.consume(
            self.auth_token,
            confirmation_token=issued["confirmation_token"],
            agent_session_id=self.session["agent_session_id"],
            tool_name="start_generation",
            parameters=self.parameters,
        )

        self.assertEqual(consumed["status"], "consumed")
        self.assertEqual(consumed["actor_id"], self.owner_id)
        with self.store.connection() as connection:
            events = [
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM outbox WHERE aggregate_id = ? ORDER BY sequence_id",
                    (self.session["agent_session_id"],),
                )
            ]
        self.assertEqual(events[-2:], [
            "agent.confirmation_issued",
            "agent.confirmation_consumed",
        ])
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.service.consume(
                self.auth_token,
                confirmation_token=issued["confirmation_token"],
                agent_session_id=self.session["agent_session_id"],
                tool_name="start_generation",
                parameters=self.parameters,
            )

    def test_parameter_or_signature_tampering_is_rejected_without_consuming(self):
        issued = self.service.issue(
            self.auth_token,
            agent_session_id=self.session["agent_session_id"],
            tool_name="start_generation",
            parameters=self.parameters,
        )
        changed = {**self.parameters, "mode": "controlled"}
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self.service.consume(
                self.auth_token,
                confirmation_token=issued["confirmation_token"],
                agent_session_id=self.session["agent_session_id"],
                tool_name="start_generation",
                parameters=changed,
            )
        tampered = issued["confirmation_token"][:-1] + (
            "A" if issued["confirmation_token"][-1] != "A" else "B"
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.service.consume(
                self.auth_token,
                confirmation_token=tampered,
                agent_session_id=self.session["agent_session_id"],
                tool_name="start_generation",
                parameters=self.parameters,
            )

    def test_expired_or_disabled_actor_confirmation_is_rejected(self):
        with patch("mvp_v2.action_confirmation.time.time", return_value=100):
            issued = self.service.issue(
                self.auth_token,
                agent_session_id=self.session["agent_session_id"],
                tool_name="start_generation",
                parameters=self.parameters,
                ttl_seconds=1,
            )
        with patch("mvp_v2.action_confirmation.time.time", return_value=102):
            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                self.service.consume(
                    self.auth_token,
                    confirmation_token=issued["confirmation_token"],
                    agent_session_id=self.session["agent_session_id"],
                    tool_name="start_generation",
                    parameters=self.parameters,
                )

        workspace = ProjectService(self.store).get_workspace(self.auth_token, self.workspace_id)
        issued = self.service.issue(
            self.auth_token,
            agent_session_id=self.session["agent_session_id"],
            tool_name="start_generation",
            parameters=self.parameters,
        )
        self.identity.disable_actor(
            self.auth_token,
            workspace_id=self.workspace_id,
            actor_id=self.owner_id,
            expected_workspace_version=workspace["workspace_version"],
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "AUTH_SESSION_INVALID"):
            self.service.consume(
                self.auth_token,
                confirmation_token=issued["confirmation_token"],
                agent_session_id=self.session["agent_session_id"],
                tool_name="start_generation",
                parameters=self.parameters,
            )

    def test_channel_isolation_rejects_manual_r3_from_agent_confirmation(self):
        manual_actions = (
            "record_version_review", "select_candidate_version", "publish_version",
            "publish_asset_import", "approve_knowledge",
        )
        for tool_name in manual_actions:
            with self.subTest(tool_name=tool_name), self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
                self.service.issue(
                    self.auth_token,
                    agent_session_id=self.session["agent_session_id"],
                    tool_name=tool_name,
                    parameters={},
                )
            with self.subTest(tool_name=f"consume-{tool_name}"), self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
                self.service.consume(
                    self.auth_token,
                    confirmation_token="invalid-token",
                    agent_session_id=self.session["agent_session_id"],
                    tool_name=tool_name,
                    parameters={},
                )
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM action_confirmations").fetchone()[0], 0)

    def test_channel_isolation_tokens_are_not_cross_consumable(self):
        parameters = {
            "project_id": self.project_id,
            "version_id": "version_12345678",
            "review_id": "review_12345678",
            "expected_project_version": 0,
            "idempotency_key": "publish-00000001",
        }
        manual = ManualPageConfirmationService(self.store, b"x" * 32)
        issued = manual.issue(
            self.auth_token,
            agent_session_id=self.session["agent_session_id"],
            tool_name="publish_version",
            parameters=parameters,
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self.service.consume(
                self.auth_token,
                confirmation_token=issued["confirmation_token"],
                agent_session_id=self.session["agent_session_id"],
                tool_name="publish_version",
                parameters={**parameters, "confirmation_token": issued["confirmation_token"]},
            )
        self.assertEqual(
            manual.consume(
                self.auth_token,
                confirmation_token=issued["confirmation_token"],
                agent_session_id=self.session["agent_session_id"],
                tool_name="publish_version",
                parameters={**parameters, "confirmation_token": issued["confirmation_token"]},
            )["tool_name"],
            "publish_version",
        )


if __name__ == "__main__":
    unittest.main()
