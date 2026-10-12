"""Anonymous HTTP/SQLite regression for lifecycle reasons and immediate navigation.

Business responses are real; the browser uses the established anonymous owner
identity helper. This small fixture verifies lifecycle state and requests, while
the retained round-three project's source preservation is verified separately.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse

from playwright.sync_api import expect

from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests.test_mvp_v2_readonly_agent import ReadonlyAgentFixture


class Round3ProjectLifecycleBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.f = ReadonlyAgentFixture(root, with_project=False, with_run=False)
        self.project = self.f.projects.create_project(self.f.owner_token,
            workspace_id=self.f.workspace_id, name="匿名项目管理回归", category_id="launch",
            expected_workspace_version=0)
        self.project_id = self.project["project_id"]
        adapter = FormalBriefInputAdapter(FormalBriefWorkspaceService(self.f.store,
            artifact_root=root / "runtime"), data_root=root / "runtime")
        app = local_ui.create_app(agent_api=self.f.agent, formal_brief_adapter=adapter,
            formal_brief_upload_factory=uploaded_document)
        self.page, self.origin = self.open_page(app, self.f.owner_token, self.f.workspace_id)
        self.requests, self.responses, self.errors, self.steps = [], [], [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("request", self.capture_request)
        self.page.on("response", lambda response: self.responses.append({
            "path": urlparse(response.url).path, "method": response.request.method,
            "status": response.status}) if "/api/" in response.url else None)
        self.url = self.origin + "/projects/" + self.project_id + "/overview"
        self.page.goto(self.url)
        self.open_lifecycle()
        self.page.wait_for_load_state("networkidle")

    def capture_request(self, request):
        if "/api/" not in request.url:
            return
        body = deepcopy(request.post_data_json) if request.method == "POST" else None
        if isinstance(body, dict) and "command_token" in body:
            body["command_token"] = "[REDACTED confirmation token]"
        self.requests.append({"path": urlparse(request.url).path,
            "method": request.method, "body": body})

    def open_lifecycle(self):
        details = self.page.locator("#formal-project-lifecycle")
        expect(details).to_be_visible()
        if details.get_attribute("open") is None:
            details.locator(":scope > summary").click()
        expect(self.page.locator("#formal-lifecycle-prepare")).to_be_enabled()

    def database_snapshot(self):
        with self.f.store.connection() as connection:
            return tuple(connection.iterdump())

    def current(self):
        return self.f.projects.get_project(self.f.owner_token, self.project_id)

    def snapshot(self, label):
        self.steps.append({"label": label, "url": self.page.url,
            "project_status": self.current()["status"],
            "project_version": self.current()["project_version"],
            "sidebar": self.page.locator("#formal-nav-project-state").inner_text(),
            "lifecycle": self.page.locator("#formal-lifecycle-state").text_content(),
            "api_request_count": len(self.requests)})

    def tearDown(self):
        configured = os.environ.get("MPLUS_ROUND3_LIFECYCLE_BROWSER_ARTIFACTS", "").strip()
        if configured:
            target = Path(configured)
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{self._testMethodName}.json").write_text(json.dumps({
                "steps": self.steps, "requests": self.requests, "responses": self.responses,
                "page_errors": self.errors, "actual_http_authorities_and_sqlite": True,
                "business_api_mocked": False, "external_provider_called": False,
                "browser_business_state_injected": False,
                "retained_user_project_verified": False,
            }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_blank_reason_is_local_and_archive_restore_syncs_sidebar_without_reload(self):
        versions_before = self.f.projects.list_project_versions(self.f.owner_token, self.project_id)
        initial_version = self.current()["project_version"]
        for action, label, status, sidebar in (
                ("archive", "归档", "archived", "已归档 · 只读"),
                ("restore", "恢复", "active", "进行中")):
            self.open_lifecycle()
            self.page.wait_for_load_state("networkidle")
            request_count = len(self.requests)
            database_before = self.database_snapshot()
            self.page.locator("#formal-lifecycle-reason").fill("   ")
            self.page.locator("#formal-lifecycle-prepare").click()
            expect(self.page.locator("#toast")).to_have_text(f"请填写{label}原因，不能只填空格。")
            expect(self.page.locator("#formal-lifecycle-reason")).to_be_focused()
            expect(self.page.locator("#formal-lifecycle-confirm")).to_be_hidden()
            expect(self.page.locator("#formal-lifecycle-prepare")).to_be_enabled()
            self.assertEqual(len(self.requests), request_count, "空白原因不能发 GET 或 POST")
            self.assertEqual(self.database_snapshot(), database_before)
            self.snapshot(action + "-blank-reason-zero-api-focused")
            reason = f"匿名{label}回归"
            self.page.locator("#formal-lifecycle-reason").fill("  " + reason + "  ")
            with self.page.expect_response(lambda response: response.request.method == "POST"
                    and response.url.endswith(f"/lifecycle/{action}/prepare")) as prepared:
                self.page.locator("#formal-lifecycle-prepare").click()
            self.assertEqual(prepared.value.status, 200, prepared.value.text())
            self.assertEqual(prepared.value.request.post_data_json["reason"], reason)
            self.page.locator("#formal-lifecycle-confirm-check").check()
            with self.page.expect_response(lambda response: response.request.method == "POST"
                    and response.url.endswith(f"/lifecycle/{action}/execute")) as executed:
                self.page.locator("#formal-lifecycle-execute").click()
            self.assertEqual(executed.value.status, 200, executed.value.text())
            expect(self.page.locator("#formal-nav-project-state")).to_have_text(sidebar)
            expect(self.page.locator("#formal-lifecycle-state")).to_have_text("已归档" if action == "archive" else "进行中")
            if action == "archive":
                expect(self.page.locator("#formal-archive-banner")).to_be_visible()
            else:
                expect(self.page.locator("#formal-archive-banner")).to_be_hidden()
            self.assertEqual(self.current()["status"], status)
            self.assertEqual(self.page.url, self.url)
            self.snapshot(action + "-real-execute-sidebar-synced-without-reload")
        self.page.reload()
        expect(self.page.locator("#formal-nav-project-state")).to_have_text("进行中")
        expect(self.page.locator("#formal-archive-banner")).to_be_hidden()
        self.open_lifecycle()
        self.assertEqual(self.current()["project_version"], initial_version + 2)
        self.assertEqual(self.f.projects.list_project_versions(self.f.owner_token, self.project_id), versions_before)
        self.assertEqual(self.errors, [])
        self.snapshot("restored-active-after-reload")


if __name__ == "__main__":
    unittest.main()
