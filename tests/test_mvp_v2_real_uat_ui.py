from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse
import unittest

from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_creative_workspace_browser import _CreativeFixture
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID, UI_ROOT, _handler


class _RealUatUiFixture(_CreativeFixture):
    """Anonymous server DTOs for UI regressions; not real service acceptance."""

    def __init__(self):
        super().__init__()
        self.dirty_draft = False
        self.overview_reads = 0
        self.blocked_review = False
        self.review_counts = True
        self.failed_save = False

    def script(self):
        model = super().script()
        model.update(state="editing" if self.dirty_draft else "ready",
                     can_save_version=self.dirty_draft, can_undo=self.dirty_draft,
                     can_redo=bool(self.script_revision and not self.dirty_draft))
        return model

    def response(self, method, path):
        route = urlparse(path).path
        if route.endswith("/overview"):
            self.overview_reads += 1
            return 200, {"draft": {"can_save_version": self.dirty_draft}, "next_action": {
                "label": "保存当前稿" if self.dirty_draft else "进入配图",
                "reason": "请先保存当前稿" if self.dirty_draft else "为已保存版本逐镜配图",
                "href": f"/projects/{PROJECT_ID}/script?panel={'script' if self.dirty_draft else 'images'}",
            }}
        if method == "POST" and route.rsplit("/", 1)[-1] in {"undo", "redo", "discard"}:
            self.dirty_draft = route.endswith("/redo")
            self.script_revision += 1
            return 200, self.script()
        if route.endswith("/script/table") and method == "POST":
            self.dirty_draft = True
        if route.endswith("/script/save") and self.failed_save:
            return 200, {"task_id": "failed-save", "response": {
                "type": "action_proposal", "draft_only": False, "proposal_digest": "d" * 64,
                "summary": "保存第3镜声音修改", "affected_shots": ["3"],
                "preview": {"changes": [{"shot_id": "shot-stable-3", "shot_no": "3",
                    "field": "sound", "label": "第 3 镜 · 声音", "before": "环境声", "after": "保留环境声并平滑衔接"}]},
            }}
        if route == "/api/agent/tasks/failed-save/confirm" and self.failed_save:
            return 200, {"task": {"status": "failed", "error_code": "FORMAL_EXECUTION_FAILED"},
                "response": {"type": "action_failed", "error_code": "FORMAL_EXECUTION_FAILED",
                    "summary": "internal exception details must stay private"}}
        if route.endswith("/review") and self.blocked_review:
            model = {"state": "blocked", "project_version": self.project_version,
                "base_version_id": self.base_version_id, "review_started": False,
                "ready_for_final": False, "read_only": True,
                "message": "请先完成逐镜配图确认。",
                "issues": [{"shot_id": shot["shot_id"], "shot_no": shot["shot_no"],
                    "issue_key": "", "preview": True, "read_only": True,
                    "status": "open", "status_label": "待处理", "severity": "attention",
                    "severity_label": "提醒", "focus_required": False,
                    "title": "检查本镜图片、内容与来源", "visual": shot["visual"],
                    "image_key": "candidate-3" if shot["shot_no"] == "3" else "",
                    "image_caption": "匿名推荐图片" if shot["shot_no"] == "3" else "",
                } for shot in self.shots]}
            if self.review_counts:
                model["decision"] = {"problem_count": 3, "serious_count": 0,
                    "attention_count": 3, "approved_count": 0}
            return 200, model
        return super().response(method, path)


class RealUatUiRegressionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _RealUatUiFixture()
        fixture = self.fixture

        class Handler(_handler(fixture)):
            def _api(handler, method):
                fixture.body = {}
                if method in {"POST", "PATCH"} and "application/json" in handler.headers.get("Content-Type", ""):
                    fixture.body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", "0"))) or "{}")
                return super()._api(method)

            def _file(handler):
                if urlparse(handler.path).path.startswith("/assets/"):
                    return super()._file()
                body = (UI_ROOT / "index.html").read_text().replace("__MPLUS_UI_BUILD__", "updated-anonymous-build").encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.page.set_default_timeout(5000)
        self.page.add_init_script("localStorage.setItem('mplus.agent.auth', 'browser-session');localStorage.setItem('mplus.agent.workspace', 'browser-workspace');")
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def capture(self, name):
        directory = os.environ.get("MPLUS_REAL_UAT_UI_ARTIFACTS")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target / f"{name}.png"), full_page=True)

    def test_undo_redo_refreshes_saved_draft_without_blocking_image_work(self):
        self.fixture.dirty_draft = True
        self.fixture.script_revision = 1
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        expect(page.locator("#creative-image-draft-gate")).to_be_hidden()
        page.locator('[data-creative-panel="script"]').click()
        page.locator("#formal-script-undo").click()
        page.wait_for_function("state.formalScript?.can_save_version === false")
        expect(page.locator("#creative-next-label")).to_have_text("配图进行中 · 全稿 3 镜")
        expect(page.locator("#creative-next-action")).to_have_text("继续第 1 镜")
        expect(page.locator("#creative-next-copy")).to_contain_text("当前修改可保存进度，配齐后统一保存新版本。")
        page.locator('[data-creative-panel="images"]').click()
        expect(page.locator("#creative-image-draft-gate")).to_be_hidden()
        expect(page.locator('[data-image-shot="0"]')).to_be_visible()
        expect(page.locator("#formal-shot-image-project-required")).to_be_hidden()
        page.locator('[data-creative-panel="script"]').click()
        page.locator("#formal-script-redo").click()
        page.wait_for_function("state.formalScript?.can_save_version === true")
        expect(page.locator("#creative-next-action")).to_have_text("继续第 1 镜")
        page.locator('[data-creative-panel="images"]').click()
        expect(page.locator("#creative-image-draft-gate")).to_be_hidden()
        self.assertGreaterEqual(self.fixture.overview_reads, 3)
        self.assertEqual(self.errors, [])
        self.capture("undo-redo-gate")

    def test_library_return_keeps_jump_and_current_shot_in_sync(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        expect(page.locator('[data-image-shot="0"]')).to_be_visible()
        page.locator('[data-creative-shot="shot-stable-3"]').click()
        page.locator("[data-creative-open-library]").click()
        expect(page.locator("#creative-image-picker")).to_be_visible()
        page.locator("#creative-image-picker [data-picker-close]").click()
        expect(page.locator("#creative-image-picker")).to_be_hidden()
        expect(page.locator('[data-image-shot="2"]')).to_be_visible()
        expect(page.locator('[data-creative-shot="shot-stable-3"]')).to_have_attribute("aria-current", "true")
        expect(page.locator("#formal-image-jump")).to_have_value("2")
        self.assertEqual(self.errors, [])
        self.capture("library-return-third-shot")

    def test_discard_keeps_immediate_edits_while_overview_refresh_is_pending(self):
        self.fixture.dirty_draft = True
        self.fixture.script_revision = 1
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=script")
        expect(page.locator("#formal-script-discard")).to_be_enabled()
        page.wait_for_load_state("networkidle")
        pending = []

        def hold_overview(route):
            pending.append(route)

        page.route("**/overview", hold_overview)
        try:
            page.once("dialog", lambda dialog: dialog.accept())
            with page.expect_request("**/overview"):
                page.locator("#formal-script-discard").click()
            expect(page.locator("#formal-script-save-state")).to_have_text("当前版本已保存，可直接编辑。")
            self.assertEqual(len(pending), 1)
            subtitle = page.locator('[data-script-shot-key="shot-stable-1"][data-script-field="subtitle"]')
            # Optional fields are collapsed in the shot view until opened.
            subtitle.locator('..').locator('summary').click()
            value = "放弃后立即输入的新字幕"
            with page.expect_response("**/script/table?*") as saved:
                subtitle.fill(value)
            self.assertEqual(saved.value.status, 200)
            expect(page.locator("#creative-next-label")).to_have_text("配图进行中 · 全稿 3 镜")
            pending.pop().continue_()
            page.unroute("**/overview", hold_overview)
            page.wait_for_load_state("networkidle")
            expect(subtitle).to_have_value(value)
            self.assertEqual(self.fixture.shots[0]["subtitle"], value)
            self.capture("discard-immediate-edit-after-delayed-overview")
            page.reload()
            expect(subtitle).to_have_value(value)
            expect(page.locator("#creative-next-label")).to_have_text("配图进行中 · 全稿 3 镜")
            self.assertEqual(self.errors, [])
        finally:
            for route in pending:
                route.continue_()

    def test_blocked_review_does_not_offer_batch_approval(self):
        self.fixture.blocked_review = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/review")
        expect(page.locator("#formal-review-state")).to_have_text("前置未完成")
        expect(page.locator("#formal-review-problem-count")).to_have_text("3")
        expect(page.locator("#formal-review-attention-count")).to_have_text("3")
        expect(page.locator("#formal-review-issues")).not_to_contain_text("可选择批量通过")
        expect(page.locator("#formal-review-batch")).to_be_hidden()
        expect(page.locator("[data-review-select]")).to_have_count(0)
        expect(page.locator("#formal-review-final-submit")).to_be_disabled()
        self.assertEqual(self.errors, [])
        self.capture("blocked-review-preview")

    def test_late_discard_overview_does_not_clear_newly_opened_image_controls(self):
        self.fixture.dirty_draft = True
        self.fixture.script_revision = 1
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=script")
        expect(page.locator("#formal-script-discard")).to_be_enabled()
        page.wait_for_load_state("networkidle")
        overviews, image_reads = [], []
        page.route("**/overview", lambda route: overviews.append(route))
        try:
            page.once("dialog", lambda dialog: dialog.accept())
            with page.expect_request("**/overview"):
                page.locator("#formal-script-discard").click()
            page.locator('[data-creative-panel="images"]').click()
            select = page.locator("[data-image-shot-select='0']")
            expect(select).to_be_visible()
            select.select_option("candidate-2")
            expect(select).to_have_value("candidate-2")
            page.route("**/image-workspace?*", lambda route: image_reads.append(route))
            with page.expect_response("**/overview") as refreshed:
                overviews.pop().continue_()
            self.assertEqual(refreshed.value.status, 200)
            page.wait_for_load_state("networkidle")
            expect(page.locator("#creative-next-copy")).to_have_text("第 1 镜：填写复用理由。当前修改可保存进度，配齐后统一保存新版本。")
            expect(select).to_be_visible()
            expect(select).to_have_value("candidate-2")
            self.assertEqual(image_reads, [])
            self.assertEqual(self.errors, [])
            self.capture("discard-late-overview-image-controls")
        finally:
            for route in overviews + image_reads:
                route.continue_()

    def test_blocked_review_distinguishes_preview_and_missing_images(self):
        self.fixture.blocked_review = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/review")
        page.locator(".ui00-review-others > summary").click()
        expect(page.locator("#formal-review-image-2 img")).to_have_attribute("alt", "镜头 3 配图预览：匿名推荐图片")
        expect(page.locator("#formal-review-image-0")).to_have_text("本镜尚未配图")
        expect(page.locator("#formal-review-issues")).not_to_contain_text("正在读取已确认图片")
        self.assertEqual(self.errors, [])
        self.capture("blocked-review-images")

    def test_missing_server_review_counts_are_not_reported_as_zero(self):
        self.fixture.blocked_review = True
        self.fixture.review_counts = False
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/review")
        expect(page.locator("#formal-review-state")).to_have_text("前置未完成")
        expect(page.locator("#formal-review-problem-count")).to_have_text("—")
        expect(page.locator("#formal-review-attention-count")).to_have_text("—")
        expect(page.locator("#formal-review-approved-count")).to_have_text("—")
        self.assertEqual(self.errors, [])

    def test_confirmed_brief_renders_while_template_catalog_is_pending(self):
        page = self.page
        pending = []
        page.route("**/templates?*", lambda route: pending.append(route))
        page.goto(f"{self.base}/projects/{PROJECT_ID}/brief")
        try:
            expect(page.locator("#formal-brief-readiness")).to_have_text("已确认")
            expect(page.locator("#formal-fact-count")).to_have_text("1 项")
            expect(page.locator("#formal-fact-list")).to_contain_text("项目名称：匿名发布会")
            self.assertEqual(len(pending), 1)
            self.assertEqual(self.errors, [])
            self.capture("brief-before-template-catalog")
        finally:
            for route in pending:
                route.continue_()

    def test_current_structure_uses_saved_shots_when_no_plan_is_waiting(self):
        self.fixture.waiting = False
        self.fixture.shots = [self.fixture.shots[1], self.fixture.shots[0], self.fixture.shots[2]]
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=structure")
        expect(page.locator("#formal-plan-action")).to_be_visible()
        current = page.locator("#creative-current-structure")
        expect(current).to_be_visible()
        expect(current).to_contain_text("版本 1")
        expect(current).to_contain_text("3 镜")
        rows = current.locator("[data-structure-shot-id]")
        expect(rows).to_have_count(3)
        self.assertEqual([row.get_attribute("data-structure-shot-id") for row in rows.all()],
                         [shot["shot_id"] for shot in self.fixture.shots])
        for shot, row in zip(self.fixture.shots, rows.all()):
            expect(row).to_contain_text(shot["visual"])
            expect(row).to_contain_text(shot["structure"])
        expect(page.locator("#formal-plan-create")).to_be_enabled()
        expect(page.locator("#formal-plan-confirmation")).to_be_hidden()
        expect(page.locator("#formal-plan-preview")).to_be_hidden()
        self.capture("saved-current-structure")
        page.locator('[data-creative-panel="script"]').click()
        page.locator('[data-creative-shot="shot-stable-3"]').click()
        page.locator('[data-script-shot-key="shot-stable-3"][data-script-field="visual"]').fill("第3镜当前草稿的画面")
        expect(page.locator("#creative-next-label")).to_have_text("配图进行中 · 全稿 3 镜")
        page.locator('[data-creative-panel="structure"]').click()
        expect(current).to_contain_text("当前草稿")
        expect(current.locator('[data-structure-shot-id="shot-stable-3"]')).to_contain_text("第3镜当前草稿的画面")
        self.assertEqual(self.fixture.plan_posts, 0)
        self.assertEqual(self.fixture.confirm_posts, 0)
        self.assertEqual(self.errors, [])
        self.capture("current-draft-structure")
        self.fixture.waiting = True
        page.reload()
        expect(page.locator("#formal-plan-preview")).to_be_visible()
        expect(current).to_be_hidden()
        expect(page.locator("#formal-plan-confirmation")).to_be_visible()
        self.assertEqual(self.fixture.plan_posts, 0)
        self.assertEqual(self.fixture.confirm_posts, 0)

    def test_structure_updates_when_table_save_finishes_after_switching_panels(self):
        self.fixture.waiting = False
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=structure")
        expect(page.locator("#formal-plan-action")).to_be_visible()
        page.locator('[data-creative-panel="script"]').click()
        page.locator('[data-creative-shot="shot-stable-3"]').click()
        pending = []
        page.route("**/script/table?*", lambda route: pending.append(route))
        try:
            with page.expect_request("**/script/table?*"):
                page.locator('[data-script-shot-key="shot-stable-3"][data-script-field="visual"]').fill("第3镜稍后完成暂存的画面")
                page.locator('[data-creative-panel="structure"]').click()
            current = page.locator("#creative-current-structure")
            expect(current).to_be_visible()
            expect(page.locator("#creative-next-label")).to_have_text("文字修改尚未保存")
            expect(page.locator("#creative-progress-status")).to_have_attribute("data-state", "pending")
            self.assertEqual(len(pending), 1)
            pending.pop().continue_()
            expect(page.locator("#creative-next-label")).to_have_text("配图进行中 · 全稿 3 镜")
            expect(current).to_contain_text("当前草稿")
            expect(current.locator('[data-structure-shot-id="shot-stable-3"]')).to_contain_text("第3镜稍后完成暂存的画面")
            self.assertEqual(self.errors, [])
            self.capture("late-table-save-current-structure")
        finally:
            for route in pending:
                route.continue_()

    def test_failed_save_confirmation_shows_safe_failure_without_success_claim(self):
        self.fixture.dirty_draft = True
        self.fixture.script_revision = 1
        self.fixture.failed_save = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=script")
        page.locator("#creative-save > summary").click()
        page.locator("#formal-script-save-reason").fill("保存第3镜声音修改")
        page.locator("#formal-script-save").click()
        page.locator("[data-confirm-script-revision]").click()
        proposal = page.locator("#formal-script-save-proposal")
        expect(proposal).to_contain_text("修改未完成")
        expect(proposal).to_contain_text("本次操作已停止，请查看任务记录并联系管理员核对。")
        expect(proposal).not_to_contain_text("新版本已保存")
        expect(proposal).not_to_contain_text("internal exception details")
        expect(page.locator("#creative-next-label")).to_have_text("配图进行中 · 全稿 3 镜")
        self.assertEqual(self.errors, [])
        self.capture("failed-save-safe-message")


if __name__ == "__main__":
    unittest.main()
