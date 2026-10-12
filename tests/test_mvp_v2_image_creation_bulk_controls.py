"""Batch selection and preview recovery with actual browser images; no paid provider."""
from copy import deepcopy
import unittest
from playwright.sync_api import expect
from tests import test_mvp_v2_image_creation_browser as image_browser


class ImageCreationBulkControlsTests(unittest.TestCase):
    setUp = image_browser.ImageCreationBrowserTests.setUp
    open_batch = image_browser.ImageCreationBrowserTests.open_batch
    open = image_browser.ImageCreationBrowserTests.open
    generate = image_browser.ImageCreationBrowserTests.generate

    def seed(self, task_id, target_id):
        self.fixture.creation_items[task_id] = {
            'task_id': task_id, 'status': 'completed', 'target': {'kind': 'formal_shot', 'id': target_id, 'label': target_id},
            'preview_url': self.fixture.image_url('created-image-' + task_id), 'result_uncertain': False}

    def test_scope_controls_show_invalid_without_generating_or_silently_skipping(self):
        self.open_batch()
        self.page.locator('[data-batch-targets] summary').nth(1).click()
        self.page.locator('[data-batch-prompt="1"]').fill('')
        expect(self.page.locator('[data-target-count]')).to_contain_text('可生成 1 镜 · 待处理 1 镜')
        expect(self.page.locator('[data-batch-generate]')).to_be_disabled()
        self.page.locator('[data-targets-select="eligible"]').click()
        expect(self.page.locator('[data-batch-select="0"]')).to_be_checked()
        expect(self.page.locator('[data-batch-select="1"]')).not_to_be_checked()
        expect(self.page.locator('[data-batch-generate]')).to_be_enabled()
        self.page.locator('[data-targets-select="none"]').click()
        expect(self.page.locator('[data-batch-generate]')).to_be_disabled()
        self.page.locator('[data-targets-select="all"]').click()
        expect(self.page.locator('[data-batch-generate]')).to_be_disabled()
        self.assertEqual(self.fixture.creations, [])
        self.assertEqual(self.errors, [])

    def test_results_select_latest_per_target_and_apply_only_explicit_selection(self):
        self.seed('old', 'shot-stable-2'); self.seed('new', 'shot-stable-2'); self.seed('third', 'shot-stable-3')
        self.open_batch()
        expect(self.page.locator('[data-adopt-task]').first).to_be_enabled()
        self.page.locator('[data-results-select="eligible"]').click()
        expect(self.page.locator('[data-result-select="old"]')).not_to_be_checked()
        expect(self.page.locator('[data-result-select="new"]')).to_be_checked()
        expect(self.page.locator('[data-result-select="third"]')).to_be_checked()
        self.assertEqual(self.fixture.adoptions, [])
        self.page.locator('[data-results-adopt]').click()
        expect(self.page.locator('[data-creation-notice]')).to_contain_text('本次已采用 2 张 · 待处理 0 张')
        self.assertEqual({request['target_id'] for request in self.fixture.adoptions}, {'shot-stable-2', 'shot-stable-3'})
        self.assertEqual(len(self.fixture.adoptions), 2)
        self.assertEqual(self.fixture.image_version_posts, 0)
        expect(self.page.locator('[data-results-adopt]')).to_be_disabled()
        self.assertEqual(self.errors, [])

    def test_failed_adoption_keeps_only_failed_selected_and_does_not_repeat_success(self):
        self.seed('first', 'shot-stable-2'); self.seed('second', 'shot-stable-3')
        original = self.fixture.response
        fail = [True]
        def response(method, path):
            if path.split('?')[0].endswith('/first/adopt') and fail[0]:
                return 409, {'detail': {'code': 'IMAGE_PROGRESS_CONFLICT', 'message': '第 2 镜进度已变化'}}
            return original(method, path)
        self.fixture.response = response
        self.open_batch()
        expect(self.page.locator('[data-adopt-task]').last).to_be_enabled()
        self.page.locator('[data-results-select="eligible"]').click()
        self.page.locator('[data-results-adopt]').click()
        expect(self.page.locator('[data-creation-notice]')).to_contain_text('本次已采用 1 张 · 待处理 1 张')
        expect(self.page.locator('[data-result-select="first"]')).to_be_checked()
        expect(self.page.locator('[data-result-select="second"]')).not_to_be_checked()
        fail[0] = False
        self.page.locator('[data-results-adopt]').click()
        expect(self.page.locator('[data-creation-notice]')).to_contain_text('本次已采用 1 张 · 待处理 0 张')
        self.assertEqual(len(self.fixture.adoptions), 2)
        self.assertEqual(self.errors, [])

    def test_broken_pixels_block_adoption_until_explicit_preview_retry(self):
        self.seed('bad', 'shot-stable-2')
        original = self.fixture.response
        broken = [True]
        def response(method, path):
            if 'created-image-bad/preview' in path and broken[0]:
                return 200, b'not an image'
            return original(method, path)
        self.fixture.response = response
        self.open()
        expect(self.page.locator('[data-result-preview-retry]')).to_be_visible()
        expect(self.page.locator('[data-adopt-task]')).to_be_disabled()
        self.assertEqual(self.fixture.adoptions, [])
        broken[0] = False
        self.page.locator('[data-result-preview-retry]').click()
        expect(self.page.locator('[data-adopt-task]')).to_be_enabled()
        self.page.locator('[data-adopt-task]').click()
        expect(self.page.locator('[data-adopt-task]')).to_have_text('已采用')
        self.assertEqual(len(self.fixture.adoptions), 1)
        self.assertEqual(self.fixture.creations, [])
        self.assertEqual(self.errors, [])

    def test_deferred_batch_uses_suggestion_language_and_pool_receipts(self):
        self.seed('first', 'shot-stable-2'); self.seed('second', 'shot-stable-3')
        original = self.fixture.response
        def response(method, path):
            if path.split('?')[0].endswith('/adopt'):
                self.fixture.adoptions.append(deepcopy(self.fixture.body))
                return 200, {'status': 'pool_saved', 'message': '已加入素材，尚未应用选图进度'}
            return original(method, path)
        self.fixture.response = response
        self.open_batch()
        self.page.locator('[data-creation-close]').click()
        self.page.evaluate('''() => window.MPlusImageCreation.openBatch({projectId:decodeURIComponent(location.pathname.split('/')[2]),workspaceId:'browser-workspace',deferSelection:true,proposalTaskId:'deferred',targets:[{targetId:'shot-stable-2'},{targetId:'shot-stable-3'}]})''')
        expect(self.page.locator('[data-adopt-task]').first).to_have_text('加入素材并更新本镜建议')
        expect(self.page.locator('[data-adopt-task]').last).to_be_enabled()
        self.page.locator('[data-results-select="eligible"]').click()
        expect(self.page.locator('[data-results-adopt]')).to_have_text('更新所选 2 镜建议')
        self.page.locator('[data-results-adopt]').click()
        expect(self.page.locator('[data-creation-notice]')).to_contain_text('本次已采用 2 张')
        self.assertTrue(all(request['defer_selection'] for request in self.fixture.adoptions))
        self.assertEqual(self.fixture.progress_revision, 0)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertEqual(self.errors, [])
