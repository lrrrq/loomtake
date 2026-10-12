import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


class ReadonlyAgentFixture:
    def __init__(self, root: Path, *, with_project: bool = True, with_run: bool = True):
        self.root = root
        self.runs_root = root / "runs"
        self.store = WorkspaceStore(root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="本机管理员",
            confirmed=True,
            token="owner-token-for-tests",
        )
        self.owner_token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.versions = VersionService(self.store)
        self.agent = ReadonlyAgentService(
            ReadonlyAgentServices(
                store=self.store,
                identity=self.identity,
                projects=self.projects,
                tasks=self.tasks,
                versions=self.versions,
                artifacts=RunArtifactResolver(self.runs_root),
            )
        )
        self.project = None
        self.run_id = "run_1234567890abcdef"
        if with_project:
            self.project = self.projects.create_project(
                self.owner_token,
                workspace_id=self.workspace_id,
                name="匿名发布会",
                category_id="launch",
                brief_ref="artifact://brief/anonymous/freeze",
                brief_sha256=_sha_text("anonymous brief"),
                expected_workspace_version=0,
            )
        if with_project and with_run:
            self._bind_waiting_review_run()

    def _bind_waiting_review_run(self) -> None:
        run_dir = self.runs_root / "匿名发布会" / "runs" / self.run_id
        run_dir.mkdir(parents=True)
        report = run_dir / "run_report.json"
        report.write_text(
            json.dumps(
                {
                    "status": "waiting_human_review",
                    "machine_audit_status": "pass",
                    "human_review_status": "pending",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_id": self.run_id,
                    "project_name": "不应依赖 manifest 项目名",
                    "category_id": "launch",
                    "status": "waiting_human_review",
                    "stages": {
                        "input_freeze": {"status": "succeeded", "attempt": 1},
                        "machine_audit": {"status": "succeeded", "attempt": 1},
                        "human_review": {
                            "status": "waiting_human_review",
                            "attempt": 1,
                        },
                        "publish_feedback": {"status": "invalidated", "attempt": 0},
                    },
                    "artifacts": {
                        "run_report": {
                            "path": str(report),
                            "sha256": _sha_bytes(report.read_bytes()),
                            "size_bytes": report.stat().st_size,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        bound = self.projects.bind_run(
            self.owner_token,
            project_id=self.project["project_id"],
            run_id=self.run_id,
            source_kind="native",
            manifest_ref=f"artifact://run/{self.run_id}/manifest",
            manifest_sha256=_sha_bytes(manifest.read_bytes()),
            expected_project_version=self.project["project_version"],
        )
        self.project["project_version"] = bound["project_version"]
        version = self.projects.create_project_version(
            self.owner_token,
            project_id=self.project["project_id"],
            run_id=self.run_id,
            content_sha256=_sha_text("anonymous output"),
            artifact_ref=f"artifact://run/{self.run_id}/workbook",
            expected_project_version=self.project["project_version"],
        )
        self.project["project_version"] = version["project_version"]
        self.version_id = version["version_id"]


class ReadonlyAgentServiceTests(unittest.TestCase):
    def test_state_uses_verified_run_and_preserves_human_review_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))

            state = fixture.agent.get_project_state(
                fixture.owner_token, fixture.project["project_id"]
            )

            self.assertEqual(state["blocker"]["code"], "WAITING_HUMAN_REVIEW")
            self.assertEqual(state["blocker"]["title"], "等待人工审核")
            self.assertFalse(state["blocker"]["is_technical_failure"])
            self.assertIn("不代表质量已经通过", state["blocker"]["detail"])
            self.assertEqual(
                state["run"]["verified_artifacts"][0]["ref"],
                f"artifact://run/{fixture.run_id}/run_report",
            )
            serialized = json.dumps(state, ensure_ascii=False)
            self.assertNotIn(str(Path(tmp)), serialized)
            self.assertNotIn("anonymous brief", serialized)

    def test_missing_brief_and_run_are_honest_blockers(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp), with_project=False, with_run=False)
            project = fixture.projects.create_project(
                fixture.owner_token,
                workspace_id=fixture.workspace_id,
                name="空项目",
                category_id="event",
                expected_workspace_version=0,
            )

            state = fixture.agent.get_project_state(
                fixture.owner_token, project["project_id"]
            )

            self.assertEqual(state["blocker"]["code"], "BRIEF_REQUIRED")
            self.assertIsNone(state["run"])

        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp), with_project=True, with_run=False)

            state = fixture.agent.get_project_state(
                fixture.owner_token, fixture.project["project_id"]
            )

            self.assertEqual(state["blocker"]["code"], "RUN_REQUIRED")
            self.assertIn("E02 只提供状态解释", state["blocker"]["next_step"])

    def test_execution_request_is_persisted_as_e02_readonly_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))
            session = fixture.agent.create_session(
                fixture.owner_token, fixture.project["project_id"]
            )["session"]

            result = fixture.agent.submit_message(
                fixture.owner_token,
                session["agent_session_id"],
                content="请开始生成一版并发布",
            )

            self.assertEqual(result["task"]["status"], "blocked")
            self.assertEqual(result["task"]["error_code"], "E02_READ_ONLY")
            self.assertEqual(result["response"]["title"], "E02 不可执行")
            self.assertEqual(
                [event["status"] for event in result["events"]],
                ["received", "blocked"],
            )

    def test_state_question_containing_generation_word_is_not_misrouted_as_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))
            session = fixture.agent.create_session(
                fixture.owner_token, fixture.project["project_id"]
            )["session"]

            result = fixture.agent.submit_message(
                fixture.owner_token,
                session["agent_session_id"],
                content="现在生成到哪了？",
            )

            self.assertEqual(result["task"]["status"], "completed")
            self.assertEqual(result["response"]["type"], "project_state")

    def test_failed_read_is_persisted_as_blocked_instead_of_stuck_executing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))
            session = fixture.agent.create_session(
                fixture.owner_token, fixture.project["project_id"]
            )["session"]

            result = fixture.agent.submit_message(
                fixture.owner_token,
                session["agent_session_id"],
                content="查看运行节点详情",
                run_id="run_missing_123456",
            )

            self.assertEqual(result["task"]["status"], "blocked")
            self.assertEqual(result["task"]["error_code"], "NOT_FOUND")
            self.assertEqual(result["response"]["title"], "只读查询被阻断")

    def test_review_record_advances_status_without_rewriting_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))
            review = fixture.versions.record_review(
                fixture.owner_token,
                version_id=fixture.version_id,
                decision="quality_pass",
                evidence_ref="artifact://review/anonymous/pass",
                expected_project_version=fixture.project["project_version"],
            )
            fixture.project["project_version"] = review["project_version"]

            state = fixture.agent.get_project_state(
                fixture.owner_token, fixture.project["project_id"]
            )

            self.assertEqual(state["blocker"]["code"], "HUMAN_REVIEW_PASSED")
            self.assertIn("不等于已经正式发布", state["blocker"]["detail"])

    def test_task_and_events_recover_after_service_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))
            session = fixture.agent.create_session(
                fixture.owner_token, fixture.project["project_id"]
            )["session"]
            result = fixture.agent.submit_message(
                fixture.owner_token,
                session["agent_session_id"],
                content="现在到哪了？",
            )
            restarted = ReadonlyAgentService(
                ReadonlyAgentServices(
                    store=WorkspaceStore(fixture.store.database_path),
                    identity=LocalIdentityAdapter(WorkspaceStore(fixture.store.database_path)),
                    projects=ProjectService(WorkspaceStore(fixture.store.database_path)),
                    tasks=TaskService(WorkspaceStore(fixture.store.database_path)),
                    versions=VersionService(WorkspaceStore(fixture.store.database_path)),
                    artifacts=RunArtifactResolver(fixture.runs_root),
                )
            )

            restored = restarted.get_session(
                fixture.owner_token, session["agent_session_id"]
            )

            self.assertNotEqual(
                restored["session"]["agent_session_id"],
                restored["session"]["auth_session_id"],
            )
            self.assertEqual(restored["current_task"]["task_id"], result["task"]["task_id"])
            self.assertEqual(restored["current_task"]["status"], "completed")
            self.assertEqual(restored["timeline"][-1]["response"]["type"], "project_state")

    def test_permission_prevents_reading_another_actor_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))
            actor = fixture.identity.create_actor(
                fixture.owner_token,
                workspace_id=fixture.workspace_id,
                display_name="只读同事",
                auth_source="local-test",
                role="viewer",
                expected_workspace_version=1,
            )
            viewer_token = fixture.identity.issue_auth_session(
                actor_id=actor["actor_id"], token="viewer-token-for-tests"
            )["auth_token"]
            owner_session = fixture.agent.create_session(
                fixture.owner_token, fixture.project["project_id"]
            )["session"]

            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                fixture.agent.get_session(
                    viewer_token, owner_session["agent_session_id"]
                )

    def test_manifest_hash_drift_blocks_explanation_without_leaking_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))
            manifest = next(fixture.runs_root.glob("*/runs/*/run_manifest.json"))
            manifest.write_text(
                manifest.read_text(encoding="utf-8") + " ", encoding="utf-8"
            )

            state = fixture.agent.get_project_state(
                fixture.owner_token, fixture.project["project_id"]
            )

            self.assertEqual(state["blocker"]["code"], "ARTIFACT_HASH_MISMATCH")
            self.assertNotIn(str(Path(tmp)), json.dumps(state, ensure_ascii=False))


