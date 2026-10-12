from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_formal_browser_journey import _FixtureServer, _open_new_project_brief


class FormalFrontendReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-frontend-review-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.server = _FixtureServer(self.root, scenario="qingyu_star_activity")
        self.server.start()
        self.addCleanup(self.server.stop)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.page.set_default_timeout(10000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("console", lambda message: self.errors.append(message.text) if message.type == "error" else None)
        self.page.goto(self.server.base_url + "/projects")
        self.page.wait_for_load_state("networkidle")
        self.page.locator("#formal-owner-confirm").check()
        self.page.locator("#formal-owner-enable").click()
        expect(self.page.locator("#formal-project-new")).not_to_have_attribute("aria-disabled", "true")

    def capture(self, label):
        configured = os.environ.get("MPLUS_FRONTEND_EVIDENCE_ROOT", "")
        if configured:
            target = Path(configured) / self._testMethodName
            target.mkdir(parents=True, exist_ok=True)
            self.page.locator("#toast").wait_for(state="hidden", timeout=5000)
            self.page.wait_for_timeout(600)
            self.page.screenshot(path=str(target / f"{label}.png"), full_page=True)
            (target / f"{label}.json").write_text(json.dumps({
                "url": self.page.url,
                "console_errors": self.errors,
                "brief_text": self.page.locator("#formal-brief-text").input_value(),
                "brief_selection": self.page.locator("#formal-brief-selection").inner_text(),
                "plan_state": self.page.locator("#formal-plan-state").inner_text(),
            }, ensure_ascii=False, indent=2), encoding="utf-8")

    def create_project(self, name):
        self.page.locator("#formal-project-new").click()
        self.page.wait_for_url("**/projects/new")
        self.page.locator("#formal-project-name").fill(name)
        expect(self.page.locator("#formal-project-category")).not_to_be_visible()
        with self.page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/projects")) as created:
            self.page.locator("#formal-project-create button[type=submit]").click()
        self.assertNotIn("category_id", created.value.request.post_data_json)
        _open_new_project_brief(self.page)
        self.page.locator("#formal-brief-readiness", has_text="等待资料").wait_for()
        return urlparse(self.page.url).path.split("/")[2]

    def dashboard(self):
        self.page.locator(".nav-item[data-route=projects]").click()
        self.page.wait_for_url("**/projects")
        self.page.locator("#formal-project-new:not([aria-disabled=true])").wait_for()

    def open_brief(self, project_id):
        self.dashboard()
        self.page.locator(f'#formal-project-list a[href^="/projects/{project_id}/"]').click()
        self.page.locator(f'#formal-breadcrumb a[href="/projects/{project_id}/overview"]').click()
        self.page.wait_for_url(f"**/projects/{project_id}/overview")
        self.page.locator('#formal-project-navigation a[data-route="brief"]').click()
        self.page.wait_for_url(f"**/projects/{project_id}/brief")

    def upload_fixture(self, *, image=False):
        names = ["01-brief.pdf", "02-brief.docx", "03-brief.xlsx", "04-brief.pptx"]
        if image:
            names.append("qingyu-project-01.png")
        self.page.locator("#formal-brief-files").set_input_files([str(self.root / "fixtures" / name) for name in names])
        self.page.locator("#formal-brief-upload-button").click()
        self.page.locator("#formal-standard-card-state", has_text="待确认").wait_for()

    def test_brief_unsaved_text_files_and_fact_edit_are_project_scoped(self):
        project_a = self.create_project("匿名项目甲")
        self.upload_fixture()
        self.page.locator("[data-edit-formal-fact]").first.click()
        marker = "项目名称: 只属于匿名项目甲的未提交内容"
        self.page.locator("#formal-brief-text").fill(marker)
        self.page.locator("#formal-brief-files").set_input_files(str(self.root / "fixtures" / "qingyu-project-01.png"))
        self.capture("a-unsaved")
        self.dashboard()
        project_b = self.create_project("匿名项目乙")
        self.capture("b-clean")
        expect(self.page.locator("#formal-brief-text")).to_have_value("")
        expect(self.page.locator("#formal-brief-files")).to_have_value("")
        expect(self.page.locator("#formal-brief-selection")).to_have_text("尚未选择文件")
        expect(self.page.locator("#formal-brief-upload-button")).to_have_text("导入并整理")
        self.page.locator("#formal-brief-text").fill("项目名称: 匿名项目乙的资料\n品牌: 乙品牌")
        with self.page.expect_response(lambda r: r.request.method == "POST" and "/brief/files" in r.url) as submitted:
            self.page.locator("#formal-brief-upload-button").click()
        self.assertIn(f"/projects/{project_b}/", submitted.value.url)
        self.assertNotIn("只属于匿名项目甲", submitted.value.text())
        self.open_brief(project_a)
        expect(self.page.locator("#formal-brief-text")).to_have_value(marker)
        expect(self.page.locator("#formal-brief-selection")).to_contain_text("qingyu-project-01.png")
        expect(self.page.locator("#formal-brief-upload-button")).to_have_text("保存字段修改")
        self.capture("a-restored")
        self.page.go_back()
        self.page.wait_for_url(f"**/projects/{project_a}/overview")
        self.page.go_forward()
        self.page.wait_for_url(f"**/projects/{project_a}/brief")
        expect(self.page.locator("#formal-brief-text")).to_have_value(marker)
        expect(self.page.locator("#formal-brief-selection")).to_contain_text("qingyu-project-01.png")
        self.assertEqual(self.errors, [])

    def test_late_brief_upload_does_not_replace_another_projects_input(self):
        project_a = self.create_project("匿名异步项目甲")
        self.dashboard()
        project_b = self.create_project("匿名异步项目乙")
        self.open_brief(project_a)
        held = []

        def hold_upload(route):
            held.append((route, route.fetch()))

        self.page.route(f"**/projects/{project_a}/brief/files?*", hold_upload)
        self.page.locator("#formal-brief-text").fill("项目名称: 匿名异步甲\n品牌: 甲品牌")
        self.page.locator("#formal-brief-upload-button").click()
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
        self.assertEqual(len(held), 1)
        self.open_brief(project_b)
        marker = "项目名称: 匿名异步乙的未提交输入"
        self.page.locator("#formal-brief-text").fill(marker)
        self.page.locator("#formal-brief-files").set_input_files(str(self.root / "fixtures" / "qingyu-project-02.png"))
        held[0][0].fulfill(response=held[0][1])
        self.page.wait_for_timeout(200)
        self.capture("b-after-late-a-response")
        expect(self.page.locator("#formal-brief-text")).to_have_value(marker)
        expect(self.page.locator("#formal-brief-selection")).to_contain_text("qingyu-project-02.png")
        expect(self.page.locator("#formal-fact-list")).not_to_contain_text("甲品牌")
        expect(self.page.locator("#formal-brief-upload-button")).to_be_enabled()
        self.open_brief(project_a)
        expect(self.page.locator("#formal-brief-text")).to_have_value("")
        self.page.locator("#formal-brief-text").fill("项目名称: 匿名甲补充资料")
        self.page.locator("#formal-brief-upload-button").click()
        deadline = time.monotonic() + 10
        while len(held) < 2 and time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
        self.assertEqual(len(held), 2)
        self.page.locator("#formal-brief-text").fill("项目名称: 上传期间新输入仍应保留")
        held[1][0].fulfill(response=held[1][1])
        expect(self.page.locator("#formal-brief-upload-button")).to_be_enabled()
        expect(self.page.locator("#formal-brief-text")).to_have_value("项目名称: 上传期间新输入仍应保留")
        self.assertEqual(self.errors, [])

    def test_late_brief_read_does_not_replace_current_projects_facts(self):
        project_a = self.create_project("匿名延迟读取甲")
        self.upload_fixture()
        expect(self.page.locator("#formal-brief-upload-button")).to_be_enabled()
        self.dashboard()
        project_b = self.create_project("匿名延迟读取乙")
        held = []
        self.page.route(f"**/projects/{project_a}/brief/current?*", lambda route: held.append((route, route.fetch())))
        self.open_brief(project_a)
        deadline = time.monotonic() + 10
        while not held and time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
        self.assertEqual(len(held), 1)
        self.open_brief(project_b)
        expect(self.page.locator("#formal-brief-readiness")).to_have_text("等待资料")
        self.page.locator("#formal-brief-text").fill("乙项目独立输入")
        held[0][0].fulfill(response=held[0][1])
        self.page.wait_for_timeout(200)
        self.capture("b-after-late-a-read")
        expect(self.page.locator("#formal-fact-count")).to_have_text("0 项")
        expect(self.page.locator("#formal-brief-text")).to_have_value("乙项目独立输入")
        expect(self.page.locator("#formal-brief-confirm")).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_pending_images_allow_explicit_text_only_and_real_generation(self):
        self._text_only_generation(failed=False)

    def test_failed_images_allow_explicit_text_only_and_real_generation(self):
        self._text_only_generation(failed=True)

    def _text_only_generation(self, *, failed):
        project_id = self.create_project("匿名文字草稿项目")
        self.upload_fixture(image=True)
        self.page.locator("#formal-brief-confirm").click()
        self.page.locator("#formal-standard-card-state", has_text="已确认").wait_for()
        with sqlite3.connect(self.root / "workspace.sqlite3") as database:
            self.assertEqual(database.execute("SELECT category_id FROM projects WHERE project_id=?", (project_id,)).fetchone()[0], "star_activity")
        if failed:
            self.page.locator('#formal-project-navigation a[data-route="materials"]').click()
            card = self.page.locator(".ui00-image-card[data-state=pending]").first
            card.locator(".ui00-image-review summary").click()
            form = card.locator("[data-formal-image-form]")
            form.locator("[name=status]").select_option("failed")
            for field in ("caption", "tags", "subjects", "scene", "purpose", "quality", "brand", "evidence"):
                form.locator(f"[name={field}]").fill("匿名人工核对：图片无法采用")
            form.locator("button[type=submit]").click()
            self.page.locator(".ui00-image-card[data-state=failed]").wait_for()
        self.page.goto(self.server.base_url + f"/projects/{project_id}/script?mode=text-only&panel=structure")
        self.page.wait_for_load_state("networkidle")
        self.capture("explicit-text-only")
        expect(self.page.locator("#formal-plan-create")).to_be_visible()
        expect(self.page.locator("#formal-plan-create")).to_have_text("生成文字规划")
        expect(self.page.locator("#formal-plan-image-block-title")).to_have_text("文字草稿")
        self.page.goto(self.server.base_url + f"/projects/{project_id}/plan")
        self.page.locator("#formal-plan-state", has_text="暂不能生成规划").wait_for()
        choice = self.page.locator("#formal-plan-image-block a", has_text="仅生成文字草稿")
        expect(choice).to_have_attribute("href", f"/projects/{project_id}/script?mode=text-only&panel=structure")
        choice.click()
        self.page.wait_for_url("**/script?mode=text-only&panel=structure")
        self.page.locator("#formal-plan-create").wait_for()
        self.page.locator("#formal-plan-image-block a", has_text="前往项目素材").click()
        self.page.wait_for_url("**/script?return_to=plan&mode=text-only&panel=images&aux=materials")
        self.page.locator("#formal-image-to-plan:not([aria-disabled=true])").wait_for()
        if not failed:
            with sqlite3.connect(self.root / "workspace.sqlite3") as database:
                brief_before = database.execute(
                    "SELECT current_brief_snapshot_id, lock_version FROM projects WHERE project_id=?", (project_id,),
                ).fetchone()
                brief_count = database.execute(
                    "SELECT COUNT(*) FROM brief_versions WHERE project_id=?", (project_id,),
                ).fetchone()[0]
            self.page.locator("#formal-image-upload-files").set_input_files(str(self.root / "fixtures" / "qingyu-project-02.png"))
            with self.page.expect_response(lambda r: r.request.method == "POST" and urlparse(r.url).path.endswith("/image-workspace/supplements")) as supplement:
                self.page.locator("#formal-image-upload-button").click()
            self.assertEqual(supplement.value.status, 200, supplement.value.text())
            self.assertFalse(supplement.value.json()["brief_reupload_required"])
            self.assertTrue(supplement.value.json()["requires_visual_analysis"])
            expect(self.page.locator("#formal-image-upload-state")).to_contain_text("已加入 1 张图片")
            self.page.wait_for_url("**/script?return_to=plan&mode=text-only&panel=images&aux=materials")
            expect(self.page.locator("#formal-image-total")).to_have_text("2")
            with sqlite3.connect(self.root / "workspace.sqlite3") as database:
                self.assertEqual(database.execute(
                    "SELECT current_brief_snapshot_id, lock_version FROM projects WHERE project_id=?", (project_id,),
                ).fetchone(), brief_before)
                self.assertEqual(database.execute(
                    "SELECT COUNT(*) FROM brief_versions WHERE project_id=?", (project_id,),
                ).fetchone()[0], brief_count)
        self.page.locator("#formal-image-to-plan").click()
        self.page.wait_for_url("**/script?mode=text-only&panel=structure")
        self.page.locator("#formal-plan-create").wait_for()
        self.capture("returned-to-text-plan")
        with self.page.expect_response(lambda r: r.request.method == "POST" and urlparse(r.url).path.endswith("/plans")) as plan:
            self.page.locator("#formal-plan-create").click()
        self.assertEqual(plan.value.status, 200, plan.value.text())
        self.assertEqual(plan.value.request.post_data_json["generation_mode"], "text_only_draft")
        self.page.locator("#formal-plan-confirmation").wait_for()
        self.page.locator("#formal-plan-confirm-check").check()
        self.page.locator("#formal-plan-confirm").click()
        expect(self.page.locator("#formal-plan-script-rows tbody tr")).to_have_count(8)
        self.capture("text-draft-created")
        self.page.locator("#formal-plan-to-script").click()
        self.page.wait_for_url(f"**/projects/{project_id}/script")
        expect(self.page.locator("#formal-script-table tbody tr")).to_have_count(8)
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
