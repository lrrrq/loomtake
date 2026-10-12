"""Browser boundary journey; database/API guarantees live in the paired tests."""
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote, unquote, urlparse, parse_qs
import unittest
from unittest.mock import patch

from playwright.sync_api import expect

from tests import test_mvp_v2_creative_workspace_browser as browser_fixture
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class _ImageBatchFixture(browser_fixture._CreativeFixture):
    def __init__(self):
        super().__init__()
        self.batch_conflict = False
        self.batch_saves = 0
        self.preview_reads = []
        self.long_caption = False
        self.reference_gap = False
        self.batch = None
        self.progress_selections['shot-stable-1'] = {
            'shot_id': 'shot-stable-1', 'image_key': 'candidate-1', 'checked': True,
            'decision_kind': 'image_confirmed', 'selection_active': True,
        }

    def response(self, method, path):
        route = unquote(urlparse(path).path)
        if '/images/batch-' in route and route.endswith('/preview'):
            query = parse_qs(urlparse(path).query)
            if query.get('workspace_id') != ['browser-workspace'] or query.get('expected_project_version') != [str(self.project_version)]:
                return 422, {'detail': {'message': 'missing workspace/version'}}
            variant = 'b' if 'batch-2-b/' in route else 'a'
            self.preview_reads.append(variant)
            return 200, (Path(__file__).parent / 'fixtures' / 'formal_visual_quality'
                         / f'anonymous-launch-atlas-{variant}.png').read_bytes()
        if route.endswith('/image-workspace/assistant') and method == 'GET':
            return 200, deepcopy(self.batch) if self.batch else {'task': None, 'response': None}
        if route.endswith('/image-workspace/assistant/browser-image-batch') and method == 'PATCH':
            self.writes.append((route, deepcopy(self.body)))
            response = self.batch['response']
            for update in self.body['updates']:
                shot = next(s for s in response['shots'] if s['shot_id'] == update['shot_id'])
                if 'candidate_key' in update:
                    shot['selected_candidate_key'] = update['candidate_key']
                    shot['included'] = bool(update['candidate_key'])
                if 'included' in update:
                    shot['included'] = update['included']
                if 'prepare_generation' in update:
                    shot['prepare_generation'] = update['prepare_generation']
            response['proposal_revision'] += 1
            response['proposal_digest'] = str(response['proposal_revision']).rjust(64, '0')
            return 200, deepcopy(self.batch)
        if route.endswith('/image-workspace/assistant/browser-image-batch/cancel'):
            self.batch['task']['status'] = 'cancelled'
            self.batch['response']['status'] = 'cancelled'
            self.batch['response']['summary'] = '已取消建议，原选图进度保持不变。'
            return 200, deepcopy(self.batch)
        if route.endswith('/image-workspace/assistant') and method == 'POST':
            self.writes.append((route, deepcopy(self.body)))
            prefix = f'/api/formal/projects/{quote(PROJECT_ID, safe="")}/images/'
            candidates = [{'candidate_key': f'choice-2-{variant}',
                'image_key': f'batch-2-{variant}', 'caption': ('大型科技发布会现场，舞台背景为蓝色主视觉，台下坐满参会观众。' * 8 if self.long_caption else f'匿名现场候选 {variant}'),
                'source_label': '项目素材', 'preview_url': prefix + f'batch-2-{variant}/preview',
                'sources': [{'file': '匿名来源.xlsx', 'sheet': '素材', 'row': index}],
                'match_basis': '场景与当前镜头相符'} for index, variant in enumerate(('a', 'b'), 1)]
            result = {'task': {'task_id': 'browser-image-batch', 'status': 'waiting_user'},
                'response': {'type': 'image_batch_proposal', 'proposal_digest': 'd' * 64,
                    'proposal_revision': 0, 'mode': self.body.get('mode', 'fill_missing'),
                    'project_id': PROJECT_ID, 'workspace_id': 'browser-workspace',
                    'project_version': self.project_version, 'affected_shot_ids': ['shot-stable-2'],
                    'shots': [
                        {'shot_id': 'shot-stable-1', 'shot_no': '1', 'visual': '开场',
                         'state': 'preserved', 'reason': '保留已有选择', 'candidates': [], 'reason_code': 'locked',
                         'before': {'image_key': 'batch-1-before', 'preview_url': prefix + 'batch-1-before/preview'}},
                        {'shot_id': 'shot-stable-2', 'shot_no': '2', 'visual': '现场互动',
                         'state': 'gap' if self.reference_gap else 'ready', 'reason': '', 'candidates': candidates,
                         'selected_candidate_key': '' if self.reference_gap else 'choice-2-a', 'included': not self.reference_gap},
                        {'shot_id': 'shot-stable-3', 'shot_no': '3', 'visual': '结尾',
                         'state': 'gap', 'reason': '暂无合适图片，可上传、生成或稍后补图', 'candidates': []},
                    ]}}
            if self.body.get('save_eligible'):
                shot = result['response']['shots'][1]
                if self.reference_gap:
                    shot['selected_candidate_key'] = ''
                    result['response']['gap_shot_ids'] = ['shot-stable-2', 'shot-stable-3']
                else:
                    self.progress_selections['shot-stable-2'] = {
                        'shot_id': 'shot-stable-2', 'image_key': 'batch-2-a', 'checked': False,
                        'decision_kind': 'image_confirmed', 'selection_active': True,
                    }
                    self.progress_revision += 1
                    shot['state'] = 'saved'
                    result['response']['saved_shot_ids'] = ['shot-stable-2']
                    result['response']['gap_shot_ids'] = ['shot-stable-3']
            self.batch = deepcopy(result)
            return 200, result
        if route.endswith('/image-workspace/assistant/browser-image-batch/confirm'):
            self.writes.append((route, deepcopy(self.body)))
            self.batch_saves += 1
            if self.batch_conflict:
                return 409, {'detail': {'code': 'IMAGE_PROGRESS_CONFLICT', 'message': '另一处已更新选图进度，当前输入仍保留'}}
            chosen = self.body['selected_candidate_keys'][0]
            self.progress_revision += 1
            self.progress_selections['shot-stable-2'] = {
                'shot_id': 'shot-stable-2', 'image_key': chosen.replace('choice-', 'batch-'),
                'checked': False, 'decision_kind': 'image_confirmed', 'selection_active': True,
            }
            _, progress = super().response('GET', f'/api/formal/projects/{PROJECT_ID}/image-workspace/progress')
            self.batch['task']['status'] = 'completed'
            self.batch['response']['status'] = 'completed'
            return 200, {'type': 'image_batch_saved', 'summary': '已保存选图进度，请核对图片后保存配图版本。', 'progress': progress}
        return super().response(method, path)


