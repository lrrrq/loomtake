"""Anonymous multi-shot checks for search snapshots and scoped image restoration."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as fixture_module
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class _RestoreFixture(fixture_module._CreativeFixture):
    def response(self, method, path):
        if path.split('?')[0].endswith('/image-workspace/progress') and method == 'PATCH' and any(s.get('remove') for s in self.body['selections']):
            self.writes.append((path.split('?')[0], deepcopy(self.body)))
            for selection in self.body['selections']:
                self.progress_selections.pop(selection['shot_id'], None)
            self.progress_revision += 1
            return super().response('GET', path)
        status, value = super().response(method, path)
        if '/candidates?' in path and parse_qs(urlparse(path).query).get('query') == ['新的查询']:
            value = deepcopy(value)
            value['candidates'] = [c for c in value.get('candidates', []) if c.get('image_key') != 'candidate-1']
            value['candidate_snapshot_digest'] = 'new-query-digest'
        return status, value


class ImageChoiceRestoreBrowserTests(unittest.TestCase):
    def setUp(self):
        with patch.object(fixture_module, '_CreativeFixture', _RestoreFixture):
            fixture_module.CreativeWorkspaceBrowserTests.setUp(self)
        self.page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        expect(self.page.locator('[data-image-shot="0"]')).to_be_visible()

    def test_search_keeps_selected_image_bound_to_original_candidate_snapshot(self):
        page = self.page
        page.locator('[data-image-picker="0"]').click()
        picker = page.get_by_role('dialog', name='第 1 镜', exact=True)
        picker.locator('[data-picker-candidate="0"]').click()
        expect(picker.locator('[data-picker-use]')).to_be_enabled()
        original = page.evaluate('({query:creativeImagePicker.selectedContext.query,digest:creativeImagePicker.selectedContext.digest,image:creativeImagePicker.selected.image_key})')
        picker.locator('[data-picker-query]').fill('新的查询')
        picker.locator('form').get_by_role('button', name='搜索', exact=True).click()
        page.wait_for_function("creativeImagePicker.digest === 'new-query-digest'")
        self.assertFalse(page.evaluate("creativeImagePicker.candidates.some(c => c.image_key === 'candidate-1')"))
        picker.locator('[data-picker-use]').click()
        expect(picker).not_to_be_visible()
        actual = page.evaluate('({query:state.formalImageWorkspace.shots[0].query,digest:state.formalImageWorkspace.shots[0].candidate_snapshot_digest,image:state.formalImageWorkspace.shots[0].selected_image_key})')
        self.assertEqual(actual, original)
        self.assertEqual(self.fixture.progress_selections['shot-stable-1']['query'], original['query'])
        self.assertFalse(self.errors)

    def test_restore_one_then_selected_shots_preserves_other_progress(self):
        page = self.page
        page.evaluate('''async () => {
            for (const shot of state.formalImageWorkspace.shots) { shot.explicitly_changed=true; shot.confirmed=false; shot.reuse_reason='匿名修改'; creativeState().imageChanges.add(shot.shot_id); }
            renderFormalImageWorkspace(state.formalImageWorkspace);
            captureFormalImageInputs(); await flushCreativeProgress({manual:true});
        }''')
        before = deepcopy(self.fixture.progress_selections['shot-stable-3'])
        page.locator('[data-image-undo="0"]').click()
        expect(page.locator('#toast')).to_contain_text('已撤销 1 镜')
        self.assertNotIn('shot-stable-1', self.fixture.progress_selections)
        self.assertEqual(self.fixture.progress_selections['shot-stable-3'], before)
        page.evaluate("creativeState().selectedShotIds = new Set(['shot-stable-2'])")
        page.locator('[data-image-undo-selected]').first.click()
        expect(page.locator('#toast')).to_contain_text('已撤销 1 镜')
        page.wait_for_function("!state.formalImageWorkspace.shots[1].explicitly_changed")
        self.assertNotIn('shot-stable-2', self.fixture.progress_selections)
        self.assertEqual(self.fixture.progress_selections['shot-stable-3'], before)
        self.assertFalse(self.errors)
