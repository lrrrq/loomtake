"""Confirmed design regressions at the UI/DTO boundary, not production certification."""
from pathlib import Path
import os
from urllib.parse import urlparse
import unittest

from playwright.sync_api import expect

from tests import test_mvp_v2_workbench_v2_browser as workbench_browser


class ConfirmedDesignBrowserTests(unittest.TestCase):
    setUp = workbench_browser.WorkbenchV2BrowserTests.setUp

    def capture(self, name):
        directory = os.environ.get("MPLUS_CONFIRMED_DESIGN_ARTIFACTS")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target / (name + ".png")), full_page=True)

    def test_completed_version_keeps_delivery_next_action_and_editor_in_view(self):
        original = self.fixture.response

        def response(method, path):
            status, value = original(method, path)
            route = urlparse(path).path
            if route.endswith("/overview"):
                status = 200
                value = {**value, "draft": {"state": "ready", "base_version_id": self.fixture.base_version_id},
                         "saved_delivery": {"version_label": "版本 1", "href": "/projects/p-a/review"},
                         "next_action": {"label": "下载正式交付", "href": "/projects/p-a/review",
                                         "reason": "整片确认已完成，可下载正式 Excel。"}}
            if route.endswith("/image-workspace"):
                status, value = 200, self.fixture.image_workspace()
                value["state"] = "confirmed"
                for shot in value["shots"]:
                    shot.update(confirmed=True, needs_recheck=False, explicitly_changed=False)
            return status, value

        self.fixture.response = response
        page = self.page
        page.goto(self.base + "/projects/p-a/script")
        expect(page.locator("#creative-next-action")).to_have_text("下载正式交付")
        expect(page.locator("#creative-tab-script")).to_have_attribute("aria-selected", "true")
        expect(page.locator("#creative-next-copy")).not_to_contain_text("进入人工审核")
        expect(page.locator('.creative-workflow [aria-current="step"]')).to_have_count(0)
        field = page.locator('[data-script-shot-id="shot-stable-1"] textarea').first
        expect(field).to_be_visible()
        self.capture("script-completed-desktop")
        self.assertLess(field.bounding_box()["y"], 700)
        self.assertEqual(self.errors, [])

    def test_image_modification_changes_examples_without_replacing_user_input(self):
        page = self.page
        page.goto(self.base + "/projects/p-a/script")
        page.locator("#creative-assistant > summary").click()
        page.locator("#creative-assistant-kind").select_option("images")
        expect(page.locator("#formal-script-instruction")).to_have_attribute("placeholder", "例如：第 2 镜换成工厂全景，避免人物特写。")
        page.locator(".creative-quick-feedback > summary").click()
        expect(page.locator("#formal-script-quick-feedback")).not_to_contain_text("旁白更自然")
        page.locator("#formal-script-instruction").fill("保留我写的选图要求")
        page.locator("#creative-assistant-kind").select_option("text")
        expect(page.locator("#formal-script-instruction")).to_have_value("保留我写的选图要求")
        expect(page.locator("#formal-script-quick-feedback")).to_contain_text("旁白更自然")
        self.assertEqual(self.errors, [])

    def test_unavailable_project_list_does_not_report_zero_and_can_retry(self):
        self.fixture.failure = ("/projects", 503)
        self.page.goto(self.base + "/projects")
        expect(self.page.locator("#formal-project-list")).to_contain_text("读取失败")
        expect(self.page.locator("#formal-project-count")).to_have_text("—")
        self.fixture.failure = None
        self.page.locator("[data-projects-retry]").click()
        expect(self.page.locator("#formal-project-count")).to_have_text("2 个")
        expect(self.page.locator("#formal-project-list")).to_contain_text("项目A")
        self.assertEqual(self.errors, [])

    def test_completed_review_does_not_ask_for_more_approvals(self):
        original = self.fixture.response

        def response(method, path):
            status, value = original(method, path)
            if urlparse(path).path.endswith("/review"):
                value.update(state="confirmed", review_started=True, ready_for_final=False)
                for issue in value.get("issues", []):
                    issue.update(status="approved", focus_required=False)
            return status, value

        self.fixture.response = response
        self.page.goto(self.base + "/projects/p-a/review")
        expect(self.page.locator("#formal-review-issues")).to_contain_text("本版审核已完成")
        expect(self.page.locator("#formal-review-issues")).not_to_contain_text("可选择批量通过")
        expect(self.page.locator("#formal-review-final-form")).to_be_hidden()
        self.assertEqual(self.errors, [])

    def test_asset_search_explains_real_kind_scope_and_source(self):
        original = self.fixture.response

        def response(method, path):
            if urlparse(path).path == "/api/agent/workbench/assets/search":
                return 200, {"results": [{"asset_id": "anonymous-source", "asset_kind": "workbook",
                    "title": "匿名结构模板.xlsx", "workflow_state": "published",
                    "source": {"scope": "shared", "source_file": "匿名结构模板.xlsx", "source_sheet": "模版", "source_row": 3}}]}
            return original(method, path)

        self.fixture.response = response
        self.page.goto(self.base + "/assets?project_id=p-a")
        expect(self.page.locator("#asset-desk")).to_be_visible()
        self.page.locator("#asset-search-query").fill("结构")
        self.page.locator("#asset-search").click()
        expect(self.page.locator("#asset-search-results")).to_contain_text("Excel")
        expect(self.page.locator("#asset-search-results")).to_contain_text("同类型共享")
        expect(self.page.locator("#asset-search-results")).to_contain_text("已登记")
        self.page.locator("#asset-search-results summary").click()
        expect(self.page.locator("#asset-search-results")).to_contain_text("模版 · 第 3 行")
        self.assertEqual(self.errors, [])

    def test_project_paging_and_search_do_not_hide_matching_projects(self):
        self.fixture.projects = [{"project_id": f"p-{index}", "name": f"活动项目{index:02}",
                                  "status": "archived" if index > 24 else "active", "project_version": 1}
                                 for index in range(1, 27)]
        page = self.page
        page.goto(self.base + "/projects")
        expect(page.locator("#formal-project-count")).to_have_text("24 个")
        expect(page.locator("#formal-project-list article")).to_have_count(12)
        page.get_by_role("navigation", name="项目分页", exact=True).get_by_role("button", name="下一页").click()
        expect(page.locator("#formal-project-list")).to_contain_text("活动项目24")
        page.locator("#formal-project-search").fill("01")
        expect(page.locator("#formal-project-list")).to_contain_text("活动项目01")
        expect(page.locator("#formal-project-list article")).to_have_count(1)
        page.locator("#formal-project-search").fill("25")
        page.locator('[data-page-section="formal-archived-list"]').click()
        expect(page.locator("#formal-archived-list")).to_be_visible()
        expect(page.locator("#formal-archived-list")).to_contain_text("活动项目25")
        self.assertEqual(self.errors, [])

    def test_section_navigation_preserves_inputs_and_opens_closed_import_region(self):
        page = self.page
        page.goto(self.base + "/assets?project_id=p-a")
        page.locator("#asset-search-query").fill("保留这个关键词")
        page.locator('[data-page-section="asset-local-path"]').click()
        expect(page.locator(".asset-intake-panel")).to_have_attribute("open", "")
        expect(page.locator("#asset-local-path")).to_be_focused()
        page.locator('[data-page-section="asset-search-query"]').click()
        expect(page.locator("#asset-search-query")).to_have_value("保留这个关键词")
        expect(page.locator("#asset-search-query")).to_be_focused()
        page.set_viewport_size({"width": 390, "height": 844})
        self.capture("assets-mobile")
        self.assertEqual(self.errors, [])