class ImageBatchBrowserTests(unittest.TestCase):
    def setUp(self):
        with patch.object(browser_fixture, '_CreativeFixture', _ImageBatchFixture):
            browser_fixture.CreativeWorkspaceBrowserTests.setUp(self)

    capture = browser_fixture.CreativeWorkspaceBrowserTests.capture

    def propose(self, instruction='帮整份稿配图'):
        self.page.goto(f'{self.base}/projects/{PROJECT_ID}/script')
        self.page.locator('[data-creative-open-tool="assistant"]:visible').first.click()
        self.page.locator('#creative-assistant-kind').select_option('text')
        self.page.locator('#creative-assistant-scope').select_option('all')
        self.page.locator('#formal-script-instruction').fill(instruction)
        self.page.locator('#formal-script-instruction-submit').click()
        expect(self.page.locator('[data-image-batch]')).to_be_visible()

    def test_natural_language_with_intervening_words_routes_to_images(self):
        self.propose('帮整份稿配一版图')
        self.assertFalse(any('/script/draft-proposals' in route for route, _ in self.fixture.writes))

    def test_long_library_caption_keeps_preview_inside_card_on_desktop_and_mobile(self):
        self.fixture.long_caption = True
        self.propose()
        card = self.page.locator('[data-image-batch-shot="shot-stable-2"]')
        image = card.locator('img')
        expect(image).to_be_visible()
        for width in (1440, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 1000})
                dimensions = card.evaluate('node => ({width:node.clientWidth, content:node.scrollWidth})')
                self.assertLessEqual(dimensions['content'], dimensions['width'] + 2)
                self.assertLessEqual(image.bounding_box()['width'], card.bounding_box()['width'])
                self.capture(f'image-batch-long-caption-{width}')

    def test_image_context_routes_followup_without_repeating_the_word_image(self):
        self.page.goto(f'{self.base}/projects/{PROJECT_ID}/script')
        self.page.locator('#creative-current-image [data-creative-open-tool="assistant"]').click()
        expect(self.page.locator('#creative-assistant-kind')).to_have_value('images')
        self.page.locator('#creative-assistant-scope').select_option('all')
        self.page.locator('#formal-script-instruction').fill('后半段多一些客户交流')
        self.page.locator('#formal-script-instruction-submit').click()
        expect(self.page.locator('[data-image-batch]')).to_be_visible()
        request = next(body for route, body in self.fixture.writes if route.endswith('/image-workspace/assistant'))
        self.assertEqual(request['instruction'], '后半段多一些客户交流')
        self.assertEqual(request['selected_shot_ids'], ['shot-stable-1', 'shot-stable-2', 'shot-stable-3'])
        self.assertEqual(self.fixture.batch_saves, 0)
        self.assertFalse(any('/script/draft-proposals' in route for route, _ in self.fixture.writes))

    def test_natural_language_shows_real_thumbnail_changes_candidate_and_saves_only_progress(self):
        self.propose()
        page = self.page
        expect(page.locator('[data-image-batch-shot="shot-stable-1"]')).to_contain_text('保留已有选择')
        expect(page.locator('[data-image-batch-shot="shot-stable-3"]')).to_contain_text('暂无合适图片')
        preview = page.locator('[data-image-batch-preview] img')
        expect(preview).to_be_visible()
        expect(preview).to_have_attribute('alt', '匿名现场候选 a')
        self.assertIn('a', self.fixture.preview_reads)
        page.locator('[data-image-batch-choice]').select_option('choice-2-b')
        expect(preview).to_have_attribute('alt', '匿名现场候选 b')
        self.assertIn('b', self.fixture.preview_reads)
        self.capture('image-batch-real-candidate')
        page.locator('[data-image-batch-save]').click()
        expect(page.locator('[data-image-batch-save]')).to_have_text('选图进度已保存')
        expect(page.locator('[data-image-batch-choice]')).to_be_disabled()
        self.assertEqual(self.fixture.batch_saves, 1)
        self.assertEqual(self.fixture.progress_selections['shot-stable-2']['image_key'], 'batch-2-b')
        self.assertFalse(self.fixture.progress_selections['shot-stable-2']['checked'])
        self.assertTrue(self.fixture.progress_selections['shot-stable-1']['checked'])
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertEqual(self.fixture.default_image_confirm_posts, 0)
        self.assertFalse(any('/script/draft-proposals' in route for route, _ in self.fixture.writes))
        self.assertEqual(self.errors, [])
        self.capture('image-batch-saved-progress')

    def test_save_conflict_keeps_selected_candidate_and_clear_recovery_message(self):
        self.fixture.batch_conflict = True
        self.propose()
        self.page.locator('[data-image-batch-choice]').select_option('choice-2-b')
        self.page.locator('[data-image-batch-save]').click()
        expect(self.page.locator('[data-image-batch-notice]')).to_contain_text('进度')
        expect(self.page.locator('[data-image-batch-save]')).to_be_enabled()
        expect(self.page.locator('[data-image-batch-choice]')).to_have_value('choice-2-b')
        self.assertNotIn('shot-stable-2', self.fixture.progress_selections)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.capture('image-batch-progress-conflict')
        self.assertEqual(self.errors, [])

    def test_refresh_recovers_edited_proposal_and_cancel_keeps_original_progress(self):
        self.propose()
        before = deepcopy(self.fixture.progress_selections)
        card = self.page.locator('[data-image-batch-shot="shot-stable-2"]')
        card.locator('[data-image-batch-choice]').select_option('choice-2-b')
        expect(card.locator('[data-image-batch-include]')).to_be_checked()
        card.locator('[data-image-batch-include]').uncheck()
        expect(card.locator('[data-image-batch-include]')).not_to_be_checked()
        self.page.reload()
        self.page.locator('[data-creative-open-tool="assistant"]:visible').first.click()
        expect(self.page.locator('[data-image-batch]')).to_have_count(1)
        expect(card.locator('[data-image-batch-choice]')).to_have_value('choice-2-b')
        expect(card.locator('[data-image-batch-include]')).not_to_be_checked()
        self.page.locator('[data-image-batch-cancel]').click()
        expect(self.page.locator('[data-image-batch]')).to_contain_text('已取消建议')
        self.assertEqual(self.fixture.progress_selections, before)
        self.page.reload()
        self.page.locator('[data-creative-open-tool="assistant"]:visible').first.click()
        expect(self.page.locator('[data-image-batch]')).to_have_count(1)
        expect(self.page.locator('[data-image-batch-cancel]')).to_have_count(0)
        self.assertEqual(self.fixture.batch_saves, 0)

    def test_refresh_of_changed_basis_keeps_text_without_retrying_old_previews(self):
        self.propose()
        self.page.wait_for_load_state('networkidle')
        before = deepcopy(self.fixture.progress_selections)
        self.fixture.batch['response']['basis_changed'] = True
        self.fixture.preview_reads.clear()
        self.page.reload()
        self.page.locator('[data-creative-open-tool="assistant"]:visible').first.click()
        batch = self.page.locator('[data-image-batch]')
        expect(batch).to_contain_text('依据已过期')
        expect(batch.locator('[data-image-batch-save], [data-image-batch-generate]')).to_have_count(0)
        batch.locator('summary').click()
        expect(batch.locator('li')).not_to_have_count(0)
        batch.locator('[data-image-batch-check]').click()
        self.page.wait_for_load_state('networkidle')
        self.assertEqual(self.fixture.preview_reads, [])
        self.assertEqual(self.fixture.progress_selections, before)
        self.assertEqual(self.fixture.batch_saves, 0)
        self.assertEqual(self.errors, [])

    def test_saving_reference_choice_removes_only_filled_generation_gap(self):
        self.fixture.reference_gap = True
        self.propose()
        filled = self.page.locator('[data-image-batch-shot="shot-stable-2"]')
        remaining = self.page.locator('[data-image-batch-shot="shot-stable-3"]')
        expect(filled.locator('[data-image-batch-gap]')).to_be_checked()
        remaining.locator('[data-image-batch-gap]').uncheck()
        filled.locator('[data-image-batch-choice]').select_option('choice-2-b')
        self.page.locator('[data-image-batch-save]').click()
        expect(self.page.locator('[data-image-batch-save]')).to_have_text('选图进度已保存')
        expect(filled.locator('[data-image-batch-gap]')).to_have_count(0)
        expect(remaining.locator('[data-image-batch-gap]')).not_to_be_checked()
        expect(self.page.locator('[data-image-batch]')).to_contain_text('已保存选图进度')
        expect(filled.locator('[data-image-batch-choice]')).to_have_value('choice-2-b')

    def test_selected_scope_saves_eligible_and_keeps_gap_in_current_draft(self):
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script')
        page.locator('[data-creative-batch-toggle]').click()
        for number in (2, 3):
            page.locator(f'[data-creative-select-shot="shot-stable-{number}"]').check()
        page.locator('[data-creative-batch-images][data-image-batch-mode=fill_missing]').click()
        expect(page.locator('[data-image-batch]')).to_be_visible()
        request = next(body for route, body in self.fixture.writes if route.endswith('/image-workspace/assistant'))
        self.assertEqual(request['selected_shot_ids'], ['shot-stable-2', 'shot-stable-3'])
        self.assertEqual(request['mode'], 'fill_missing')
        self.assertNotIn('save_eligible', request)
        expect(page.locator('#creative-image-scope')).to_have_value('selected')
        self.assertNotIn('shot-stable-2', self.fixture.progress_selections)
        page.locator('[data-image-batch-save]').click()
        expect(page.locator('[data-image-batch-shot="shot-stable-2"]')).to_contain_text('图片已保存到当前稿')
        expect(page.locator('[data-image-batch-shot="shot-stable-3"] [data-image-batch-gap]')).to_be_checked()
        self.assertNotIn('shot-stable-3', self.fixture.progress_selections)
        self.assertFalse(self.fixture.progress_selections['shot-stable-2']['checked'])
        self.assertEqual(self.fixture.default_image_confirm_posts, 0)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertFalse(self.errors)

    def test_all_scope_batch_preserves_protected_choice_without_navigation(self):
        self.fixture.shots[0]['assistant_locked'] = True
        original = deepcopy(self.fixture.progress_selections['shot-stable-1'])
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        page.locator('#creative-image-scope').select_option('all')
        page.locator('[data-creative-match-images][data-image-batch-mode=fill_missing]').click()
        expect(page.locator('[data-image-batch-shot="shot-stable-1"]')).to_contain_text('保留已有选择')
        request = next(body for route, body in self.fixture.writes if route.endswith('/image-workspace/assistant'))
        self.assertEqual(request['selected_shot_ids'], ['shot-stable-1', 'shot-stable-2', 'shot-stable-3'])
        self.assertEqual(self.fixture.progress_selections['shot-stable-1'], original)
        expect(page.locator('[data-creative-shot="shot-stable-1"]')).to_contain_text('已保护')
        expect(page.locator('#creative-image-scope')).to_have_value('all')
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertFalse(self.errors)

    def test_reference_choice_is_explicit_and_does_not_fill_other_gap(self):
        self.fixture.reference_gap = True
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        page.locator('#creative-image-scope').select_option('all')
        page.locator('[data-creative-match-images][data-image-batch-mode=fill_missing]').click()
        card = page.locator('[data-image-batch-shot="shot-stable-2"]')
        expect(card.locator('[data-image-batch-choice]')).to_have_value('')
        self.assertNotIn('shot-stable-2', self.fixture.progress_selections)
        card.locator('[data-image-batch-choice]').select_option('choice-2-b')
        page.locator('[data-image-batch-save]').click()
        expect(page.locator('[data-image-batch-save]')).to_have_text('选图进度已保存')
        self.assertEqual(self.fixture.progress_selections['shot-stable-2']['image_key'], 'batch-2-b')
        self.assertFalse(self.fixture.progress_selections['shot-stable-2']['checked'])
        self.assertNotIn('shot-stable-3', self.fixture.progress_selections)
        expect(page.locator('[data-image-batch-shot="shot-stable-3"] [data-image-batch-gap]')).to_be_checked()
        self.assertEqual(self.fixture.default_image_confirm_posts, 0)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertFalse(self.errors)

    def test_batch_selection_controls_change_proposal_only_and_keep_failed_choice(self):
        self.propose()
        expect(self.page.locator('[data-image-batch-selection-count]')).to_contain_text('应用已选 1 镜 · 可处理 1 镜')
        self.page.locator('[data-image-batch-select="none"]').click()
        expect(self.page.locator('[data-image-batch-include]:checked')).to_have_count(0)
        expect(self.page.locator('[data-image-batch-save]')).to_be_disabled()
        self.page.locator('[data-image-batch-select="eligible"]').click()
        expect(self.page.locator('[data-image-batch-select="all"]')).to_be_enabled()
        expect(self.page.locator('[data-image-batch-include]:checked')).to_have_count(1)
        original = self.fixture.response
        failed = [True]
        def response(method, path):
            if method == 'PATCH' and '/image-workspace/assistant/' in path and failed[0]:
                return 503, {'detail': {'message': '建议暂未保存，请重试'}}
            return original(method, path)
        self.fixture.response = response
        self.page.locator('[data-image-batch-select="none"]').click()
        expect(self.page.locator('[data-image-batch-notice]')).to_contain_text('当前页面选择保留')
        expect(self.page.locator('[data-image-batch-include]:checked')).to_have_count(0)
        failed[0] = False
        self.page.locator('[data-image-batch-select="none"]').click()
        expect(self.page.locator('[data-image-batch-select="all"]')).to_be_enabled()
        self.page.locator('[data-image-batch-select="all"]').click()
        expect(self.page.locator('[data-image-batch-save]')).to_be_enabled()
        self.assertEqual(self.fixture.batch_saves, 0)
        self.assertNotIn('shot-stable-2', self.fixture.progress_selections)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertFalse(self.errors)

    def test_gap_selection_controls_preserve_explicit_scope_without_generation(self):
        self.fixture.reference_gap = True
        self.propose()
        expect(self.page.locator('[data-image-batch-gap]:checked')).to_have_count(2)
        self.page.locator('[data-image-batch-gap-select="none"]').click()
        expect(self.page.locator('[data-image-batch-gap-select="all"]')).to_be_enabled()
        expect(self.page.locator('[data-image-batch-gap-count]')).to_contain_text('已选 0 / 2 镜')
        self.page.locator('[data-image-batch-gap-select="all"]').click()
        expect(self.page.locator('[data-image-batch-gap-count]')).to_contain_text('已选 2 / 2 镜')
        expect(self.page.locator('[data-image-batch-gap-select="none"]')).to_be_enabled()
        updates = [body['updates'] for route, body in self.fixture.writes if route.endswith('/assistant/browser-image-batch')]
        self.assertEqual({item['shot_id'] for item in updates[-1]}, {'shot-stable-2', 'shot-stable-3'})
        self.assertTrue(all(item['prepare_generation'] for item in updates[-1]))
        self.assertEqual(self.fixture.batch_saves, 0)
        self.assertFalse(any('/image-creations' in route for route, _ in self.fixture.writes))
        self.assertFalse(self.errors)

    def test_all_suggestions_and_only_actionable_keep_manual_reference_distinct(self):
        original = self.fixture.response
        def response(method, path):
            status, payload = original(method, path)
            if method == 'POST' and path.split('?')[0].endswith('/image-workspace/assistant'):
                for value in (payload, self.fixture.batch):
                    shot = value['response']['shots'][2]
                    candidate = deepcopy(value['response']['shots'][1]['candidates'][0])
                    candidate.update(candidate_key='choice-3-reference', default_eligible=False)
                    shot.update(state='ready', candidates=[candidate], selected_candidate_key=candidate['candidate_key'], included=False)
            return status, payload
        self.fixture.response = response
        self.propose()
        self.page.locator('[data-image-batch-select="all"]').click()
        expect(self.page.locator('[data-image-batch-select="eligible"]')).to_be_enabled()
        expect(self.page.locator('[data-image-batch-include]:checked')).to_have_count(2)
        self.page.locator('[data-image-batch-select="eligible"]').click()
        expect(self.page.locator('[data-image-batch-select="all"]')).to_be_enabled()
        expect(self.page.locator('[data-image-batch-include]:checked')).to_have_count(1)
        expect(self.page.locator('[data-image-batch-shot="shot-stable-3"] [data-image-batch-choice]')).to_have_value('choice-3-reference')
        self.assertEqual(self.fixture.batch_saves, 0)
        self.assertFalse(self.errors)

    def test_both_rerecommend_buttons_use_selection_even_while_browsing_all(self):
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        page.locator('[data-creative-batch-toggle]').click()
        page.locator('[data-creative-select-shot="shot-stable-2"]').check()
        for attribute in ('data-creative-match-images', 'data-creative-batch-images'):
            with self.subTest(button=attribute):
                page.locator('#creative-image-scope').select_option('all')
                with page.expect_request(lambda r: r.method == 'POST' and '/image-workspace/assistant?' in r.url) as requested:
                    page.locator(f'[{attribute}][data-image-batch-mode="replace_selected"]').click()
                self.assertEqual(requested.value.post_data_json['selected_shot_ids'], ['shot-stable-2'])
                self.assertEqual(requested.value.post_data_json['mode'], 'replace_selected')
                expect(page.locator('[data-image-batch]')).to_be_visible()
        self.assertEqual(self.errors, [])

    def test_rerecommend_without_selection_does_not_fall_back_to_all(self):
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        page.locator('#creative-image-scope').select_option('all')
        page.locator('[data-creative-match-images][data-image-batch-mode="replace_selected"]').click()
        expect(page.get_by_text('请先选择需要配图的镜头。', exact=True)).to_be_visible()
        self.assertFalse(any(path.endswith('/image-workspace/assistant') for path, _ in self.fixture.writes))
