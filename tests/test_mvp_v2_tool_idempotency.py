import tempfile
import unittest
from pathlib import Path

from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class ToolIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = WorkspaceStore(Path(self.temporary.name) / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="本机管理员",
            confirmed=True,
            token="owner-idempotency-token",
        )
        self.workspace_id = owner["workspace_id"]
        self.auth_token = owner["auth_token"]
        self.projects = ProjectService(self.store)
        project = self.projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="匿名项目",
            category_id="launch",
            expected_workspace_version=0,
        )
        self.project_id = project["project_id"]
        self.project_version = project["project_version"]
        self.agent_session_id = TaskService(self.store).create_agent_session(
            self.auth_token,
            project_id=self.project_id,
        )["agent_session_id"]
        self.service = ToolIdempotencyService(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def _parameters(self, **changes):
        payload = {
            "project_id": self.project_id,
            "expected_project_version": self.project_version,
            "idempotency_key": "request-idempotency-0001",
            "mode": "standard",
            "asset_package_version": "",
        }
        payload.update(changes)
        return payload

    def test_repeat_claim_replays_one_record_without_duplicate_outbox(self):
        first = self.service.claim(
            self.auth_token,
            agent_session_id=self.agent_session_id,
            tool_name="start_generation",
            parameters=self._parameters(),
        )
        second = self.service.claim(
            self.auth_token,
            agent_session_id=self.agent_session_id,
            tool_name="start_generation",
            parameters=self._parameters(),
        )

        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["idempotency_record_id"], second["idempotency_record_id"])
        with self.store.connection() as connection:
            records = connection.execute("SELECT COUNT(*) FROM tool_idempotency_records").fetchone()[0]
            events = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type = 'agent_tool.idempotency_claimed'"
            ).fetchone()[0]
        self.assertEqual(records, 1)
        self.assertEqual(events, 1)

    def test_same_key_with_changed_business_parameters_is_rejected(self):
        self.service.claim(
            self.auth_token,
            agent_session_id=self.agent_session_id,
            tool_name="start_generation",
            parameters=self._parameters(),
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.service.claim(
                self.auth_token,
                agent_session_id=self.agent_session_id,
                tool_name="start_generation",
                parameters=self._parameters(mode="controlled"),
            )

    def test_recorded_tool_result_is_stable_and_replayable(self):
        claim = self.service.claim(
            self.auth_token,
            agent_session_id=self.agent_session_id,
            tool_name="start_generation",
            parameters=self._parameters(),
        )
        result = {
            "task_id": "task_anonymous0001",
            "status": "accepted",
            "artifact_refs": [],
            "warnings": [],
            "error": None,
        }
        recorded = self.service.record_result(
            self.auth_token,
            idempotency_record_id=claim["idempotency_record_id"],
            result=result,
        )
        replay = self.service.record_result(
            self.auth_token,
            idempotency_record_id=claim["idempotency_record_id"],
            result=result,
        )

        self.assertEqual(recorded["status"], "accepted")
        self.assertEqual(recorded["response"], result)
        self.assertTrue(replay["replayed"])

        changed = {**result, "status": "completed"}
        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.service.record_result(
                self.auth_token,
                idempotency_record_id=claim["idempotency_record_id"],
                result=changed,
            )

    def test_project_scope_cannot_escape_agent_session(self):
        other = self.projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="其他匿名项目",
            category_id="launch",
            expected_workspace_version=1,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.service.claim(
                self.auth_token,
                agent_session_id=self.agent_session_id,
                tool_name="start_generation",
                parameters=self._parameters(project_id=other["project_id"]),
            )

    def test_read_tool_and_invalid_payload_cannot_claim_a_key(self):
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self.service.claim(
                self.auth_token,
                agent_session_id=self.agent_session_id,
                tool_name="get_project_state",
                parameters={"project_id": self.project_id},
            )

    def test_channel_isolation_rejects_manual_r3_claims_without_records(self):
        for tool_name in (
            "record_version_review", "select_candidate_version", "publish_version",
            "publish_asset_import", "approve_knowledge",
        ):
            with self.subTest(tool_name=tool_name), self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
                self.service.claim(
                    self.auth_token,
                    agent_session_id=self.agent_session_id,
                    tool_name=tool_name,
                    parameters={},
                )
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tool_idempotency_records").fetchone()[0], 0)
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self.service.claim(
                self.auth_token,
                agent_session_id=self.agent_session_id,
                tool_name="start_generation",
                parameters={"project_id": self.project_id},
            )


if __name__ == "__main__":
    unittest.main()
