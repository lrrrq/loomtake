from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentService, ExecutableAgentServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import ArtifactRegistry, JobManager, LocalJobExecutionBackend
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


BRIEF = {
    "项目名称": "匿名修订项目",
    "项目区域文件名": "匿名修订项目-发布会脚本.xlsx",
    "脚本类型": "发布会",
    "品牌或客户": "匿名品牌",
    "行业或活动类别": "匿名活动",
    "视频时长": "60s",
    "风格调性": "清晰",
    "必须出现的内容": "匿名产品",
    "禁忌或不能出现的内容": "无",
    "素材目录或素材文件": "",
    "备注": "确定性测试",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _FinishedProcess:
    stdout = ["deterministic child revision finished\n"]

    def poll(self):
        return 3

    def wait(self):
        return 3


class DeliveryRevisionFixture:
    def __init__(self, root: Path):
        self.root = root
        self.data_root = root / "data"
        self.runs_root = root / "runs"
        self.cache = self.runs_root / "anonymous"
        self.store = WorkspaceStore(self.data_root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="delivery-revision-owner",
        )
        self.token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.resolver = RunArtifactResolver(self.runs_root)
        self.delivery = VersionDeliveryService(self.store, self.resolver)
        self.registry = ArtifactRegistry()
        self.jobs = JobManager(self.registry)
        self.backend = LocalJobExecutionBackend(
            self.jobs,
            self.registry,
            self.resolver,
            BriefArtifactStore(self.data_root),
            {
                "data_root": str(self.data_root),
                "runs_root": str(self.runs_root),
                "exports_root": str(root / "exports"),
                "source_root": str(root / "source"),
                "model_profile": "current",
            },
        )
        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=self.identity,
            projects=self.projects,
            tasks=self.tasks,
            versions=VersionService(self.store),
            artifacts=self.resolver,
        ))
        self.agent = ExecutableAgentService(ExecutableAgentServices(
            readonly=readonly,
            projects=self.projects,
            tasks=self.tasks,
            confirmations=ActionConfirmationService(self.store, b"r" * 32),
            idempotency=ToolIdempotencyService(self.store),
            brief_store=BriefArtifactStore(self.data_root),
            backend=self.backend,
            delivery=self.delivery,
        ))
        onboarded = self.agent.onboard_project(
            self.token,
            workspace_id=self.workspace_id,
            expected_workspace_version=0,
            brief=BRIEF,
            category_id="launch",
            confirmed=True,
        )
        self.project = onboarded["project"]
        self.parent_run_id = "run_parent_0001"
        self.parent_version, self.parent_dir = self._create_parent_version()
        self.session = self.agent.create_session(
            self.token, self.project["project_id"]
        )["session"]
        self.popen_calls = 0
        self.child_manifest_sha = ""

    @staticmethod
    def _rows(marker: str) -> tuple[list[dict], list[dict], list[dict]]:
        rows = [
            {"镜号": "1", "结构": "开场", "画面描述": marker, "声音": "音乐起"},
            {"镜号": "2", "结构": "展开", "画面描述": "匿名展示", "声音": "环境声"},
            {"镜号": "3", "结构": "收束", "画面描述": "匿名落版", "声音": "音乐收束"},
        ]
        details = [
            {"模板镜号": str(index), "source_refs": [{
                "source_file": "anonymous-source.xlsx",
                "source_sheet": "脚本",
                "source_row": index + 1,
            }]}
            for index in range(1, 4)
        ]
        bindings = [{"shot_no": str(index), "image_id": f"image-{index}"} for index in range(1, 4)]
        return rows, details, bindings

    def _write_run(self, run_id: str, marker: str, *, parent_run_id: str = "") -> Path:
        directory = self.cache / "runs" / run_id
        directory.mkdir(parents=True)
        rows, details, bindings = self._rows(marker)
        stage_paths = {}
        for stage, schema in (
            ("script_plan", "mplus-script-plan/v2"),
            ("script_draft", "mplus-script-draft/v1"),
            ("visual_binding", "mplus-visual-binding/v1"),
        ):
            payload = {"schema_version": schema, "script_rows": rows, "detail_rows": details}
            if stage == "visual_binding":
                payload["bindings"] = bindings
            path = directory / f"{stage}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            stage_paths[stage] = path
        workbook = directory / "anonymous-version.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.title = "脚本"
        sheet.append(["镜号", "结构", "画面描述", "声音"])
        for row in rows:
            sheet.append([row["镜号"], row["结构"], row["画面描述"], row["声音"]])
        book.save(workbook)
        report = directory / "run_report.json"
        report.write_text(json.dumps({
            "run_id": run_id,
            "run_manifest_path": str(directory / "run_manifest.json"),
            "output_path": str(workbook),
            "exit_code": 3,
            "machine_audit_status": "pass",
            "human_review_status": "pending",
        }, ensure_ascii=False), encoding="utf-8")
        manifest = directory / "run_manifest.json"
        manifest.write_text(json.dumps({
            "run_id": run_id,
            "parent_run_id": parent_run_id,
            "artifacts": {
                name: {"path": str(path), "sha256": _sha(path)}
                for name, path in {"workbook": workbook, "run_report": report}.items()
            },
            "stages": {
                stage: {
                    "stage_id": stage,
                    "status": "succeeded",
                    "output_refs": [str(path)],
                    "output_sha256": _sha(path),
                }
                for stage, path in stage_paths.items()
            },
        }, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return directory

    def _create_parent_version(self) -> tuple[dict, Path]:
        directory = self._write_run(self.parent_run_id, "匿名旧开场")
        manifest = directory / "run_manifest.json"
        project = self.projects.get_project(self.token, self.project["project_id"])
        bound = self.projects.bind_run(
            self.token,
            project_id=self.project["project_id"],
            run_id=self.parent_run_id,
            source_kind="native",
            manifest_ref=f"artifact://run/{self.parent_run_id}/manifest",
            manifest_sha256=_sha(manifest),
            expected_project_version=project["project_version"],
        )
        version = self.projects.create_project_version(
            self.token,
            project_id=self.project["project_id"],
            run_id=self.parent_run_id,
            content_sha256=_sha(directory / "anonymous-version.xlsx"),
            artifact_ref=f"artifact://run/{self.parent_run_id}/workbook",
            expected_project_version=bound["project_version"],
        )
        return version, directory

    def advance_current_version(self) -> dict:
        run_id = "run_replacement_0003"
        directory = self._write_run(
            run_id, "匿名替代开场", parent_run_id=self.parent_run_id
        )
        manifest = directory / "run_manifest.json"
        project = self.projects.get_project(self.token, self.project["project_id"])
        bound = self.projects.bind_run(
            self.token,
            project_id=self.project["project_id"],
            run_id=run_id,
            source_kind="native",
            manifest_ref=f"artifact://run/{run_id}/manifest",
            manifest_sha256=_sha(manifest),
            expected_project_version=project["project_version"],
        )
        return self.projects.create_project_version(
            self.token,
            project_id=self.project["project_id"],
            run_id=run_id,
            content_sha256=_sha(directory / "anonymous-version.xlsx"),
            artifact_ref=f"artifact://run/{run_id}/workbook",
            expected_project_version=bound["project_version"],
            parent_version_id=self.parent_version["version_id"],
        )

    def popen(self, *_args, **_kwargs):
        self.popen_calls += 1
        child = self._write_run(
            "run_child_0002", "匿名新版开场", parent_run_id=self.parent_run_id
        )
        self.child_manifest_sha = _sha(child / "run_manifest.json")
        return _FinishedProcess()

    def propose(self):
        return self.agent.submit_message(
            self.token,
            self.session["agent_session_id"],
            content="把第1镜文案改为：匿名新版开场",
        )


class ExecutableAgentDeliveryTests(unittest.TestCase):
    def test_authority_field_mapping_keeps_copy_and_subtitles_in_visual_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = DeliveryRevisionFixture(Path(tmp))
            with patch("mvp_v2.local_ui.preflight_payload") as preflight:
                preflight.return_value = type("Passed", (), {"passed": True, "checks": ()})()
                for message, expected in (
                    ("把第1镜文案改为：匿名新文案", "画面描述"),
                    ("把第1镜字幕改为：匿名新字幕", "画面描述"),
                    ("把第1镜旁白改为：匿名新旁白", "声音"),
                ):
                    result = fixture.agent.submit_message(
                        fixture.token, fixture.session["agent_session_id"], content=message
                    )
                    self.assertEqual(result["task"]["status"], "proposed")
                    self.assertEqual(
                        result["response"]["parameters"]["modification_plan"]["target_field"],
                        expected,
                    )

    def test_confirmed_current_context_starts_real_jobmanager_child_and_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = DeliveryRevisionFixture(Path(tmp))
            with patch("mvp_v2.local_ui.preflight_payload") as preflight, patch(
                "mvp_v2.local_ui.project_cache_dir", return_value=fixture.cache
            ), patch("mvp_v2.local_ui.subprocess.Popen", side_effect=fixture.popen):
                preflight.return_value = type("Passed", (), {"passed": True, "checks": ()})()
                proposal = fixture.propose()
                self.assertEqual(proposal["task"]["status"], "proposed")
                self.assertEqual(fixture.popen_calls, 0)
                params = proposal["response"]["parameters"]
                self.assertEqual(params["parent_run_id"], fixture.parent_run_id)
                self.assertNotIn("delivery_context", params)
                self.assertNotIn("internal_delivery_binding", json.dumps(proposal["events"]))
                proposed_event = next(
                    item
                    for item in fixture.tasks.list_task_events(
                        fixture.token, proposal["task"]["task_id"]
                    )
                    if item["status"] == "proposed"
                )
                binding = proposed_event["details"]["internal_delivery_binding"]
                self.assertEqual(binding["run_id"], fixture.parent_run_id)
                self.assertNotIn(str(fixture.runs_root), json.dumps(binding))
                running = fixture.agent.confirm_action(
                    fixture.token,
                    proposal["task"]["task_id"],
                    proposal_digest=proposal["response"]["proposal_digest"],
                )
                for _ in range(100):
                    result = fixture.agent.sync_task(fixture.token, running["task"]["task_id"])
                    if result["task"]["status"] != "executing":
                        break
                    time.sleep(0.01)

            self.assertEqual(fixture.popen_calls, 1)
            self.assertEqual(result["task"]["status"], "waiting_human_review")
            project = fixture.projects.get_project(fixture.token, fixture.project["project_id"])
            versions = fixture.projects.list_project_versions(fixture.token, fixture.project["project_id"])
            self.assertEqual(len(versions), 2)
            child = versions[-1]
            self.assertEqual(project["current_version_id"], child["version_id"])
            self.assertEqual(child["parent_version_id"], fixture.parent_version["version_id"])
            self.assertEqual(
                _sha(fixture.cache / "runs" / "run_child_0002" / "run_manifest.json"),
                fixture.child_manifest_sha,
            )
            workbook = fixture.cache / "runs" / "run_child_0002" / "anonymous-version.xlsx"
            self.assertEqual(load_workbook(workbook)["脚本"]["C2"].value, "匿名新版开场")
            report_path = fixture.data_root / "revision_reports" / f"{child['version_id']}.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["n_chain"], {
                "parent_shot_count": 3, "child_shot_count": 3,
                "parent_adjacent_review_count": 2, "child_adjacent_review_count": 2,
            })
            actual = report["revision_report"]["actual_impact"]
            self.assertEqual(actual["changed_shot_nos"], ["1"])
            self.assertEqual(actual["changed_fields"], ["画面描述"])
            self.assertFalse(actual["unexpected_fields"])
            self.assertFalse(actual["unexpected_shot_nos"])
            self.assertTrue(report["revision_report"]["locked_field_verification"]["all_preserved"])
            self.assertEqual(report["revision_report"]["completion"]["status"], "waiting_human_review")
            self.assertTrue(result["task"]["result_ref"].startswith("artifact://revision/"))
            self.assertEqual(result["task"]["result_ref"].rsplit("/", 1)[-1], _sha(report_path))

    def test_context_stage_drift_after_proposal_never_starts_jobmanager(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = DeliveryRevisionFixture(Path(tmp))
            with patch("mvp_v2.local_ui.preflight_payload") as preflight:
                preflight.return_value = type("Passed", (), {"passed": True, "checks": ()})()
                proposal = fixture.propose()
            stage = fixture.parent_dir / "visual_binding.json"
            stage.write_text("{}", encoding="utf-8")
            blocked = fixture.agent.confirm_action(
                fixture.token,
                proposal["task"]["task_id"],
                proposal_digest=proposal["response"]["proposal_digest"],
            )
            self.assertEqual(blocked["task"]["status"], "blocked")
            self.assertEqual(blocked["task"]["error_code"], "ARTIFACT_HASH_MISMATCH")
            self.assertEqual(fixture.popen_calls, 0)

    def test_stale_project_lock_after_proposal_never_starts_jobmanager(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = DeliveryRevisionFixture(Path(tmp))
            with patch("mvp_v2.local_ui.preflight_payload") as preflight:
                preflight.return_value = type("Passed", (), {"passed": True, "checks": ()})()
                proposal = fixture.propose()
            project = fixture.projects.get_project(fixture.token, fixture.project["project_id"])
            fixture.projects.rename_project(
                fixture.token,
                project_id=fixture.project["project_id"],
                name="匿名修订项目-已变化",
                expected_project_version=project["project_version"],
            )
            blocked = fixture.agent.confirm_action(
                fixture.token,
                proposal["task"]["task_id"],
                proposal_digest=proposal["response"]["proposal_digest"],
            )
            self.assertEqual(blocked["task"]["status"], "blocked")
            self.assertEqual(blocked["task"]["error_code"], "PROJECT_VERSION_CONFLICT")
            self.assertEqual(fixture.popen_calls, 0)

    def test_current_version_drift_after_proposal_never_starts_jobmanager(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = DeliveryRevisionFixture(Path(tmp))
            with patch("mvp_v2.local_ui.preflight_payload") as preflight:
                preflight.return_value = type("Passed", (), {"passed": True, "checks": ()})()
                proposal = fixture.propose()
            replacement = fixture.advance_current_version()
            blocked = fixture.agent.confirm_action(
                fixture.token,
                proposal["task"]["task_id"],
                proposal_digest=proposal["response"]["proposal_digest"],
            )
            self.assertEqual(blocked["task"]["status"], "blocked")
            self.assertEqual(blocked["task"]["error_code"], "PROJECT_VERSION_CONFLICT")
            current = fixture.projects.get_project(
                fixture.token, fixture.project["project_id"]
            )
            self.assertEqual(current["current_version_id"], replacement["version_id"])
            self.assertEqual(fixture.popen_calls, 0)

    def test_cross_project_parent_run_is_rejected_before_proposal(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = DeliveryRevisionFixture(Path(tmp))
            workspace = fixture.projects.get_workspace(fixture.token, fixture.workspace_id)
            other = fixture.projects.create_project(
                fixture.token,
                workspace_id=fixture.workspace_id,
                name="匿名其他项目",
                category_id="launch",
                expected_workspace_version=workspace["workspace_version"],
            )
            session = fixture.agent.create_session(fixture.token, other["project_id"])["session"]
            with self.assertRaises(WorkspaceServiceError) as caught:
                fixture.agent.submit_message(
                    fixture.token,
                    session["agent_session_id"],
                    content="把第1镜文案改为：跨项目修改",
                    run_id=fixture.parent_run_id,
                )
            self.assertEqual(caught.exception.code, "PERMISSION_DENIED")
            self.assertEqual(fixture.popen_calls, 0)

    def test_forged_client_delivery_context_cannot_be_confirmed_or_started(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = DeliveryRevisionFixture(Path(tmp))
            with patch("mvp_v2.local_ui.preflight_payload") as preflight:
                preflight.return_value = type("Passed", (), {"passed": True, "checks": ()})()
                proposal = fixture.propose()
            forged = {
                **proposal["response"]["parameters"],
                "delivery_context": {"version_id": "forged-context"},
            }
            with self.assertRaises(WorkspaceServiceError) as caught:
                fixture.agent.services.confirmations.issue(
                    fixture.token,
                    agent_session_id=fixture.session["agent_session_id"],
                    tool_name="fork_revision",
                    parameters=forged,
                )
            self.assertEqual(caught.exception.code, "INVALID_INPUT")
            self.assertEqual(fixture.popen_calls, 0)