class ReadonlyAgentApiTests(unittest.TestCase):
    def test_owner_enable_requires_explicit_confirmation_and_uses_injected_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = WorkspaceStore(root / "workspace.sqlite3")
            agent = ReadonlyAgentService(
                ReadonlyAgentServices(
                    store=store,
                    identity=LocalIdentityAdapter(store),
                    projects=ProjectService(store),
                    tasks=TaskService(store),
                    versions=VersionService(store),
                    artifacts=RunArtifactResolver(root / "runs"),
                )
            )
            client = TestClient(create_app(agent_api=agent))
            ui_token = client.get("/api/bootstrap").json()["token"]
            headers = {"X-MPlus-UI-Token": ui_token}

            denied = client.post(
                "/api/agent/owner/enable",
                json={"workspace_name": "测试", "display_name": "owner", "confirmed": False},
                headers=headers,
            )
            enabled = client.post(
                "/api/agent/owner/enable",
                json={"workspace_name": "测试", "display_name": "owner", "confirmed": True},
                headers=headers,
            )

            self.assertEqual(denied.status_code, 409)
            self.assertEqual(
                denied.json()["detail"]["code"], "BOOTSTRAP_CONFIRMATION_REQUIRED"
            )
            self.assertEqual(enabled.status_code, 200)
            self.assertTrue((root / "workspace.sqlite3").is_file())

    def test_local_owner_session_can_be_reissued_without_recreating_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp), with_run=False)
            client = TestClient(create_app(agent_api=fixture.agent))
            ui_token = client.get("/api/bootstrap").json()["token"]
            headers = {"X-MPlus-UI-Token": ui_token}

            denied = client.post(
                "/api/agent/owner/reissue",
                json={"confirmed": False},
                headers=headers,
            )
            recovered = client.post(
                "/api/agent/owner/reissue",
                json={"confirmed": True},
                headers=headers,
            )

            self.assertEqual(denied.status_code, 409)
            self.assertEqual(
                denied.json()["detail"]["code"],
                "OWNER_SESSION_CONFIRMATION_REQUIRED",
            )
            self.assertEqual(recovered.status_code, 200)
            payload = recovered.json()
            self.assertEqual(payload["workspace_id"], fixture.workspace_id)
            listed = client.get(
                f"/api/agent/projects?workspace_id={fixture.workspace_id}",
                headers={"X-MPlus-Agent-Auth": payload["auth_token"]},
            )
            self.assertEqual(listed.status_code, 200)
            self.assertEqual(
                [item["project_id"] for item in listed.json()["items"]],
                [fixture.project["project_id"]],
            )
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0],
                    2,
                )

    def test_session_message_and_event_api_are_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ReadonlyAgentFixture(Path(tmp))
            client = TestClient(create_app(agent_api=fixture.agent))
            ui_token = client.get("/api/bootstrap").json()["token"]
            headers = {
                "X-MPlus-UI-Token": ui_token,
                "X-MPlus-Agent-Auth": fixture.owner_token,
            }
            created = client.post(
                "/api/agent/sessions",
                json={"project_id": fixture.project["project_id"]},
                headers=headers,
            ).json()
            session_id = created["session"]["agent_session_id"]

            message = client.post(
                f"/api/agent/sessions/{session_id}/messages",
                json={"content": "为什么停住了？"},
                headers=headers,
            )
            task_id = message.json()["task"]["task_id"]
            restored = client.get(
                f"/api/agent/sessions/{session_id}", headers=headers
            )
            events = client.get(
                f"/api/agent/tasks/{task_id}/events", headers=headers
            )

            self.assertEqual(message.status_code, 200)
            self.assertEqual(message.json()["response"]["title"], "等待人工审核")
            self.assertEqual(restored.json()["current_task"]["task_id"], task_id)
            self.assertEqual(
                [item["status"] for item in events.json()["items"]],
                ["received", "executing", "completed"],
            )

    def test_agent_page_is_interactive_but_has_no_parallel_business_controls(self):
        index = (
            Path(__file__).parents[1] / "mvp_v2" / "ui" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="agent-project-select"', index)
        self.assertIn('id="agent-refresh"', index)
        agent_page = index.split('id="page-agent"', 1)[1].split('id="page-versions"', 1)[0]
        self.assertIn('id="agent-message-form"', agent_page)
        self.assertIn('id="agent-message"', agent_page)
        self.assertIn("修改会先展示预览。", agent_page)
        self.assertIn('agent-mode-stamp">需确认', agent_page)
        self.assertIn("核对结果后确认修改", agent_page)
        self.assertNotIn("不会创建任务或修改项目", agent_page)
        self.assertNotIn("parameters", agent_page)
        for retired_control in (
            'id="agent-owner-confirm"', 'id="agent-submit"', 'id="agent-form"',
            'id="agent-onboard"', 'data-agent-prompt',
        ):
            self.assertNotIn(retired_control, agent_page)


if __name__ == "__main__":
    unittest.main()
