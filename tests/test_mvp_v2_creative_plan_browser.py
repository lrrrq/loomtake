"""Browser regression for rebuilding a frozen creative plan without losing its binding."""
import unittest
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright

from tests import test_mvp_v2_ui00_journey3 as ui


class _CreativePlanFixture(ui._Fixture):
    def __init__(self, stale):
        super().__init__(waiting=True)
        self.stale = stale

    def response(self, method, path):
        route = urlparse(path).path
        if route.endswith("/creative-draft"):
            return 200, {"project_id": ui.PROJECT_ID, "project_version": self.project_version,
                "revision": 12, "blocks": []}
        if route.endswith("/plans/plan-for-browser") and self.stale:
            return 409, {"detail": {"code": "CREATIVE_PLAN_STALE", "message": "初稿已更新"}}
        status, value = super().response(method, path)
        if route.endswith("/confirm") and status == 200:
            value["pending_confirmations"] = [{"item": "启动形式待确认", "shot_no": "2", "status": "待确认"}]
            self.script_model = {'state': 'ready', 'project_name': '匿名发布会', 'version_label': '版本 1',
                'project_version': self.project_version, 'base_version_id': 'version-newly-created',
                'revision': 0, 'shots': [{**shot, 'shot_id': f'shot-{index + 1}', 'structure': '发布会结构'}
                    for index, shot in enumerate(ui._preview()['shots'])], 'versions': [], 'pending_confirmations': []}
        if ((route.endswith("/plans") and method == "POST")
                or route.endswith("/plans/plan-for-browser")) and status == 200:
            self.stale = False
            value["creative_conversion"] = {"mapped_count": 3, "added_shot_count": 0}
        return status, value


class CreativePlanBrowserTests(unittest.TestCase):
    def exercise(self, stale):
        fixture = _CreativePlanFixture(stale)
        helper = ui.Ui00JourneyThreeTests("runTest")
        self.addCleanup(helper.doCleanups)
        base = helper._server(fixture)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context, page = helper._page(browser, 1440)
            page.set_default_timeout(7000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            # A recovered plan can be reached without the original creative URL flag.
            page.goto(f"{base}/projects/{ui.PROJECT_ID}/script?panel=structure&mode=text-only")
            button = page.locator("#formal-plan-create" if stale else "#formal-plan-regenerate")
            expect(button).to_be_visible()
            self.assertEqual(fixture.plan_posts, 0)
            self.assertEqual(fixture.confirm_posts, 0)
            with page.expect_request(lambda req: req.method == "POST" and urlparse(req.url).path.endswith("/plans")) as posted:
                button.click()
            self.assertEqual(posted.value.post_data_json["expected_creative_revision"], 12)
            self.assertEqual(posted.value.post_data_json["generation_mode"], "text_only_draft")
            expect(page.locator("#formal-plan-confirmation")).to_be_visible()
            expect(page.locator("#formal-plan-confirm-check")).not_to_be_checked()
            expect(page.locator("#formal-plan-confirm")).to_be_disabled()
            self.assertEqual(fixture.plan_posts, 1)
            self.assertEqual(fixture.confirm_posts, 0)
            self.assertEqual(fixture.provider_calls, 0)
            page.reload()
            expect(page.locator("#formal-plan-regenerate")).to_be_visible()
            self.assertEqual(fixture.plan_posts, 1)
            self.assertEqual(errors, [])
            page.locator("#formal-plan-confirm-check").check()
            page.locator("#formal-plan-confirm").click()
            expect(page.locator("#formal-plan-script-rows")).to_contain_text("第 2 镜：启动形式待确认")
            expect(page.locator("#formal-plan-script-rows")).not_to_contain_text('"shot_no"')
            expect(page.locator('#formal-script-version-label')).to_have_text('版本 1')
            context.close()
            browser.close()

    def test_stale_plan_can_rebuild_from_current_creative_revision(self):
        self.exercise(True)

    def test_explicit_rebuild_keeps_recovered_creative_binding_and_requires_confirmation(self):
        self.exercise(False)
