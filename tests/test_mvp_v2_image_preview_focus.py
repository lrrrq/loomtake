from __future__ import annotations

from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.parse import quote
import unittest

from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_ui00_journey3 import IMAGE_BYTES, PROJECT_ID, _Fixture, _handler


class ImagePreviewFocusTests(unittest.TestCase):
    def test_delayed_supplement_preview_preserves_open_form_input_and_focus(self):
        fixture = _Fixture(image_status="pending")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fixture))
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.add_init_script(
                "localStorage.setItem('mplus.agent.auth', 'browser-session');"
                "localStorage.setItem('mplus.agent.workspace', 'browser-workspace');"
            )
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            pending_previews = []
            page.route("**/images/opaque-browser-image/preview?*", lambda route: pending_previews.append(route))
            page.goto(
                f"{base}/projects/{quote(PROJECT_ID)}/materials"
                "?return_to=shot-images&focus_shot=1&supplement_image_key=opaque-browser-image"
            )
            card = page.locator('[data-formal-image-key="opaque-browser-image"]')
            review = card.locator("details.ui00-image-review")
            expect(review).to_be_visible()
            self.assertIsNone(review.get_attribute("open"), "待分析图片不应自动展开人工表单")
            review.locator("summary").click()
            caption = review.locator('[name="caption"]')
            caption.fill("正在核对的匿名舞台图片")
            expect(caption).to_be_focused()
            self.assertEqual(len(pending_previews), 1)

            pending_previews.pop().fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
            preview = card.locator(".ui00-image-preview img")
            expect(preview).to_be_visible()
            expect(preview).to_have_js_property("complete", True)
            expect(preview).to_have_js_property("naturalWidth", 1)
            expect(review).to_have_attribute("open", "")
            expect(caption).to_have_value("正在核对的匿名舞台图片")
            expect(caption).to_be_focused()
            self.assertEqual(fixture.image_analysis_posts, 0)
            self.assertEqual(fixture.image_version_posts, 0)
            self.assertEqual(errors, [])
            browser.close()
