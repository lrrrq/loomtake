from __future__ import annotations

from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, quote, urlparse
import unittest

from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_ui00_journey3 import IMAGE_BYTES, PROJECT_ID, UI_ROOT, _Fixture, _handler, _preview


class _ClarityFixture(_Fixture):
    def __init__(self):
        super().__init__(image_status="pending", default_image_partial=True, waiting=True)
        self.reference_choices = False
        self.library_page_size = 12

    def library_page_first_index(self, page):
        return (page - 1) * self.library_page_size + 1

    def image_workspace(self):
        value = super().image_workspace()
        for shot in value.get("shots", []):
            shot["shot_id"] = f"shot-stable-{shot['shot_no']}"
        return value

    def response(self, method, path):
        route = urlparse(path).path
        query = parse_qs(urlparse(path).query)
        if route == "/api/formal/setup":
            status, value = super().response(method, path)
            return status, {**value, "script_availability": "available"}
        if route.endswith("/script"):
            return 200, {"state": "ready", "base_version_id": "version-browser-current",
                "project_version": self.project_version, "version_label": "版本 1", "revision": 0,
                "can_save_version": False, "shots": [{"shot_id": f"shot-stable-{index}", "shot_no": str(index),
                    "structure": "主体", "visual": f"匿名画面 {index}", "duration_seconds": 20}
                    for index in (1, 2, 3)], "versions": []}
        if route == "/api/health":
            return 200, {"ui_build_id": "updated-anonymous-build"}
        if route.endswith("/images/default-candidates"):
            _, value = super().response(method, path)
            value["selection_mode"] = "category_library_browse"
            for shot in value.get("shots", []):
                shot["shot_id"] = f"shot-stable-{shot['shot_no']}"
            if self.reference_choices:
                for shot in value["shots"]:
                    if shot["state"] == "gap":
                        continue
                    shot["selected_candidate_key"] = ""
                    shot["candidates"] = [{"candidate_key": f"choice-{shot['shot_no']}-{variant}",
                        "caption": f"第{shot['shot_no']}镜参考图{variant}", "default_eligible": False,
                        "preview_url": f"/api/fixture/choice-{shot['shot_no']}-{variant}/preview"}
                        for variant in ("a", "b")]
            page = int(query.get("page", ["1"])[0])
            page_size = int(query.get("page_size", ["12"])[0])
            self.library_page_size = page_size
            candidates = [{"candidate_key": f"library-{index}", "caption": f"匿名活动图片 {index}",
                           "preview_url": f"/api/fixture/library-{index}/preview",
                           "source": {"file": "匿名图库.xlsx", "sheet": "素材", "row": index}}
                          for index in range(1, 31)]
            if query.get("query", [""])[0]:
                candidates = candidates[-1:]
            total = len(candidates)
            page_candidates = candidates[(page - 1) * page_size:page * page_size] if query.get("view") == ["library"] else []
            value.update(library_candidates=page_candidates, page=page, page_size=page_size,
                         inventory_records=30, inventory_groups=30, filtered_groups=total,
                         page_records=len(page_candidates), page_groups=len(page_candidates),
                         total_groups=total, total_images=total, has_more=page * page_size < total)
            return 200, value
        if route.startswith("/api/fixture/library-"):
            return 200, IMAGE_BYTES
        if route.startswith("/api/fixture/choice-"):
            return 200, IMAGE_BYTES
        if route == "/api/agent/tasks/default-image-proposal/confirm" and method == "POST":
            status, value = super().response(method, path)
            self.image_workspace_enabled = True
            value["response"].update(base_version_id="version-browser-current", project_version=self.project_version,
                adopted_selections=[{"shot_id": "shot-stable-2", "shot_no": "2", "image_key": "adopted-reference", "content_sha256": "f" * 64}])
            return status, value
        if route.endswith("/images/adopted-reference/preview"):
            return 200, IMAGE_BYTES
        if route.endswith("/image-workspace") and self.default_image_adopted:
            value = self.image_workspace()
            source_row = self.library_page_first_index(2)
            value["shots"][1]["candidates"] = [*value["shots"][1]["candidates"],
                {"image_key": "adopted-reference", "content_sha256": "f" * 64,
                 "caption": f"匿名活动图片 {source_row}", "default_eligible": False,
                 "sources": [{"label": "匿名图库.xlsx", "sheet": "素材", "row": source_row}]}]
            return 200, value
        if route.endswith("/plans/plan-for-browser"):
            value = _preview()
            value["template_summary"] = {"label": "匿名原模板", "package_version": 2,
                "selection_mode": "default", "original_segments": [{"structure": "开场", "shot_count": 1}, {"structure": "主体", "shot_count": 1}],
                "current_segments": [{"structure": "开场", "shot_count": 1}, {"structure": "主体", "shot_count": 2}],
                "structure_change_count": 1, "position_requirement_count": 1,
                "adjustments": [{"kind": "structure", "summary": "主体段补充一镜活动细节", "before": "1镜", "after": "2镜"},
                    {"kind": "position", "summary": "第3镜落实固定位置要求", "before": "未指定位置", "after": "第3镜"}],
                "structure_note": "按已确认需求调整"}
            value["traceability"][0]["position_rule"] = {"type": "fixed_shot", "shot_no": 3}
            value["traceability"][0]["template_source"] = [{"source_file": "匿名模板.xlsx", "source_sheet": "模版", "source_row": 7}]
            return 200, value
        if route.endswith("/review") or route.endswith("/review/decisions"):
            return 200, {"state": "reviewing", "project_version": self.project_version,
                "review_started": True, "ready_for_final": False,
                "decision": {"label": "请核对当前版本", "copy": "检查镜头后，选择需要通过的项目。", "problem_count": 3, "attention_count": 3},
                "issues": [{"issue_key": f"issue-{index}", "shot_no": str(index), "status": "open",
                            "focus_required": index == 1, "title": f"检查镜头 {index}", "visual": f"匿名活动画面 {index}",
                            "image_key": f"candidate-{index}", "image_caption": f"匿名图 {index}", "severity": "attention"}
                           for index in (1, 2, 3)]}
        return super().response(method, path)


class WorkbenchClarityBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _ClarityFixture()
        class Handler(_handler(self.fixture)):
            def _file(handler):
                if urlparse(handler.path).path.startswith("/assets/"):
                    return super()._file()
                body = (UI_ROOT / "index.html").read_text().replace("__MPLUS_UI_BUILD__", "loaded-anonymous-build").encode()
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
        self.page.set_default_timeout(7000)
        self.page.add_init_script("localStorage.setItem('mplus.agent.auth', 'browser-session');localStorage.setItem('mplus.agent.workspace', 'browser-workspace');")
        self.errors = []
        self.measurements = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def capture(self, name):
        directory = os.environ.get("MPLUS_CLARITY_UI_ARTIFACTS")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target / f"{name}.png"), full_page=True)

    def assert_no_overflow(self, selector, label):
        size = self.page.locator(selector).evaluate("element => ({width: element.clientWidth, scroll: element.scrollWidth})")
        self.assertLessEqual(size["scroll"], size["width"] + 1)
        self.measurements.append({"label": label, "viewport": self.page.viewport_size, **size})
        directory = os.environ.get("MPLUS_CLARITY_UI_ARTIFACTS")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            (target / f"{self._testMethodName}-layout.json").write_text(json.dumps(self.measurements, ensure_ascii=False, indent=2))

    def test_library_pages_keep_selection_and_late_preview_preserves_search_input(self):
        page = self.page
        pending, cancelled_page_preview = [], []
        page.route("**/api/fixture/library-1/preview?*", lambda route: pending.append(route))
        def hold_old_page_preview(route):
            if not cancelled_page_preview:
                cancelled_page_preview.append(route)
            else:
                route.fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        page.route("**/api/fixture/library-2/preview?*", hold_old_page_preview)
        proposals = []
        page.on("request", lambda request: proposals.append(request.post_data_json) if request.method == "POST" and "/images/default-proposals?" in request.url else None)
        page.goto(f"{self.base}/projects/{PROJECT_ID}/materials?return_to=shot-images&focus_shot=2")
        page.get_by_role("button", name="稍后", exact=True).click()
        page.get_by_role("button", name="从创意库选图", exact=True).first.click()
        expect(page.get_by_role("button", name="浏览图库", exact=True)).to_have_attribute("aria-pressed", "true")
        expect(page.locator(".ui00-library-candidate-group")).to_have_count(12)
        search = page.locator("[data-library-search]")
        search.fill("尚未提交的搜索")
        expect(search).to_be_focused()
        self.assertEqual(len(pending), 1)
        pending.pop().fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        expect(page.locator('[data-default-preview="library-1"] img')).to_have_js_property("naturalWidth", 1)
        expect(search).to_have_value("尚未提交的搜索")
        expect(search).to_be_focused()
        search.fill("")
        self.assertEqual(len(cancelled_page_preview), 1)
        with page.expect_event("requestfailed", predicate=lambda request: "/api/fixture/library-2/preview?" in request.url):
            page.get_by_role("button", name="下一页", exact=True).click()
        cancelled_page_preview[0].fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        selected_key = f"library-{self.fixture.library_page_first_index(2)}"
        choice = page.locator(f'[data-use-library-candidate="{selected_key}"]')
        expect(choice).to_be_enabled()
        expect(page.locator('[data-default-preview="library-2"]')).to_have_count(0)
        choice.click()
        expect(page.locator(".ui00-library-picked summary")).to_contain_text("已选 1 镜")
        page.get_by_role("button", name="上一页", exact=True).click()
        expect(page.locator('[data-use-library-candidate="library-1"]')).to_be_enabled()
        expect(page.locator(".ui00-library-picked summary")).to_contain_text("已选 1 镜")
        for width in (1440, 1024, 375):
            page.set_viewport_size({"width": width, "height": 1000})
            self.capture(f"library-{width}")
            self.assert_no_overflow(".ui00-library-grid", "library")
            self.assert_no_overflow("html", "document")
        page.get_by_role("button", name="采用可用参考图", exact=True).click()
        expect(page.locator("[data-default-proposal]")).to_be_visible()
        self.assertEqual(proposals[0]["selections"], [{"shot_no": "2", "candidate_key": selected_key}])
        self.assertEqual(self.fixture.default_image_confirm_posts, 0)
        self.assertEqual(self.errors, [])

    def test_plan_structure_and_mobile_trace_are_readable_without_generation(self):
        page = self.page
        self.fixture.image_status = "pass"
        page.goto(f"{self.base}/projects/{PROJECT_ID}/plan")
        summary = page.locator("#formal-plan-template-summary")
        expect(summary).to_contain_text("匿名原模板 · 版本 2")
        expect(summary).to_contain_text("按 Brief 调整 1 处")
        expect(page.locator("#formal-plan-capacity-details")).not_to_have_attribute("open")
        expect(page.locator("#formal-plan-shots-details")).not_to_have_attribute("open")
        self.assertEqual(page.locator(".ui00-plan-trace-table th").count(), 3)
        page.locator(".ui00-plan-trace-table details summary").first.click()
        expect(page.locator(".ui00-plan-trace-table details").first).to_contain_text("第 3 镜")
        expect(page.locator(".ui00-plan-trace-table details").first).to_contain_text("模版 Sheet · 第 7 行")
        for width in (1440, 1024, 375):
            page.set_viewport_size({"width": width, "height": 1000})
            self.capture(f"plan-{width}")
            self.assert_no_overflow(".ui00-plan-trace-table", "plan-trace")
            self.assert_no_overflow("html", "document")
        self.assertEqual(self.fixture.confirm_posts, 0)
        self.assertEqual(self.errors, [])

    def test_rapid_cross_shot_choices_keep_both_inputs_when_previews_finish_in_reverse(self):
        self.fixture.reference_choices = True
        page = self.page
        pending, submitted = {}, []
        page.route("**/api/fixture/choice-*/preview?*", lambda route: pending.update({urlparse(route.request.url).path: route}))
        page.on("request", lambda request: submitted.append(request.post_data_json) if request.method == "POST" and "/images/default-proposals?" in request.url else None)
        page.goto(f"{self.base}/projects/{PROJECT_ID}/materials")
        page.get_by_role("button", name="从创意库选图", exact=True).first.click()
        page.get_by_role("button", name="智能推荐", exact=True).click()
        page.locator("[data-library-scope]").select_option("all")
        first, second = page.locator('[data-default-image-select="0"]'), page.locator('[data-default-image-select="1"]')
        first.select_option("choice-1-a")
        second.select_option("choice-2-a")
        expect(first).to_have_value("choice-1-a")
        expect(second).to_have_value("choice-2-a")
        action = page.locator("[data-propose-default-images]")
        expect(action).to_be_disabled()
        self.assertEqual(len(pending), 2)
        pending.pop("/api/fixture/choice-2-a/preview").fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        expect(page.locator('[data-default-preview="choice-2-a"] img')).to_have_js_property("naturalWidth", 1)
        expect(action).to_be_disabled()
        second.focus()
        pending.pop("/api/fixture/choice-1-a/preview").fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        expect(page.locator('[data-default-preview="choice-1-a"] img')).to_have_js_property("naturalWidth", 1)
        expect(second).to_be_focused()
        expect(first).to_have_value("choice-1-a")
        expect(second).to_have_value("choice-2-a")
        expect(action).to_be_enabled()
        action.click()
        expect(page.locator("[data-default-proposal]")).to_be_visible()
        self.assertEqual(submitted[0]["selections"], [{"shot_no": "1", "candidate_key": "choice-1-a"}, {"shot_no": "2", "candidate_key": "choice-2-a"}])
        self.assertEqual(self.fixture.default_image_confirm_posts, 0)
        self.assertEqual(self.errors, [])

    def test_adopted_reference_returns_to_exact_shot_unconfirmed_and_survives_refresh(self):
        page = self.page
        for returning in (True, False):
            with self.subTest(returning=returning):
                suffix = "?return_to=shot-images&focus_shot=2" if returning else ""
                page.goto(f"{self.base}/projects/{PROJECT_ID}/materials{suffix}")
                page.get_by_role("button", name="从创意库选图", exact=True).first.click()
                if not returning:
                    page.locator("[data-library-shot]").select_option("2")
                page.get_by_role("button", name="下一页", exact=True).click()
                selected_key = f"library-{self.fixture.library_page_first_index(2)}"
                choice = page.locator(f'[data-use-library-candidate="{selected_key}"]')
                expect(choice).to_be_enabled()
                choice.click()
                page.get_by_role("button", name="采用可用参考图", exact=True).click()
                page.locator("[data-confirm-default-images]").click()
                if not returning:
                    page.get_by_role("link", name="继续逐镜确认", exact=True).click()
                page.wait_for_url(f"**/projects/{quote(PROJECT_ID)}/script?*panel=images*")
                expect(page.locator('[data-image-shot-select="1"]')).to_have_value("adopted-reference")
                expect(page.locator('[data-image-shot-confirm="1"]')).not_to_be_checked()
                expect(page.locator('#formal-image-shot-preview-1 img')).to_have_js_property("naturalWidth", 1)
                page.reload()
                expect(page.locator('[data-image-shot-select="1"]')).to_have_value("adopted-reference")
                expect(page.locator('[data-image-shot-confirm="1"]')).not_to_be_checked()
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertEqual(self.errors, [])

    def test_batch_controls_are_visible_and_only_select_the_active_group(self):
        page = self.page
        decisions = []
        page.on("request", lambda request: decisions.append(request.post_data_json) if request.method == "POST" and "/review/decisions?" in request.url else None)
        page.goto(f"{self.base}/projects/{PROJECT_ID}/review")
        page.get_by_role("button", name="稍后", exact=True).click()
        batch = page.locator("#formal-review-batch")
        expect(batch).to_be_visible()
        expect(page.get_by_role("button", name="批量通过", exact=True)).to_be_disabled()
        for width in (1440, 1024, 375):
            page.set_viewport_size({"width": width, "height": 1000})
            self.capture(f"review-{width}")
            self.assert_no_overflow("#formal-review-batch", "review-batch")
            self.assert_no_overflow("html", "document")
            box = batch.bounding_box()
            self.assertLess(box["y"], 1000, "批量栏在首屏可发现")
        page.locator(".ui00-review-others > summary").click()
        page.get_by_role("button", name="选择当前组", exact=True).click()
        expect(page.locator('[data-review-select="0"]')).not_to_be_checked()
        expect(page.locator('[data-review-select="1"]')).to_be_checked()
        expect(page.locator('[data-review-select="2"]')).to_be_checked()
        page.get_by_role("button", name="批量通过", exact=True).click()
        expect(page.locator("#formal-review-batch-count")).to_contain_text("已选")
        self.assertEqual(decisions[0]["issue_keys"], ["issue-2", "issue-3"])
        self.assertEqual(decisions[0]["decision"], "approve")
        self.assertEqual(self.errors, [])

    def test_update_refresh_retains_manual_image_input_until_cleared(self):
        page = self.page
        documents = []
        page.on("request", lambda request: documents.append(request.url) if request.resource_type == "document" else None)
        page.goto(f"{self.base}/projects/{PROJECT_ID}/materials")
        review = page.locator("details.ui00-image-review").first
        review.locator("summary").click()
        caption = review.locator('[name="caption"]')
        original = caption.input_value()
        caption.fill("尚未提交的图片说明")
        page.get_by_role("button", name="刷新页面", exact=True).click()
        expect(caption).to_have_value("尚未提交的图片说明")
        expect(page.locator("#ui-update-copy")).to_contain_text("未提交")
        self.assertEqual(len(documents), 1)
        caption.fill(original)
        page.get_by_role("button", name="刷新页面", exact=True).click()
        page.wait_for_load_state("networkidle")
        self.assertEqual(len(documents), 2)
        self.assertEqual(self.fixture.image_analysis_posts, 0)
        self.assertEqual(self.errors, [])
