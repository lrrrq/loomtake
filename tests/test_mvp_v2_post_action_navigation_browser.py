"""Late post-action refreshes must not continue into a different page."""
import json
import unittest
from playwright.sync_api import expect
from tests import test_mvp_v2_script_confirmation_recovery_browser as save_fixture
from tests import test_mvp_v2_global_discard_confirmation_browser as discard_fixture
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class SavePostActionNavigationBrowserTests(unittest.TestCase):
    setUp = save_fixture.ScriptConfirmationRecoveryBrowserTests.setUp

    def test_completed_save_waiting_for_overview_cannot_write_success_into_new_page(self):
        page = self.page
        self.fixture.script_revision = 0
        page.route('**/api/agent/tasks/unknown-save/confirm', lambda route: route.fulfill(
            status=200, content_type='application/json', body=json.dumps({'task':{'status':'completed'},'response':{}})))
        held = []
        def delay_first_overview(route):
            if not held:
                held.append(route)
            else:
                route.continue_()
        page.route('**/overview', delay_first_overview)
        with page.expect_request(lambda request: request.url.endswith('/overview')):
            page.locator('[data-confirm-script-revision]').click()
        for _ in range(20):
            if held:
                break
            page.wait_for_timeout(25)
        self.assertEqual(len(held), 1)
        page.evaluate("navigateFormal('/projects')")
        expect(page.locator('#page-projects')).to_be_visible()
        with page.expect_response(lambda response: response.url.endswith('/overview')):
            held[0].fulfill(response=held[0].fetch())
        page.wait_for_timeout(100)
        expect(page.locator('#page-projects')).to_be_visible()
        self.assertNotIn('文字与配图已保存为一个新版本', page.locator('#formal-script-save-proposal').inner_text())
        self.assertFalse(self.errors)


class DiscardPostActionNavigationBrowserTests(unittest.TestCase):
    setUp = discard_fixture.GlobalDiscardConfirmationBrowserTests.setUp

    def test_completed_discard_waiting_for_script_cannot_load_old_project_images_after_navigation(self):
        discard_fixture.GlobalDiscardConfirmationBrowserTests.seed_image_changes(self)
        page = self.page
        self.fixture.script_revision = 1
        page.evaluate('async () => { await loadFormalScript(formalRoute().projectId); }')
        held = []
        def delay_refreshed_script(route):
            if self.fixture.image_discards and not held:
                held.append(route)
            else:
                route.continue_()
        page.route('**/script?*', delay_refreshed_script)
        page.on('dialog', lambda dialog: dialog.accept())
        with page.expect_request(lambda request: '/script?' in request.url and bool(self.fixture.image_discards)):
            page.locator('#formal-script-discard').click()
        for _ in range(20):
            if held:
                break
            page.wait_for_timeout(25)
        self.assertEqual(len(held), 1)
        self.assertEqual(self.fixture.progress_selections, {})
        page.evaluate("navigateFormal('/projects')")
        expect(page.locator('#page-projects')).to_be_visible()
        subsequent_image_reads = []
        page.on('request', lambda request: subsequent_image_reads.append(request.url)
                if '/image-workspace?' in request.url else None)
        with page.expect_response(lambda response: '/script?' in response.url):
            held[0].fulfill(response=held[0].fetch())
        page.wait_for_timeout(100)
        expect(page.locator('#page-projects')).to_be_visible()
        self.assertEqual(subsequent_image_reads, [])
        self.assertNotIn('全部草稿修改已放弃', page.locator('#toast').inner_text())
        self.assertFalse(self.errors)
