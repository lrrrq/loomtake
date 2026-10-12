from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

import uvicorn
from PIL import Image

configured_control_root = os.environ.get("MPLUS_FORMAL_CONTROL_LEGACY_ROOT", "").strip()
control_root = Path(configured_control_root).expanduser().resolve() if configured_control_root else None
if control_root is not None:
    control_root.mkdir(parents=True, exist_ok=True)
fixture_runtime_root = (
    control_root / "runtime"
    if control_root is not None
    else Path(tempfile.mkdtemp(prefix="mplus-ui-runtime-"))
)
fixture_runtime_root.mkdir(parents=True, exist_ok=True)
os.environ["MPLUS_CONFIG"] = str(fixture_runtime_root / "config.json")
os.environ["MPLUS_DATA_ROOT"] = str(fixture_runtime_root / "data")
os.environ["MPLUS_ASSET_PACKAGE"] = str(fixture_runtime_root / "anonymous-asset-package")

from mvp_v2 import local_ui
from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.creative_strategy_artifacts import CreativeStrategyArtifactStore
from mvp_v2.creative_strategy_tools import (
    CreativeStrategyPackageResolver,
    CreativeStrategyToolService,
    CreativeStrategyToolServices,
)
from mvp_v2.executable_agent import ExecutableAgentService, ExecutableAgentServices
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.knowledge_tools import KnowledgeToolService, KnowledgeToolServices
from mvp_v2.local_ui import Job, register_report_artifacts
from mvp_v2.manual_knowledge_commands import (
    ManualKnowledgeApprovalCommandService,
    ManualKnowledgeApprovalCommandServices,
)
from mvp_v2.preflight import CheckResult, PreflightReport
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.ui_governance import UiGovernanceService, UiGovernanceServices
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore

TEST_PORT = int(os.environ.get("MPLUS_UI_TEST_PORT", local_ui.PORT))
local_ui.PORT = TEST_PORT


def fake_preflight(payload):
    return PreflightReport(
        brief=payload.brief.to_brief(),
        project_dir=Path(payload.brief.material_path),
        output_dir=Path(payload.options.output_dir),
        checks=(
            CheckResult("Brief内容", "pass", "字段完整，发布会 60 秒"),
            CheckResult("项目素材目录", "pass", "已找到 73 个图片/视频文件"),
            CheckResult("模板槽位", "pass", "已读取 17 个发布会模板槽位"),
            CheckResult("创意候选", "pass", "已读取 52 个创意候选"),
            CheckResult("视觉AI凭据", "pass", "MiniMax 状态可用"),
            CheckResult("输出目录", "pass", str(payload.options.output_dir)),
        ),
    )


