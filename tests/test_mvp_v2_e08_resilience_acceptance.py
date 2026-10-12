from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.asset_center import AssetCenterService, AssetImportFile
from mvp_v2.asset_package import create_asset_package
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentService, ExecutableAgentServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.local_ui import create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


ANONYMOUS_BRIEF = {
    "项目名称": "匿名 E08 发布会",
    "项目区域文件名": "匿名-E08-发布会脚本.xlsx",
    "脚本类型": "发布会",
    "品牌或客户": "匿名品牌",
    "行业或活动类别": "消费科技发布会",
    "视频时长": "60s",
    "风格调性": "克制、清晰",
    "必须出现的内容": "品牌标识、产品、发布仪式",
    "禁忌或不能出现的内容": "不虚构参数",
    "素材目录或素材文件": "",
    "备注": "E08 匿名验收",
}


E08_EXTERNAL_MANUAL_BLOCKERS = (
    {
        "id": "real_second_computer",
        "automated": False,
        "evidence_required": "在另一台实体电脑复制受管 data_root，启动应用并核对项目、任务、Brief、知识和权限。",
    },
    {
        "id": "two_representative_humans",
        "automated": False,
        "evidence_required": "两名代表用户在无开发人员现场指导下完成导入、生成、审核和发布旅程并记录观察。",
    },
    {
        "id": "paid_provider_journey",
        "automated": False,
        "evidence_required": "经单独成本批准后调用真实付费模型，保留账单、模型标识和运行审计证据。",
    },
)


