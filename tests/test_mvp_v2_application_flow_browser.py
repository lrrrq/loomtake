"""Anonymous browser regressions for the shared application workflow.

Responses are controlled UI boundary fixtures. These tests do not constitute
real-provider or persisted end-to-end acceptance.
"""
from copy import deepcopy
from urllib.parse import parse_qs, urlparse
import unittest

from playwright.sync_api import expect
from tests import test_mvp_v2_creative_draft_browser as draft_browser
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class ApplicationFlowBrowserTests(unittest.TestCase):
    def setUp(self):
        self.draft = draft_browser.CreativeDraftBrowserTests('runTest')
        self.draft.setUp()
        self.addCleanup(self.draft.doCleanups)
        self.draft.complete()
        self.page = self.draft.page
        self.helper = self.draft.helper
        self.fixture = self.helper.fixture
        self.requests = []
        self.timeline = [{'response': {'type': 'brief_analysis', 'user_message': '匿名活动需求',
            'summary': '请补充两个确定的信息。', 'questions': ['新品是什么？', '在哪播放？'],
            'question_bindings': [
                {'question_id': 'analysis-1:0', 'analysis_task_id': 'analysis-1', 'question': '新品是什么？', 'field': '必须出现的内容'},
                {'question_id': 'analysis-1:1', 'analysis_task_id': 'analysis-1', 'question': '在哪播放？', 'field': '使用场景'}]}}]
        self.fail_answers = False
        self.fail_references = False
        self.formal_enabled = False
        original = self.fixture.response

        def response(method, path):
            parsed = urlparse(path)
            route = parsed.path
            if self.formal_enabled and route.endswith('/script'):
                return 200, self.fixture.script()
            if self.formal_enabled and route.endswith('/image-workspace/progress'):
                status, model = original(method, path)
                model['creative_references'] = [{'shot_id': 'shot-stable-1', 'block_id': 'block-1',
                    'reference_id': f'reference-{index}', 'primary': index == 1,
                    'origin_label': f'保留的匿名参考图 {index}',
                    'preview_url': f'/api/formal/projects/{PROJECT_ID}/images/default-candidates/default-candidate-{index}/preview'} for index in (1, 2)]
                return status, model
            if route == '/api/agent/sessions' or route == '/api/agent/sessions/flow-session':
                return 200, {'session': {'agent_session_id': 'flow-session'}, 'timeline': deepcopy(self.timeline)}
            if route.endswith('/brief-answers'):
                self.requests.append(('answers', deepcopy(self.fixture.body)))
                if self.fail_answers:
                    self.fail_answers = False
                    return 503, {'detail': {'message': '匿名保存网络中断'}}
                return 200, {'answered_question_ids': [item['question_id'] for item in self.fixture.body['answers']]}
            if route.endswith('/brief-analysis'):
                self.timeline.append({'response': {'type': 'brief_analysis', 'summary': '补充已读取。', 'questions': [], 'user_message': '本轮补充'}})
                return 200, {'response': self.timeline[-1]['response']}
            if route.endswith('/templates'):
                return 200, {'project_id': PROJECT_ID, 'project_version': self.fixture.project_version,
                    'category_id': 'annual-meeting', 'current_category_id': 'annual-meeting', 'selected_ref': 'anonymous-template@1', 'is_bound': True,
                    'categories': [{'category_id': 'annual-meeting', 'label': '年会'}], 'items': [{
                        'ref': 'anonymous-template@1', 'selected': True, 'label': '匿名年会模板', 'version': 1, 'shot_count': 3,
                        'structures': [], 'source_name': '匿名模板.xlsx', 'tone_options': [
                            {'label': '温情人文', 'value': '温情人文，关注团队与真实人物', 'source_label': '匿名模板原文'},
                            {'label': '科技庆典', 'value': '科技庆典，节奏鲜明', 'source_label': '匿名模板原文'}]}]}
            if route.endswith('/brief/field'):
                self.requests.append(('tone', deepcopy(self.fixture.body)))
                return 200, {'status': 'ready'}
            if route.endswith('/images/default-candidates'):
                params = parse_qs(parsed.query)
                self.requests.append(('candidates', params))
                page = int(params.get('page', ['1'])[0])
                candidate = {'candidate_key': f'default-candidate-{page}', 'content_sha256': str(page) * 64,
                    'caption': f'匿名参考图 {page}', 'source': {'source_file': '匿名库.xlsx', 'source_sheet': '素材', 'source_row': page + 1},
                    'preview_url': f'/api/formal/projects/{PROJECT_ID}/images/default-candidates/default-candidate-{page}/preview'}
                return 200, {'project_version': self.draft.value['project_version'], 'target_kind': 'creative_block',
                    'creative_revision': self.draft.value['revision'], 'candidates': [candidate], 'library_candidates': [candidate],
                    'inventory_records': 2, 'inventory_groups': 2, 'filtered_groups': 2, 'page_groups': 1, 'page_records': 1,
                    'page': page, 'has_more': page == 1, 'facets': {}}
            if route.endswith('/creative-draft/image-references'):
                body = deepcopy(self.fixture.body)
                self.requests.append(('references', body))
                if self.fail_references:
                    self.fail_references = False
                    return 503, {'detail': {'message': '匿名保存结果未知'}}
                self.draft.value['image_references'] = [{**item, 'reference_id': f'ref-{index}',
                    'preview_url': f"/api/formal/projects/{PROJECT_ID}/images/default-candidates/{item.get('library_candidate_key') or item.get('task_id')}/preview",
                    'origin_label': '匿名图库引用'} for index, item in enumerate(body['selections'])]
                self.draft.value['revision'] += 1
                return 200, deepcopy(self.draft.value)
            return original(method, path)
        self.fixture.response = response

    def open(self):
        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/script')
        expect(self.page.locator('#creative-first-draft')).to_be_visible()
        self.page.locator('[data-first-view="shot"]').click()

    def test_twenty_five_blocks_use_outline_single_editor_table_and_saved_location(self):
        self.draft.value['blocks'] = [{'block_id': f'block-{index}', 'shot_no': str(index),
            'fields': {'段落': f'第{index}段', '画面描述': f'匿名画面{index}', '声音': '环境声', '时长': '2秒'}} for index in range(1, 26)]
        self.draft.value['duration_status'] = {'target_seconds': 60, 'actual_seconds': 50, 'difference_seconds': -10, 'status': 'mismatch', 'pending_block_count': 0}
        self.page.set_viewport_size({'width': 862, 'height': 1056})
        self.open()
        expect(self.page.locator('[data-first-row]:visible')).to_have_count(1)
        expect(self.page.locator('#creative-first-summary')).to_contain_text('还差 10 秒')
        self.page.locator('[data-first-jump="block-25"]').click()
        expect(self.page.locator('[data-first-row="block-25"]')).to_be_visible()
        self.page.locator('[data-first-block="block-25"][data-first-field="画面描述"]').fill('末段持久修改')
        self.page.locator('#creative-first-save').click()
        expect(self.page.locator('#creative-first-status')).to_contain_text('初稿已保存')
        self.page.locator('[data-first-view="table"]').click()
        expect(self.page.locator('[data-first-row]:visible')).to_have_count(25)
        self.page.locator('[data-first-view="shot"]').click()
        self.page.reload()
        expect(self.page.locator('[data-first-row="block-25"]')).to_be_visible()
        expect(self.page.locator('[data-first-block="block-25"][data-first-field="画面描述"]')).to_have_value('末段持久修改')
        self.assertFalse(self.helper.errors)

    def test_long_draft_outline_and_floating_assistant_keep_input(self):
        self.draft.value['blocks'] = [{'block_id': f'block-{index}', 'shot_no': str(index),
            'fields': {'段落': f'第{index}段', '画面描述': '匿名现场描述。' * 40,
                '声音': '现场环境声。' * 20, '时长': '2秒'}} for index in range(1, 26)]
        for width in (1440, 862, 390):
            with self.subTest(width=width):
                self.page.set_viewport_size({'width': width, 'height': 900})
                self.open()
                self.page.locator('[data-first-jump="block-25"]').click()
                expect(self.page.locator('[data-first-row="block-25"]')).to_be_visible()
                self.page.locator('#workbench-assistant-launcher').click()
                self.page.locator('#agent-message').fill('保留末段事实，调整声音。')
                self.page.locator('[data-workbench-minimize]').click()
                self.page.locator('#workbench-assistant-launcher').click()
                expect(self.page.locator('#agent-message')).to_have_value('保留末段事实，调整声音。')
                self.page.locator('[data-workbench-close]').click()
                self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), width)
        self.assertFalse(self.helper.errors)

    def test_clean_picker_reopens_from_latest_saved_references(self):
        self.open()
        self.page.locator('[data-first-pick-image="block-1"]:visible, [data-workbench-first-picker="block-1"]:visible').click()
        panel = self.page.locator('#first-creative-image-picker')
        panel.locator('[data-first-picker-close]').click()
        # A normally adopted generated image changes the authoritative draft
        # after a clean picker was opened and closed.
        self.draft.value['image_references'] = [{'block_id': 'block-1', 'reference_id': 'new-reference',
            'task_id': 'new-image-task', 'content_sha256': 'a' * 64, 'primary': True,
            'origin_label': '刚采用的新图',
            'preview_url': f'/api/formal/projects/{PROJECT_ID}/images/default-candidates/default-candidate-1/preview'}]
        self.draft.value['revision'] += 1
        self.page.reload()
        self.page.locator('[data-first-pick-image="block-1"]:visible, [data-workbench-first-picker="block-1"]:visible').click()
        expect(panel.locator('.first-picker-assignments')).to_contain_text('刚采用的新图')
        panel.locator('[data-first-picker-candidate="0"]').click()
        panel.locator('[data-first-picker-save]').click()
        expect(panel).to_have_count(0)
        post = next(body for kind, body in self.requests if kind == 'references')
        self.assertEqual(len(post['selections']), 2)
        self.assertTrue(any(item.get('task_id') == 'new-image-task' for item in post['selections']))
        self.assertFalse(self.helper.errors)

    def test_removed_block_can_reassign_retained_images_after_rebase(self):
        self.open()
        self.page.locator('[data-first-pick-image="block-1"]:visible, [data-workbench-first-picker="block-1"]:visible').click()
        panel = self.page.locator('#first-creative-image-picker')
        panel.locator('[data-first-picker-candidate="0"]').click()
        panel.locator('[data-first-picker-close]').click()
        # Preserve the pending image but remove its old stable target, as the
        # normal delete-block operation does before reopening the picker.
        self.draft.value['blocks'] = self.draft.value['blocks'][1:]
        self.draft.value['revision'] += 1
        self.page.reload()
        self.page.locator('[data-first-pick-image="block-2"]:visible, [data-workbench-first-picker="block-2"]:visible').click()
        panel.locator('[data-first-picker-rebase]').click()
        expect(panel.locator('[data-first-picker-target] option')).to_have_count(2)
        expect(panel.locator('.first-picker-assignments')).to_contain_text('原段落已不可用')
        panel.locator('[data-first-picker-close]').click()
        self.page.reload()
        self.page.locator('[data-first-pick-image="block-2"]:visible, [data-workbench-first-picker="block-2"]:visible').click()
        expect(panel.locator('.first-picker-assignments')).to_contain_text('原段落已不可用')
        panel.locator('[data-first-picker-assign="0"]').select_option('block-2')
        panel.locator('[data-first-picker-save]').click()
        expect(panel).to_have_count(0)
        post = next(body for kind, body in self.requests if kind == 'references')
        self.assertEqual(post['replace_block_ids'], ['block-2'])
        self.assertEqual(post['selections'][0]['block_id'], 'block-2')
        self.assertFalse(self.helper.errors)

    def test_rebase_merges_new_references_for_untouched_blocks(self):
        self.open()
        self.page.locator('[data-first-pick-image="block-1"]:visible, [data-workbench-first-picker="block-1"]:visible').click()
        panel = self.page.locator('#first-creative-image-picker')
        panel.locator('[data-first-picker-candidate="0"]').click()
        panel.locator('[data-first-picker-close]').click()
        self.draft.value['image_references'] = [{'block_id': 'block-2', 'reference_id': 'new-other-reference',
            'task_id': 'new-other-task', 'content_sha256': 'a' * 64, 'primary': True,
            'origin_label': '第二段新采用图片',
            'preview_url': f'/api/formal/projects/{PROJECT_ID}/images/default-candidates/default-candidate-1/preview'}]
        self.draft.value['revision'] += 1
        self.page.reload()
        self.page.locator('[data-first-pick-image="block-1"]:visible, [data-workbench-first-picker="block-1"]:visible').click()
        panel.locator('[data-first-picker-rebase]').click()
        expect(panel.locator('.first-picker-assignments')).to_contain_text('第二段新采用图片')
        panel.locator('[data-first-picker-target]').select_option('block-2')
        panel.locator('[data-first-picker-candidate="0"]').click()
        panel.locator('[data-first-picker-save]').click()
        expect(panel).to_have_count(0)
        post = next(body for kind, body in self.requests if kind == 'references')
        self.assertEqual(len(post['selections']), 3)
        self.assertTrue(any(item.get('task_id') == 'new-other-task' and item['block_id'] == 'block-2'
                            for item in post['selections']))
        self.assertFalse(self.helper.errors)

    def test_one_send_saves_question_answers_and_attached_material(self):
        uploads = []
        def uploaded(route):
            uploads.append(route.request.post_data_buffer)
            route.fulfill(status=200, json={'status': 'ready', 'import_status': 'appended'})
        self.page.route('**/brief/files?*', uploaded)
        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/brief')
        self.page.locator('#brief-source-panel').evaluate('(element) => element.open = true')
        self.page.locator('[data-brief-reply="analysis-1:0"]').first.click()
        self.page.locator('#formal-brief-text').fill('新品匿名 A')
        self.page.locator('#formal-brief-files').set_input_files({'name': 'anonymous.txt', 'mimeType': 'text/plain', 'buffer': '补充活动资料'.encode()})
        self.page.locator('#formal-brief-upload-button').click()
        expect(self.page.locator('#brief-intake-status')).to_contain_text('分析已保存')
        self.assertEqual(len([body for kind, body in self.requests if kind == 'answers']), 1)
        self.assertEqual(len(uploads), 1)
        self.assertIn('补充活动资料'.encode(), uploads[0])
        self.assertFalse(self.helper.errors)

    def test_duration_alignment_asks_only_for_duration_changes(self):
        self.draft.value['duration_status'] = {'target_seconds': 60, 'actual_seconds': 15, 'difference_seconds': -45, 'status': 'mismatch', 'pending_block_count': 0}
        self.open()
        self.page.locator('#creative-first-align-duration').click()
        expect(self.page.locator('#creative-first-proposal')).to_be_visible()
        from mvp_v2.formal_script_assistant import analyze_creative_blocks
        from unittest.mock import patch
        instruction = self.draft.revisions[0]['instruction']
        with patch('mvp_v2.formal_script_assistant._model_rewrite', return_value={}) as rewrite:
            analyze_creative_blocks(instruction=instruction,
                blocks=[{**block, 'shot_id': block['block_id']} for block in self.draft.value['blocks']],
                selected_block_ids=['block-1', 'block-2', 'block-3'], protected_block_ids=[], editor_policy=None)
        self.assertEqual(rewrite.call_args.kwargs['fields_by_shot'],
                         {f'block-{index}': ['duration_text'] for index in (1, 2, 3)})
        self.assertFalse(self.helper.errors)

    def test_duration_proposal_shows_remaining_gap_and_updates_with_selected_groups(self):
        durations = [3, 3, 3, 3, 44]
        self.draft.value['blocks'] = [{'block_id': f'block-{index}', 'shot_no': str(index),
            'fields': {'段落': f'第{index}段', '画面描述': f'匿名画面{index}', '声音': '环境声', '时长': f'{duration}秒'}}
            for index, duration in enumerate(durations, 1)]
        self.draft.value['duration_status'] = {'target_seconds': 60, 'actual_seconds': 56,
            'pending_block_count': 0, 'difference_seconds': -4, 'status': 'mismatch'}
        changes = [{'block_id': f'block-{index}', 'field': '时长', 'before': '3秒', 'after': '2秒'} for index in range(1, 5)]
        self.draft.value['proposal'] = {'digest': 'd' * 64, 'revision': self.draft.value['revision'],
            'summary': '已准备时长建议，请核对。', 'changes': changes,
            'groups': [{'block_id': change['block_id'], 'changes': [change]} for change in changes],
            'duration_status': {'target_seconds': 60, 'actual_seconds': 52,
                'pending_block_count': 0, 'difference_seconds': -8, 'status': 'mismatch'}}
        self.draft.value['task'] = {'task_id': 'duration-proposal', 'status': 'waiting_user', 'is_running': False}
        self.open()
        preview = self.page.locator('#creative-first-proposal-duration')
        expect(preview).to_contain_text('采用所选 4 组后预计 52 秒 / 目标 60 秒 · 仍差 8 秒')
        expect(preview).to_contain_text('尚未达到目标')
        expect(self.page.locator('#creative-first-summary')).to_contain_text('当前 56 秒')
        self.helper.capture('proposal-duration-52-of-60')
        self.page.locator('[data-first-retain="block-1"]').uncheck()
        expect(preview).to_contain_text('采用所选 3 组后预计 53 秒 / 目标 60 秒 · 仍差 7 秒')
        for index in (2, 3, 4):
            self.page.locator(f'[data-first-retain="block-{index}"]').uncheck()
        expect(preview).to_contain_text('未选择建议，当前 56 秒 / 目标 60 秒 · 仍差 4 秒')
        expect(self.page.locator('#creative-first-apply')).to_be_disabled()
        self.page.locator('[data-first-proposal-duration-adjust]').click()
        expect(self.page.locator('#creative-first-proposal')).to_contain_text('现场掌声渐入')
        self.assertEqual(len(self.draft.revisions), 1)
        self.assertIn('只调整时长', self.draft.revisions[0]['instruction'])
        self.assertEqual([block['fields']['时长'] for block in self.draft.value['blocks']],
                         [f'{duration}秒' for duration in durations])
        self.assertFalse(self.helper.errors)

    def test_duration_proposal_distinguishes_expected_match_pending_and_stale(self):
        changes = [{'block_id': 'block-1', 'field': '时长', 'before': '建议 5 秒', 'after': '50秒'}]
        self.draft.value['duration_status'] = {'target_seconds': 60, 'actual_seconds': 15, 'pending_block_count': 0}
        self.draft.value['proposal'] = {'digest': 'e' * 64, 'revision': self.draft.value['revision'],
            'summary': '核对修改', 'changes': changes, 'groups': [{'block_id': 'block-1', 'changes': changes}],
            'duration_status': {'target_seconds': 60, 'actual_seconds': 60, 'pending_block_count': 0, 'status': 'matched'}}
        self.open()
        preview = self.page.locator('#creative-first-proposal-duration')
        expect(preview).to_contain_text('预计与目标一致')
        expect(preview).to_contain_text('当前稿尚未改变')
        expect(self.page.locator('[data-first-proposal-duration-adjust]')).to_have_count(0)
        self.draft.value['proposal']['duration_status']['target_seconds'] = None
        self.page.reload()
        expect(preview).to_contain_text('尚无已确认目标时长')
        expect(preview).not_to_contain_text('目标 60 秒')
        self.draft.value['proposal']['duration_status']['target_seconds'] = 60
        self.draft.value['proposal']['duration_status'].update(actual_seconds=10, pending_block_count=1, status='pending')
        self.page.reload()
        expect(preview).to_contain_text('1 段时长待定')
        expect(preview).not_to_contain_text('仍差')
        expect(preview).not_to_contain_text('预计与目标一致')
        # The public stale flag also covers changed confirmed Brief/project
        # context when the draft revision itself has not changed.
        self.draft.value['proposal']['stale'] = True
        self.page.reload()
        expect(preview).to_contain_text('需求或初稿已变化')
        expect(self.page.locator('#creative-first-apply')).to_be_disabled()
        expect(self.page.locator('[data-first-retain="block-1"]')).to_be_disabled()
        self.draft.value['proposal']['stale'] = False
        self.draft.value['revision'] += 1
        self.page.reload()
        expect(preview).to_contain_text('需求或初稿已变化')
        expect(self.page.locator('[data-first-proposal-duration-adjust]')).to_have_count(0)
        expect(self.page.locator('#creative-first-apply')).to_be_disabled()
        self.assertFalse(self.helper.errors)

    def test_current_duration_proposal_can_apply_with_other_answers_still_pending(self):
        for block, duration in zip(self.draft.value['blocks'], (20, 20, 16)):
            block['fields']['时长'] = f'{duration}秒'
        remaining = [{'field': '风格调性', 'value': '温情人文'},
                     {'field': '使用场景', 'value': '年会现场播放'}]
        changes = [{'block_id': 'block-3', 'field': '时长', 'before': '16秒', 'after': '20秒'}]
        self.draft.value.update(revision=8,
            task={'task_id': 'duration-only-proposal', 'status': 'waiting_user', 'is_running': False},
            pending_answers=deepcopy(remaining), unimplemented_answers=deepcopy(remaining),
            answers_need_preview=True,
            duration_status={'target_seconds': 60, 'actual_seconds': 56,
                'pending_block_count': 0, 'difference_seconds': -4, 'status': 'mismatch'},
            proposal={'digest': 'f' * 64, 'revision': 8, 'stale': False,
                'summary': '只调整时长，采用后为 60 秒。', 'changes': changes,
                'groups': [{'block_id': 'block-3', 'changes': changes}],
                'duration_status': {'target_seconds': 60, 'actual_seconds': 60,
                    'pending_block_count': 0, 'difference_seconds': 0, 'status': 'matched'}})
        self.open()
        preview = self.page.locator('#creative-first-proposal-duration')
        update = self.page.locator('#creative-first-requirement-update')
        expect(preview).to_contain_text('采用所选 1 组后预计 60 秒 / 目标 60 秒 · 预计与目标一致')
        expect(self.page.locator('#creative-first-proposal')).not_to_contain_text('需求或初稿已更新')
        expect(self.page.locator('[data-first-retain="block-3"]')).to_be_enabled()
        expect(self.page.locator('#creative-first-apply')).to_be_enabled()
        expect(update).to_contain_text('需要另行预览；可先采用当前建议')
        self.page.locator('#creative-first-apply').click()
        expect(self.page.locator('#creative-first-summary')).to_contain_text('当前 60 秒 / 目标 60 秒 · 时长一致')
        expect(self.page.locator('#creative-first-proposal')).to_be_hidden()
        expect(update).to_contain_text('需求变更尚未应用到当前稿')
        self.assertEqual(len(self.draft.confirmations), 1)
        self.assertEqual(self.draft.confirmations[0]['task_id'], 'duration-only-proposal')
        self.assertEqual(self.draft.confirmations[0]['retained_block_ids'], ['block-3'])
        self.assertEqual(self.draft.value['pending_answers'], remaining)
        self.assertEqual(self.draft.value['unimplemented_answers'], remaining)
        self.assertEqual(self.draft.revisions, [])
        self.assertEqual(self.draft.answers, [])
        self.page.reload()
        expect(self.page.locator('#creative-first-summary')).to_contain_text('当前 60 秒')
        expect(update).to_contain_text('需求变更尚未应用到当前稿')
        self.assertEqual(self.draft.value['unimplemented_answers'], remaining)
        self.assertFalse(self.helper.errors)

    def test_leaving_picker_closes_overlay_and_keeps_unsaved_assignment(self):
        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/brief')
        self.page.get_by_role('link', name='创作', exact=True).click()
        self.page.locator('[data-first-pick-image="block-1"]:visible, [data-workbench-first-picker="block-1"]:visible').click()
        panel = self.page.locator('#first-creative-image-picker')
        panel.locator('[data-first-picker-candidate="0"]').click()
        self.page.go_back()
        expect(self.page.locator('#page-brief')).to_be_visible()
        expect(panel).to_have_count(0)
        self.assertFalse([body for kind, body in self.requests if kind == 'references'])
        self.page.get_by_role('link', name='创作', exact=True).click()
        self.page.locator('[data-first-pick-image="block-1"]:visible, [data-workbench-first-picker="block-1"]:visible').click()
        expect(panel.locator('[data-first-picker-selection-count]')).to_contain_text('已选 1 张')
        self.assertFalse(self.helper.errors)

    def test_unknown_image_save_restores_exact_request_before_allowing_edits(self):
        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/script?panel=images&block_id=block-2')
        expect(self.page.locator('[data-workbench-card="block-2"]')).to_be_visible()
        self.page.locator('[data-first-pick-image="block-2"]:visible, [data-workbench-first-picker="block-2"]:visible').click()
        panel = self.page.locator('#first-creative-image-picker')
        panel.locator('[data-first-picker-candidate="0"]').click()
        self.fail_references = True
        panel.locator('[data-first-picker-save]').click()
        expect(panel.locator('[data-first-picker-status]')).to_contain_text('已选图片和分配仍保留')
        self.page.reload()
        self.page.locator('[data-first-pick-image="block-2"]:visible, [data-workbench-first-picker="block-2"]:visible').click()
        expect(panel.locator('[data-first-picker-save]')).to_have_text('读取原保存结果 / 重试')
        panel.locator('[data-first-picker-remove="0"]').click()
        expect(panel.locator('[data-first-picker-selection-count]')).to_contain_text('已选 1 张')
        expect(panel.locator('[data-first-picker-rebase]')).to_be_hidden()
        panel.locator('[data-first-picker-save]').click()
        expect(panel).to_have_count(0)
        posts = [body for kind, body in self.requests if kind == 'references']
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        expect(self.page.locator('[data-first-row="block-2"] .creative-first-reference')).to_have_count(1)
        self.assertFalse(self.helper.errors)

    def test_formal_workspace_keeps_all_initial_references_without_auto_adoption(self):
        self.formal_enabled = True
        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/script?panel=images&shot_id=shot-stable-1')
        self.page.locator('[data-workbench-open="shot-stable-1"]').click()
        reference_panel = self.page.locator('[data-image-shot="0"] .creative-reference-collection')
        expect(reference_panel).to_be_visible()
        reference_panel.locator('summary').click()
        expect(reference_panel.locator('figure')).to_have_count(2)
        expect(reference_panel.locator('img')).to_have_count(2)
        expect(reference_panel).to_contain_text('正式配图仍需选用和核对')
        self.assertFalse(any(route.endswith('/image-workspace/creative-references') for route, _ in self.fixture.writes))
        self.page.reload()
        expect(reference_panel.locator('summary')).to_have_text('初稿参考图 · 2 张')
        self.assertFalse(self.helper.errors)

    def test_creative_planning_explains_retained_references_without_marking_images_ready(self):
        self.fixture.waiting = False
        original = self.fixture.response

        def response(method, path):
            if urlparse(path).path.endswith('/images'):
                return 200, {'images': [], 'total': 0, 'pass': 0, 'usable_count': 0,
                    'pending': 0, 'blocked': 0, 'creative_reference_count': 3,
                    'creative_reference_block_count': 2}
            return original(method, path)

        self.fixture.response = response
        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/script?panel=structure&creative=1')
        copy = self.page.locator('#formal-plan-image-block-copy')
        expect(copy).to_contain_text('初稿已保留 3 张参考图（2 段）')
        expect(copy).to_contain_text('生成正式稿后仍需逐镜核对')
        expect(copy).to_contain_text('当前没有已确认项目图片')
        expect(self.page.locator('#formal-plan-create')).to_have_text('选择文字草稿')
        self.page.locator('#formal-plan-create').click()
        expect(copy).to_contain_text('初稿已保留 3 张参考图（2 段）')
        expect(copy).to_contain_text('保留这些参考')
        expect(self.page.locator('#formal-plan-create')).to_have_text('生成文字规划')
        self.page.reload()
        expect(copy).to_contain_text('初稿已保留 3 张参考图（2 段）')
        expect(self.page.locator('#formal-plan-create')).to_have_text('生成文字规划')
        self.assertEqual(self.fixture.writes, [])
        self.assertFalse(self.helper.errors)

    def test_delayed_brief_response_keeps_user_opened_reply_and_draft_visible(self):
        pending = []

        def hold_current(route):
            pending.append(route)

        self.page.route('**/brief/current?*', hold_current)
        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/brief')
        panel = self.page.locator('#brief-source-panel')
        question = self.page.locator('[data-brief-reply="analysis-1:0"]').first
        expect(question).to_be_visible()
        panel.locator(':scope > summary').click()
        panel.locator(':scope > summary').click()
        question.click()
        expect(self.page.locator('#brief-reply-context')).to_be_visible()
        self.assertEqual(len(pending), 1)
        self.page.unroute('**/brief/current?*', hold_current)
        for route in pending:
            status, value = self.fixture.response('GET', route.request.url)
            route.fulfill(status=status, json=value)
        expect(self.page.locator('#formal-brief-readiness')).to_have_text('已确认')
        expect(self.page.locator('#brief-reply-context')).to_be_visible()
        expect(self.page.locator('#brief-reply-context')).to_contain_text('新品是什么？')
        self.page.locator('#formal-brief-text').fill('保留的匿名新品回答')
        self.page.reload()
        expect(self.page.locator('#formal-brief-readiness')).to_have_text('已确认')
        expect(self.page.locator('#brief-reply-context')).to_be_visible()
        expect(self.page.locator('#formal-brief-text')).to_have_value('保留的匿名新品回答')
        self.assertFalse(self.helper.errors)

    def test_chat_keeps_multiple_answer_drafts_through_failure_reload_and_one_send(self):
        def open_loaded_conversation():
            expect(self.page.locator('#formal-brief-readiness')).to_have_text('已确认')
            panel = self.page.locator('#brief-source-panel')
            if panel.get_attribute('open') is None:
                panel.locator(':scope > summary').click()
            expect(self.page.locator('[data-brief-reply="analysis-1:0"]').first).to_be_visible()

        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/brief')
        open_loaded_conversation()
        expect(self.page.locator('#brief-conversation form')).to_have_count(0)
        self.page.locator('[data-brief-reply="analysis-1:0"]').first.click()
        self.page.locator('#formal-brief-text').fill('匿名新品 A')
        self.page.locator('[data-brief-reply="analysis-1:1"]').first.click()
        self.page.locator('#formal-brief-text').fill('开场大屏播放')
        expect(self.page.locator('#brief-answer-drafts')).to_contain_text('匿名新品 A')
        self.fail_answers = True
        self.page.locator('#formal-brief-upload-button').click()
        expect(self.page.locator('#brief-intake-status')).to_contain_text('回答草稿已保留')
        self.page.reload()
        open_loaded_conversation()
        expect(self.page.locator('#formal-brief-text')).to_have_value('开场大屏播放')
        expect(self.page.locator('#brief-answer-drafts')).to_contain_text('匿名新品 A')
        self.page.locator('#formal-brief-upload-button').click()
        expect(self.page.locator('#brief-intake-status')).to_contain_text('分析已保存')
        posts = [body for kind, body in self.requests if kind == 'answers']
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual([item['content'] for item in posts[-1]['answers']], ['匿名新品 A', '开场大屏播放'])
        expect(self.page.locator('#formal-brief-text')).to_have_value('')
        self.assertFalse(self.helper.errors)

    def test_template_tone_is_available_before_confirmation_and_saved_as_fact(self):
        self.draft.value['understanding']['confirmation_status'] = 'pending'
        self.page.goto(f'{self.helper.base}/projects/{PROJECT_ID}/brief')
        expect(self.page.locator('#formal-template-picker')).to_be_visible()
        self.page.locator('#formal-template-picker > summary').click()
        self.page.locator('[data-template-tone="0"]').click()
        expect(self.page.locator('#brief-tone-value')).to_have_value('温情人文，关注团队与真实人物')
        self.page.locator('#brief-tone-form button[type=submit]').click()
        expect(self.page.locator('#brief-tone-status')).to_contain_text('调性已保存')
        posted = next(body for kind, body in self.requests if kind == 'tone')
        self.assertEqual(posted['field_label'], '风格调性')
        self.assertEqual(posted['value'], '温情人文，关注团队与真实人物')
        self.assertFalse(self.helper.errors)

    def test_initial_images_preserve_cross_page_selection_and_explicit_block_mapping(self):
        self.open()
        self.page.locator('[data-first-pick-image="block-1"]:visible, [data-workbench-first-picker="block-1"]:visible').click()
        panel = self.page.locator('#first-creative-image-picker')
        expect(panel).to_be_visible()
        expect(panel.locator('[data-first-picker-intent]')).to_have_text('匿名画面1')
        panel.locator('[data-first-picker-candidate="0"]').click()
        panel.locator('[data-first-picker-source="library"]').click()
        panel.locator('[data-first-picker-page="2"]').click()
        panel.locator('[data-first-picker-candidate="0"]').click()
        expect(panel.locator('[data-first-picker-selection-count]')).to_contain_text('已选 2 张')
        panel.locator('[data-first-picker-assign="1"]').select_option('block-2')
        expect(panel.locator('[data-first-picker-selection-count]')).to_contain_text('分配到 2 段')
        panel.locator('[data-first-picker-save]').click()
        expect(panel).to_have_count(0)
        body = next(body for kind, body in self.requests if kind == 'references')
        self.assertEqual([(item['block_id'], item['library_candidate_key']) for item in body['selections']],
                         [('block-1', 'default-candidate-1'), ('block-2', 'default-candidate-2')])
        self.assertTrue(all(item['primary'] for item in body['selections']))
        self.assertEqual(set(body['replace_block_ids']), {'block-1', 'block-2'})
        self.page.reload()
        expect(self.page.locator('[data-first-row="block-1"] .creative-first-reference')).to_have_count(1)
        self.page.locator('[data-first-jump="block-2"]').click()
        expect(self.page.locator('[data-first-row="block-2"] .creative-first-reference')).to_be_visible()
        self.assertFalse(self.helper.errors)