class FakeJobs:
    def __init__(self, artifacts):
        self.artifacts = artifacts
        self.jobs = {}

    def start(self, payload):
        job = Job(job_id=uuid.uuid4().hex, payload=payload, state="running", message="正在生成测试脚本")
        job.started_at = time.time()
        job.logs = ["运行前检查通过", "正在理解项目素材…"]
        root = Path(tempfile.mkdtemp(prefix="mplus-ui-browser-")) / "匿名项目"
        run_id = uuid.uuid4().hex
        run_dir = root / "runs" / run_id
        run_dir.mkdir(parents=True)
        manifest_path = run_dir / "run_manifest.json"
        stages = {
            stage.value: {
                "stage_id": stage.value,
                "status": "running" if index == 0 else "not_started",
                "attempt": 1 if index == 0 else 0,
            }
            for index, stage in enumerate(local_ui.STAGE_ORDER)
        }
        manifest_path.write_text(json.dumps({
            "run_id": run_id,
            "status": "running",
            "stages": stages,
        }), encoding="utf-8")
        candidate = run_dir / "candidate-logo.png"
        Image.new("RGB", (1920, 1080), "white").save(candidate)
        (run_dir / "script_draft.json").write_text(json.dumps({
            "brief": {"项目名称": "本地界面测试发布会", "品牌或客户": "测试品牌"},
            "script_rows": [{"镜号": "1", "画面描述": "测试品牌 logo 标识进入画面"}],
            "detail_rows": [{"slot_index": 1, "模板镜号": "1"}],
        }, ensure_ascii=False), encoding="utf-8")
        (run_dir / "image_manifest.jsonl").write_text(json.dumps({
            "image_id": "img_logo",
            "原图路径": str(candidate),
            "预览路径": str(candidate),
            "来源文件": "测试素材",
            "来源 sheet": "图片库",
            "来源行": 1,
            "是否低清": False,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        (run_dir / "image_analysis.minimax.jsonl").write_text(json.dumps({
            "image_id": "img_logo",
            "caption": "测试品牌 logo 品牌标识特写",
            "tags": ["测试品牌", "logo", "品牌标识"],
            "brand_names": ["测试品牌"],
            "ocr_text": "测试品牌",
            "negative_flags": [],
            "source": "fixture",
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        (run_dir / "visual_binding.json").write_text(json.dumps({
            "bindings": [{"slot_index": 1, "resolution_status": "visual_resolution_required"}],
            "visual_asset_rows": [],
        }), encoding="utf-8")
        job.manifest_path = manifest_path
        job.cache_dir = root
        job.last_output_at = time.time()
        self.jobs[job.job_id] = job
        threading.Thread(target=self._finish, args=(job,), daemon=True).start()
        return job

    def get(self, job_id):
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    def cancel(self, job_id):
        job = self.get(job_id)
        job.state = "cancelled"
        job.message = "本次运行已停止"
        job.finished_at = time.time()
        return job

    def _finish(self, job):
        time.sleep(0.7)
        root = job.manifest_path.parent
        workbook = root / "测试发布会脚本.xlsx"
        workbook.write_bytes(b"fixture workbook")
        review = root / "测试发布会.visual_review.html"
        review.write_text("<h1>visual review</h1>", encoding="utf-8")
        audit = root / "测试发布会.audit.json"
        audit.write_text('{"status":"pass"}', encoding="utf-8")
        report_path = root / "run_report.json"
        report = {
            "project_name": "本地界面测试发布会",
            "project_hint": "ui-fixture",
            "machine_audit_status": "pass",
            "human_review_status": "pending",
            "release_decision": "blocked",
            "exit_code": 3,
            "output_path": str(workbook),
            "visual_review_html": str(review),
            "audit_path": str(audit),
            "shot_count": 17,
            "total_duration_seconds": 60,
            "visual_analysis_coverage": 1.0,
            "score": 100,
            "run_id": json.loads(job.manifest_path.read_text(encoding="utf-8"))["run_id"],
            "run_manifest_path": str(job.manifest_path),
        }
        manifest = json.loads(job.manifest_path.read_text(encoding="utf-8"))
        for stage in local_ui.STAGE_ORDER:
            manifest["stages"][stage.value]["status"] = (
                "waiting_human_review" if stage.value == "human_review"
                else "invalidated" if stage.value == "publish_feedback"
                else "succeeded"
            )
        manifest["status"] = "waiting_human_review"
        job.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        job.report = report
        job.artifacts = register_report_artifacts(self.artifacts, report, report_path)
        job.logs.extend(["质量审计通过", "发布门: machine=pass, human=pending"])
        job.return_code = 3
        job.state = "waiting_review"
        job.message = "机器审核通过，等待人工复核"
        job.finished_at = time.time()


class BrowserExecutionBackend:
    def __init__(self, jobs, brief_store):
        self.jobs = jobs
        self.brief_store = brief_store

    def inspect_action(self, action, parameters, brief_snapshot):
        if action == "prepare_human_review":
            return {"passed": True, "calls_models": False, "checks": []}
        if action == "start_generation":
            self.brief_store.read(
                brief_snapshot["brief_ref"], brief_snapshot["brief_sha256"]
            )
        return {
            "passed": True,
            "calls_models": action in {"start_generation", "retry_stage", "fork_revision"},
            "checks": [{"name": "浏览器 fixture 真实预检", "status": "pass"}],
            "message": "浏览器 fixture 预检通过",
        }

    def start_action(self, action, parameters, brief_snapshot):
        brief = self.brief_store.read(
            brief_snapshot["brief_ref"], brief_snapshot["brief_sha256"]
        )
        payload = local_ui.LaunchPayload(
            brief=local_ui.BriefForm(**local_ui.brief_to_form(brief)),
            options=local_ui.LaunchOptions(),
        )
        return self.jobs.start(payload).snapshot()

    def get_job(self, job_id):
        return self.jobs.get(job_id).snapshot()

    def cancel_job(self, job_id):
        return self.jobs.cancel(job_id).snapshot()

    def review_readiness(self, run_id):
        return {
            "type": "review_readiness",
            "title": "人工审核资格",
            "summary": "机器审核通过，当前仍明确等待人工审核。",
            "run_id": run_id,
            "ready": True,
            "machine_audit_status": "pass",
            "human_review_status": "pending",
            "checks": [],
            "next_steps": ["准备人工复核包。"],
        }

    def prepare_human_review(self, run_id):
        return {"artifact_ref": f"artifact://review/{run_id}/browser-fixture"}

    def finalized_artifacts(self, snapshot):
        return None


class BrowserUiWorkbench:
    def __init__(self, tasks):
        self.tasks = tasks
        self.import_record = None
        self.version_review = None
        self.version_candidate = None
        self.version_publication = None
        self.version_project_version = 4

    def _version_governance(self, version_id):
        is_child = version_id == "version_browser_child"
        review = self.version_review if is_child else None
        candidate = self.version_candidate if is_child else None
        publication = self.version_publication if is_child else None
        return {
            "reviews": [review] if review else [],
            "candidates": [candidate] if candidate else [],
            "publications": [publication] if publication else [],
            "latest_review": review,
            "latest_candidate": candidate,
            "latest_publication": publication,
            "is_current_candidate": bool(candidate),
        }

    def version_tree(self, auth_token, project_id):
        return {
            "project_id": project_id,
            "project_version": self.version_project_version,
            "version_count": 2,
            "roots": [{
                "version_id": "version_browser_parent",
                "project_id": project_id,
                "run_id": "run_browser_parent",
                "parent_version_id": None,
                "content_sha256": "a" * 64,
                "artifact_ref": "artifact://run/run_browser_parent/workbook",
                "created_at": "2026-08-11T10:00:00+00:00",
                "governance": self._version_governance("version_browser_parent"),
                "children": [{
                    "version_id": "version_browser_child",
                    "project_id": project_id,
                    "run_id": "run_browser_child",
                    "parent_version_id": "version_browser_parent",
                    "content_sha256": "b" * 64,
                    "artifact_ref": "artifact://run/run_browser_child/workbook",
                    "created_at": "2026-08-11T10:05:00+00:00",
                    "governance": self._version_governance("version_browser_child"),
                    "children": [],
                }],
            }],
        }

    def compare_versions(self, auth_token, **parameters):
        return {
            "schema_version": "mplus-version-diff/v1",
            "project_id": parameters["project_id"],
            "parent_version_id": parameters["left_version_id"],
            "child_version_id": parameters["right_version_id"],
            "lineage_needs_review": False,
            "lineage_review_items": [],
            "summary": {
                "parent_shot_count": 17,
                "child_shot_count": 17,
                "relation_counts": {"preserved": 17},
                "changed_shot_count": 1,
                "changed_field_count": 1,
            },
            "shot_diffs": [{
                "relation": "preserved",
                "parent_shot_ids": ["shot_browser_01"],
                "parent_shot_nos": ["1"],
                "child_shot_id": "shot_browser_01",
                "child_shot_no": "1",
                "position_changed": False,
                "field_diffs": [{
                    "field": "文案",
                    "status": "changed",
                    "before": "旧版开场",
                    "after": "确认后的新版开场",
                }],
            }],
        }

    def prepare_version_action(self, auth_token, *, agent_session_id, tool_name, parameters):
        project_id = self._project(auth_token, agent_session_id)
        if parameters["project_id"] != project_id:
            raise RuntimeError("fixture project mismatch")
        if parameters["expected_project_version"] != self.version_project_version:
            raise RuntimeError("fixture project version mismatch")
        return {
            "confirmation_id": f"confirmation_browser_{tool_name}",
            "confirmation_token": f"browser-confirmation-{tool_name}-00000000",
            "tool_name": tool_name,
            "parameters_sha256": "e" * 64,
            "expires_at": "2026-08-11T23:59:59+00:00",
            "action": tool_name,
            "risk": "R3",
            "will_change": {
                "record_version_review": "登记 run_report 中已验证的人工审核结论",
                "select_candidate_version": "把目标 version 设为当前项目候选",
                "publish_version": "创建唯一正式发布登记并进入投影等待队列",
            }[tool_name],
            "will_not_change": "不会调用模型，不会修改运行产物，不会写入或更新 CURRENT",
            "calls_models": False,
            "impact_scope": f"当前项目 {project_id} 的目标 version",
        }

    def execute_version_action(self, auth_token, *, agent_session_id, tool_name, parameters, confirmation_token):
        self._project(auth_token, agent_session_id)
        self.version_project_version += 1
        if tool_name == "record_version_review":
            self.version_review = {
                "review_id": "review_browser_fixture",
                "project_id": parameters["project_id"],
                "version_id": parameters["version_id"],
                "decision": "quality_pass",
                "evidence_ref": "artifact://run/run_browser_child/run_report",
                "created_at": "2026-08-11T10:10:00Z",
            }
            business_result = {**self.version_review, "project_version": self.version_project_version}
        elif tool_name == "select_candidate_version":
            self.version_candidate = {
                "candidate_id": "candidate_browser_fixture",
                "project_id": parameters["project_id"],
                "version_id": parameters["version_id"],
                "reason": "Agent 受控动作确认",
                "created_at": "2026-08-11T10:11:00Z",
            }
            business_result = {**self.version_candidate, "project_version": self.version_project_version}
        else:
            self.version_publication = {
                "publication_id": "publication_browser_fixture",
                "project_id": parameters["project_id"],
                "version_id": parameters["version_id"],
                "review_id": parameters["review_id"],
                "status": "published",
                "projection_status": "pending",
                "created_at": "2026-08-11T10:12:00Z",
            }
            business_result = {**self.version_publication, "project_version": self.version_project_version}
        return {
            "tool_name": tool_name,
            "task_id": f"task_browser_{tool_name}",
            "status": "completed",
            "replayed": False,
            "result": business_result,
            "error": None,
        }

    def _project(self, auth_token, agent_session_id):
        return self.tasks.get_agent_session(auth_token, agent_session_id)["project_id"]

    def asset_context(self, auth_token, *, agent_session_id):
        project_id = self._project(auth_token, agent_session_id)
        return {
            "project_id": project_id,
            "category_id": "launch",
            "package_id": "launch-browser-package",
            "package_version": 3,
            "imports": [self.import_record] if self.import_record else [],
            "provider_calls": 0,
        }

    def freeze_asset(self, auth_token, *, agent_session_id, source):
        self._project(auth_token, agent_session_id)
        return {
            "artifact_ref": f"artifact://upload/{'c' * 64}/anonymous.png",
            "storage_ref": f"sha256://{'c' * 64}",
            "content_sha256": "c" * 64,
            "basename": "anonymous.png",
            "kind": "image",
        }

    def prepare_asset_action(self, auth_token, *, agent_session_id, tool_name, parameters):
        self._project(auth_token, agent_session_id)
        return {
            "confirmation_id": "confirmation_browser_fixture",
            "confirmation_token": "browser-confirmation-token-00000000",
            "tool_name": tool_name,
            "parameters_sha256": "d" * 64,
            "expires_at": "2026-08-11T23:59:59+00:00",
            "action": tool_name,
            "risk": "R3" if tool_name == "publish_asset_import" else "R2",
            "calls_models": False,
            "will_not_change": "不会更新 CURRENT，不会覆盖资产包，也不会调用视觉 Provider",
        }

    def execute_asset_action(self, auth_token, *, agent_session_id, tool_name, parameters, confirmation_token):
        project_id = self._project(auth_token, agent_session_id)
        states = {
            "create_asset_import": ("staged", 1, "unavailable"),
            "review_asset_import": ("reviewed", 2, "awaiting_publish"),
            "publish_asset_import": ("published", 3, "available"),
        }
        state, revision, availability = states[tool_name]
        self.import_record = {
            "import_id": "import_browser_fixture",
            "package_id": "launch-browser-package",
            "package_version": 3,
            "category_id": "launch",
            "scope": "project",
            "target_project_id": project_id,
            "state": state,
            "revision": revision,
            "machine_review_status": "passed",
            "last_error_code": "",
            "items": [{
                "position": 1,
                "asset_id": "asset_browser_fixture",
                "asset_kind": "image",
                "artifact_ref": f"artifact://upload/{'c' * 64}/anonymous.png",
                "storage_ref": f"sha256://{'c' * 64}",
                "content_sha256": "c" * 64,
                "filename": "anonymous.png",
                "validation_status": "valid",
                "duplicate_of": "",
                "validation": {"width": 64, "height": 64},
                "availability": availability,
            }],
        }
        return {
            "tool_name": tool_name,
            "task_id": f"task_browser_{tool_name}",
            "status": "completed",
            "replayed": False,
            "result": self.import_record,
            "error": None,
        }

    def prepare_asset_publish(self, auth_token, *, project_id, parameters):
        if (
            not self.import_record
            or self.import_record["target_project_id"] != project_id
            or self.import_record["state"] != "reviewed"
            or parameters.get("import_id") != self.import_record["import_id"]
            or parameters.get("expected_import_revision") != self.import_record["revision"]
        ):
            raise RuntimeError("fixture asset publish state mismatch")
        return {
            "command_token": "browser-manual-publish-token-00000000",
            "parameters": dict(parameters),
            "risk": "R3",
            "expires_at": "2099-08-11T23:59:59+00:00",
            "will_not_change": "不会更新 CURRENT，不会覆盖资产包，也不会调用视觉 Provider",
        }

    def execute_asset_publish(self, auth_token, *, project_id, parameters, command_token):
        if command_token != "browser-manual-publish-token-00000000":
            raise RuntimeError("fixture manual publish token mismatch")
        if (
            not self.import_record
            or self.import_record["target_project_id"] != project_id
            or parameters.get("import_id") != self.import_record["import_id"]
        ):
            raise RuntimeError("fixture asset publish target mismatch")
        self.import_record["state"] = "published"
        self.import_record["revision"] = 3
        self.import_record["items"][0]["availability"] = "available"
        return {
            "tool_name": "publish_asset_import",
            "task_id": "task_browser_publish_asset_import",
            "status": "completed",
            "replayed": False,
            "result": self.import_record,
            "error": None,
        }

    def search_assets(self, auth_token, *, agent_session_id, category_id, query, kind):
        self._project(auth_token, agent_session_id)
        results = [] if not self.import_record or self.import_record["state"] != "published" else [{
            "asset_id": "asset_browser_fixture",
            "asset_kind": "image",
            "title": "anonymous.png",
            "workflow_state": "published",
            "source": {"scope": "project"},
        }]
        return {
            "project_id": self.import_record["target_project_id"] if self.import_record else "",
            "category_id": category_id,
            "package_id": "launch-browser-package",
            "package_version": 3,
            "results": results,
        }


local_ui.preflight_payload = fake_preflight
agent_root = (
    control_root / "agent"
    if control_root is not None
    else Path(tempfile.mkdtemp(prefix="mplus-ui-agent-browser-"))
)
agent_root.mkdir(parents=True, exist_ok=True)
agent_store = WorkspaceStore(agent_root / "workspace.sqlite3")
agent_identity = LocalIdentityAdapter(agent_store)
agent_projects = ProjectService(agent_store)
agent_tasks = TaskService(agent_store)
brief_store = BriefArtifactStore(agent_root / "data")
confirmations = ActionConfirmationService(agent_store, b"b" * 32)
governance_runtime = {
    "data_root": str(agent_root / "data"),
    "workspace_db": str(agent_root / "workspace.sqlite3"),
    "asset_packages": {},
    "asset_package": "",
}
strategy_service = CreativeStrategyToolService(
    CreativeStrategyToolServices(
        store=agent_store,
        projects=agent_projects,
        tasks=agent_tasks,
        confirmations=confirmations,
        idempotency=ToolIdempotencyService(agent_store),
        briefs=brief_store,
        portfolios=CreativeStrategyArtifactStore(agent_root / "data"),
        packages=CreativeStrategyPackageResolver(governance_runtime),
    )
)
knowledge_service = KnowledgeService(agent_store)
governance_service = UiGovernanceService(
    UiGovernanceServices(
        strategies=strategy_service,
        knowledge=KnowledgeToolService(
            KnowledgeToolServices(
                store=agent_store,
                projects=agent_projects,
                knowledge=knowledge_service,
                tasks=agent_tasks,
                confirmations=confirmations,
                idempotency=ToolIdempotencyService(agent_store),
            )
        ),
        identity=agent_identity,
        manual_knowledge_approval=ManualKnowledgeApprovalCommandService(
            ManualKnowledgeApprovalCommandServices(
                store=agent_store,
                projects=agent_projects,
                knowledge=knowledge_service,
            )
        ),
    )
)
agent_api = ReadonlyAgentService(
    ReadonlyAgentServices(
        store=agent_store,
        identity=agent_identity,
        projects=agent_projects,
        tasks=agent_tasks,
        versions=VersionService(agent_store),
        artifacts=RunArtifactResolver(agent_root / "runs"),
    )
)
app = local_ui.create_app(
    agent_api=agent_api,
    ui_workbench=BrowserUiWorkbench(agent_tasks),
    ui_governance=governance_service,
)
app.state.jobs = FakeJobs(app.state.artifacts)
app.state.agent_api = ExecutableAgentService(
    ExecutableAgentServices(
        readonly=agent_api,
        projects=agent_projects,
        tasks=agent_tasks,
        confirmations=confirmations,
        idempotency=ToolIdempotencyService(agent_store),
        brief_store=brief_store,
        backend=BrowserExecutionBackend(app.state.jobs, brief_store),
    )
)


if __name__ == "__main__":
    uvicorn.run(
        app,
        host=local_ui.HOST,
        port=TEST_PORT,
        log_level="warning",
    )
