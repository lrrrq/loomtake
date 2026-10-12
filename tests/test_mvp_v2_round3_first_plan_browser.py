"""Anonymous real-HTTP diagnostics for navigation before the first draft exists.

Reuses the existing authority fixture. It does not verify retained user projects
or external model quality, and it does not inject browser application state.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect
from tests import test_mvp_v2_rounds_repair_browser as rounds_fixture


class Round3FirstPlanBrowserTests(unittest.TestCase):
    def setUp(self):
        self.helper = rounds_fixture.RoundsRepairBrowserTests("runTest")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.page = self.helper.page
        self.snapshots = []

    def snapshot(self, label):
        directory = os.environ.get("MPLUS_ROUND3_PLAN_BROWSER_ARTIFACTS", "").strip()
        link = self.page.locator("#creative-first-plan-link")
        self.snapshots.append({"label": label, "url": self.page.url,
            "plan_href": link.get_attribute("href"),
            "plan_aria_disabled": link.get_attribute("aria-disabled"),
            "summary": self.page.locator("#creative-first-summary").text_content(),
            "plan_status": self.page.locator("#formal-plan-state").text_content(),
            "plan_copy": self.page.locator("#formal-plan-status-copy").text_content()})
        if directory:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(path / f"{self._testMethodName}-{label}.png"), full_page=True)

    def tearDown(self):
        directory = os.environ.get("MPLUS_ROUND3_PLAN_BROWSER_ARTIFACTS", "").strip()
        if not directory:
            return
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path / f"{self._testMethodName}-final.png"), full_page=True)
        (path / f"{self._testMethodName}.json").write_text(json.dumps({
            "snapshots": self.snapshots, "url": self.page.url,
            "requests": self.helper.requests, "page_errors": self.helper.errors,
            "actual_http_authorities_and_sqlite": True, "external_models": "deterministic",
            "retained_user_projects_verified": False, "browser_state_injection": False,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    def enter_confirmed_empty_draft(self):
        self.helper.ready_brief()
        self.page.locator("#brief-enter-creative").click()
        expect(self.page.locator("#creative-first-draft")).to_be_visible()
        expect(self.page.locator("#creative-first-status")).to_have_text("需求已确认，可以开始创作。")
        expect(self.page.locator("#creative-first-summary")).to_have_text("0 段 · 总时长 0 秒")
        expect(self.page.locator("#creative-first-plan-link")).to_have_attribute("aria-disabled", "false")
        value = self.helper.draft()
        self.assertEqual(value["understanding"]["confirmation_status"], "confirmed")
        self.assertEqual(value["blocks"], [])
        self.assertIsNone(value["task"])
        return value

    def assert_structure_route(self):
        expect(self.page.locator("#creative-tab-structure")).to_have_attribute("aria-selected", "true")
        expect(self.page.locator("#page-plan")).to_be_visible()
        expect(self.page.locator("#formal-plan-workbench")).to_be_visible()
        expect(self.page.locator("#formal-plan-project-required")).to_be_hidden()
        expect(self.page.locator("#formal-plan-state")).to_have_text("等待生成规划")
        expect(self.page.locator("#formal-plan-status-copy")).to_contain_text("需求与创作标准已确认")
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        query = parse_qs(urlparse(self.page.url).query)
        self.assertEqual(query.get("panel"), ["structure"])
        self.assertNotIn("creative", query)

    def test_confirmed_zero_blocks_normal_click_return_and_reload(self):
        before = self.enter_confirmed_empty_draft()
        self.snapshot("before-click")
        self.page.locator("#creative-first-plan-link").click()
        self.assert_structure_route()
        self.snapshot("structure")
        self.page.go_back()
        expect(self.page.locator("#creative-first-draft")).to_be_visible()
        expect(self.page.locator("#creative-first-status")).to_have_text("需求已确认，可以开始创作。")
        self.page.locator("#creative-first-plan-link").click()
        self.assert_structure_route()
        self.page.reload()
        self.assert_structure_route()
        self.snapshot("reloaded")
        after = self.helper.draft()
        self.assertEqual(after["blocks"], [])
        self.assertEqual(after["revision"], before["revision"])
        writes = [r for r in self.helper.requests if r["method"] == "POST"
            and "/creative-draft/" in r["path"]]
        self.assertEqual(writes, [])
        self.assertEqual(self.helper.errors, [])

    def test_confirmed_zero_blocks_href_agrees_with_plain_click_destination(self):
        self.enter_confirmed_empty_draft()
        self.snapshot("zero-block-href")
        query = parse_qs(urlparse(self.page.locator("#creative-first-plan-link").get_attribute("href")).query)
        self.assertEqual(query.get("panel"), ["structure"])
        self.assertNotIn("creative", query,
            "零段查看结构的链接与普通点击目的地应一致，不能携带不存在的初稿输入")


if __name__ == "__main__":
    unittest.main()
