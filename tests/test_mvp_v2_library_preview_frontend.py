from pathlib import Path
import shutil
import subprocess
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as creative_browser
from tests.test_mvp_v2_ui00_journey3 import IMAGE_BYTES, PROJECT_ID


class LibraryPreviewFrontendTests(unittest.TestCase):
    def test_library_scope_and_shared_preview_lifecycle(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js is required for the browser state regression probe")
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [node, str(root / "tests/support/library_preview_frontend_probe.cjs")],
            cwd=root, capture_output=True, text=True, timeout=45, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS new package and project versions", result.stdout, "The async probe must finish every check")


class LibraryPreviewBrowserTests(unittest.TestCase):
    setUp = creative_browser.CreativeWorkspaceBrowserTests.setUp

    def test_thumbnail_browsing_cannot_confirm_and_invalid_original_can_retry(self):
        original_response = self.fixture.response
        catalog_requests = []
        preview_requests = []
        originals = []

        def response(method, path):
            status, value = original_response(method, path)
            if urlparse(path).path.endswith("/images/default-candidates"):
                catalog_requests.append(parse_qs(urlparse(path).query))
                candidates = value["library_candidates"][:2]
                for item in candidates:
                    item["preview_url"] = f"/api/formal/projects/{PROJECT_ID}/images/default-candidates/{item['candidate_key']}/preview"
                value.update(library_candidates=candidates, shots=[], inventory_records=321,
                             inventory_groups=266, filtered_groups=266, page_groups=2, page_records=2,
                             facets={"scene": [{"value": "现场", "label": "现场", "count": 2}]})
            return status, value

        def preview(route):
            variant = parse_qs(urlparse(route.request.url).query).get("variant", ["original"])[0]
            preview_requests.append(variant)
            if variant == "thumbnail":
                route.fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
            else:
                originals.append(route)

        self.fixture.response = response
        page = self.page
        page.route("**/images/default-candidates/*/preview?*", preview)
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        page.get_by_role("button", name="换图", exact=True).click()
        picker = page.get_by_role("dialog", name="第 1 镜", exact=True)
        picker.get_by_role("button", name="创意库", exact=True).click()
        expect(picker.locator('[data-picker-pixel="0"] img')).to_have_js_property("naturalWidth", 1)
        expect(picker.locator("[data-picker-use]")).to_be_disabled()
        expect(picker.locator(".creative-picker-counts")).to_contain_text("321 条")
        self.assertEqual(catalog_requests[-1]["match_scope"], ["all"])
        self.assertEqual(preview_requests, ["thumbnail", "thumbnail"])
        picker.locator('[data-picker-candidate="0"]').click()
        expect(picker.locator(".creative-preview-status").last).to_contain_text("正在读取原图")
        expect(picker.locator("[data-picker-use]")).to_be_disabled()
        self.assertEqual(len(originals), 1)
        originals.pop().fulfill(status=200, content_type="image/png", body=b"invalid image bytes")
        expect(picker.locator("[data-picker-detail-retry]")).to_be_visible()
        expect(picker.locator("[data-picker-use]")).to_be_disabled()
        picker.locator("[data-picker-detail-retry]").click()
        expect(picker.locator(".creative-preview-status").last).to_contain_text("正在读取原图")
        self.assertEqual(len(originals), 1)
        originals.pop().fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        expect(picker.locator(".creative-picker-selected img")).to_have_js_property("naturalWidth", 1)
        expect(picker.locator("[data-picker-use]")).to_be_enabled()
        self.assertFalse(self.fixture.progress_selections)
        self.assertFalse(self.fixture.writes)
        self.assertFalse(self.errors)

    def test_thumbnail_progress_counts_requested_sources_retry_and_current_page(self):
        original_response = self.fixture.response
        pending, requested = {}, []
        candidates = [{"candidate_key": f"progress-{index}", "duplicate_group_id": f"group-{index}",
                       "caption": f"匿名缩略图 {index}"} for index in range(1, 15)]
        duplicate = {"candidate_key": "progress-copy", "duplicate_group_id": "group-1",
                     "duplicate_of": "progress-1", "caption": "匿名同图另一个来源"}
        for item in [*candidates, duplicate]:
            item["preview_url"] = f"/api/formal/projects/{PROJECT_ID}/images/default-candidates/{item['candidate_key']}/preview"

        def response(method, path):
            status, value = original_response(method, path)
            if urlparse(path).path.endswith("/images/default-candidates"):
                page_no = int(parse_qs(urlparse(path).query).get("page", ["1"])[0])
                shown = [*candidates[:12], duplicate] if page_no == 1 else candidates[12:]
                value.update(library_candidates=shown, shots=[], inventory_records=15, inventory_groups=14,
                             filtered_groups=14, total_groups=14, page=page_no, page_size=12,
                             page_groups=12 if page_no == 1 else 2, page_records=len(shown),
                             has_more=page_no == 1, facets={})
            return status, value

        def preview(route):
            key = urlparse(route.request.url).path.split("/")[-2]
            self.assertEqual(parse_qs(urlparse(route.request.url).query)["variant"], ["thumbnail"])
            requested.append(key)
            if key in {"progress-1", "progress-2", "progress-3", "progress-copy", "progress-13"}:
                pending.setdefault(key, []).append(route)
            else:
                route.fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)

        self.fixture.response = response
        page = self.page
        page.route("**/images/default-candidates/*/preview?*", preview)
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        page.get_by_role("button", name="换图", exact=True).click()
        picker = page.get_by_role("dialog", name="第 1 镜", exact=True)
        picker.get_by_role("button", name="创意库", exact=True).click()
        progress = picker.locator("[data-picker-preview-progress]")
        expect(progress).to_have_text("缩略图：已加载 9 · 加载中 3 · 失败 0")
        self.assertEqual(set(requested), {f"progress-{index}" for index in range(1, 13)})
        self.assertEqual(len(requested), 12)
        expect(picker.locator(".creative-picker-counts")).to_have_text(
            "库存 15 条 · 14 组图片 · 筛选后 14 组 · 本页 12 组 / 13 条 · 第 1 页")
        pending["progress-1"][0].fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        pending["progress-2"][0].fulfill(status=200, content_type="image/png", body=b"invalid pixels")
        expect(progress).to_have_text("缩略图：已加载 10 · 加载中 1 · 失败 1")
        sources = picker.locator("[data-picker-duplicates] summary")
        sources.click()
        expect(progress).to_have_text("缩略图：已加载 10 · 加载中 2 · 失败 1")
        self.assertEqual(requested.count("progress-copy"), 1)
        sources.click()
        expect(progress).to_have_text("缩略图：已加载 10 · 加载中 2 · 失败 1")
        sources.click()
        expect(progress).to_have_text("缩略图：已加载 10 · 加载中 2 · 失败 1")
        self.assertEqual(requested.count("progress-copy"), 1)
        picker.locator('[data-picker-retry="1"]').click()
        expect(progress).to_have_text("缩略图：已加载 10 · 加载中 3 · 失败 0")
        pending["progress-2"][1].fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        expect(progress).to_have_text("缩略图：已加载 11 · 加载中 2 · 失败 0")
        pending["progress-copy"][0].fulfill(status=500, body="anonymous preview unavailable")
        expect(progress).to_have_text("缩略图：已加载 11 · 加载中 1 · 失败 1")
        picker.locator('[data-picker-retry="12"]').click()
        expect(progress).to_have_text("缩略图：已加载 11 · 加载中 2 · 失败 0")
        with page.expect_event("requestfailed", predicate=lambda request: "/progress-3/preview" in request.url):
            picker.get_by_role("button", name="下一页", exact=True).click()
        expect(progress).to_have_text("缩略图：已加载 1 · 加载中 1 · 失败 0")
        pending["progress-3"][0].fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        pending["progress-copy"][1].fulfill(status=200, content_type="image/png", body=b"late invalid pixels")
        expect(progress).to_have_text("缩略图：已加载 1 · 加载中 1 · 失败 0")
        expect(picker.locator(".creative-picker-counts")).to_have_text(
            "库存 15 条 · 14 组图片 · 筛选后 14 组 · 本页 2 组 / 2 条 · 第 2 页")
        pending["progress-13"][0].fulfill(status=200, content_type="image/png", body=IMAGE_BYTES)
        expect(progress).to_have_text("缩略图：已加载 2 · 加载中 0 · 失败 0")
        expect(picker.locator("[data-picker-use]")).to_be_disabled()
        self.assertFalse(self.fixture.progress_selections)
        self.assertFalse(self.fixture.writes)
        self.assertFalse(self.errors)
