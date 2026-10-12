"""Anonymous normal-UI regression for blank version reasons and receipt recovery.

HTTP, SQLite, script/image services and confirmation are real. The existing
version and image decisions are seeded with the established anonymous fixtures;
only the recovery test loses a successful response at the transport boundary.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect

from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.task_service import TaskService
from tests import test_mvp_v2_image_progress as progress_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests.test_mvp_v2_library_browse import library_confirmation_agent


class Round3VersionReasonBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def setUp(self):
        self.fixture = progress_fixture.ImageProgressTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.images = self.fixture.service
        self.f = self.fixture.fixture
        self.script = self.f.base.workspace
        self.token, self.scope = self.fixture.token, self.fixture.scope
        selections = [self.fixture.selection(index) for index in range(len(self.fixture.current["shots"]))]
        for index, selection in enumerate(selections):
            selection["reuse_reason"] = "匿名 fixture 已核对同场景参考图复用"
            if self._testMethodName == "test_unconfirmed_image_save_error_link_enters_same_project_images_without_losing_draft" and index < 2:
                selection["checked"] = False
        self.fixture.patch(selections, key="reason-regression-confirmed-image-progress")
        agent = library_confirmation_agent(root=self.f.base.generated.root, store=self.f.store,
            projects=self.f.projects, service=None)
        agent.script_revision = FormalRevisionService(projects=self.f.projects, tasks=TaskService(self.f.store),
            workspace=self.script, image_workspace=self.images, writes_enabled=True)
        app_options = {}
        if self._testMethodName == "test_unconfirmed_image_save_error_link_enters_same_project_images_without_losing_draft":
            app_options.update(formal_brief_adapter=FormalBriefInputAdapter(self.f.base.generated.briefs,
                data_root=self.f.base.generated.root / "runtime"), formal_brief_upload_factory=uploaded_document)
        app = local_ui.create_app(agent_api=agent, formal_script_service=self.script,
            formal_image_service=self.images, **app_options)
        self.page, self.origin = self.open_page(app, self.token, self.scope["workspace_id"])
        self.requests, self.errors, self.steps = [], [], []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("request", self.capture_request)
        self.url = self.origin + "/projects/" + self.scope["project_id"] + "/script"
        self.page.goto(self.url)
        self.page.locator('[data-creative-edit-mode="table"]').click()
        self.changed_subtitle = "匿名正常页面保存说明回归字幕"
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/script/table?" in response.url) as changed:
            self.cell().fill(self.changed_subtitle)
            self.cell().press("Tab")
        self.assertEqual(changed.value.status, 200, changed.value.text())
        self.before = self.current()
        self.initial_versions = deepcopy(self.versions())
        details = self.page.locator("#creative-save")
        if details.get_attribute("open") is None:
            details.locator(":scope > summary").click()
        expect(self.page.locator("#formal-script-save")).to_be_enabled()

    def capture_request(self, request):
        path = urlparse(request.url).path
        if path.endswith("/script/save") or "/api/agent/tasks/" in path:
            self.requests.append({"method": request.method, "path": path,
                "body": deepcopy(request.post_data_json) if request.method == "POST" else None})

    def current(self):
        return self.script.read_current(self.token, **self.scope)

    def versions(self):
        return self.f.projects.list_project_versions(self.token, self.scope["project_id"])

    def cell(self):
        return self.page.locator('[data-script-shot="1"][data-script-field="subtitle"]')

    def save_requests(self):
        return [request for request in self.requests
            if request["method"] == "POST" and request["path"].endswith("/script/save")]

    def snapshot(self, label):
        self.steps.append({"label": label, "url": self.page.url,
            "proposal": self.page.locator("#formal-script-save-proposal").inner_text(),
            "draft_revision": self.current()["revision"], "version_count": len(self.versions())})

    def tearDown(self):
        configured = os.environ.get("MPLUS_ROUND3_REASON_BROWSER_ARTIFACTS", "").strip()
        if not configured:
            return
        target = Path(configured)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{self._testMethodName}.json").write_text(json.dumps({
            "steps": self.steps, "requests": self.requests, "page_errors": self.errors,
            "persisted_version_count": len(self.versions()),
            "actual_http_authorities_and_sqlite": True, "business_api_mocked": False,
            "anonymous_seed": "deterministic version Provider plus anonymous persisted image progress fixture",
            "transport_response_loss": "lost_receipt" in self._testMethodName,
            "browser_application_state_injection": False, "retained_user_projects_verified": False,
            "navigation_dialogs": getattr(self, "navigation_dialogs", []),
            "navigation_document_requests": getattr(self, "navigation_document_requests", []),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def propose(self, reason):
        self.page.locator("#formal-script-save-reason").fill(reason)
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/script/save?" in response.url) as proposed:
            self.page.locator("#formal-script-save").click()
        self.assertEqual(proposed.value.status, 200, proposed.value.text())
        expect(self.page.locator("[data-confirm-script-revision]")).to_be_visible()
        return proposed.value.json()

    def assert_saved_child(self):
        expect(self.page.locator("#formal-script-save-proposal")).to_contain_text("已保存为一个新版本", timeout=60000)
        versions = self.versions()
        self.assertEqual(len(versions), len(self.initial_versions) + 1)
        self.assertEqual(versions[:-1], self.initial_versions)
        self.assertEqual(versions[-1]["parent_version_id"], self.initial_versions[-1]["version_id"])
        self.page.reload()
        self.page.locator('[data-creative-edit-mode="table"]').click()
        expect(self.cell()).to_have_value(self.changed_subtitle)
        final = self.current()
        self.assertEqual(final["shots"][1:], self.before["shots"][1:])
        self.assertFalse(final["can_save_version"])
        self.assertEqual(self.errors, [])

    def test_three_spaces_show_actionable_guard_preserve_progress_and_valid_reason_saves(self):
        images_before = self.images.read_progress(self.token, **self.scope)
        self.page.locator("#formal-script-save-reason").fill("   ")
        self.page.locator("#formal-script-save").click()
        proposal = self.page.locator("#formal-script-save-proposal")
        expect(proposal).to_contain_text("请填写本次修改说明，不能只填空格")
        expect(proposal).to_contain_text("已保存的进度仍保留，填写后可继续保存版本")
        expect(proposal).not_to_contain_text("刷新")
        expect(self.page.locator("#formal-script-save-reason")).to_be_focused()
        self.assertEqual(self.save_requests(), [])
        self.assertEqual(self.current(), self.before)
        self.assertEqual(self.images.read_progress(self.token, **self.scope), images_before)
        self.assertEqual(self.versions(), self.initial_versions)
        self.snapshot("three-spaces-guard-no-post")
        proposal = self.propose("核对第一镜字幕与全稿配图后保存")
        self.assertEqual(len(self.save_requests()), 1)
        self.assertEqual(self.save_requests()[0]["body"]["reason"], "核对第一镜字幕与全稿配图后保存")
        task_id = proposal["task_id"]
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and response.url.endswith(f"/tasks/{task_id}/confirm"), timeout=60000) as confirmed:
            self.page.locator("[data-confirm-script-revision]").click()
        self.assertEqual(confirmed.value.status, 200, confirmed.value.text())
        self.snapshot("valid-reason-real-child-created")
        self.assert_saved_child()

    def test_blank_trimmed_reason_does_not_block_lost_receipt_recovery_or_create_another_proposal(self):
        proposal = self.propose("匿名确认回执恢复验证")
        task_id = proposal["task_id"]
        lost = []

        def lose_successful_confirmation(route):
            response = route.fetch(timeout=60000)
            self.assertEqual(response.status, 200, response.text())
            lost.append(response.json()["task"]["status"])
            route.abort("connectionreset")

        self.page.route(f"**/tasks/{task_id}/confirm", lose_successful_confirmation)
        self.page.locator("[data-confirm-script-revision]").click()
        expect(self.page.locator("#formal-script-save-proposal")).to_contain_text("保存结果待核实", timeout=60000)
        self.assertEqual(lost, ["completed"])
        self.assertEqual(len(self.versions()), len(self.initial_versions) + 1)
        self.page.locator("#formal-script-save-reason").fill("   ")
        self.page.locator("#formal-script-save").click()
        expect(self.page.locator("#formal-script-save-proposal")).to_contain_text("上次保存结果待核实")
        expect(self.page.locator("#formal-script-save-proposal")).not_to_contain_text("不能只填空格")
        self.assertEqual(len(self.save_requests()), 1)
        self.snapshot("three-spaces-allow-existing-receipt-recovery")
        with self.page.expect_response(lambda response: response.request.method == "GET"
                and response.url.endswith(f"/tasks/{task_id}")) as recovered:
            self.page.locator("[data-confirm-script-revision]").click()
        self.assertEqual(recovered.value.status, 200, recovered.value.text())
        confirmations = [request for request in self.requests if request["method"] == "POST"
            and request["path"].endswith(f"/tasks/{task_id}/confirm")]
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(len(self.save_requests()), 1)
        self.assert_saved_child()

    def test_unconfirmed_image_save_error_link_enters_same_project_images_without_losing_draft(self):
        self.assertGreaterEqual(len(self.before["shots"]), 2)
        first, second = self.before["shots"][:2]
        progress_before = self.images.read_progress(self.token, **self.scope)
        unchecked = {item["shot_id"] for item in progress_before["selections"] if not item["checked"]}
        self.assertEqual(unchecked, {first["shot_id"], second["shot_id"]})
        self.page.locator("#formal-script-save-reason").fill("已核对正文，继续处理两镜待确认配图")
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/script/save?" in response.url) as rejected:
            self.page.locator("#formal-script-save").click()
        self.assertEqual(rejected.value.status, 409, rejected.value.text())
        detail = rejected.value.json()["detail"]
        self.assertEqual(detail["code"], "IMAGE_CONFIRMATION_REQUIRED")
        self.assertEqual({item["shot_id"] for item in detail["issues"]}, unchecked)
        proposal = self.page.locator("#formal-script-save-proposal")
        expect(proposal).to_contain_text("进度已保存，尚有 2 镜待处理")
        link = proposal.get_by_role("link", name="第 1 镜", exact=True)
        expect(link).to_be_visible()
        self.snapshot("real-save-409-with-two-unconfirmed-images")
        self.steps[-1].update(http_status=rejected.value.status, response_code=detail["code"],
            issue_shot_ids=sorted(unchecked), retained_shot_count=len(self.before["shots"]))
        self.navigation_dialogs, self.navigation_document_requests = [], []

        def cancel_document_leave(dialog):
            self.navigation_dialogs.append({"type": dialog.type, "message": dialog.message})
            dialog.dismiss()

        self.page.on("dialog", cancel_document_leave)
        self.page.on("request", lambda request: self.navigation_document_requests.append(request.url)
            if request.resource_type == "document" else None)
        link.click()
        expect(self.page.locator("#creative-tab-images")).to_have_attribute("aria-selected", "true")
        expect(self.page.locator("#formal-shot-image-workbench")).to_be_visible()
        parsed = urlparse(self.page.url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.path, f'/projects/{self.scope["project_id"]}/script')
        self.assertEqual(query.get("panel"), ["images"])
        self.assertEqual(query.get("shot_id"), [first["shot_id"]])
        expect(self.page.locator(f'[data-creative-shot="{first["shot_id"]}"]')).to_have_attribute("aria-current", "true")
        self.assertEqual(self.navigation_dialogs, [], "配图恢复入口必须走正常 SPA，不能触发离开页面确认")
        self.assertEqual(self.navigation_document_requests, [])
        self.assertEqual(self.current(), self.before)
        self.assertEqual(self.images.read_progress(self.token, **self.scope), progress_before)
        self.assertEqual(self.versions(), self.initial_versions)
        self.assertEqual(len(self.save_requests()), 1)
        self.snapshot("same-project-first-shot-image-panel")
        self.page.locator("#creative-tab-script").click()
        expect(self.cell()).to_have_value(self.changed_subtitle)
        self.assertEqual(self.errors, [])
        self.snapshot("same-project-shot-navigation-keeps-persisted-draft-and-parent")


if __name__ == "__main__":
    unittest.main()