class AnonymousExecutionBackend:
    """只模拟不可持久化的 OS job 句柄，不调用任何模型。"""

    def __init__(self):
        self.start_count = 0
        self.jobs: dict[str, dict] = {}
        self.evidence: dict | None = None

    def inspect_action(self, action, parameters, brief_snapshot):
        return {
            "passed": True,
            "calls_models": action in {"start_generation", "retry_stage", "fork_revision"},
            "checks": [{"name": "E08 匿名预检", "status": "pass"}],
        }

    def start_action(self, action, parameters, brief_snapshot):
        self.start_count += 1
        job_id = f"job_e08_{self.start_count:04d}"
        snapshot = {
            "job_id": job_id,
            "state": "running",
            "message": "匿名 fake job 正在运行",
            "run_id": "",
            "active_stage": {"stage_id": "input_freeze", "status": "running"},
            "stages": [
                {"stage_id": "input_freeze", "status": "running", "attempt": 1},
                {"stage_id": "machine_audit", "status": "not_started", "attempt": 0},
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
        snapshot.update(state="cancelled", message="匿名 fake job 已停止")
        self.jobs[job_id] = snapshot
        return dict(snapshot)

    def review_readiness(self, run_id):
        return {
            "ready": True,
            "run_id": run_id,
            "machine_audit_status": "pass",
            "human_review_status": "pending",
            "summary": "机器审核通过，仍等待人工审核。",
        }

    def prepare_human_review(self, run_id):
        return {"artifact_ref": f"artifact://review/{run_id}/anonymous"}

    def finalized_artifacts(self, snapshot):
        return dict(self.evidence) if self.evidence else None


def _readonly_service(data_root: Path) -> ReadonlyAgentService:
    store = WorkspaceStore(data_root / "workspace.sqlite3")
    return ReadonlyAgentService(
        ReadonlyAgentServices(
            store=store,
            identity=LocalIdentityAdapter(store),
            projects=ProjectService(store),
            tasks=TaskService(store),
            versions=VersionService(store),
            artifacts=RunArtifactResolver(data_root / "runs"),
        )
    )


def _executable_service(
    data_root: Path,
    backend: AnonymousExecutionBackend,
    *,
    signing_key: bytes = b"e" * 32,
) -> ExecutableAgentService:
    readonly = _readonly_service(data_root)
    return ExecutableAgentService(
        ExecutableAgentServices(
            readonly=readonly,
            projects=readonly.services.projects,
            tasks=readonly.services.tasks,
            confirmations=ActionConfirmationService(readonly.services.store, signing_key),
            idempotency=ToolIdempotencyService(readonly.services.store),
            brief_store=BriefArtifactStore(data_root),
            backend=backend,
        )
    )


def _database_text(database_path: Path) -> str:
    values: list[str] = []
    with sqlite3.connect(database_path) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = [
                row[1]
                for row in connection.execute(f'SELECT * FROM pragma_table_info("{table}")')
                if str(row[2]).upper() in {"TEXT", ""}
            ]
            if not columns:
                continue
            selected = ",".join(f'"{column}"' for column in columns)
            for row in connection.execute(f'SELECT {selected} FROM "{table}"'):
                values.extend(str(value) for value in row if value is not None)
    return "\n".join(values)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_source_workbook(path: Path) -> None:
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    template.cell(5, 1, "结构")
    template.cell(5, 2, "镜号")
    for column, value in enumerate(
        ["画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"],
        3,
    ):
        template.cell(6, column, value)
    template.cell(7, 1, "开场造势")
    template.cell(7, 2, 1)
    template.cell(7, 5, "转场")
    template.cell(7, 6, "实拍")
    template.cell(7, 7, "全景")
    template.cell(7, 8, "推进")
    template.cell(7, 9, "匿名镜头")
    creative = workbook.create_sheet("创意库")
    creative.cell(5, 1, "镜号")
    creative.cell(5, 2, "结构")
    for column, value in enumerate(
        ["画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
        4,
    ):
        creative.cell(6, column, value)
    creative.cell(7, 1, "C001")
    creative.cell(7, 2, "开场造势")
    creative.cell(7, 5, "转场")
    creative.cell(7, 6, "实拍")
    creative.cell(7, 7, "全景")
    creative.cell(7, 8, "推进")
    creative.cell(7, 9, "匿名创意")
    workbook.save(path)
    workbook.close()


def _workbook_bytes() -> bytes:
    stream = BytesIO()
    workbook = Workbook()
    workbook.active.title = "匿名素材"
    workbook.active.append(["匿名编号", "说明"])
    workbook.active.append(["E08-001", "仅导入工作簿，不含新增图片"])
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


class E08ResilienceAcceptanceTests(unittest.TestCase):
    def test_data_root_copy_recovers_workspace_brief_task_events_and_knowledge(self):
        with tempfile.TemporaryDirectory(prefix="mplus-e08-relocation-") as temporary:
            sandbox = Path(temporary)
            source_root = sandbox / "first-computer" / "data"
            second_root = sandbox / "second-computer" / "data"
            store = WorkspaceStore(source_root / "workspace.sqlite3")
            identity = LocalIdentityAdapter(store)
            owner = identity.bootstrap_owner(
                workspace_name="E08 匿名工作区",
                display_name="匿名管理员",
                confirmed=True,
                token="e08-relocation-owner-token",
            )
            token = owner["auth_token"]
            projects = ProjectService(store)
            briefs = BriefArtifactStore(source_root)
            frozen = briefs.freeze(ANONYMOUS_BRIEF)
            project = projects.create_project(
                token,
                workspace_id=owner["workspace_id"],
                name=ANONYMOUS_BRIEF["项目名称"],
                category_id="launch",
                expected_workspace_version=0,
                brief_ref=frozen["brief_ref"],
                brief_sha256=frozen["brief_sha256"],
            )
            readonly = _readonly_service(source_root)
            session = readonly.create_session(token, project["project_id"])["session"]
            message = readonly.submit_message(
                token,
                session["agent_session_id"],
                content="现在项目到哪了？",
            )
            knowledge = KnowledgeService(store)
            candidate = knowledge.create_candidate(
                token,
                workspace_id=owner["workspace_id"],
                project_id=project["project_id"],
                scope="project",
                target_id=project["project_id"],
                statement="匿名项目发布前必须由人工确认。",
                evidence_refs=["artifact://knowledge/e08/anonymous-evidence"],
                expected_knowledge_version=0,
            )
            approved = knowledge.approve_candidate(
                token,
                workspace_id=owner["workspace_id"],
                project_id=project["project_id"],
                candidate_id=candidate["candidate_id"],
                level="project",
                review_evidence="E08 匿名人工批准证据",
                expected_knowledge_version=candidate["knowledge_version"],
            )
            with store.connection() as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

            shutil.copytree(source_root, second_root)
            relocated = _readonly_service(second_root)
            relocated_projects = relocated.services.projects
            relocated_brief = relocated_projects.get_current_brief_snapshot(
                token, project["project_id"]
            )
            relocated_knowledge = KnowledgeService(relocated.services.store)
            effective = relocated_knowledge.list_effective_rules(
                token, project["project_id"]
            )
            app = create_app(agent_api=relocated)
            with TestClient(app) as client:
                restored_session = client.get(
                    f"/api/agent/sessions/{session['agent_session_id']}",
                    headers={"x-mplus-agent-auth": token},
                )
                restored_events = client.get(
                    f"/api/agent/tasks/{message['task']['task_id']}/events",
                    headers={"x-mplus-agent-auth": token},
                )

            self.assertEqual(restored_session.status_code, 200)
            self.assertEqual(restored_events.status_code, 200)
            self.assertEqual(
                restored_session.json()["session"]["project_id"], project["project_id"]
            )
            self.assertEqual(
                [event["status"] for event in restored_events.json()["items"]],
                ["received", "executing", "completed"],
            )
            self.assertEqual(
                BriefArtifactStore(second_root).read(
                    relocated_brief["brief_ref"], relocated_brief["brief_sha256"]
                ),
                ANONYMOUS_BRIEF,
            )
            self.assertEqual(effective["rules"][0]["rule_id"], approved["rule_id"])
            self.assertFalse(effective["pending_candidates_included"])
            self.assertNotIn(
                str(source_root.resolve()),
                _database_text(second_root / "workspace.sqlite3"),
            )

    def test_service_restart_reports_lost_job_handle_without_duplicate_start(self):
        with tempfile.TemporaryDirectory(prefix="mplus-e08-restart-") as temporary:
            data_root = Path(temporary) / "data"
            store = WorkspaceStore(data_root / "workspace.sqlite3")
            identity = LocalIdentityAdapter(store)
            owner = identity.bootstrap_owner(
                workspace_name="E08 重启工作区",
                display_name="匿名管理员",
                confirmed=True,
                token="e08-restart-owner-token",
            )
            frozen = BriefArtifactStore(data_root).freeze(ANONYMOUS_BRIEF)
            project = ProjectService(store).create_project(
                owner["auth_token"],
                workspace_id=owner["workspace_id"],
                name=ANONYMOUS_BRIEF["项目名称"],
                category_id="launch",
                expected_workspace_version=0,
                brief_ref=frozen["brief_ref"],
                brief_sha256=frozen["brief_sha256"],
            )
            original_backend = AnonymousExecutionBackend()
            original = _executable_service(data_root, original_backend)
            session = original.create_session(
                owner["auth_token"], project["project_id"]
            )["session"]
            proposal = original.submit_message(
                owner["auth_token"],
                session["agent_session_id"],
                content="请开始生成一版脚本",
            )
            running = original.confirm_action(
                owner["auth_token"],
                proposal["task"]["task_id"],
                proposal_digest=proposal["response"]["proposal_digest"],
            )

            restarted_backend = AnonymousExecutionBackend()
            restarted = _executable_service(data_root, restarted_backend)
            restored = restarted.get_session(
                owner["auth_token"], session["agent_session_id"]
            )
            restored_again = restarted.get_session(
                owner["auth_token"], session["agent_session_id"]
            )
            task = next(
                item
                for item in restored["tasks"]
                if item["task_id"] == running["task"]["task_id"]
            )

            self.assertEqual(original_backend.start_count, 1)
            self.assertEqual(restarted_backend.start_count, 0)
            self.assertEqual(task["status"], "blocked")
            self.assertEqual(task["error_code"], "RECOVERY_RECONCILIATION_REQUIRED")
            self.assertEqual(
                restored_again["current_task"]["error_code"],
                "RECOVERY_RECONCILIATION_REQUIRED",
            )
            with store.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM tool_idempotency_records WHERE tool_name='start_generation'"
                    ).fetchone()[0],
                    1,
                )

    def test_editor_and_reviewer_complete_controlled_journey_with_human_gate(self):
        with tempfile.TemporaryDirectory(prefix="mplus-e08-journey-") as temporary:
            data_root = Path(temporary) / "data"
            store = WorkspaceStore(data_root / "workspace.sqlite3")
            identity = LocalIdentityAdapter(store)
            owner = identity.bootstrap_owner(
                workspace_name="E08 旅程工作区",
                display_name="匿名管理员",
                confirmed=True,
                token="e08-journey-owner-token",
            )
            editor = identity.create_actor(
                owner["auth_token"],
                workspace_id=owner["workspace_id"],
                display_name="匿名项目人员",
                auth_source="local",
                role="editor",
                expected_workspace_version=0,
            )
            reviewer = identity.create_actor(
                owner["auth_token"],
                workspace_id=owner["workspace_id"],
                display_name="匿名审核人员",
                auth_source="local",
                role="reviewer",
                expected_workspace_version=editor["workspace_version"],
            )
            editor_token = identity.issue_auth_session(
                actor_id=editor["actor_id"], token="e08-editor-token"
            )["auth_token"]
            reviewer_token = identity.issue_auth_session(
                actor_id=reviewer["actor_id"], token="e08-reviewer-token"
            )["auth_token"]
            briefs = BriefArtifactStore(data_root)
            frozen = briefs.freeze(ANONYMOUS_BRIEF)
            projects = ProjectService(store)
            project = projects.create_project(
                editor_token,
                workspace_id=owner["workspace_id"],
                name=ANONYMOUS_BRIEF["项目名称"],
                category_id="launch",
                expected_workspace_version=reviewer["workspace_version"],
                brief_ref=frozen["brief_ref"],
                brief_sha256=frozen["brief_sha256"],
            )
            backend = AnonymousExecutionBackend()
            agent = _executable_service(data_root, backend)
            session = agent.create_session(editor_token, project["project_id"])["session"]
            proposal = agent.submit_message(
                editor_token,
                session["agent_session_id"],
                content="请开始生成一版脚本",
            )
            self.assertEqual(backend.start_count, 0)
            running = agent.confirm_action(
                editor_token,
                proposal["task"]["task_id"],
                proposal_digest=proposal["response"]["proposal_digest"],
            )
            job_id = running["job"]["job_id"]
            run_id = "run_e08_1234567890"
            backend.jobs[job_id].update(
                state="waiting_review",
                message="机器审核通过，等待人工复核",
                run_id=run_id,
                report={
                    "machine_audit_status": "pass",
                    "human_review_status": "pending",
                },
            )
            backend.evidence = {
                "run_id": run_id,
                "parent_run_id": "",
                "manifest_ref": f"artifact://run/{run_id}/manifest",
                "manifest_sha256": _sha("e08-manifest"),
                "artifact_ref": f"artifact://run/{run_id}/workbook",
                "content_sha256": _sha("e08-workbook"),
            }
            waiting = agent.sync_task(editor_token, running["task"]["task_id"])
            version = projects.list_project_versions(
                editor_token, project["project_id"]
            )[0]
            versions = VersionService(store)
            current = projects.get_project(editor_token, project["project_id"])

            with self.assertRaises(WorkspaceServiceError) as editor_candidate:
                versions.select_candidate(
                    editor_token,
                    version_id=version["version_id"],
                    expected_project_version=current["project_version"],
                    reason="编辑者不得代替审核者选候选",
                )
            self.assertEqual(editor_candidate.exception.code, "PERMISSION_DENIED")
            candidate = versions.select_candidate(
                reviewer_token,
                version_id=version["version_id"],
                expected_project_version=current["project_version"],
                reason="审核者选择 E08 匿名候选",
            )
            with self.assertRaises(WorkspaceServiceError) as unreviewed:
                versions.publish_version(
                    reviewer_token,
                    version_id=version["version_id"],
                    review_id="review_missing_e08",
                    expected_project_version=candidate["project_version"],
                )
            self.assertEqual(unreviewed.exception.code, "REVIEW_REQUIRED")
            with store.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0],
                    0,
                )
            with self.assertRaises(WorkspaceServiceError) as editor_review:
                versions.record_review(
                    editor_token,
                    version_id=version["version_id"],
                    decision="quality_pass",
                    evidence_ref="artifact://review/e08/editor-cannot-approve",
                    expected_project_version=candidate["project_version"],
                )
            self.assertEqual(editor_review.exception.code, "PERMISSION_DENIED")
            review = versions.record_review(
                reviewer_token,
                version_id=version["version_id"],
                decision="quality_pass",
                evidence_ref="artifact://review/e08/anonymous-quality-pass",
                expected_project_version=candidate["project_version"],
            )
            publication = versions.publish_version(
                reviewer_token,
                version_id=version["version_id"],
                review_id=review["review_id"],
                expected_project_version=review["project_version"],
            )

            self.assertEqual(backend.start_count, 1)
            self.assertEqual(waiting["task"]["status"], "waiting_human_review")
            self.assertNotEqual(waiting["task"]["status"], "completed")
            self.assertEqual(publication["status"], "published")
            self.assertEqual(publication["projection_status"], "pending")
            self.assertFalse((data_root / "CURRENT").exists())

    def test_workbook_only_import_makes_zero_visual_provider_calls(self):
        with tempfile.TemporaryDirectory(prefix="mplus-e08-zero-provider-") as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            _write_source_workbook(source / "M+发布会类通用脚本库.xlsx")
            package = create_asset_package(
                root / "package",
                package_id="package_e08_anonymous",
                source_root=source,
                category_id="launch",
            )
            storage = LocalAssetStorage(root / "asset-blobs")
            storage_ref, digest = storage.put(BytesIO(_workbook_bytes()))
            service = AssetCenterService(package, storage=storage)
            workbook = AssetImportFile(
                artifact_ref="artifact://upload/e08/anonymous-workbook.xlsx",
                storage_ref=storage_ref,
                content_sha256=digest,
                filename="anonymous-workbook.xlsx",
                asset_kind="workbook",
            )

            with patch(
                "mvp_v2.image_understanding.generate_minimax_visual_analysis"
            ) as provider:
                staged = service.stage_import(
                    import_id="import_e08_workbook_only",
                    category_id="launch",
                    scope="shared",
                    files=[workbook],
                    requested_by_actor_id="actor_e08_editor",
                    expected_package_version=0,
                )

            self.assertEqual(staged["machine_review_status"], "passed")
            self.assertEqual([item["asset_kind"] for item in staged["items"]], ["workbook"])
            provider.assert_not_called()

    def test_e08_runtime_artifacts_stay_outside_git_and_manual_gaps_are_explicit(self):
        repository = Path(__file__).resolve().parents[1]
        before = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        with tempfile.TemporaryDirectory(prefix="mplus-e08-source-only-") as temporary:
            runtime = Path(temporary) / "runtime" / "runs" / "run_e08_anonymous"
            runtime.mkdir(parents=True)
            (runtime / "workspace.sqlite3").write_bytes(b"anonymous runtime fixture")
            (runtime / "run_manifest.json").write_text(
                json.dumps({"run_id": "run_e08_anonymous"}), encoding="utf-8"
            )
            self.assertFalse(runtime.is_relative_to(repository))
        after = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        tracked = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=repository,
            check=True,
            capture_output=True,
        ).stdout.split(b"\0")
        forbidden = []
        for raw_path in tracked:
            if not raw_path:
                continue
            path = Path(raw_path.decode("utf-8"))
            fixture = path.parts[:2] == ("tests", "fixtures")
            static_document = bool(path.parts) and path.parts[0] in {"各需求文档", "方案"}
            if (
                path.name == ".env"
                or path.suffix.lower() in {".sqlite3", ".db", ".mp4", ".mov"}
                or (
                    not fixture
                    and not static_document
                    and path.suffix.lower() in {".xlsx", ".xlsm", ".png", ".jpg", ".jpeg"}
                )
                or any(part in {"runtime", "runs", "outputs", "cache"} for part in path.parts)
            ):
                forbidden.append(path.as_posix())

        self.assertEqual(before, after)
        self.assertEqual(forbidden, [])
        self.assertEqual(
            {item["id"] for item in E08_EXTERNAL_MANUAL_BLOCKERS},
            {"real_second_computer", "two_representative_humans", "paid_provider_journey"},
        )
        self.assertTrue(all(item["automated"] is False for item in E08_EXTERNAL_MANUAL_BLOCKERS))


if __name__ == "__main__":
    unittest.main()
