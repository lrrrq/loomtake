"""Actual multipart upload: preserve partial success and retry only corrected input."""
from io import BytesIO
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from PIL import Image
from playwright.sync_api import expect, sync_playwright
from tests import test_mvp_v2_single_shot_library_journey as gallery
from tests import test_mvp_v2_formal_zero_image_browser_journeys as zero


class MaterialUploadRecoveryBrowserTests(unittest.TestCase):
    def test_partial_upload_keeps_success_visible_and_corrected_retry_does_not_repeat_it(self):
        root = Path(tempfile.mkdtemp(prefix="material-upload-", dir=os.environ.get("MPLUS_V3_EVIDENCE_ROOT") or None))
        if not os.environ.get("MPLUS_V3_EVIDENCE_ROOT"):
            self.addCleanup(shutil.rmtree, root)
        server = gallery._GalleryServer(root)
        self.addCleanup(server.stop)
        server.start()
        def image_bytes(color):
            out = BytesIO()
            Image.new("RGB", (80, 64), color).save(out, "PNG")
            return out.getvalue()
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            errors, uploads = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: uploads.append(response.status)
                    if response.request.method == "POST" and "/image-workspace/supplements?" in response.url else None)
            project = gallery.SingleShotLibraryBrowserJourneyTests("runTest")._create_project(page, server.base_url)
            zero.FormalZeroImageBrowserJourneyTests("runTest")._confirm_zero_image_brief(page, server.base_url, root, project)
            page.goto(server.base_url + "/projects/" + project + "/materials")
            expect(page.locator("#formal-image-total")).to_have_text("0")
            page.locator("#formal-image-upload-files").set_input_files([
                {"name": "good-first.png", "mimeType": "image/png", "buffer": image_bytes("red")},
                {"name": "broken-second.png", "mimeType": "image/png", "buffer": b"not a PNG"},
                {"name": "good-third.png", "mimeType": "image/png", "buffer": image_bytes("green")},
            ])
            page.locator("#formal-image-upload-button").click()
            status = page.locator("#formal-image-upload-state")
            expect(status).to_contain_text("已加入 1 张")
            expect(status).to_contain_text("good-first.png：已加入")
            expect(status).to_contain_text("broken-second.png：未完成")
            expect(status).to_contain_text("good-third.png：未上传")
            expect(page.locator("#formal-image-total")).to_have_text("1")
            self.assertEqual(page.locator("#formal-image-upload-files").evaluate("el => Array.from(el.files, f => f.name)"),
                             ["broken-second.png", "good-third.png"])
            page.screenshot(path=str(root / "partial.png"), full_page=True)
            page.locator("#formal-image-upload-files").set_input_files([
                {"name": "broken-second.png", "mimeType": "image/png", "buffer": image_bytes("blue")},
                {"name": "good-third.png", "mimeType": "image/png", "buffer": image_bytes("green")},
            ])
            page.locator("#formal-image-upload-button").click()
            expect(status).to_contain_text("已加入 2 张图片")
            expect(page.locator("#formal-image-total")).to_have_text("3")
            page.reload()
            expect(page.locator("#formal-image-total")).to_have_text("3")
            self.assertEqual(uploads[0], 200)
            self.assertGreaterEqual(uploads[1], 400)
            self.assertEqual(uploads[2:], [200, 200])
            self.assertEqual(errors, [])
            with sqlite3.connect(root / "workspace.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM project_images WHERE project_id=?", (project,)).fetchone()[0], 3)
            page.screenshot(path=str(root / "recovered.png"), full_page=True)
            browser.close()
