import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentService, ExecutableAgentServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import ArtifactRegistry, LocalJobExecutionBackend, create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


BRIEF = {
    "项目名称": "匿名发布会",
    "项目区域文件名": "匿名发布会-发布会脚本.xlsx",
    "脚本类型": "发布会",
    "品牌或客户": "匿名品牌",
    "行业或活动类别": "消费科技发布会",
    "视频时长": "60s",
    "风格调性": "克制、清晰",
    "必须出现的内容": "品牌标识、产品、发布仪式",
    "禁忌或不能出现的内容": "不虚构参数",
    "素材目录或素材文件": "",
    "备注": "测试 Brief",
}


class FakeExecutionBackend:
    def __init__(self):
        self.start_count = 0
        self.cancel_count = 0
        self.inspect_passed = True
        self.jobs = {}
        self.review_ready = True
        self.publication_calls = 0
        self.evidence = None

    def inspect_action(self, action, parameters, brief_snapshot):
        return {
            "passed": self.inspect_passed,
            "calls_models": action in {"start_generation", "retry_stage", "fork_revision"},
            "checks": [{"name": "匿名预检", "status": "pass" if self.inspect_passed else "fail"}],
            "error_code": None if self.inspect_passed else "INVALID_INPUT",
            "message": "通过" if self.inspect_passed else "匿名预检失败",
        }

    def start_action(self, action, parameters, brief_snapshot):
        self.start_count += 1
        job_id = f"job_{self.start_count:08d}"
        snapshot = {
            "job_id": job_id,
            "state": "running",
            "message": "真实 fake backend 正在运行",
            "run_id": "",
            "active_stage": {"stage_id": "input_freeze", "status": "running"},
            "stages": [
                {"stage_id": "input_freeze", "status": "running", "attempt": 1},
                {"stage_id": "script_plan", "status": "not_started", "attempt": 0},
            ],
            "progress": {"completed": 0, "total": 2, "percent": 0},
            "report": None,
        }
        self.jobs[job_id] = snapshot
        return dict(snapshot)

    def get_job(self, job_id):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return dict(self.jobs[job_id])

    def cancel_job(self, job_id):
        snapshot = self.get_job(job_id)
        if snapshot["state"] in {"running", "queued", "cancelling"}:
            self.cancel_count += 1
            snapshot.update(state="cancelled", message="本次运行已停止")
            self.jobs[job_id] = snapshot
        return dict(snapshot)

    def review_readiness(self, run_id):
        return {
            "type": "review_readiness",
            "title": "人工审核资格",
            "summary": "机器审核通过，当前仍明确等待人工审核。" if self.review_ready else "机器审核未通过。",
            "run_id": run_id,
            "ready": self.review_ready,
            "machine_audit_status": "pass" if self.review_ready else "fail",
            "human_review_status": "pending",
            "checks": [],
            "next_steps": ["准备人工复核包。"],
        }

    def prepare_human_review(self, run_id):
        if not self.review_ready:
            raise WorkspaceServiceError("REVIEW_REQUIRED", "机器审核未通过")
        return {"artifact_ref": f"artifact://review/{run_id}/anonymous", "sha256": "a" * 64}

    def finalized_artifacts(self, snapshot):
        return dict(self.evidence) if self.evidence else None


