from __future__ import annotations

from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.workspace_store import WorkspaceServiceError
from mvp_v2.formal_console import create_formal_console_app
from mvp_v2.formal_generation_service import (
    FormalGenerationService,
    FormalGenerationServices,
    FormalTextProviderPolicy,
)
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.project_image_authority import ProjectImagePoolService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore


class FormalConsoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mplus-formal-ui-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = WorkspaceStore(self.root / "data" / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名正式工作区", display_name="匿名 owner", confirmed=True,
            token="formal-owner-token",
        )
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store, identity=self.identity, projects=self.projects,
            tasks=TaskService(self.store), versions=VersionService(self.store),
            artifacts=RunArtifactResolver(self.root / "runs"),
        ))
        self.adapter = FormalBriefInputAdapter(
            FormalBriefWorkspaceService(self.store, artifact_root=self.root / "data"),
            data_root=self.root / "data",
        )
        self.provider_calls: list[str] = []

        def provider(_prompt, _model, _max_tokens):
            self.provider_calls.append("called")
            raise AssertionError("创作标准卡未就绪时不得调用 Provider")

        self.generation = FormalGenerationService(
            FormalGenerationServices(
                briefs=self.adapter.service,
                projects=self.projects,
                tasks=TaskService(self.store),
                artifacts=RunArtifactResolver(self.root / "runs"),
                images=ProjectImagePoolService(self.store, data_root=self.root / "data"),
            ),
            runs_root=self.root / "runs",
            source_root=self.root / "source",
            provider_policy=FormalTextProviderPolicy(
                writer=provider,
                editor=provider,
                writer_provider="test-writer",
                writer_model="test-writer-model",
                editor_provider="test-editor",
                editor_model="test-editor-model",
            ),
        )
        self.factory_calls: list[tuple[str, bool]] = []

        def uploaded_document_factory(filename: str, stream, critical: bool) -> UploadedDocument:
            self.factory_calls.append((filename, critical))
            return UploadedDocument(filename=filename, stream=stream, critical=critical)

        self.client = TestClient(create_formal_console_app(
            agent_api=self.agent, brief_adapter=self.adapter,
            brief_upload_factory=uploaded_document_factory,
            formal_generation_service=self.generation,
        ))
        self.shell_client = TestClient(create_app(
            agent_api=self.agent, formal_brief_adapter=self.adapter,
            formal_brief_upload_factory=uploaded_document_factory,
            formal_generation_service=self.generation,
        ))
        self.headers = {"X-MPlus-Agent-Auth": self.token}

    def _project(self, name="匿名正式项目"):
        workspace = self.projects.get_workspace(self.token, self.workspace_id)
        return self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name=name,
            category_id="launch", expected_workspace_version=workspace["workspace_version"],
        )

    def _table_count(self, table: str) -> int:
        with self.store.transaction() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _upload(self, project_id: str, expected_version: int, parts):
        return self.client.post(
            f"/api/formal/projects/{project_id}/brief/files?workspace_id={self.workspace_id}&expected_project_version={expected_version}",
            headers=self.headers, data={"critical": "true"}, files=parts,
        )

    def _confirm_formal_brief(self, project_id: str) -> int:
        uploaded = self._upload(project_id, 0, _five_format_parts()).json()
        conflict = next(
            item for item in uploaded["brief"]["conflicts"]
            if item["resolution_status"] == "unresolved"
        )
        resolved = self.client.post(
            f"/api/formal/projects/{project_id}/brief/conflicts/{conflict['conflict_key']}/resolve?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "brief_version_id": uploaded["brief_key"],
                "selected_fact_id": conflict["choices"][0]["fact_key"],
                "expected_project_version": 1,
            },
        )
        self.assertEqual(resolved.status_code, 200)
        confirmed = self.client.post(
            f"/api/formal/projects/{project_id}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "brief_version_id": resolved.json()["brief_key"],
                "expected_project_version": 2,
            },
        )
        self.assertEqual(confirmed.status_code, 200)
        synchronized = self.client.post(
            f"/api/formal/projects/{project_id}/images/synchronize?workspace_id={self.workspace_id}",
            headers=self.headers, json={"expected_project_version": 3},
        )
        self.assertEqual(synchronized.status_code, 200)
        return 3

    def test_formal_page_has_no_alpha_or_client_script_input(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("本机 owner", page.text)
        self.assertIn("多文件 Brief 输入", page.text)
        self.assertIn("从服务器重读 Brief", page.text)
        self.assertNotIn("Alpha", page.text)

    def test_single_workbench_routes_remove_replaced_legacy_surfaces(self):
        for path in (
            "/projects", "/projects/new", "/tasks", "/assistant", "/assets", "/knowledge", "/system",
            "/projects/anonymous-project/overview",
            "/projects/anonymous-project/brief", "/projects/anonymous-project/plan",
            "/projects/anonymous-project/script", "/projects/anonymous-project/script?view=versions", "/projects/anonymous-project/materials",
            "/projects/anonymous-project/review", "/projects/anonymous-project/assistant",
        ):
            response = self.shell_client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertNotIn('id="legacy-compatibility"', response.text)
            self.assertNotIn("兼容工作区", response.text)
        root = self.shell_client.get("/", follow_redirects=False)
        self.assertEqual(root.status_code, 307)
        self.assertEqual(root.headers["location"], "/projects")
        project_tasks = self.shell_client.get(
            "/projects/anonymous-project/tasks", follow_redirects=False
        )
        self.assertEqual(project_tasks.status_code, 307)
        self.assertEqual(
            project_tasks.headers["location"],
            "/tasks?project_id=anonymous-project",
        )
        self.assertEqual(self.shell_client.get("/projects/anonymous-project/unknown").status_code, 422)
        unknown_page = self.shell_client.get("/definitely-not-a-route")
        self.assertEqual(unknown_page.status_code, 404)
        self.assertTrue(unknown_page.headers["content-type"].startswith("text/html"))
        self.assertIn("页面不存在", unknown_page.text)
        self.assertIn('href="/projects"', unknown_page.text)
        self.assertNotIn('{"detail":"Not Found"}', unknown_page.text)
        unknown_api = self.shell_client.get("/api/definitely-not-a-route")
        self.assertEqual(unknown_api.status_code, 404)
        self.assertEqual(unknown_api.json(), {"detail": "Not Found"})
        unknown_api_post = self.shell_client.post("/api/definitely-not-a-route")
        self.assertEqual(unknown_api_post.status_code, 404)
        self.assertEqual(unknown_api_post.json(), {"detail": "Not Found"})
        self.assertEqual(self.shell_client.get("/api/health").status_code, 200)
        self.assertNotIn("M+ 脚本工作台", self.shell_client.get("/api/health").text)
        index = Path(__file__).parents[1] / "mvp_v2" / "ui" / "index.html"
        app_js = (index.parent / "app.js").read_text(encoding="utf-8")
        page = index.read_text(encoding="utf-8")
        for page_id in ("agent", "versions", "assets", "governance", "system"):
            self.assertIn(f'id="page-{page_id}"', page)
        for retired_page_id in ("create", "history"):
            self.assertNotIn(f'id="page-{retired_page_id}"', page)
        self.assertNotIn('data-page=', page)
        self.assertNotIn('querySelectorAll("[data-page]")', app_js)
        self.assertNotIn('plan: "agent"', app_js)
        self.assertNotIn('script: "versions"', app_js)
        self.assertIn('id="page-plan"', page)
        self.assertIn('id="page-script"', page)
        self.assertIn('id="formal-plan-script-rows"', page)
        self.assertIn("请先完成并确认项目创作标准卡", page)
        self.assertIn('id="formal-brief-dropzone"', page)
        self.assertIn('id="formal-brief-text"', page)
        self.assertIn('zone.addEventListener("drop"', app_js)
        self.assertIn('data.append("text", directText)', app_js)
        self.assertNotIn("FixtureFormalBriefAdapter", page)
        workspace_nav = page.split('<nav class="nav-list workspace-nav"', 1)[1].split("</nav>", 1)[0]
        project_nav = page.split('<nav class="nav-list project-nav"', 1)[1].split("</nav>", 1)[0]
        self.assertEqual(workspace_nav.count('data-route="'), 4)
        self.assertEqual(project_nav.count('data-route="'), 4)
        for main_route in ("overview", "brief", "script", "review"):
            self.assertIn(f'data-route="{main_route}"', project_nav)
        self.assertIn('data-route="materials"', page)
        self.assertIn('id="page-shot-images" class="creative-panel"', page)
        self.assertIn('data-creative-panel="images"', page)
        self.assertIn("usable_count", app_js)
        self.assertIn("ui00-overview-warnings", app_js)
        self.assertNotIn('data-route="images"', page)
        self.assertNotIn('id="formal-project-files"', page)
        self.assertNotIn('id="formal-project-text"', page)
        self.assertIn('id="formal-brief-upload-button"', page)
        self.assertIn('id="formal-image-upload-form"', page)
        self.assertIn('id="formal-image-upload-files"', page)
        self.assertIn('multiple accept=".jpg,.jpeg,.png,.webp', page)
        self.assertIn('uploadFormalProjectImages', app_js)
        self.assertIn('/brief/files?', app_js)
        self.assertIn('/images/understand?', app_js)
        self.assertIn('查看归档设置', page)
        self.assertNotIn("legacyCompatibility", app_js)
        self.assertNotIn("clearLegacy", app_js)
        self.assertNotIn("async function bootstrap()", app_js)
        self.assertNotIn('path === "/" || path === "/projects"', app_js)
        self.assertFalse((index.parents[1] / "legacy_retirement.json").exists())
        for development_page in ("alpha.html", "formal.html"):
            content = (index.parent / development_page).read_text(encoding="utf-8")
            self.assertNotIn('data-route=', content)
            self.assertNotIn('href="/projects', content)

    def test_script_page_exposes_scoped_version_governance_and_projection_status_contract(self):
        index = Path(__file__).parents[1] / "mvp_v2" / "ui" / "index.html"
        page = index.read_text(encoding="utf-8")
        app_js = (index.parent / "app.js").read_text(encoding="utf-8")
        css = (index.parent / "app.css").read_text(encoding="utf-8")

        self.assertIn('id="formal-script-version-governance"', page)
        self.assertIn("formalPath(\"script\", route.projectId, { view: \"versions\" })", app_js)
        self.assertIn('id="formal-script-save-state"', page)
        self.assertIn("修改自动保存到当前稿；核对后再保存新版本。", page)
        self.assertIn('id="formal-script-discard"', page)
        self.assertIn("推荐依据当前已保存工作稿", app_js)
        self.assertIn('id="formal-script-save-proposal"', page)
        self.assertIn("projection_status", app_js)
        self.assertIn("complete: publicationState === \"published\"", app_js)
        self.assertIn("重新发布确认", app_js)
        self.assertIn("publicationProjectionNotice(publicationProjectionStatus(actionResult), actionResult)", app_js)
        self.assertIn("等待人工审核；这不是技术失败", app_js)
        self.assertIn(".version-projection-note.is-failed", css)
        self.assertNotIn('publication ? `<span class="is-published">已发布</span>` : ""', app_js)
        self.assertNotIn("projection=pending，CURRENT 未写入", app_js)

    def test_workspace_tasks_are_available_without_entering_a_project(self):
        project = self._project("匿名跨项目任务")
        session = self.agent.create_session(self.token, project["project_id"])
        created = self.generation.services.tasks.create_task(
            self.token,
            agent_session_id=session["session"]["agent_session_id"],
            task_type="formal_generation",
            payload_sha256="a" * 64,
            expected_project_version=0,
        )
        path = f"/api/formal/workspaces/{self.workspace_id}/tasks"
        self.assertEqual(self.client.get(path).status_code, 401)
        response = self.client.get(
            path,
            headers=self.headers,
            params={"project_id": project["project_id"], "status": "received"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 1)
        item = response.json()["items"][0]
        self.assertEqual(item["task_id"], created["task_id"])
        self.assertEqual(item["project_name"], "匿名跨项目任务")
        self.assertNotIn("payload_sha256", item)
        self.assertNotIn("auth_session_id", item)
        self.assertIn("next_action", item)
        self.assertIn("action_href", item)

    def test_planning_readiness_and_overview_show_current_missing_fields_and_recover(self):
        project = self._project()
        project_id = project["project_id"]
        uploaded = self._upload(project_id, 0, [("files", ("brief.txt",
            "脚本类型: 发布会\n风格调性: 简约科技\n关键事件与流程: 开场、交流、品牌收束".encode(), "text/plain"))])
        self.assertEqual(uploaded.status_code, 200)
        confirmed = self.client.post(
            f"/api/formal/projects/{project_id}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers, json={"brief_version_id": uploaded.json()["brief_key"], "expected_project_version": 1})
        self.assertEqual(confirmed.status_code, 200)
        path = f"/api/formal/projects/{project_id}/planning-readiness?workspace_id={self.workspace_id}"
        before = {table: self._table_count(table) for table in ("tasks", "task_events", "project_versions", "brief_versions")}
        read = self.client.get(path + "&expected_project_version=2", headers=self.headers)
        self.assertEqual(read.status_code, 200)
        self.assertFalse(read.json()["ready"])
        self.assertIn("视频时长", read.json()["message"])
        self.assertNotIn("项目名称", read.json()["message"])
        self.assertEqual(before, {table: self._table_count(table) for table in before})
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path.replace(self.workspace_id, "wrong-workspace"), headers=self.headers).status_code, 403)
        self.assertEqual(self.client.get(path + "&expected_project_version=1", headers=self.headers).status_code, 409)
        generated = self.client.post(f"/api/formal/projects/{project_id}/plans?workspace_id={self.workspace_id}",
            headers=self.headers, json={"expected_project_version": 2, "generation_mode": "text_only_draft"})
        self.assertEqual(generated.status_code, 409)
        self.assertEqual(generated.json()["detail"]["message"], read.json()["message"])
        overview_path = f"/api/formal/projects/{project_id}/overview"
        overview = self.client.get(overview_path, headers=self.headers).json()
        self.assertIn("视频时长", overview["next_action"]["reason"])
        self.assertEqual(overview["next_action"]["href"], f"/projects/{project_id}/brief")
        updated = self._upload(project_id, 2, [("files", ("details.txt",
            "项目名称: 匿名客户大会\n目标时长: 60秒".encode(), "text/plain"))])
        self.assertEqual(updated.status_code, 200)
        confirmed = self.client.post(
            f"/api/formal/projects/{project_id}/brief/confirm?workspace_id={self.workspace_id}", headers=self.headers,
            json={"brief_version_id": updated.json()["brief_key"], "expected_project_version": 3})
        self.assertEqual(confirmed.status_code, 200)
        self.assertTrue(self.client.get(path + "&expected_project_version=4", headers=self.headers).json()["ready"])
        refreshed = self.client.get(overview_path, headers=self.headers).json()
        self.assertEqual(refreshed["next_action"]["label"], "重新生成规划")
        self.assertFalse(refreshed["blockers"])

    def test_project_overview_is_composed_from_authoritative_services(self):
        project = self._project("匿名概览项目")
        response = self.client.get(
            f"/api/formal/projects/{project['project_id']}/overview",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        overview = response.json()
        self.assertEqual(
            [stage["id"] for stage in overview["stages"]],
            ["brief", "materials", "planning", "script", "shot_images", "review"],
        )
        self.assertEqual(overview["next_action"]["label"], "进入 Brief 与资料")
        self.assertEqual(overview["next_action"]["href"], f"/projects/{project['project_id']}/brief")

    def test_project_overview_uses_image_and_review_authorities_after_version_exists(self):
        project = self._project("匿名交付概览项目")
        bound = self.projects.bind_run(
            self.token,
            project_id=project["project_id"],
            run_id="run-overview-delivery",
            source_kind="native",
            manifest_ref="artifact://run/run-overview-delivery/manifest",
            manifest_sha256="a" * 64,
            expected_project_version=project["project_version"],
        )
        version = self.projects.create_project_version(
            self.token,
            project_id=project["project_id"],
            run_id="run-overview-delivery",
            content_sha256="b" * 64,
            artifact_ref="artifact://run/run-overview-delivery/script_draft",
            expected_project_version=bound["project_version"],
        )

        class ImageAuthority:
            def read_current(self, *_args, **_kwargs):
                return {"state": "confirmed", "shot_count": 5}

        class ReviewAuthority:
            state = "ready_final"

            def read_current(self, *_args, **_kwargs):
                return {"state": self.state, "ready_for_final": self.state == "ready_final"}

        review = ReviewAuthority()
        client = TestClient(create_app(
            agent_api=self.agent,
            formal_brief_adapter=self.adapter,
            formal_generation_service=self.generation,
            formal_image_service=ImageAuthority(),
            formal_review_service=review,
        ))
        response = client.get(
            f"/api/formal/projects/{project['project_id']}/overview",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        overview = response.json()
        stages = {item["id"]: item for item in overview["stages"]}
        self.assertEqual(stages["shot_images"]["state"], "已完成")
        self.assertEqual(stages["review"]["state"], "待确认")
        self.assertEqual(overview["next_action"], {
            "label": "完成整片确认",
            "href": f"/projects/{project['project_id']}/review",
            "reason": "全部审核项已处理，请完成整片最终确认。",
        })

        review.state = "confirmed"
        completed = client.get(
            f"/api/formal/projects/{project['project_id']}/overview",
            headers=self.headers,
        ).json()
        completed_stages = {item["id"]: item for item in completed["stages"]}
        self.assertEqual(completed_stages["review"]["state"], "已完成")
        self.assertEqual(completed["next_action"], {
            "label": "下载正式交付",
            "href": f"/projects/{project['project_id']}/review",
            "reason": "整片确认已完成，可下载正式 Excel；PDF 按需生成。",
        })
        self.assertEqual(
            self.projects.get_project(self.token, project["project_id"])["project_version"],
            version["project_version"],
        )

        class ScriptAuthority:
            def read_current(self, *_args, **_kwargs):
                return {"state": "editing", "can_save_version": True, "revision": 2,
                        "version_label": "版本 1", "base_version_id": version["version_id"]}

        client.app.state.formal_script_service = ScriptAuthority()
        pending = client.get(
            f"/api/formal/projects/{project['project_id']}/overview", headers=self.headers,
        ).json()
        self.assertEqual(pending["next_action"]["label"], "保存当前稿")
        self.assertEqual(pending["draft"]["can_save_version"], True)
        self.assertEqual(pending["draft"]["revision"], 2)
        self.assertIn("版本 1", pending["saved_delivery"]["label"])
        self.assertEqual(next(s for s in pending["stages"] if s["id"] == "script")["state"], "待保存")

        class UnavailableScriptAuthority:
            def read_current(self, *_args, **_kwargs):
                raise WorkspaceServiceError("ARTIFACT_INVALID", "当前草稿暂不可读取")

        client.app.state.formal_script_service = UnavailableScriptAuthority()
        unavailable = client.get(
            f"/api/formal/projects/{project['project_id']}/overview", headers=self.headers,
        ).json()
        self.assertEqual(unavailable["draft"]["state"], "unknown")
        self.assertNotEqual(unavailable["next_action"]["label"], "下载正式交付")

    def test_project_overview_reports_image_attention_as_warning_not_blocker(self):
        project = self._project("青屿明星见面日")
        self._confirm_formal_brief(project["project_id"])
        with patch.object(
            self.generation,
            "get_image_readiness",
            return_value={
                "total": 6, "pass": 2, "pending": 1,
                "low_confidence": 1, "blocked": 1, "failed": 1,
            },
        ), patch.object(self.generation, "list_project_activity", return_value={"items": [None]}):
            overview = self.client.get(
                f"/api/formal/projects/{project['project_id']}/overview",
                headers=self.headers,
            ).json()
        self.assertTrue(overview["warnings"])
        self.assertTrue(any("项目图片" in item for item in overview["warnings"]))
        self.assertFalse(any("项目图片" in item for item in overview["blockers"]))

    def test_materials_and_shot_images_routes_split_the_workbenches(self):
        for path in ("/materials", "/shot-images", "/tools"):
            response = self.shell_client.get(path)
            self.assertEqual(response.status_code, 200, path)
        legacy = self.shell_client.get("/images", follow_redirects=False)
        self.assertEqual(legacy.status_code, 307)
        self.assertEqual(legacy.headers["location"], "/materials")
        legacy_project = self.shell_client.get(
            "/projects/anonymous-project/images", follow_redirects=False,
        )
        self.assertEqual(legacy_project.status_code, 307)
        self.assertEqual(legacy_project.headers["location"], "/projects/anonymous-project/materials")

    def test_workspace_history_and_system_dtos_are_safe_and_report_actual_status(self):
        project = self._project("匿名历史 DTO 项目")
        history = [{
            "run_id": "run-anonymous",
            "parent_run_id": "",
            "project_id": project["project_id"],
            "state": "completed",
            "message": "运行完成",
            "updated_at": 1.0,
            "brief": {"project_name": "匿名历史项目"},
            "report": {"output_path": "/private/customer/output.xlsx"},
            "artifacts": [{"path": "/private/customer/output.xlsx"}],
        }, {
            "run_id": "run-unowned",
            "parent_run_id": "",
            "project_id": "",
            "state": "completed",
            "message": "无项目归属",
            "updated_at": 2.0,
            "brief": {"project_name": "不得展示的无归属项目"},
            "report": {},
            "artifacts": [],
        }]
        with patch("mvp_v2.local_ui.scan_history", return_value=history):
            response = self.shell_client.get(
                f"/api/formal/workspaces/{self.workspace_id}/legacy-runs",
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["project_name"], "匿名历史 DTO 项目")
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("不得展示的无归属项目", rendered)
        self.assertNotIn("/private/customer", rendered)
        self.assertNotIn("report", rendered)
        self.assertNotIn("artifacts", rendered)

        checks = [
            {"name": "发布会资料库", "status": "pass", "message": "/private/source.xlsx"},
            {"name": "输出根目录", "status": "pass", "message": "/private/output"},
        ]
        with patch("mvp_v2.local_ui.system_checks", return_value=checks):
            response = self.shell_client.get(
                f"/api/formal/workspaces/{self.workspace_id}/system",
                headers=self.headers,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["overall_status"], "ready")
        self.assertEqual(response.json()["attention_count"], 0)
        self.assertEqual(response.json()["summary"], "本机运行依赖可用")
        self.assertNotIn("production_certification", response.json())
        self.assertNotIn("/private/", json.dumps(response.json(), ensure_ascii=False))

    def test_formal_history_uses_configured_run_root_and_excludes_default_root(self):
        project = self._project("匿名 Formal 历史项目")
        configured_root = self.root / "configured-runs"
        default_root = self.root / "must-not-scan-default"

        def write_run(root: Path, run_id: str, *, physical_project_id: str = "") -> Path:
            run_dir = root / "launch" / "runs" / run_id
            run_dir.mkdir(parents=True)
            workbook = run_dir / "formal-script.xlsx"
            book = Workbook()
            book.active.append(["镜号", "画面描述"])
            book.active.append(["1", "匿名产品画面"])
            book.save(workbook)
            manifest = run_dir / "run_manifest.json"
            manifest.write_text(json.dumps({
                "run_id": run_id,
                "parent_run_id": "",
                "project_id": physical_project_id,
                "stages": {},
                "artifacts": {},
            }, ensure_ascii=False), encoding="utf-8")
            (run_dir / "input_freeze.json").write_text(json.dumps({
                "brief": {
                    "项目名称": "匿名 Formal 历史项目",
                    "品牌或客户": "匿名品牌",
                    "脚本类型": "发布会",
                    "视频时长": "60秒",
                    "必须出现的内容": "匿名产品",
                    "禁忌或不能出现的内容": "竞品标识",
                },
            }, ensure_ascii=False), encoding="utf-8")
            (run_dir / "run_report.json").write_text(json.dumps({
                "run_id": run_id,
                "project_id": physical_project_id,
                "human_review_status": "quality_pass",
                "quality_review_status": "quality_pass",
                "release_decision": "pass",
                "exit_code": 0,
                "output_path": str(workbook),
                "run_manifest_path": str(manifest),
            }, ensure_ascii=False), encoding="utf-8")
            return manifest

        manifest = write_run(configured_root, "run-formal-native")
        write_run(default_root, "run-forbidden-old", physical_project_id=project["project_id"])
        self.projects.bind_run(
            self.token,
            project_id=project["project_id"],
            run_id="run-formal-native",
            source_kind="native",
            manifest_ref="artifact://run/run-formal-native/manifest",
            manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
            expected_project_version=project["project_version"],
        )
        runtime = {
            "runs_root": str(configured_root),
            "legacy_catalog_path": str(self.root / "missing-legacy-catalog.json"),
        }
        with patch("mvp_v2.local_ui.load_runtime_config", return_value=runtime), patch(
            "mvp_v2.local_ui.DEFAULT_ASSET_CACHE_ROOT", default_root,
        ), patch(
            "mvp_v2.local_ui.legacy_roots",
            side_effect=AssertionError("隔离配置根不得扫描本机 legacy roots"),
        ):
            response = self.shell_client.get(
                f"/api/formal/workspaces/{self.workspace_id}/legacy-runs",
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["count"], 1)
            self.assertEqual(response.json()["items"][0]["run_id"], "run-formal-native")
            self.assertNotIn("run-forbidden-old", response.text)

            detail = self.shell_client.get(
                f"/api/formal/workspaces/{self.workspace_id}/legacy-runs/run-formal-native",
                headers=self.headers,
            )
            self.assertEqual(detail.status_code, 200, detail.text)
            self.assertEqual(detail.json()["brief"]["project_name"], "匿名 Formal 历史项目")
            self.assertFalse(detail.json()["actions"]["review_import"])
            self.assertEqual(
                detail.json()["actions"]["review_import_reason"],
                "当前没有可用的正式审核服务，不能导入复核结果。",
            )
            self.assertTrue(detail.json()["actions"]["version_feedback"])
            self.assertTrue(detail.json()["actions"]["continue_from_brief"])
            self.assertEqual(len(detail.json()["artifacts"]), 2)
            downloaded = self.shell_client.get(
                detail.json()["artifacts"][0]["download_url"], headers=self.headers,
            )
            self.assertEqual(downloaded.status_code, 200)
            self.assertTrue(downloaded.content.startswith(b"PK"))
            changed = Workbook()
            changed.active.append(["镜号", "画面描述"])
            changed.active.append(["1", "已漂移的匿名画面"])
            changed.save(configured_root / "launch" / "runs" / "run-formal-native" / "formal-script.xlsx")
            drifted = self.shell_client.get(
                detail.json()["artifacts"][0]["download_url"], headers=self.headers,
            )
            self.assertEqual(drifted.status_code, 404)

    def test_historical_detail_artifact_and_brief_continuation_are_controlled(self):
        project = self._project("匿名历史续作项目")
        artifact_path = self.root / "匿名历史产物.pdf"
        artifact_path.write_bytes(b"anonymous-history-artifact")
        registered = self.shell_client.app.state.artifacts.register(
            artifact_path, "review", "允许查看的历史产物",
        )
        history = [{
            "run_id": "run-controlled-history",
            "parent_run_id": "",
            "project_id": project["project_id"],
            "state": "completed",
            "message": "历史运行完成",
            "updated_at": 1.0,
            "brief": {
                "project_name": "匿名历史续作项目",
                "brand": "匿名品牌",
                "script_type": "发布会",
                "duration": "60秒",
                "audience": "公开观众",
                "core_message": "说明匿名产品重点",
                "must_include": ["匿名产品"],
                "forbidden_content": ["竞品标识"],
            },
            "report": {"raw_config": "/private/customer/config.json"},
            "artifacts": [registered],
        }]
        base = f"/api/formal/workspaces/{self.workspace_id}/legacy-runs/run-controlled-history"
        with patch("mvp_v2.local_ui.scan_history", return_value=history):
            detail = self.shell_client.get(base, headers=self.headers)
            self.assertEqual(detail.status_code, 200, detail.text)
            public = json.dumps(detail.json(), ensure_ascii=False)
            for forbidden in ("/private/", "raw_config", "sha256", "provider", "model", "actor_id"):
                self.assertNotIn(forbidden, public)
            artifact = self.shell_client.get(
                detail.json()["artifacts"][0]["download_url"], headers=self.headers,
            )
            self.assertEqual(artifact.content, b"anonymous-history-artifact")
            proposal = self.shell_client.post(
                f"{base}/continuation-proposals",
                headers=self.headers,
                json={"action": "current_project", "idempotency_key": "history-current-1"},
            )
            self.assertEqual(proposal.status_code, 200, proposal.text)
            self.assertTrue(proposal.json()["confirmation_required"])
            confirmed = self.shell_client.post(
                f"{base}/continuation-proposals/{proposal.json()['proposal_id']}/confirm",
                headers=self.headers,
                json={"proposal_digest": proposal.json()["proposal_digest"]},
            )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertTrue(confirmed.json()["brief_confirmation_required"])
        self.assertEqual(confirmed.json()["target_project_id"], project["project_id"])

    def test_failed_historical_new_project_continuation_reuses_target_and_ends_task(self):
        project = self._project("匿名失败续作项目")
        history = [{
            "run_id": "run-controlled-retry",
            "parent_run_id": "",
            "project_id": project["project_id"],
            "state": "completed",
            "message": "历史运行完成",
            "updated_at": 1.0,
            "brief": {
                "project_name": "匿名失败续作项目",
                "brand": "匿名品牌",
                "script_type": "发布会",
                "duration": "60秒",
                "core_message": "匿名历史续作",
            },
            "report": {},
            "artifacts": [],
        }]
        base = (
            f"/api/formal/workspaces/{self.workspace_id}"
            "/legacy-runs/run-controlled-retry"
        )
        original_import = self.adapter.import_uploads
        attempts = 0

        def flaky_import(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise WorkspaceServiceError("INJECTED_IMPORT_FAILURE", "匿名注入失败")
            return original_import(*args, **kwargs)

        before_projects = len(self.projects.list_projects(self.token, self.workspace_id))
        with patch("mvp_v2.local_ui.scan_history", return_value=history), patch.object(
            self.adapter, "import_uploads", side_effect=flaky_import,
        ):
            first = self.shell_client.post(
                f"{base}/continuation-proposals",
                headers=self.headers,
                json={"action": "new_project", "idempotency_key": "history-new-fail-1"},
            )
            failed = self.shell_client.post(
                f"{base}/continuation-proposals/{first.json()['proposal_id']}/confirm",
                headers=self.headers,
                json={"proposal_digest": first.json()["proposal_digest"]},
            )
            self.assertGreaterEqual(failed.status_code, 400)
            failed_task = self.agent.services.tasks.get_task(
                self.token, first.json()["proposal_id"],
            )
            self.assertEqual(failed_task["status"], "failed")
            after_failure = self.projects.list_projects(self.token, self.workspace_id)
            self.assertEqual(len(after_failure), before_projects + 1)

            second = self.shell_client.post(
                f"{base}/continuation-proposals",
                headers=self.headers,
                json={"action": "new_project", "idempotency_key": "history-new-retry-2"},
            )
            recovered = self.shell_client.post(
                f"{base}/continuation-proposals/{second.json()['proposal_id']}/confirm",
                headers=self.headers,
                json={"proposal_digest": second.json()["proposal_digest"]},
            )
        self.assertEqual(recovered.status_code, 200, recovered.text)
        after_retry = self.projects.list_projects(self.token, self.workspace_id)
        self.assertEqual(len(after_retry), before_projects + 1)
        self.assertEqual(
            recovered.json()["target_project_id"],
            next(item["project_id"] for item in after_failure if item["project_id"] != project["project_id"]),
        )

    def test_generation_completion_dto_does_not_expose_internal_asset_paths(self):
        project = self._project("匿名生成回执")
        completion = {
            "plan_id": "plan-anonymous",
            "status": "completed",
            "version_id": "version-anonymous",
            "pending_confirmations": [],
            "script_rows": [{
                "镜号": "1",
                "画面描述": "匿名现场画面",
                "声音": "现场音乐",
                "时长(秒)": 5,
                "画面示意": "/private/customer/reference.png",
                "备注": "内部字段",
            }],
        }
        with patch.object(
            self.generation, "confirm_plan_and_generate", return_value=completion
        ):
            response = self.client.post(
                f"/api/formal/projects/{project['project_id']}/plans/plan-anonymous/confirm"
                f"?workspace_id={self.workspace_id}",
                headers=self.headers,
                json={"expected_project_version": 0},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["script_rows"],
            [{"镜号": "1", "画面描述": "匿名现场画面", "声音": "现场音乐", "时长(秒)": 5}],
        )
        self.assertNotIn("/private/", json.dumps(response.json(), ensure_ascii=False))

    def test_brief_upload_accepts_direct_text_and_rejects_empty_input(self):
        project = self._project()
        path = (
            f"/api/formal/projects/{project['project_id']}/brief/files"
            f"?workspace_id={self.workspace_id}&expected_project_version=0"
        )
        empty = self.client.post(path, headers=self.headers, data={"critical": "true"})
        self.assertEqual(empty.status_code, 409)
        self.assertEqual(empty.json()["detail"]["code"], "INVALID_INPUT")
        direct = self.client.post(
            path,
            headers=self.headers,
            data={
                "critical": "true",
                "text": "项目名称: 匿名直接输入项目\n目标时长: 60秒",
            },
        )
        self.assertEqual(direct.status_code, 200, direct.text)
        self.assertIn(("直接输入.txt", True), self.factory_calls)
        facts = {item["label"]: json.loads(item["value"]) for item in direct.json()["brief"]["facts"]}
        self.assertEqual(facts["项目名称"], "匿名直接输入项目")
        self.assertEqual(facts["目标时长"], "60秒")

    def test_journey_one_lists_creates_enters_and_enforces_project_rbac(self):
        project = self._project()
        listed = self.client.get(
            f"/api/formal/workspaces/{self.workspace_id}/projects", headers=self.headers,
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["items"][0]["project_id"], project["project_id"])
        workspace = self.projects.get_workspace(self.token, self.workspace_id)
        created = self.client.post(
            f"/api/formal/workspaces/{self.workspace_id}/projects", headers=self.headers,
            json={"name": "匿名新项目", "category_id": "launch", "expected_workspace_version": workspace["workspace_version"]},
        )
        self.assertEqual(created.status_code, 200)
        entered = self.client.get(f"/api/formal/projects/{project['project_id']}", headers=self.headers)
        self.assertEqual(entered.status_code, 200)
        workspace = self.projects.get_workspace(self.token, self.workspace_id)
        viewer = self.identity.create_actor(
            self.token, workspace_id=self.workspace_id, project_id=project["project_id"],
            display_name="匿名项目查看者", auth_source="local", role="viewer",
            expected_workspace_version=workspace["workspace_version"],
        )
        viewer_token = self.identity.issue_auth_session(
            actor_id=viewer["actor_id"], token="formal-scoped-viewer",
        )["auth_token"]
        denied = self.client.get(
            f"/api/formal/projects/{created.json()['project_id']}",
            headers={"X-MPlus-Agent-Auth": viewer_token},
        )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json()["detail"]["code"], "PERMISSION_DENIED")

    def test_default_adapter_block_never_falls_back_to_legacy_brief(self):
        project = self._project()
        blocked = TestClient(create_formal_console_app(agent_api=self.agent)).get(
            f"/api/formal/projects/{project['project_id']}/brief?workspace_id={self.workspace_id}&brief_version_id=brief_version_missing",
            headers=self.headers,
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"]["code"], "FORMAL_BRIEF_ADAPTER_UNAVAILABLE")

    def test_formal_setup_is_injected_safe_employee_dto_without_runtime_or_legacy_bootstrap(self):
        isolated = self.root / "formal-setup-isolated"
        home = isolated / "home"
        workspace = home / ".mplus" / "workspace.sqlite3"
        runtime = isolated / "runtime"
        with patch.dict(os.environ, {
            "MPLUS_CONFIG": str(runtime / "config.json"),
            "MPLUS_DATA_ROOT": str(runtime / "data"),
            "MPLUS_SECRETS": str(runtime / "secrets.env"),
            "HOME": str(home),
        }, clear=False), patch(
            "mvp_v2.local_ui.load_runtime_config", side_effect=AssertionError("runtime reader used")
        ) as runtime_reader, patch(
            "mvp_v2.local_ui.system_checks", side_effect=AssertionError("system checks used")
        ) as checks, patch(
            "mvp_v2.local_ui.asset_package_index_summary", side_effect=AssertionError("legacy bootstrap reader used")
        ) as legacy_reader, patch(
            "mvp_v2.local_ui.load_or_create_confirmation_key", side_effect=AssertionError("confirmation key reader used")
        ) as confirmation_key_reader:
            self.assertFalse(workspace.exists())
            response = self.shell_client.get("/api/formal/setup")
            self.assertEqual(response.status_code, 200)
            standalone = self.client.get("/api/formal/setup")
            self.assertEqual(standalone.status_code, 200)
            self.assertFalse(workspace.exists())
        runtime_reader.assert_not_called()
        checks.assert_not_called()
        legacy_reader.assert_not_called()
        confirmation_key_reader.assert_not_called()
        setup = response.json()
        self.assertEqual(standalone.json(), setup)
        self.assertEqual(set(setup), {
            "setup_state", "workspace_capability", "project_capability",
            "brief_availability", "plan_availability", "generation_availability",
            "script_availability",
            "review_availability",
            "admin_diagnostics_availability",
            "project_archival_availability", "project_purge_availability",
            "capabilities",
            "enabled", "brief_adapter", "formal_plan", "formal_plan_code",
        })
        self.assertEqual(set(setup["capabilities"]), {
            "read", "plan_write", "script_revision", "default_image_adoption",
        })
        self.assertEqual(setup["project_capability"], "available")
        self.assertEqual(setup["brief_availability"], "available")
        self.assertEqual(setup["plan_availability"], "available")
        self.assertEqual(setup["generation_availability"], "available")
        self.assertEqual(setup["script_availability"], "blocked")
        self.assertEqual(setup["review_availability"], "blocked")
        self.assertEqual(setup["project_archival_availability"], "available")
        self.assertEqual(setup["project_purge_availability"], "blocked")
        rendered = json.dumps(setup, ensure_ascii=False).casefold()
        for forbidden in ("token", "path", "model", "provider", "asset", "runtime", "system_check", "cache", "node", "current"):
            self.assertNotIn(forbidden, rendered)

    def test_project_archive_restore_http_journey_is_confirmed_and_archived_writes_fail_closed(self):
        project = self._project()
        parameters = {
            "expected_project_version": project["project_version"],
            "idempotency_key": "archive-http-00000001",
            "reason": "项目阶段结束，退出进行中列表",
        }
        prepared = self.client.post(
            f"/api/formal/projects/{project['project_id']}/lifecycle/archive/prepare",
            headers=self.headers, json=parameters,
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        self.assertIn("不会删除或覆盖 Brief", prepared.json()["will_not_change"])
        before = self.client.get(
            f"/api/formal/projects/{project['project_id']}", headers=self.headers,
        ).json()
        self.assertEqual(before["status"], "active")
        executed = self.client.post(
            f"/api/formal/projects/{project['project_id']}/lifecycle/archive/execute",
            headers=self.headers,
            json={**parameters, "command_token": prepared.json()["command_token"]},
        )
        self.assertEqual(executed.status_code, 200, executed.text)
        self.assertEqual(executed.json()["result"]["project_status"], "archived")

        blocked = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": "brief_version_12345678", "expected_project_version": 1},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertEqual(blocked.json()["detail"]["code"], "PROJECT_ARCHIVED")
        lifecycle = self.client.get(
            f"/api/formal/projects/{project['project_id']}/lifecycle", headers=self.headers,
        )
        self.assertTrue(lifecycle.json()["restore"]["available"])

        restore_parameters = {
            "expected_project_version": 1,
            "idempotency_key": "restore-http-00000001",
            "reason": "项目重新启动",
        }
        restore = self.client.post(
            f"/api/formal/projects/{project['project_id']}/lifecycle/restore/prepare",
            headers=self.headers, json=restore_parameters,
        )
        restored = self.client.post(
            f"/api/formal/projects/{project['project_id']}/lifecycle/restore/execute",
            headers=self.headers,
            json={**restore_parameters, "command_token": restore.json()["command_token"]},
        )
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertEqual(restored.json()["result"]["project_status"], "active")

        product = self.shell_client.get("/projects").text
        for element_id in (
            "formal-archived-list", "formal-project-lifecycle",
            "formal-lifecycle-confirm", "formal-archive-banner",
        ):
            self.assertIn(f'id="{element_id}"', product)

    def test_plan_routes_are_explicitly_blocked_without_all_three_dependencies(self):
        project = self._project()
        blocked = TestClient(create_formal_console_app(
            agent_api=self.agent,
            brief_adapter=self.adapter,
            brief_upload_factory=lambda filename, stream, critical: UploadedDocument(filename, stream, critical),
        )).get(
            f"/api/formal/projects/{project['project_id']}/plans?workspace_id={self.workspace_id}",
            headers=self.headers,
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"]["code"], "FORMAL_PLAN_UNAVAILABLE")

    def test_image_http_rejects_unknown_fields_and_unauthenticated_preview_has_no_storage_data(self):
        project = self._project()
        invalid = self.client.post(
            f"/api/formal/projects/{project['project_id']}/images/image_fake/analysis?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "expected_project_version": 0, "status": "pass", "caption": "匿名画面",
                "tags": ["发布"], "subjects": ["产品"], "scene": "舞台", "purpose": "说明",
                "quality": "清晰", "brand": "匿名品牌", "evidence": ["人工核验"],
                "artifact_ref": "artifact://forbidden",
            },
        )
        self.assertEqual(invalid.status_code, 422)
        preview = self.client.get(
            f"/api/formal/projects/{project['project_id']}/images/image_fake/preview?workspace_id={self.workspace_id}&expected_project_version=0",
        )
        self.assertEqual(preview.status_code, 401)
        body = preview.text.casefold()
        for forbidden in ("artifact", "sha", "path", "actor", "image_fake"):
            self.assertNotIn(forbidden, body)

    def test_image_http_readiness_manual_preview_enforces_scope_lock_and_redaction(self):
        project = self._project()
        lock = self._confirm_formal_brief(project["project_id"])
        ready = self.client.get(
            f"/api/formal/projects/{project['project_id']}/images?workspace_id={self.workspace_id}&expected_project_version={lock}",
            headers=self.headers,
        )
        self.assertEqual(ready.status_code, 200)
        body = ready.json()
        self.assertEqual((body["total"], body["pending"]), (1, 1))
        self.assertEqual(body["usable_count"], 0)
        self.assertEqual(body["attention_count"], 1)
        self.assertFalse(body["can_plan_with_usable_images"])
        self.assertTrue(body["can_continue_text_only"])
        for forbidden in ("artifact://", "sha256", "artifact_ref", "path", "actor"):
            self.assertNotIn(forbidden, json.dumps(body, ensure_ascii=False).casefold())
        image_id = body["images"][0]["image_key"]
        invalid_automatic = self.client.post(
            f"/api/formal/projects/{project['project_id']}/images/understand?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"expected_project_version": lock, "provider": "browser-forbidden"},
        )
        self.assertEqual(invalid_automatic.status_code, 422)
        unavailable = self.client.post(
            f"/api/formal/projects/{project['project_id']}/images/understand?workspace_id={self.workspace_id}",
            headers=self.headers, json={"expected_project_version": lock},
        )
        self.assertEqual(unavailable.status_code, 409)
        self.assertEqual(
            unavailable.json()["detail"]["code"], "FORMAL_VISION_PROVIDER_UNAVAILABLE",
        )
        self.assertEqual(self._table_count("project_image_analysis_versions"), 0)
        pending_preview = self.client.get(
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/preview?workspace_id={self.workspace_id}&expected_project_version={lock}",
            headers=self.headers,
        )
        self.assertEqual(pending_preview.status_code, 200)
        self.assertTrue(pending_preview.content.startswith(b"\x89PNG"))
        manual_payload = {
            "expected_project_version": lock, "status": "pass", "caption": "匿名产品发布现场",
            "tags": ["发布"], "subjects": ["产品"], "scene": "主舞台", "purpose": "说明发布重点",
            "quality": "清晰", "brand": "匿名品牌", "evidence": ["人工核验"],
        }
        fake = self.client.post(
            f"/api/formal/projects/{project['project_id']}/images/image_fake/analysis?workspace_id={self.workspace_id}",
            headers=self.headers, json=manual_payload,
        )
        self.assertEqual(fake.status_code, 404)
        old = self.client.post(
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/analysis?workspace_id={self.workspace_id}",
            headers=self.headers, json={**manual_payload, "expected_project_version": lock - 1},
        )
        self.assertEqual(old.status_code, 409)
        written = self.client.post(
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/analysis?workspace_id={self.workspace_id}",
            headers=self.headers, json=manual_payload,
        )
        self.assertEqual(written.status_code, 200)
        self.assertEqual((written.json()["pass"], written.json()["pending"]), (1, 0))
        written_text = json.dumps(written.json(), ensure_ascii=False).casefold()
        for forbidden in ("artifact://", "sha256", "artifact_ref", "path", "actor"):
            self.assertNotIn(forbidden, written_text)
        preview = self.client.get(
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/preview?workspace_id={self.workspace_id}&expected_project_version={lock}",
            headers=self.headers,
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.headers["content-type"], "image/png")
        self.assertEqual(preview.headers["cache-control"], "no-store")
        self.assertTrue(preview.content.startswith(b"\x89PNG"))
        for path in (
            f"/api/formal/projects/{project['project_id']}/images",
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/preview",
        ):
            query = f"workspace_id={self.workspace_id}&expected_project_version={lock - 1}"
            stale = self.client.get(f"{path}?{query}", headers=self.headers)
            self.assertEqual(stale.status_code, 409)
        with self.store.connection() as connection:
            digest = connection.execute(
                "SELECT image_sha256 FROM project_images WHERE project_image_id=?", (image_id,),
            ).fetchone()[0]
            recorded_actor = connection.execute(
                "SELECT created_by_actor_id FROM project_image_analysis_versions WHERE project_image_id=?", (image_id,),
            ).fetchone()[0]
            authenticated_actor = connection.execute(
                "SELECT actor_id FROM auth_sessions WHERE token_hash IS NOT NULL LIMIT 1"
            ).fetchone()[0]
        self.assertEqual(recorded_actor, authenticated_actor)
        (self.root / "data" / "artifacts" / "formal-images" / digest[:2] / digest).write_bytes(b"tampered")
        tampered = self.client.get(
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/preview?workspace_id={self.workspace_id}&expected_project_version={lock}",
            headers=self.headers,
        )
        self.assertEqual(tampered.status_code, 409)
        error_text = tampered.text.casefold()
        for forbidden in ("artifact://", "sha256", "artifact_ref", "path", "actor", str(self.root).casefold()):
            self.assertNotIn(forbidden, error_text)

    def test_brief_reconfirmation_keeps_images_visible_and_allows_current_reanalysis(self):
        project = self._project()
        first_lock = self._confirm_formal_brief(project["project_id"])
        first_ready = self.client.get(
            f"/api/formal/projects/{project['project_id']}/images?workspace_id={self.workspace_id}&expected_project_version={first_lock}",
            headers=self.headers,
        )
        self.assertEqual(first_ready.status_code, 200)
        image_id = first_ready.json()["images"][0]["image_key"]
        manual_payload = {
            "expected_project_version": first_lock,
            "status": "pass",
            "caption": "匿名产品发布现场",
            "tags": ["发布"],
            "subjects": ["产品"],
            "scene": "主舞台",
            "purpose": "说明发布重点",
            "quality": "清晰",
            "brand": "匿名品牌",
            "evidence": ["人工核验"],
        }
        first_analysis = self.client.post(
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/analysis?workspace_id={self.workspace_id}",
            headers=self.headers,
            json=manual_payload,
        )
        self.assertEqual(first_analysis.status_code, 200)

        revised_parts = _five_format_parts()
        revised_parts[0] = (
            "files",
            ("01-brief.pdf", _text_pdf("Project: Anonymous Revised"), "application/pdf"),
        )
        uploaded = self._upload(project["project_id"], first_lock, revised_parts)
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        brief_version_id = uploaded.json()["brief_key"]
        next_lock = self.projects.get_project(
            self.token, project["project_id"],
        )["project_version"]
        for conflict in (
            item for item in uploaded.json()["brief"]["conflicts"]
            if item["resolution_status"] == "unresolved"
        ):
            resolved = self.client.post(
                f"/api/formal/projects/{project['project_id']}/brief/conflicts/{conflict['conflict_key']}/resolve?workspace_id={self.workspace_id}",
                headers=self.headers,
                json={
                    "brief_version_id": brief_version_id,
                    "selected_fact_id": conflict["choices"][0]["fact_key"],
                    "expected_project_version": next_lock,
                },
            )
            self.assertEqual(resolved.status_code, 200, resolved.text)
            brief_version_id = resolved.json()["brief_key"]
            next_lock = self.projects.get_project(
                self.token, project["project_id"],
            )["project_version"]
        confirmed = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={
                "brief_version_id": brief_version_id,
                "expected_project_version": next_lock,
            },
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        current_lock = self.projects.get_project(
            self.token, project["project_id"],
        )["project_version"]

        stale_for_current_brief = self.client.get(
            f"/api/formal/projects/{project['project_id']}/images?workspace_id={self.workspace_id}&expected_project_version={current_lock}",
            headers=self.headers,
        )
        self.assertEqual(stale_for_current_brief.status_code, 200, stale_for_current_brief.text)
        self.assertEqual(
            (stale_for_current_brief.json()["total"], stale_for_current_brief.json()["pending"]),
            (1, 1),
        )
        self.assertEqual(stale_for_current_brief.json()["images"][0]["status"], "pending")
        self.assertIsNone(stale_for_current_brief.json()["images"][0]["summary"])
        preview = self.client.get(
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/preview?workspace_id={self.workspace_id}&expected_project_version={current_lock}",
            headers=self.headers,
        )
        self.assertEqual(preview.status_code, 200)

        blocked_plan = self.client.post(
            f"/api/formal/projects/{project['project_id']}/plans?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"expected_project_version": current_lock},
        )
        self.assertEqual(blocked_plan.status_code, 409)
        self.assertEqual(
            blocked_plan.json()["detail"]["code"],
            "IMAGE_STRATEGY_CONFIRMATION_REQUIRED",
        )
        reanalyzed = self.client.post(
            f"/api/formal/projects/{project['project_id']}/images/{image_id}/analysis?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={**manual_payload, "expected_project_version": current_lock},
        )
        self.assertEqual(reanalyzed.status_code, 200, reanalyzed.text)
        self.assertEqual((reanalyzed.json()["pass"], reanalyzed.json()["pending"]), (1, 0))
        self.assertEqual(self._table_count("project_image_analysis_versions"), 2)

    def test_image_http_cross_project_boundary_uses_real_same_database_projects(self):
        project_a = self._project("匿名正式项目 A")
        lock_a = self._confirm_formal_brief(project_a["project_id"])
        project_b = self._project("匿名正式项目 B")
        lock_b = self._confirm_formal_brief(project_b["project_id"])

        ready_a = self.client.get(
            f"/api/formal/projects/{project_a['project_id']}/images?workspace_id={self.workspace_id}&expected_project_version={lock_a}",
            headers=self.headers,
        )
        ready_b = self.client.get(
            f"/api/formal/projects/{project_b['project_id']}/images?workspace_id={self.workspace_id}&expected_project_version={lock_b}",
            headers=self.headers,
        )
        self.assertEqual((ready_a.status_code, ready_b.status_code), (200, 200))
        self.assertEqual((ready_a.json()["total"], ready_b.json()["total"]), (1, 1))
        image_a = ready_a.json()["images"][0]["image_key"]
        image_b = ready_b.json()["images"][0]["image_key"]
        self.assertNotEqual(image_a, image_b)
        self.assertNotIn(image_a, json.dumps(ready_b.json(), ensure_ascii=False))

        analysis_count = self._table_count("project_image_analysis_versions")
        current_count = self._table_count("project_image_analysis_current_links")
        manual_payload = {
            "expected_project_version": lock_b, "status": "pass", "caption": "匿名项目图片",
            "tags": ["发布"], "subjects": ["产品"], "scene": "主舞台", "purpose": "说明发布重点",
            "quality": "清晰", "brand": "匿名品牌", "evidence": ["人工核验"],
        }
        cross_analysis = self.client.post(
            f"/api/formal/projects/{project_b['project_id']}/images/{image_a}/analysis?workspace_id={self.workspace_id}",
            headers=self.headers,
            json=manual_payload,
        )
        cross_preview = self.client.get(
            f"/api/formal/projects/{project_b['project_id']}/images/{image_a}/preview?workspace_id={self.workspace_id}&expected_project_version={lock_b}",
            headers=self.headers,
        )
        stale_preview = self.client.get(
            f"/api/formal/projects/{project_b['project_id']}/images/{image_b}/preview?workspace_id={self.workspace_id}&expected_project_version={lock_b - 1}",
            headers=self.headers,
        )

        self.assertEqual(cross_analysis.status_code, 403)
        self.assertEqual(cross_analysis.json()["detail"]["code"], "CROSS_PROJECT_DENIED")
        self.assertEqual(cross_preview.status_code, 403)
        self.assertEqual(cross_preview.json()["detail"]["code"], "CROSS_PROJECT_DENIED")
        self.assertEqual(stale_preview.status_code, 409)
        self.assertEqual(stale_preview.json()["detail"]["code"], "PROJECT_VERSION_CONFLICT")
        self.assertEqual(self._table_count("project_image_analysis_versions"), analysis_count)
        self.assertEqual(self._table_count("project_image_analysis_current_links"), current_count)

        rendered = json.dumps(
            [ready_b.json(), cross_analysis.json(), cross_preview.json(), stale_preview.json()],
            ensure_ascii=False,
        ).casefold()
        for forbidden in (
            image_a.casefold(), "artifact://", "sha256", "artifact_ref", "path", "actor",
            str(self.root).casefold(),
        ):
            self.assertNotIn(forbidden, rendered)

    def test_plan_http_rejects_browser_inputs_and_blocks_without_confirmed_creative_standard(self):
        project = self._project()
        expected = self._confirm_formal_brief(project["project_id"])
        task_count = self._table_count("tasks")
        unknown = self.client.post(
            f"/api/formal/projects/{project['project_id']}/plans?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"expected_project_version": expected, "confirmed_brief": {}},
        )
        self.assertEqual(unknown.status_code, 422)
        self.assertEqual(self._table_count("tasks"), task_count)

        blocked = self.client.post(
            f"/api/formal/projects/{project['project_id']}/plans?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"expected_project_version": expected},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"]["code"], "IMAGE_STRATEGY_CONFIRMATION_REQUIRED")
        self.assertEqual(self._table_count("tasks"), task_count + 1)
        self.assertEqual(self.provider_calls, [])

        listed = self.client.get(
            f"/api/formal/projects/{project['project_id']}/plans?workspace_id={self.workspace_id}&expected_project_version={expected}",
            headers=self.headers,
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json(), {"items": []})
        self.assertNotIn("artifact", json.dumps(listed.json(), ensure_ascii=False).casefold())

        confirmation = self.client.post(
            f"/api/formal/projects/{project['project_id']}/plans/task_unknown/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"expected_project_version": expected},
        )
        self.assertEqual(confirmation.status_code, 404)
        self.assertEqual(confirmation.json()["detail"]["code"], "NOT_FOUND")
        self.assertEqual(self._table_count("tasks"), task_count + 1)
        self.assertEqual(self.provider_calls, [])

    def test_plan_http_blocks_before_standard_when_brief_is_unconfirmed_or_lock_is_old(self):
        project = self._project()
        unconfirmed = self.client.post(
            f"/api/formal/projects/{project['project_id']}/plans?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"expected_project_version": 0},
        )
        self.assertEqual(unconfirmed.status_code, 409)
        self.assertEqual(unconfirmed.json()["detail"]["code"], "CREATIVE_STANDARD_MISSING")

        expected = self._confirm_formal_brief(project["project_id"])
        old_lock = self.client.post(
            f"/api/formal/projects/{project['project_id']}/plans?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"expected_project_version": expected - 1},
        )
        self.assertEqual(old_lock.status_code, 409)
        self.assertEqual(old_lock.json()["detail"]["code"], "PROJECT_VERSION_CONFLICT")

    def test_current_formal_brief_recovers_latest_pending_after_refresh(self):
        project = self._project()
        current = self.client.get(
            f"/api/formal/projects/{project['project_id']}/brief/current?workspace_id={self.workspace_id}",
            headers=self.headers,
        )
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json(), {"status": "empty", "upload_limits": self.adapter.upload_limits})

        uploaded = self._upload(project["project_id"], 0, _five_format_parts())
        self.assertEqual(uploaded.status_code, 200)
        pending = self.client.get(
            f"/api/formal/projects/{project['project_id']}/brief/current?workspace_id={self.workspace_id}",
            headers=self.headers,
        )
        self.assertEqual(pending.status_code, 200)
        pending_body = pending.json()
        self.assertEqual(pending_body["status"], "ready")
        self.assertEqual(pending_body["confirmation_status"], "pending")
        self.assertEqual(pending_body["brief_key"], uploaded.json()["brief_key"])
        self.assertEqual(pending_body["brief"], uploaded.json()["brief"])
        self.assertNotIn("artifact://", json.dumps(pending_body, ensure_ascii=False))

        restarted = FormalBriefInputAdapter(
            FormalBriefWorkspaceService(self.store, artifact_root=self.root / "data"),
            data_root=self.root / "data",
        ).read_current_formal(
            self.token, workspace_id=self.workspace_id, project_id=project["project_id"],
        )
        self.assertEqual(restarted["confirmation_status"], "pending")
        self.assertEqual(restarted["brief_key"], uploaded.json()["brief_key"])

    def test_write_dtos_reject_unknown_fields_without_domain_writes(self):
        owner_count = self._table_count("workspaces")
        owner = self.client.post(
            "/api/formal/owner",
            json={"workspace_name": "ignored", "display_name": "ignored", "confirmed": True, "brief": {}},
        )
        self.assertEqual(owner.status_code, 422)
        self.assertEqual(owner.json()["detail"][0]["type"], "extra_forbidden")
        self.assertEqual(self._table_count("workspaces"), owner_count)

        project = self._project()
        projects_before = self._table_count("projects")
        created = self.client.post(
            f"/api/formal/workspaces/{self.workspace_id}/projects", headers=self.headers,
            json={"name": "ignored", "category_id": "launch", "expected_workspace_version": project["workspace_version"], "path": "/tmp/forbidden"},
        )
        self.assertEqual(created.status_code, 422)
        self.assertEqual(created.json()["detail"][0]["type"], "extra_forbidden")
        self.assertEqual(self._table_count("projects"), projects_before)

        confirmed = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": "brief_version_missing", "expected_project_version": 0, "confirmed": True},
        )
        self.assertEqual(confirmed.status_code, 422)
        self.assertEqual(confirmed.json()["detail"][0]["type"], "extra_forbidden")
        self.assertEqual(self._table_count("brief_confirmations"), 0)

        rejected_upload = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/files?workspace_id={self.workspace_id}&expected_project_version=0",
            headers=self.headers, data={"critical": "true", "path": "/tmp/forbidden"},
            files=[("files", ("brief.txt", b"anonymous", "text/plain"))],
        )
        self.assertEqual(rejected_upload.status_code, 409)
        self.assertEqual(rejected_upload.json()["detail"]["code"], "INVALID_INPUT")
        self.assertEqual(self._table_count("brief_versions"), 0)
        self.assertEqual(self.factory_calls, [])

    def test_real_adapter_upload_resolve_confirm_and_server_reread(self):
        project = self._project()
        uploaded = self._upload(project["project_id"], 0, _five_format_parts())
        self.assertEqual(uploaded.status_code, 200)
        imported = uploaded.json()
        self.assertEqual(len(imported["brief"]["documents"]), 5)
        self.assertEqual({item["status"] for item in imported["brief"]["documents"]}, {"succeeded"})
        self.assertEqual({name for name, _critical in self.factory_calls}, {
            "01-brief.pdf", "02-brief.docx", "03-brief.xlsx", "04-brief.pptx", "05-reference.png",
        })
        self.assertTrue(all(critical for _name, critical in self.factory_calls))
        self.assertTrue(all(fact["sources"] for fact in imported["brief"]["facts"]))
        self.assertEqual(imported["creative_standard"]["status"], "draft")
        self.assertNotIn("source_document_id", json.dumps(imported, ensure_ascii=False))
        self.assertNotIn("artifact://", json.dumps(imported, ensure_ascii=False))
        conflict = next(item for item in imported["brief"]["conflicts"] if item["resolution_status"] == "unresolved")
        selected_fact_key = conflict["choices"][0]["fact_key"]

        versions_before_invalid_resolution = self._table_count("brief_versions")
        invalid_resolution = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/conflicts/{conflict['conflict_key']}/resolve?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": imported["brief_key"], "selected_fact_id": selected_fact_key, "expected_project_version": 1, "path": "/tmp/forbidden"},
        )
        self.assertEqual(invalid_resolution.status_code, 422)
        self.assertEqual(invalid_resolution.json()["detail"][0]["type"], "extra_forbidden")
        self.assertEqual(self._table_count("brief_versions"), versions_before_invalid_resolution)

        premature = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": imported["brief_key"], "expected_project_version": 1},
        )
        self.assertEqual(premature.status_code, 409)
        self.assertEqual(self._table_count("brief_confirmations"), 0)

        other = self._project("匿名跨项目")
        cross_project = self.client.post(
            f"/api/formal/projects/{other['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": imported["brief_key"], "expected_project_version": 0},
        )
        self.assertEqual(cross_project.status_code, 403)
        self.assertEqual(cross_project.json()["detail"]["code"], "CROSS_PROJECT_DENIED")

        stale = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/conflicts/{conflict['conflict_key']}/resolve?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": imported["brief_key"], "selected_fact_id": selected_fact_key, "expected_project_version": 0},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self._table_count("brief_confirmations"), 0)

        resolved_response = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/conflicts/{conflict['conflict_key']}/resolve?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": imported["brief_key"], "selected_fact_id": selected_fact_key, "expected_project_version": 1},
        )
        self.assertEqual(resolved_response.status_code, 200)
        resolved = resolved_response.json()
        resolved_conflict = resolved["brief"]["conflicts"][0]
        self.assertEqual(resolved_conflict["resolution_status"], "resolved")
        self.assertEqual(resolved["creative_standard"]["status"], "draft")

        stale_confirmation = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": resolved["brief_key"], "expected_project_version": 1},
        )
        self.assertEqual(stale_confirmation.status_code, 409)
        self.assertEqual(self._table_count("brief_confirmations"), 0)

        confirmed_response = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": resolved["brief_key"], "expected_project_version": 2},
        )
        self.assertEqual(confirmed_response.status_code, 200)
        confirmation = confirmed_response.json()
        self.assertEqual(confirmation["confirmation_status"], "confirmed")
        self.assertEqual(confirmation["creative_standard"]["status"], "confirmed")
        self.assertNotIn("actor", json.dumps(confirmation, ensure_ascii=False))
        confirmations_after_first = self._table_count("brief_confirmations")

        reread = self.client.get(
            f"/api/formal/projects/{project['project_id']}/brief?workspace_id={self.workspace_id}&brief_version_id={resolved['brief_key']}",
            headers=self.headers,
        )
        self.assertEqual(reread.status_code, 200)
        self.assertEqual(reread.json()["confirmation_status"], "confirmed")
        self.assertEqual(reread.json()["creative_standard"]["status"], "confirmed")
        self.assertEqual(self.projects.get_project(self.token, project["project_id"])["project_version"], 3)

        current = self.client.get(
            f"/api/formal/projects/{project['project_id']}/brief/current?workspace_id={self.workspace_id}",
            headers=self.headers,
        )
        self.assertEqual(current.status_code, 200)
        current_body = current.json()
        self.assertEqual(current_body["status"], "ready")
        self.assertEqual(current_body["confirmation_status"], "confirmed")
        self.assertEqual(current_body["creative_standard"]["status"], "confirmed")
        self.assertFalse(current_body["has_failed_extractions"])
        self.assertEqual(current_body["unresolved_conflict_count"], 0)
        self.assertEqual(len(current_body["brief"]["documents"]), 5)
        self.assertNotIn("workspace_id", current_body["brief"])
        self.assertNotIn("project_id", current_body["brief"])
        self.assertNotIn("source_document_id", json.dumps(current_body, ensure_ascii=False))
        self.assertNotIn("artifact://", json.dumps(current_body, ensure_ascii=False))
        fact_sources = [fact["sources"] for fact in current_body["brief"]["facts"]]
        self.assertIn(["03-brief.xlsx · Sheet：资料 · 第 2 行"], fact_sources)
        self.assertIn(["01-brief.pdf · 第 1 页"], fact_sources)
        repeated = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": resolved["brief_key"], "expected_project_version": 3},
        )
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(self._table_count("brief_confirmations"), confirmations_after_first)

    def test_current_formal_brief_rejects_cross_project_and_snapshot_drift(self):
        project = self._project()
        uploaded = self._upload(project["project_id"], 0, _five_format_parts()).json()
        conflict = next(item for item in uploaded["brief"]["conflicts"] if item["resolution_status"] == "unresolved")
        resolved = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/conflicts/{conflict['conflict_key']}/resolve?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": uploaded["brief_key"], "selected_fact_id": conflict["choices"][0]["fact_key"], "expected_project_version": 1},
        ).json()
        self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": resolved["brief_key"], "expected_project_version": 2},
        )
        other = self._project("匿名其他项目")
        denied = self.client.get(
            f"/api/formal/projects/{other['project_id']}/brief/current?workspace_id={self.workspace_id}",
            headers=self.headers,
        )
        self.assertEqual(denied.status_code, 200)
        self.assertEqual(denied.json(), {"status": "empty", "upload_limits": self.adapter.upload_limits})
        with self.store.transaction() as connection:
            actor_id = connection.execute("SELECT actor_id FROM actors LIMIT 1").fetchone()[0]
            connection.execute(
                """INSERT INTO project_brief_snapshots(
                    brief_snapshot_id, project_id, brief_ref, brief_sha256, created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                ("brief_snapshot_drift", project["project_id"], "artifact://formal-brief/" + "0" * 64, "0" * 64, actor_id, "2026-08-13T00:00:00Z"),
            )
            connection.execute(
                """UPDATE projects
                   SET current_brief_snapshot_id=?, current_creative_standard_version_id=NULL
                   WHERE project_id=?""",
                ("brief_snapshot_drift", project["project_id"]),
            )
        drift = self.client.get(
            f"/api/formal/projects/{project['project_id']}/brief/current?workspace_id={self.workspace_id}",
            headers=self.headers,
        )
        self.assertEqual(drift.status_code, 409)
        self.assertEqual(drift.json()["detail"]["code"], "FORMAL_BRIEF_DRIFT")

    def test_critical_ocr_failure_cannot_confirm(self):
        project = self._project()
        uploaded = self._upload(
            project["project_id"], 0,
            [("files", ("scanned.pdf", _scanned_pdf(), "application/pdf"))],
        )
        self.assertEqual(uploaded.status_code, 200)
        body = uploaded.json()
        self.assertEqual(body["brief"]["documents"][0]["status"], "failed")
        rejected = self.client.post(
            f"/api/formal/projects/{project['project_id']}/brief/confirm?workspace_id={self.workspace_id}",
            headers=self.headers,
            json={"brief_version_id": body["brief_key"], "expected_project_version": 1},
        )
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(self._table_count("brief_confirmations"), 0)


def _five_format_parts():
    return [
        ("files", ("01-brief.pdf", _text_pdf("Project: Anonymous One"), "application/pdf")),
        ("files", ("02-brief.docx", _office_bytes("word/document.xml", "<w:document xmlns:w=\"w\"><w:body><w:p><w:r><w:t>项目名称: 匿名二号</w:t></w:r></w:p></w:body></w:document>"), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        ("files", ("03-brief.xlsx", _xlsx_bytes(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ("files", ("04-brief.pptx", _office_bytes("ppt/slides/slide1.xml", "<p:sld xmlns:p=\"p\"><p:spTree><p:txBody><a:p xmlns:a=\"a\"><a:r><a:t>关键事件: 产品演示</a:t></a:r></a:p></p:txBody></p:spTree></p:sld>"), "application/vnd.openxmlformats-officedocument.presentationml.presentation")),
        ("files", ("05-reference.png", _image_bytes(), "image/png")),
    ]


def _office_bytes(member: str, value: str) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(member, value)
    return output.getvalue()


def _text_pdf(text: str) -> bytes:
    writer = PdfWriter(); page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject(); content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")); page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO(); writer.write(output); return output.getvalue()


def _scanned_pdf() -> bytes:
    writer = PdfWriter(); writer.add_blank_page(width=612, height=792)
    output = BytesIO(); writer.write(output); return output.getvalue()


def _xlsx_bytes() -> bytes:
    workbook = Workbook(); sheet = workbook.active; sheet.title = "资料"
    for row, value in enumerate((
        "脚本类型: 发布会", "目标时长: 60秒", "必须出现: 产品演示",
        "风格调性: 克制、真实", "禁止: 不得虚构事实",
    ), start=1):
        sheet.cell(row, 1, value)
    output = BytesIO(); workbook.save(output); return output.getvalue()


def _image_bytes() -> bytes:
    output = BytesIO(); Image.new("RGB", (20, 10), "blue").save(output, format="PNG"); return output.getvalue()


if __name__ == "__main__":
    unittest.main()
