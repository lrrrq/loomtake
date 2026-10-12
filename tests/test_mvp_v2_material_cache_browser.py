from __future__ import annotations

from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import unquote, urlparse
import unittest

from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_ui00_journey3 import IMAGE_BYTES, PROJECT_ID, _Fixture, _handler


class _MaterialCacheFixture(_Fixture):
    """Anonymous HTTP boundary: three usable images and one awaiting analysis."""

    def __init__(self):
        super().__init__(image_workspace=True)
        self.analyzed = False

    def image_readiness(self):
        value = super().image_readiness()
        image = value["images"][3]
        if self.analyzed:
            image["summary"]["caption"] = "已核对的活动近景"
        else:
            image.update(status="pending", summary=None)
            value["pass"] = 3
            value["pending"] = 1
        return value

    def image_workspace(self):
        value = super().image_workspace()
        value["available_image_count"] = 4 if self.analyzed else 3
        for shot in value["shots"]:
            if self.analyzed:
                shot["candidates"][3]["caption"] = "已核对的活动近景"
            else:
                shot["candidates"] = shot["candidates"][:3]
        return value

    def response(self, method, path):
        route = unquote(urlparse(path).path)
        if method == "POST" and route.endswith("/image-workspace/supplements"):
            version = self.project_version
            status, value = super().response(method, path)
            self.project_version = version
            if status == 200:
                value["project_version"] = version
            return status, value
        if method == "POST" and route.endswith("/images/candidate-4/analysis"):
            self.requests.append((method, route))
            self.image_analysis_posts += 1
            self.analyzed = True
            return 200, self.image_readiness()
        if method == "POST" and route.endswith("/images/understand"):
            self.requests.append((method, route))
            self.image_understand_posts += 1
            self.analyzed = True
            value = self.image_readiness()
            value["automatic_understanding"] = {
                "processed": 1, "remaining": 0, "needs_review": 0,
                "failed": 0, "has_more": False,
            }
            return 200, value
        return super().response(method, path)


class MaterialCacheBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _MaterialCacheFixture()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self.fixture))
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.page.add_init_script(
            "localStorage.setItem('mplus.agent.auth', 'browser-session');"
            "localStorage.setItem('mplus.agent.workspace', 'browser-workspace');"
        )
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.goto(
            f"http://127.0.0.1:{self.server.server_port}/projects/{PROJECT_ID}/script?panel=images"
        )
        expect(self.page.locator('[data-image-shot="0"]')).to_be_visible()
        self.workspace_reads = self.fixture.request_count(
            f"/api/formal/projects/{PROJECT_ID}/image-workspace"
        )
        self.page.locator('#formal-project-navigation a[data-route="materials"]').click()
        expect(self.page.locator("#formal-image-pending")).to_have_text("1")

    def assert_return_reads_current_image_workspace(self):
        self.page.locator("[data-creative-close-materials]").click()
        expect(self.page.locator("#page-shot-images")).to_be_visible()
        self.page.wait_for_load_state("networkidle")
        reads = self.fixture.request_count(f"/api/formal/projects/{PROJECT_ID}/image-workspace")
        self.assertEqual(
            reads, self.workspace_reads + 1,
            "Returning from changed materials must read the current server image candidates",
        )
        self.assertEqual(self.fixture.project_version, 7)
        self.assertEqual(self.errors, [])

    def assert_newly_analyzed_candidate_is_available(self):
        select = self.page.locator('[data-image-shot-select="0"]')
        expect(select.locator('option[value="candidate-4"]')).to_contain_text("已核对的活动近景")
        select.select_option("candidate-4")
        expect(select).to_have_value("candidate-4")

    def test_multi_upload_reloads_previously_visited_image_workspace(self):
        self.page.locator("#formal-image-upload-files").set_input_files([
            {"name": name, "mimeType": "image/png", "buffer": IMAGE_BYTES}
            for name in ("anonymous-one.png", "anonymous-two.png")
        ])
        self.page.locator("#formal-image-upload-button").click()
        expect(self.page.locator("#formal-image-upload-state")).to_contain_text("已加入 2 张图片")
        expect(self.page.locator("#formal-image-total")).to_have_text("6")
        self.assertEqual(len(self.fixture.supplement_payloads), 2)
        self.assert_return_reads_current_image_workspace()

    def test_manual_analysis_refreshes_candidates_when_returning_to_image_workspace(self):
        card = self.page.locator('[data-formal-image-key="candidate-4"]')
        card.locator("details.ui00-image-review > summary").click()
        form = card.locator("[data-formal-image-form]")
        for field, value in {
            "caption": "已核对的活动近景", "tags": "活动,近景", "subjects": "产品",
            "scene": "活动现场", "purpose": "镜头配图", "quality": "清晰", "brand": "无",
            "evidence": "已核对原图及资料来源",
        }.items():
            form.locator(f'[name="{field}"]').fill(value)
        form.locator('button[type="submit"]').click()
        expect(card.locator("h2")).to_have_text("已核对的活动近景")
        self.assertEqual(self.fixture.image_analysis_posts, 1)
        self.assert_return_reads_current_image_workspace()
        self.assert_newly_analyzed_candidate_is_available()

    def test_automatic_analysis_refreshes_candidates_when_returning_to_image_workspace(self):
        self.page.locator("#formal-image-understand").click()
        expect(self.page.locator('[data-formal-image-key="candidate-4"] h2')).to_have_text("已核对的活动近景")
        self.assertEqual(self.fixture.image_understand_posts, 1)
        self.assert_return_reads_current_image_workspace()
        self.assert_newly_analyzed_candidate_is_available()