class ExecutableFixture:
    def __init__(self, root: Path, *, backend=None, create_project=True):
        self.root = root
        self.store = WorkspaceStore(root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="本机管理员",
            confirmed=True,
            token="owner-token-for-e03-tests",
        )
        self.token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.versions = VersionService(self.store)
        self.briefs = BriefArtifactStore(root / "data")
        self.backend = backend or FakeExecutionBackend()
        readonly = ReadonlyAgentService(
            ReadonlyAgentServices(
                store=self.store,
                identity=self.identity,
                projects=self.projects,
                tasks=self.tasks,
                versions=self.versions,
                artifacts=RunArtifactResolver(root / "runs"),
            )
        )
        self.agent = ExecutableAgentService(
            ExecutableAgentServices(
                readonly=readonly,
                projects=self.projects,
                tasks=self.tasks,
                confirmations=ActionConfirmationService(self.store, b"c" * 32),
                idempotency=ToolIdempotencyService(self.store),
                brief_store=self.briefs,
                backend=self.backend,
            )
        )
        self.project = None
        self.session = None
        if create_project:
            onboarded = self.agent.onboard_project(
                self.token,
                workspace_id=self.workspace_id,
                expected_workspace_version=0,
                brief=BRIEF,
                category_id="launch",
                confirmed=True,
            )
            self.project = onboarded["project"]
            self.session = self.agent.create_session(
                self.token, self.project["project_id"]
            )["session"]

    def propose(self, message="请开始生成一版脚本"):
        return self.agent.submit_message(
            self.token,
            self.session["agent_session_id"],
            content=message,
        )

    def confirm(self, proposal):
        return self.agent.confirm_action(
            self.token,
            proposal["task"]["task_id"],
            proposal_digest=proposal["response"]["proposal_digest"],
        )

    def bind_verified_run(self, run_id="run_1234567890abcdef"):
        run_dir = self.root / "runs" / "anonymous" / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "category_id": "launch",
                    "status": "waiting_human_review",
                    "stages": {
                        "machine_audit": {"status": "succeeded", "attempt": 1},
                        "human_review": {
                            "status": "waiting_human_review",
                            "attempt": 1,
                        },
                    },
                    "artifacts": {},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        project = self.projects.get_project(self.token, self.project["project_id"])
        bound = self.projects.bind_run(
            self.token,
            project_id=self.project["project_id"],
            run_id=run_id,
            source_kind="native",
            manifest_ref=f"artifact://run/{run_id}/manifest",
            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            expected_project_version=project["project_version"],
        )
        self.project["project_version"] = bound["project_version"]
        return bound


