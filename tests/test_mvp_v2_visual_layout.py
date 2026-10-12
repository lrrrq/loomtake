from __future__ import annotations

import os
from pathlib import Path
import unittest

from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as creative_browser
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class VisualLayoutTests(unittest.TestCase):
    """Anonymous browser evidence for the reported outline clipping regression."""

    setUp = creative_browser.CreativeWorkspaceBrowserTests.setUp

    def capture(self, name):
        destination = os.environ.get("MPLUS_VISUAL_UI_ARTIFACTS")
        if destination:
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(folder / f"{name}.png"), full_page=True)

    def test_long_shot_outline_stays_inside_card_before_and_after_batch_selection(self):
        self.fixture.shots[0]["visual"] = (
            "活动现场的参与者围绕长桌进行交流，镜头自然移向窗边的主持人，保留人物关系和完整环境。"
            "InternationalProductLaunchAudienceInteractionAndBrandPresentation "
        ) * 3
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        button = page.locator('[data-creative-shot="shot-stable-1"]')
        expect(button).to_be_visible()
        for width in (1440, 1024, 375, 1920):
            page.set_viewport_size({"width": width, "height": 1000})
            for batch in (False, True, False):
                workspace = page.locator(".creative-workspace")
                if workspace.get_attribute("data-batch-mode") != str(batch).lower():
                    page.locator('[data-creative-batch-toggle]').click()
                button.focus()
                expect(button).to_be_focused()
                self.capture(f"outline-{width}-batch-{batch}")
                item_box = button.locator("..").bounding_box()
                text_box = button.locator("strong").bounding_box()
                self.assertGreaterEqual(text_box["x"] - item_box["x"], 12, "Shot summary must not touch the card edge")
                self.assertLessEqual(text_box["x"] + text_box["width"], item_box["x"] + item_box["width"] - 12)
                expect(button.locator("b")).to_have_css("font-style", "normal")
                self.assertLessEqual(page.locator("html").evaluate("el => el.scrollWidth"), width + 1)
        self.assertFalse(self.errors)
        self.assertFalse(self.fixture.writes, "Presentation controls must not create business writes")

    def test_open_filters_keep_picker_actions_visible_in_short_viewports(self):
        page = self.page
        for width, height in ((375, 667), (960, 666), (1152, 800), (1440, 1000), (1920, 1080)):
            page.set_viewport_size({"width": width, "height": height})
            page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
            page.get_by_role("button", name="换图", exact=True).click()
            picker = page.get_by_role("dialog", name="第 1 镜", exact=True)
            picker.get_by_role("button", name="创意库", exact=True).click()
            expect(picker.locator(".creative-picker-filter-panel > summary")).to_be_visible()
            picker.locator(".creative-picker-filter-panel > summary").click()
            for index in (0, 5):
                control = picker.locator(".creative-picker-filters select").nth(index)
                control.focus()
                expect(control).to_be_focused()
            self.capture(f"picker-filters-{width}x{height}")
            footer = picker.locator("footer").bounding_box()
            self.assertLessEqual(footer["y"] + footer["height"], height, "Picker actions must remain in the viewport while filters expand")
            self.assertGreaterEqual(picker.locator(".creative-picker-results").bounding_box()["height"], 80)
            page.keyboard.press("Escape")
            expect(picker).not_to_be_visible()
        page.emulate_media(reduced_motion="reduce")
        page.get_by_role("button", name="换图", exact=True).click()
        expect(page.locator(".creative-picker")).to_have_css("animation-name", "none")
        expect(page.locator(".creative-picker-card").first).to_have_css("transition-duration", "0s")
        self.assertFalse(self.errors)
        self.assertFalse(self.fixture.writes)


if __name__ == "__main__":
    unittest.main()
