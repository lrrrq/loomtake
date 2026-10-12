from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.alpha_console import (
    AlphaConsole,
    AlphaIsolationError,
    AlphaRuntime,
    create_alpha_app,
)
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentService, ExecutableAgentServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


BRIEF = {
    "项目名称": "匿名 Alpha 项目",
    "脚本类型": "发布会",
    "视频时长": "60s",
    "风格调性": "清晰",
    "必须出现的内容": "匿名产品",
}
ALPHA_BRIEF_REF = f"artifact://alpha-brief/{'a' * 64}"
ALPHA_BRIEF_SHA256 = "a" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Backend:
    def __init__(self):
        self.start_count = 0

    def inspect_action(self, action, parameters, brief_snapshot):
        return {"passed": True, "calls_models": False, "checks": []}

    def start_action(self, action, parameters, brief_snapshot):
        self.start_count += 1
        return {"job_id": "job_alpha_0001", "state": "running", "progress": {}}

    def get_job(self, job_id):
        return {"job_id": job_id, "state": "running", "progress": {}}

    def cancel_job(self, job_id):
        return {"job_id": job_id, "state": "cancelled", "progress": {}}

    def review_readiness(self, run_id):
        return {"ready": False, "run_id": run_id, "summary": "Alpha 不导出复核包"}

    def prepare_human_review(self, run_id):
        raise AssertionError("T05 不负责导出")

    def finalized_artifacts(self, snapshot):
        return None


class AlphaConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-alpha-console-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "isolated"
        self.runtime = AlphaRuntime.isolated(self.root, port=18765)
        store = WorkspaceStore(self.runtime.data_root / "workspace.sqlite3")
        identity = LocalIdentityAdapter(store)
        owner = identity.bootstrap_owner(
            workspace_name="匿名 Alpha 工作区",
            display_name="匿名审核人",
            confirmed=True,
            token="alpha-owner-token",
        )
        self.token = owner["auth_token"]
        self.actor_id = owner["actor_id"]
        self.workspace_id = owner["workspace_id"]
        readonly = ReadonlyAgentService(
            ReadonlyAgentServices(
                store=store,
                identity=identity,
                projects=ProjectService(store),
                tasks=TaskService(store),
                versions=VersionService(store),
                artifacts=RunArtifactResolver(self.runtime.runs_root),
            )
        )
        self.backend = _Backend()
        self.agent = ExecutableAgentService(
            ExecutableAgentServices(
                readonly=readonly,
                projects=readonly.services.projects,
                tasks=readonly.services.tasks,
                confirmations=ActionConfirmationService(store, b"a" * 32),
            idempotency=ToolIdempotencyService(store),
            brief_store=BriefArtifactStore(self.runtime.data_root),
            backend=self.backend,
            delivery=VersionDeliveryService(store, RunArtifactResolver(self.runtime.runs_root)),
            )
        )
        self.reader_calls: list[tuple[str, str, str]] = []

        def verified_brief_reader(project_id: str, brief_ref: str, expected_sha256: str) -> dict:
            self.reader_calls.append((project_id, brief_ref, expected_sha256))
            if project_id != self.project_id or brief_ref != ALPHA_BRIEF_REF or expected_sha256 != ALPHA_BRIEF_SHA256:
                raise WorkspaceServiceError("ALPHA_BRIEF_REJECTED", "T01 已确认 Brief 引用、项目或 SHA 不匹配")
            return {"项目名称": "匿名 Alpha 项目", "确认状态": "confirmed"}

        self.verified_brief_reader = verified_brief_reader
        self.console = AlphaConsole(
            self.runtime, self.agent, verified_brief_reader=self.verified_brief_reader
        )
        self.client = TestClient(create_alpha_app(self.console))
        self.headers = {"X-MPlus-Agent-Auth": self.token}

    def _upstream_project(self):
        created = self.agent.services.projects.create_project(
            self.token,
            workspace_id=self.workspace_id,
            expected_workspace_version=0,
            name=BRIEF["项目名称"],
            category_id="launch",
            brief_ref=ALPHA_BRIEF_REF,
            brief_sha256=ALPHA_BRIEF_SHA256,
        )
        self.project_id = created["project_id"]
        return created, {"brief_ref": ALPHA_BRIEF_REF, "brief_sha256": ALPHA_BRIEF_SHA256}

    @staticmethod
    def _ref_payload(frozen: dict) -> dict:
        return {
            "brief_ref": frozen["brief_ref"],
            "expected_sha256": frozen["brief_sha256"],
        }

    def _project_with_verified_waiting_run(self):
        created, frozen = self._upstream_project()
        run_id = "run_alpha_12345678"
        run_dir = self.runtime.runs_root / "anonymous" / "runs" / run_id
        run_dir.mkdir(parents=True)
        rows = [
            {"镜号": "1", "画面描述": "匿名开场"},
            {"镜号": "2", "画面描述": "匿名展示"},
            {"镜号": "3", "画面描述": "匿名收束"},
        ]
        details = [
            {"模板镜号": str(index), "source_refs": [{
                "source_file": "anonymous-source.xlsx",
                "source_sheet": "脚本",
                "source_row": index + 1,
            }]}
            for index in range(1, 4)
        ]
        stage_paths = {}
        for stage, schema_version in (
            ("script_plan", "mplus-script-plan/v2"),
            ("script_draft", "mplus-script-draft/v1"),
            ("visual_binding", "mplus-visual-binding/v1"),
        ):
            payload = {
                "schema_version": schema_version,
                "script_rows": rows,
                "detail_rows": details,
            }
            if stage == "visual_binding":
                payload["bindings"] = [
                    {"shot_no": str(index), "image_id": f"anonymous-{index}"}
                    for index in range(1, 4)
                ]
            path = run_dir / f"{stage}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            stage_paths[stage] = path
        workbook = run_dir / "anonymous-workbook.xlsx"
        workbook.write_bytes(b"anonymous current immutable version")
        report = run_dir / "run_report.json"
        report.write_text(json.dumps({
            "machine_audit_status": "pass",
            "human_review_status": "pending",
            "blocking_items": [{
                "shot_no": 3,
                "code": "copy_needs_review",
                "message": "第 3 镜文案需要人工确认",
            }],
        }), encoding="utf-8")
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(json.dumps({
            "run_id": run_id,
            "status": "waiting_human_review",
            "stages": {"human_review": {"status": "waiting_human_review", "attempt": 1}},
            "artifacts": {
                "run_report": {"path": str(report), "sha256": _sha(report), "size_bytes": report.stat().st_size},
                "workbook": {"path": str(workbook), "sha256": _sha(workbook), "size_bytes": workbook.stat().st_size},
            },
        }), encoding="utf-8")
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_payload["stages"].update({
            stage: {
                "stage_id": stage,
                "status": "succeeded",
                "output_refs": [str(path)],
                "output_sha256": _sha(path),
            }
            for stage, path in stage_paths.items()
        })
        manifest.write_text(json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        projects = self.agent.services.projects
        bound = projects.bind_run(
            self.token, project_id=created["project_id"], run_id=run_id,
            source_kind="native", manifest_ref=f"artifact://run/{run_id}/manifest",
            manifest_sha256=_sha(manifest), expected_project_version=created["project_version"],
        )
        version = projects.create_project_version(
            self.token, project_id=created["project_id"], run_id=run_id,
            content_sha256=_sha(workbook),
            artifact_ref=f"artifact://run/{run_id}/workbook",
            expected_project_version=bound["project_version"],
        )
        return created, frozen, version

    def test_rejects_real_mplus_root_and_service_outside_runtime(self):
        with self.assertRaises(AlphaIsolationError):
            AlphaRuntime.isolated(Path.home() / ".mplus", port=18765)
        unsafe = AlphaRuntime.isolated(self.root / "other", port=18766)
        with self.assertRaises(AlphaIsolationError):
            AlphaConsole(unsafe, self.agent, verified_brief_reader=self.verified_brief_reader)

    def test_status_and_dashboard_use_persistent_state_after_reopen(self):
        project, frozen, _version = self._project_with_verified_waiting_run()
        entered = self.console.enter_project(
            self.token, project_id=project["project_id"], **self._ref_payload(frozen)
        )
        self.assertTrue(entered["brief"]["adapted"])
        session = self.console.create_session(self.token, project_id=project["project_id"], **self._ref_payload(frozen))["session"]
        first = self.console.submit_message(
            self.token, agent_session_id=session["agent_session_id"], content="现在到哪了？"
        )
        reopened = AlphaConsole(
            self.runtime, self.agent, verified_brief_reader=self.verified_brief_reader
        )
        dashboard = reopened.project_dashboard(
            self.token, project_id=project["project_id"], agent_session_id=session["agent_session_id"]
        )
        self.assertEqual(reopened.status()["production_certification"], "BLOCK")
        self.assertEqual(dashboard["blocker"]["code"], "WAITING_HUMAN_REVIEW")
        self.assertEqual(dashboard["timeline"][-1]["task_id"], first["task"]["task_id"])
        self.assertNotIn(str(self.runtime.root), json.dumps(dashboard, ensure_ascii=False))

    def test_high_risk_message_is_rejected_and_whole_script_confirmation_never_publishes(self):
        project, frozen, version = self._project_with_verified_waiting_run()
        session = self.console.create_session(self.token, project_id=project["project_id"], **self._ref_payload(frozen))["session"]
        with self.assertRaises(WorkspaceServiceError) as blocked:
            self.console.submit_message(
                self.token, agent_session_id=session["agent_session_id"], content="请正式发布当前版本"
            )
        self.assertEqual(blocked.exception.code, "ALPHA_HIGH_RISK_FORBIDDEN")
        confirmed = self.console.confirm_whole_script(
            self.token,
            project_id=project["project_id"],
            version_id=version["version_id"],
            evidence_ref="artifact://review/alpha/whole-script",
            expected_project_version=version["project_version"],
        )
        self.assertEqual(confirmed["review"]["decision"], "quality_pass")
        self.assertEqual(confirmed["publication"], "not_created")
        reviews = self.agent.services.readonly.services.versions.list_reviews(
            self.token, version["version_id"]
        )
        self.assertEqual(reviews[-1]["reviewer_actor_id"], self.actor_id)
        self.assertTrue(reviews[-1]["created_at"])
        self.assertEqual(reviews[-1]["version_id"], version["version_id"])
        self.assertEqual(reviews[-1]["evidence_ref"], "artifact://review/alpha/whole-script")
        with self.agent.services.readonly.services.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0], 0)
        self.assertFalse((self.runtime.data_root / "CURRENT").exists())

    def test_generation_and_specified_shot_change_require_explicit_confirmation(self):
        project, frozen, _version = self._project_with_verified_waiting_run()
        session = self.console.create_session(self.token, project_id=project["project_id"], **self._ref_payload(frozen))["session"]
        proposal = self.console.submit_message(
            self.token,
            agent_session_id=session["agent_session_id"],
            content="请开始生成一版脚本",
        )
        self.assertEqual(proposal["task"]["status"], "proposed")
        self.assertEqual(proposal["response"]["type"], "action_proposal")
        running = self.console.confirm_low_risk_action(
            self.token,
            task_id=proposal["task"]["task_id"],
            proposal_digest=proposal["response"]["proposal_digest"],
        )
        self.assertEqual(running["task"]["status"], "executing")
        self.assertEqual(running["job"]["job_id"], "job_alpha_0001")
        revision = self.console.submit_message(
            self.token,
            agent_session_id=session["agent_session_id"],
            content="把第 3 镜文案修改为匿名新品，其他字段保持不变",
        )
        self.assertEqual(revision["task"]["status"], "proposed")
        self.assertEqual(revision["response"]["action"], "fork_revision")
        self.assertNotIn("delivery_context", revision["response"]["parameters"])

    def test_api_has_fixed_dual_status_and_no_publication_endpoint(self):
        first_screen = self.client.get("/")
        self.assertEqual(first_screen.status_code, 200)
        self.assertIn("Alpha：执行中", first_screen.text)
        self.assertIn("生产认证：BLOCK", first_screen.text)
        status = self.client.get("/api/alpha/status")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["alpha"], "执行中")
        self.assertEqual(status.json()["production_certification"], "BLOCK")
        self.assertEqual(
            self.client.post("/api/alpha/publications", headers=self.headers).status_code,
            404,
        )

    def test_problem_shot_confirmation_persists_actor_time_version_and_evidence(self):
        project, frozen, version = self._project_with_verified_waiting_run()
        session = self.console.create_session(
            self.token, project_id=project["project_id"], **self._ref_payload(frozen)
        )["session"]
        projection = self.client.get(
            f"/api/alpha/projects/{project['project_id']}/problem-shots", headers=self.headers
        )
        self.assertEqual(projection.status_code, 200)
        self.assertEqual(projection.json()["items"][0]["shot_no"], "3")
        confirmed = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/problem-shot-confirmations",
            headers=self.headers,
            json={
                "agent_session_id": session["agent_session_id"],
                "shot_no": "3",
                "decision": "needs_revision",
                "evidence_ref": "artifact://review/alpha/shot-3",
                "expected_project_version": version["project_version"],
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        body = confirmed.json()
        self.assertEqual(body["actor_id"], self.actor_id)
        self.assertEqual(body["task"]["task_type"], "problem_shot_human_confirmation")
        self.assertEqual(body["task"]["result_ref"], "artifact://review/alpha/shot-3")
        self.assertEqual(body["publication"], "not_created")
        self.assertEqual(body["current"], "not_written")
        events = self.agent.services.tasks.list_task_events(self.token, body["task"]["task_id"])
        self.assertTrue(events[-1]["created_at"])
        self.assertEqual(events[-1]["details"]["shot_no"], "3")
        self.assertEqual(events[-1]["details"]["project_version_before"], version["project_version"])

    def test_problem_shot_keeps_optimistic_lock_until_dashboard_refresh(self):
        project, frozen, version = self._project_with_verified_waiting_run()
        session = self.console.create_session(
            self.token, project_id=project["project_id"], **self._ref_payload(frozen)
        )["session"]
        self.agent.services.readonly.services.versions.record_review(
            self.token,
            version_id=version["version_id"],
            decision="quality_pass",
            evidence_ref="artifact://review/alpha/version-advanced",
            expected_project_version=version["project_version"],
        )
        stale = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/problem-shot-confirmations",
            headers=self.headers,
            json={
                "agent_session_id": session["agent_session_id"], "shot_no": "3",
                "decision": "accepted_with_note", "evidence_ref": "artifact://review/alpha/stale",
                "expected_project_version": version["project_version"],
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "PROJECT_VERSION_CONFLICT")
        dashboard = self.client.get(
            f"/api/alpha/projects/{project['project_id']}", headers=self.headers
        )
        self.assertEqual(dashboard.status_code, 200)
        refreshed = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/problem-shot-confirmations",
            headers=self.headers,
            json={
                "agent_session_id": session["agent_session_id"], "shot_no": "3",
                "decision": "accepted_with_note", "evidence_ref": "artifact://review/alpha/refreshed",
                "expected_project_version": dashboard.json()["project"]["project_version"],
            },
        )
        self.assertEqual(refreshed.status_code, 200)

    def test_t01_ref_and_reader_rejections_create_nothing(self):
        project, frozen = self._upstream_project()
        baseline = {"projects": 1, "sessions": 0, "tasks": 0}

        def counts():
            with self.agent.services.readonly.services.store.connection() as connection:
                return {
                    "projects": connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
                    "sessions": connection.execute("SELECT COUNT(*) FROM agent_sessions").fetchone()[0],
                    "tasks": connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                }

        naked = self.client.post(
            "/api/alpha/projects",
            headers=self.headers,
            json={"brief": BRIEF, "confirmed": True, "workspace_id": self.workspace_id},
        )
        self.assertEqual(naked.status_code, 405)
        entered = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/enter",
            headers=self.headers,
            json=self._ref_payload(frozen),
        )
        self.assertEqual(entered.status_code, 200)
        self.assertEqual(self.reader_calls[-1], (project["project_id"], ALPHA_BRIEF_REF, ALPHA_BRIEF_SHA256))
        fake_bool = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/enter",
            headers=self.headers,
            json={"brief": BRIEF, "confirmed": True},
        )
        self.assertEqual(fake_bool.status_code, 409)
        self.assertEqual(fake_bool.json()["detail"]["code"], "BRIEF_REFERENCE_INVALID")
        wrong_ref = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/sessions",
            headers=self.headers,
            json={"brief_ref": f"artifact://wrong/{'b' * 64}", "expected_sha256": ALPHA_BRIEF_SHA256},
        )
        self.assertEqual(wrong_ref.status_code, 409)
        self.assertEqual(wrong_ref.json()["detail"]["code"], "VERIFIED_BRIEF_UNAVAILABLE")
        wrong_sha = self.client.post(
            f"/api/alpha/projects/{project['project_id']}/sessions",
            headers=self.headers,
            json={"brief_ref": frozen["brief_ref"], "expected_sha256": "0" * 64},
        )
        self.assertEqual(wrong_sha.status_code, 409)
        self.assertEqual(wrong_sha.json()["detail"]["code"], "VERIFIED_BRIEF_UNAVAILABLE")
        unavailable = AlphaConsole(
            self.runtime,
            self.agent,
            verified_brief_reader=lambda *_args: (_ for _ in ()).throw(OSError("fixture unavailable")),
        )
        unavailable_client = TestClient(create_alpha_app(unavailable))
        reader_failure = unavailable_client.post(
            f"/api/alpha/projects/{project['project_id']}/sessions",
            headers=self.headers,
            json=self._ref_payload(frozen),
        )
        self.assertEqual(reader_failure.status_code, 409)
        self.assertEqual(reader_failure.json()["detail"]["code"], "VERIFIED_BRIEF_UNAVAILABLE")
        self.assertEqual(counts(), baseline)
        self.assertEqual(self.backend.start_count, 0)


if __name__ == "__main__":
    unittest.main()