class ExecutableAgentTests(unittest.TestCase):
    def test_public_task_events_hide_internal_confirmation_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            proposal = fixture.propose()
            task = fixture.tasks.get_task(
                fixture.token, proposal["task"]["task_id"]
            )
            fixture.tasks.transition_task(
                fixture.token,
                task_id=task["task_id"],
                expected_task_version=task["task_version"],
                status="waiting_confirmation",
                details={
                    "response": {"type": "safe_public_response"},
                    "internal_formal_parameters": {
                        "evidence_path": "/private/customer/review.xlsx",
                        "evidence_sha256": "a" * 64,
                    },
                },
            )

            public_events = fixture.agent.list_task_events(
                fixture.token, task["task_id"]
            )
            internal_events = fixture.tasks.list_task_events(
                fixture.token, task["task_id"]
            )

            self.assertNotIn("internal_formal_parameters", json.dumps(public_events))
            self.assertNotIn("/private/customer/review.xlsx", json.dumps(public_events))
            self.assertIn("internal_formal_parameters", json.dumps(internal_events))

    def test_first_project_onboarding_freezes_real_brief_and_replays(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp), create_project=False)

            first = fixture.agent.onboard_project(
                fixture.token,
                workspace_id=fixture.workspace_id,
                expected_workspace_version=0,
                brief=BRIEF,
                category_id="launch",
                confirmed=True,
            )
            second = fixture.agent.onboard_project(
                fixture.token,
                workspace_id=fixture.workspace_id,
                expected_workspace_version=999,
                brief=BRIEF,
                category_id="launch",
                confirmed=True,
            )

            self.assertFalse(first["replayed"])
            self.assertTrue(second["replayed"])
            self.assertEqual(first["boundary"], "bootstrap_only")
            self.assertFalse(first["ordinary_agent_tool"])
            self.assertEqual(first["project"]["project_id"], second["project"]["project_id"])
            snapshot = fixture.projects.get_current_brief_snapshot(
                fixture.token, first["project"]["project_id"]
            )
            self.assertTrue(snapshot["brief_ref"].startswith("artifact://brief/"))
            self.assertEqual(fixture.briefs.read(snapshot["brief_ref"], snapshot["brief_sha256"]), BRIEF)
            with fixture.store.connection() as connection:
                stored = json.dumps(dict(connection.execute("SELECT * FROM project_brief_snapshots").fetchone()))
            self.assertNotIn(str(Path(tmp)), stored)

    def test_unauthorized_onboarding_cannot_write_brief_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp), create_project=False)

            with self.assertRaises(WorkspaceServiceError) as caught:
                fixture.agent.onboard_project(
                    "invalid-owner-token",
                    workspace_id=fixture.workspace_id,
                    expected_workspace_version=0,
                    brief=BRIEF,
                    category_id="launch",
                    confirmed=True,
                )

            self.assertEqual(caught.exception.code, "AUTH_SESSION_INVALID")
            self.assertFalse(fixture.briefs.root.exists())

    def test_proposal_does_not_execute_and_describes_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))

            result = fixture.propose()

            self.assertEqual(fixture.backend.start_count, 0)
            self.assertEqual(result["task"]["status"], "proposed")
            self.assertEqual(result["response"]["action"], "start_generation")
            self.assertIn("不会覆盖旧 run", result["response"]["will_not_change"])
            self.assertTrue(result["response"]["calls_models"])

    def test_dismissed_proposal_and_ambiguous_request_never_execute(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            proposal = fixture.propose()

            dismissed = fixture.agent.dismiss_proposal(
                fixture.token, proposal["task"]["task_id"]
            )
            ambiguous = fixture.propose("帮我处理一下")

            self.assertEqual(dismissed["task"]["status"], "cancelled")
            self.assertEqual(ambiguous["task"]["status"], "needs_clarification")
            self.assertEqual(fixture.backend.start_count, 0)

    def test_retry_and_revision_are_deterministic_proposals(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            fixture.bind_verified_run()

            retry = fixture.propose("重试 machine_audit 节点")
            revision = fixture.propose("修改第1镜文案，保留品牌事实")
            image_without_shot = fixture.propose("修改整片图片")

            self.assertEqual(retry["response"]["action"], "retry_stage")
            self.assertEqual(retry["response"]["parameters"]["stage_id"], "machine_audit")
            self.assertEqual(revision["task"]["status"], "blocked")
            self.assertEqual(revision["task"]["error_code"], "VERSION_DELIVERY_UNAVAILABLE")
            self.assertEqual(image_without_shot["task"]["status"], "blocked")
            self.assertEqual(image_without_shot["task"]["error_code"], "VERSION_DELIVERY_UNAVAILABLE")
            self.assertEqual(fixture.backend.start_count, 0)

    def test_one_confirmation_starts_once_and_replay_returns_same_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            proposal = fixture.propose()

            first = fixture.confirm(proposal)
            current = fixture.projects.get_project(
                fixture.token, fixture.project["project_id"]
            )
            fixture.projects.rename_project(
                fixture.token,
                project_id=fixture.project["project_id"],
                name="匿名发布会-运行中",
                expected_project_version=current["project_version"],
            )
            second = fixture.confirm(proposal)

            self.assertEqual(fixture.backend.start_count, 1)
            self.assertEqual(first["task"]["task_id"], second["task"]["task_id"])
            self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])
            with fixture.store.connection() as connection:
                confirmations = connection.execute(
                    "SELECT status FROM action_confirmations"
                ).fetchall()
                idempotency = connection.execute(
                    "SELECT status FROM tool_idempotency_records"
                ).fetchall()
            self.assertEqual([row["status"] for row in confirmations], ["consumed"])
            self.assertEqual([row["status"] for row in idempotency], ["accepted"])

    def test_digest_change_and_stale_project_version_never_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            proposal = fixture.propose()
            with self.assertRaises(WorkspaceServiceError) as caught:
                fixture.agent.confirm_action(
                    fixture.token,
                    proposal["task"]["task_id"],
                    proposal_digest="0" * 64,
                )
            self.assertEqual(caught.exception.code, "INVALID_INPUT")
            fixture.projects.rename_project(
                fixture.token,
                project_id=fixture.project["project_id"],
                name="匿名发布会-已变化",
                expected_project_version=fixture.project["project_version"],
            )

            stale = fixture.confirm(proposal)

            self.assertEqual(stale["task"]["status"], "blocked")
            self.assertEqual(stale["task"]["error_code"], "PROJECT_VERSION_CONFLICT")
            self.assertEqual(fixture.backend.start_count, 0)

    def test_run_evidence_drift_after_proposal_never_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            binding = fixture.bind_verified_run()
            proposal = fixture.propose("重试 machine_audit 节点")
            manifest = (
                fixture.root
                / "runs"
                / "anonymous"
                / "runs"
                / binding["run_id"]
                / "run_manifest.json"
            )
            manifest.write_text('{"run_id":"tampered"}', encoding="utf-8")

            blocked = fixture.confirm(proposal)

            self.assertEqual(blocked["task"]["status"], "blocked")
            self.assertEqual(blocked["task"]["error_code"], "ARTIFACT_HASH_MISMATCH")
            self.assertEqual(fixture.backend.start_count, 0)

    def test_failed_real_preflight_blocks_before_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = FakeExecutionBackend()
            backend.inspect_passed = False
            fixture = ExecutableFixture(Path(tmp), backend=backend)

            result = fixture.propose()

            self.assertEqual(result["task"]["status"], "blocked")
            self.assertEqual(result["task"]["error_code"], "INVALID_INPUT")
            self.assertEqual(backend.start_count, 0)

    def test_production_backend_refuses_unresolvable_e01_brief_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backend = LocalJobExecutionBackend(
                jobs=None,
                artifacts=ArtifactRegistry(),
                resolver=RunArtifactResolver(root / "runs"),
                brief_store=BriefArtifactStore(root / "data"),
                runtime_config={"data_root": str(root / "data")},
            )

            with self.assertRaises(WorkspaceServiceError) as caught:
                backend.inspect_action(
                    "start_generation",
                    {},
                    {
                        "brief_ref": f"artifact://brief/{'a' * 64}",
                        "brief_sha256": "a" * 64,
                    },
                )

            self.assertEqual(caught.exception.code, "BRIEF_ARTIFACT_UNAVAILABLE")

    def test_progress_is_deduplicated_and_cancel_uses_controlled_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            running = fixture.confirm(fixture.propose())
            target_id = running["task"]["task_id"]
            job_id = running["job"]["job_id"]
            before = fixture.tasks.list_task_events(fixture.token, target_id)
            fixture.agent.sync_task(fixture.token, target_id)
            self.assertEqual(
                len(fixture.tasks.list_task_events(fixture.token, target_id)), len(before)
            )
            fixture.backend.jobs[job_id]["stages"][0]["status"] = "succeeded"
            fixture.backend.jobs[job_id]["stages"][1]["status"] = "running"
            fixture.backend.jobs[job_id]["active_stage"] = {
                "stage_id": "script_plan", "status": "running"
            }
            fixture.agent.sync_task(fixture.token, target_id)
            self.assertEqual(
                len(fixture.tasks.list_task_events(fixture.token, target_id)), len(before) + 1
            )

            cancel = fixture.propose("取消当前运行")
            cancelled = fixture.confirm(cancel)

            self.assertEqual(cancel["response"]["action"], "cancel_task")
            self.assertEqual(cancelled["task"]["status"], "completed")
            self.assertEqual(fixture.backend.cancel_count, 1)
            self.assertEqual(fixture.tasks.get_task(fixture.token, target_id)["status"], "cancelled")
            with fixture.store.connection() as connection:
                tools = [row[0] for row in connection.execute("SELECT tool_name FROM tool_idempotency_records ORDER BY created_at")]
            self.assertEqual(tools, ["start_generation", "cancel_task"])

    def test_restart_never_claims_lost_process_is_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = ExecutableFixture(root)
            running = fixture.confirm(fixture.propose())
            store = WorkspaceStore(root / "workspace.sqlite3")
            projects = ProjectService(store)
            tasks = TaskService(store)
            readonly = ReadonlyAgentService(
                ReadonlyAgentServices(
                    store=store,
                    identity=LocalIdentityAdapter(store),
                    projects=projects,
                    tasks=tasks,
                    versions=VersionService(store),
                    artifacts=RunArtifactResolver(root / "runs"),
                )
            )
            restarted = ExecutableAgentService(
                ExecutableAgentServices(
                    readonly=readonly,
                    projects=projects,
                    tasks=tasks,
                    confirmations=ActionConfirmationService(store, b"c" * 32),
                    idempotency=ToolIdempotencyService(store),
                    brief_store=fixture.briefs,
                    backend=FakeExecutionBackend(),
                )
            )

            restored = restarted.get_session(
                fixture.token, fixture.session["agent_session_id"]
            )

            task = next(item for item in restored["tasks"] if item["task_id"] == running["task"]["task_id"])
            self.assertEqual(task["status"], "blocked")
            self.assertEqual(task["error_code"], "RECOVERY_RECONCILIATION_REQUIRED")

    def test_terminal_job_binds_run_and_creates_immutable_version_before_review_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            running = fixture.confirm(fixture.propose())
            job_id = running["job"]["job_id"]
            run_id = "run_new1234567890abcdef"
            fixture.backend.jobs[job_id].update(
                state="waiting_review",
                message="机器审核通过，等待人工复核",
                run_id=run_id,
                report={
                    "machine_audit_status": "pass",
                    "human_review_status": "pending",
                },
            )
            fixture.backend.evidence = {
                "run_id": run_id,
                "parent_run_id": "",
                "manifest_ref": f"artifact://run/{run_id}/manifest",
                "manifest_sha256": "b" * 64,
                "artifact_ref": f"artifact://run/{run_id}/workbook",
                "content_sha256": "c" * 64,
            }

            result = fixture.agent.sync_task(
                fixture.token, running["task"]["task_id"]
            )

            self.assertEqual(result["task"]["status"], "waiting_human_review")
            binding = fixture.projects.get_run_binding(fixture.token, run_id)
            self.assertEqual(binding["project_id"], fixture.project["project_id"])
            versions = fixture.projects.list_project_versions(
                fixture.token, fixture.project["project_id"]
            )
            self.assertEqual(len(versions), 1)
            self.assertEqual(versions[0]["run_id"], run_id)
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0], 0)

    def test_review_readiness_is_read_only_and_prepare_never_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            fixture.bind_verified_run()

            readiness = fixture.agent.submit_message(
                fixture.token,
                fixture.session["agent_session_id"],
                content="是否可以人工审核？",
                run_id="run_1234567890abcdef",
            )
            proposal = fixture.agent.submit_message(
                fixture.token,
                fixture.session["agent_session_id"],
                content="准备人工复核包",
                run_id="run_1234567890abcdef",
            )
            prepared = fixture.confirm(proposal)

            self.assertEqual(readiness["task"]["status"], "completed")
            self.assertTrue(readiness["response"]["ready"])
            self.assertEqual(proposal["task"]["status"], "proposed")
            self.assertEqual(prepared["task"]["status"], "waiting_human_review")
            self.assertTrue(prepared["task"]["result_ref"].startswith("artifact://review/"))
            self.assertEqual(fixture.backend.publication_calls, 0)
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0], 0)

    def test_http_confirm_requires_csrf_and_agent_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            proposal = fixture.propose()
            client = TestClient(create_app(agent_api=fixture.agent))
            token = client.app.state.session_token
            path = f"/api/agent/tasks/{proposal['task']['task_id']}/confirm"
            body = {"proposal_digest": proposal["response"]["proposal_digest"]}

            self.assertEqual(client.post(path, json=body).status_code, 403)
            self.assertEqual(
                client.post(path, json=body, headers={"X-MPlus-UI-Token": token}).status_code,
                401,
            )
            response = client.post(
                path,
                json=body,
                headers={
                    "X-MPlus-UI-Token": token,
                    "X-MPlus-Agent-Auth": fixture.token,
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(fixture.backend.start_count, 1)

    def test_channel_isolation_rejects_forged_manual_r3_proposals_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = ExecutableFixture(Path(tmp))
            current = fixture.projects.get_project(fixture.token, fixture.project["project_id"])
            manual_actions = (
                "record_version_review", "select_candidate_version", "publish_version",
                "publish_asset_import", "approve_knowledge",
            )
            for index, action in enumerate(manual_actions):
                parameters = {
                    "project_id": fixture.project["project_id"],
                    "expected_project_version": current["project_version"],
                    "idempotency_key": f"forged-r3-{index:08d}",
                }
                task = fixture.tasks.create_task(
                    fixture.token,
                    agent_session_id=fixture.session["agent_session_id"],
                    task_type="forged_manual_r3",
                    payload_sha256=hashlib.sha256(action.encode()).hexdigest(),
                    expected_project_version=current["project_version"],
                )
                digest = hashlib.sha256(json.dumps(
                    {"action": action, "parameters": parameters},
                    sort_keys=True, separators=(",", ":"),
                ).encode()).hexdigest()
                understanding = fixture.tasks.transition_task(
                    fixture.token,
                    task_id=task["task_id"],
                    expected_task_version=task["task_version"],
                    status="understanding",
                    details={},
                )
                fixture.tasks.transition_task(
                    fixture.token,
                    task_id=task["task_id"],
                    expected_task_version=understanding["task_version"],
                    status="proposed",
                    details={"response": {
                        "type": "action_proposal", "action": action,
                        "parameters": parameters, "proposal_digest": digest,
                    }},
                )
                with self.subTest(action=action), self.assertRaises(WorkspaceServiceError):
                    fixture.agent.confirm_action(
                        fixture.token, task["task_id"], proposal_digest=digest
                    )
            self.assertEqual(fixture.backend.start_count, 0)
            self.assertEqual(fixture.backend.publication_calls, 0)
            with fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM action_confirmations").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM tool_idempotency_records").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
