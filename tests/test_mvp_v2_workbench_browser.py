"""Workbench UI boundaries with anonymous HTTP fixtures, not provider acceptance."""
from copy import deepcopy
import re
from urllib.parse import urlparse
import unittest

from playwright.sync_api import expect
from tests import test_mvp_v2_application_flow_browser as application_flow
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class WorkbenchBrowserTests(unittest.TestCase):
    def setUp(self):
        self.flow = application_flow.ApplicationFlowBrowserTests('runTest')
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.page = self.flow.page
        self.page.set_viewport_size({'width': 1440, 'height': 1000})

    def open(self, query=''):
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script{query}')
        expect(self.page.locator('[data-workbench-card]').first).to_be_visible()

    def many_blocks(self):
        self.flow.draft.value['blocks'] = [
            {'block_id': f'block-{i}', 'shot_no': str(i), 'fields': {
                '段落': f'第{i}段', '画面描述': f'匿名现场 {i}', '声音': '环境声', '时长': '3秒'}}
            for i in range(1, 26)]

    def test_cold_open_waits_for_initial_draft_before_showing_empty_conclusions(self):
        self.many_blocks()
        pending = []
        self.page.route('**/creative-draft?*', lambda route: pending.append(route))
        with self.page.expect_request('**/creative-draft?*'):
            self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script')
        expect(self.page.locator('#creative-workspace-read-state')).to_contain_text('正在读取当前初稿与版本')
        expect(self.page.locator('#creative-workspace-read-state')).to_be_visible()
        expect(self.page.locator('#formal-script-version-label')).not_to_be_visible()
        expect(self.page.locator('#formal-script-table')).not_to_be_visible()
        expect(self.page.locator('#creative-first-confirmed-start')).not_to_be_visible()
        self.assertNotIn('当前项目还没有脚本版本', self.page.locator('#page-script').inner_text())
        pending[0].fulfill(status=200, json=deepcopy(self.flow.draft.value))
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(6)
        expect(self.page.locator('#creative-first-block-count')).to_have_text('25 段')
        expect(self.page.locator('#creative-workspace-read-state')).not_to_be_visible()
        self.assertFalse(self.flow.helper.errors)

    def test_initial_draft_read_failure_keeps_retry_without_claiming_empty_project(self):
        self.many_blocks()
        fail = True

        def read(route):
            nonlocal fail
            if fail:
                fail = False
                route.fulfill(status=503, json={'detail': {'message': '匿名读取暂不可用'}})
            else:
                route.continue_()

        self.page.route('**/creative-draft?*', read)
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script')
        expect(self.page.locator('#creative-workspace-read-state')).to_contain_text('当前初稿与版本读取失败')
        expect(self.page.locator('[data-creative-read-retry]')).to_be_visible()
        expect(self.page.locator('#creative-first-confirmed-start')).not_to_be_visible()
        self.assertNotIn('当前项目还没有脚本版本', self.page.locator('#page-script').inner_text())
        self.page.locator('[data-creative-read-retry]').click()
        expect(self.page.locator('#creative-first-block-count')).to_have_text('25 段')
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(6)
        expect(self.page.locator('#creative-workspace-read-state')).not_to_be_visible()
        self.assertFalse(self.flow.helper.errors)

    def test_formal_version_read_failure_can_retry_current_workspace(self):
        self.flow.formal_enabled = True
        fail = True

        def read(route):
            nonlocal fail
            if fail:
                fail = False
                route.fulfill(status=503, json={'detail': {'message': '匿名版本读取暂不可用'}})
            else:
                route.continue_()

        self.page.route('**/api/formal/projects/*/script?*', read)
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script')
        expect(self.page.locator('#creative-workspace-read-state')).to_contain_text('当前初稿与版本读取失败')
        self.page.locator('[data-creative-read-retry]').click()
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(3)
        expect(self.page.locator('#creative-workspace-read-state')).not_to_be_visible()
        self.assertFalse(self.flow.helper.errors)

    def test_six_card_pages_keep_selection_through_search_detail_and_refresh(self):
        self.many_blocks()
        self.open()
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(6)
        expect(self.page.locator('[data-first-field]:visible')).to_have_count(0)
        self.page.locator('[data-workbench-select="page"]').click()
        self.page.locator('[data-workbench-page="1"]').click()
        self.page.locator('[data-workbench-select-item="block-7"]').check()
        expect(self.page.locator('.workbench-bulkbar:visible')).to_contain_text('已选 7 段')
        self.page.locator('#creative-first-search').fill('25')
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(1)
        expect(self.page.locator('.workbench-bulkbar:visible')).to_contain_text('筛选外 7')
        self.page.locator('[data-workbench-open="block-25"]').click()
        field = self.page.locator('[data-first-block="block-25"][data-first-field="声音"]')
        field.fill('现场掌声')
        self.page.locator('#creative-first-save').click()
        expect(self.page.locator('#creative-first-status')).to_contain_text('初稿已保存')
        self.page.locator('[data-first-view="overview"]').click()
        self.page.reload()
        expect(self.page.locator('.workbench-bulkbar:visible')).to_contain_text('已选 7 段')
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(1)
        self.assertEqual(self.flow.draft.value['blocks'][24]['fields']['声音'], '现场掌声')
        self.assertFalse(self.flow.helper.errors)

    def test_images_are_image_cards_and_materials_have_independent_shell(self):
        self.open('?panel=images')
        expect(self.page.locator('.workbench-card-image:visible')).to_have_count(3)
        expect(self.page.locator('[data-first-field]:visible')).to_have_count(0)
        self.page.locator('[data-workbench-first-picker="block-1"]').click()
        expect(self.page.locator('#first-creative-image-picker')).to_be_visible()
        self.page.keyboard.press('Escape')
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/materials')
        expect(self.page.locator('#page-materials')).to_be_visible()
        expect(self.page.locator('#page-script')).not_to_be_visible()
        expect(self.page.locator('#materials-first-block')).to_have_count(0)
        expect(self.page.locator('#formal-image-grid')).to_be_visible()
        self.assertTrue(self.page.url.endswith('/materials'))
        self.assertFalse(self.flow.helper.errors)

    def test_image_tab_from_full_table_prioritizes_cards_and_keeps_stale_recovery(self):
        self.many_blocks()
        original = self.flow.fixture.response

        def response(method, path):
            if method == 'GET' and urlparse(path).path.endswith('/image-workspace/assistant'):
                return 200, {'task': {'task_id': 'old-image-advice', 'status': 'waiting_user'},
                    'response': {'type': 'image_batch_proposal', 'project_id': PROJECT_ID,
                        'target_kind': 'creative_block', 'basis_changed': True,
                        'shots': [{'shot_no': i, 'visual': f'旧画面 {i}', 'reason': '原候选依据已变化'}
                                  for i in range(1, 26)]}}
            return original(method, path)

        self.flow.fixture.response = response
        self.page.set_viewport_size({'width': 1265, 'height': 714})
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script?draft_view=table')
        expect(self.page.locator('[data-first-view="table"]')).to_have_attribute('aria-pressed', 'true')
        self.page.locator('[data-creative-panel="images"]').click()
        expect(self.page.locator('#creative-first-paper-title')).to_have_text('参考图')
        expect(self.page.locator('#creative-first-image-copy')).to_be_visible()
        expect(self.page.locator('.creative-first-navigation')).not_to_be_visible()
        expect(self.page.locator('.creative-first-context')).not_to_be_visible()
        expect(self.page.locator('.creative-first-actions')).not_to_be_visible()
        expect(self.page.locator('[data-first-field]:visible')).to_have_count(0)
        stale = self.page.locator('#creative-first-image-flow .image-batch-stale')
        expect(stale).to_contain_text('旧配图建议已过期：当前稿已变化')
        expect(stale.locator('details')).not_to_have_attribute('open', '')
        first = self.page.locator('.workbench-card-image:visible').first.bounding_box()
        self.assertLess(first['y'] + min(first['height'], 120), 714)
        stale.locator('summary').click()
        expect(stale.locator('li')).to_have_count(25)
        stale.locator('[data-image-batch-check]').click()
        expect(self.page.locator('#creative-first-overview')).to_be_focused()
        self.page.locator('[data-creative-panel="script"]').click()
        expect(self.page.locator('#creative-first-paper-title')).to_have_text('脚本正文')
        expect(self.page.locator('[data-first-view="table"]')).to_have_attribute('aria-pressed', 'true')
        expect(self.page.locator('[data-first-field]:visible')).to_have_count(100)
        self.assertFalse(self.flow.helper.errors)

    def test_saved_initial_draft_does_not_wait_for_slow_planning_overview(self):
        self.many_blocks()
        pending = []
        self.page.route('**/api/formal/projects/*/overview', lambda route: pending.append(route))
        self.open()
        self.assertTrue(pending)
        expect(self.page.locator('#creative-first-block-count')).to_have_text('25 段')
        expect(self.page.locator('#creative-workspace-read-state')).not_to_be_visible()
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(6)
        for route in pending:
            route.fulfill(status=503, json={'detail': {'message': '匿名规划概览暂不可用'}})
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(6)
        expect(self.page.locator('#creative-workspace-read-state')).not_to_be_visible()
        self.assertFalse(self.flow.helper.errors)

    def test_initial_structure_rebuilds_old_plain_plan_without_showing_formal_empty_state(self):
        self.many_blocks()
        self.flow.fixture.waiting = True
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script?panel=structure&mode=text-only')
        expect(self.page.locator('#workbench-current-structure li')).to_have_count(25)
        expect(self.page.locator('#formal-plan-status-copy')).to_contain_text('当前已有初稿，请按当前初稿重新生成规划')
        expect(self.page.locator('#formal-plan-confirmation')).not_to_be_visible()
        expect(self.page.locator('#formal-plan-create')).to_be_visible()
        expect(self.page.locator('.ui00-script-toolbar')).not_to_be_visible()
        self.assertNotIn('尚无脚本版本', self.page.locator('#page-script').inner_text())
        self.assertNotIn('当前项目还没有脚本版本', self.page.locator('#page-script').inner_text())
        self.assertFalse(self.flow.helper.errors)

    def test_floating_assistant_keeps_input_and_submits_scope_with_atomic_groups(self):
        fixture = self.flow.fixture
        original = fixture.response
        calls = []
        response_value = {'type': 'workbench_proposal', 'stage': 'creative', 'task_id': 'wb-1',
            'summary': '核对第 2 段文字与配图。', 'proposal_digest': 'd' * 64,
            'groups': [{'group_id': 'block-2', 'target_id': 'block-2', 'number': '2', 'label': '第 2 段',
                'selected': True, 'depends_on': [], 'image': None,
                'changes': [{'field': '声音', 'before': '环境声', 'after': '现场掌声'}]}],
            'constraints': {'preserve': ['画面描述'], 'prohibit': [], 'unknown': []}}
        sent = False

        def response(method, path):
            nonlocal sent, response_value
            route = urlparse(path).path
            if route.endswith('/messages'):
                calls.append(('message', deepcopy(fixture.body)))
                sent = True
                return 200, {'task': {'task_id': 'wb-1', 'status': 'waiting_confirmation'}, 'response': response_value}
            if route == '/api/agent/sessions/flow-session' and sent:
                return 200, {'session': {'agent_session_id': 'flow-session'},
                    'tasks': [{'task_id': 'wb-1', 'status': 'waiting_confirmation'}],
                    'timeline': [{'task_id': 'wb-1', 'status': 'waiting_confirmation', 'response': deepcopy(response_value)}]}
            if route == '/api/agent/workbench/wb-1/confirm':
                calls.append(('confirm', deepcopy(fixture.body)))
                self.flow.draft.value['blocks'][1]['fields']['声音'] = '现场掌声'
                self.flow.draft.value['revision'] += 1
                response_value = {'type': 'workbench_applied', 'summary': '已保存所选修改', 'can_undo': True}
                return 200, {'task': {'task_id': 'wb-1'}, 'response': response_value}
            if route == '/api/agent/workbench/wb-1/undo':
                calls.append(('undo', deepcopy(fixture.body)))
                self.flow.draft.value['blocks'][1]['fields']['声音'] = '环境声'
                self.flow.draft.value['revision'] += 1
                response_value = {'type': 'workbench_applied', 'summary': '已撤销', 'can_undo': False}
                return 200, {'task': {'task_id': 'wb-1'}, 'response': response_value}
            return original(method, path)

        fixture.response = response
        self.open()
        self.page.locator('[data-workbench-select-item="block-2"]').check()
        self.page.locator('[data-workbench-bulk="assistant"]').click()
        self.page.locator('#agent-message').fill('第二段声音改为现场掌声，同时推荐配图，保留画面。')
        self.page.locator('[data-workbench-minimize]').click()
        self.page.locator('#workbench-assistant-launcher').click()
        expect(self.page.locator('#agent-message')).to_have_value('第二段声音改为现场掌声，同时推荐配图，保留画面。')
        self.page.locator('#agent-message-submit').click()
        expect(self.page.locator('[data-workbench-confirm]')).to_be_visible()
        self.assertEqual(calls[0][1]['context']['stage'], 'creative')
        self.assertEqual(calls[0][1]['context']['selected_target_ids'], ['block-2'])
        self.assertEqual(calls[0][1]['context']['scope'], 'selected')
        self.assertTrue(calls[0][1]['operation_key'])
        self.page.locator('[data-workbench-confirm]').click()
        expect(self.page.locator('[data-workbench-undo]')).to_be_visible()
        self.assertEqual(calls[1][1]['retained_group_ids'], ['block-2'])
        self.assertEqual(self.flow.draft.value['blocks'][1]['fields']['声音'], '现场掌声')
        self.page.locator('[data-workbench-undo]').click()
        expect(self.page.locator('[data-workbench-proposal]')).to_contain_text('已撤销')
        self.assertEqual(self.flow.draft.value['blocks'][1]['fields']['声音'], '环境声')
        self.assertFalse(self.flow.helper.errors)

    def test_formal_cards_open_original_editor_and_image_detail(self):
        self.flow.formal_enabled = True
        self.open()
        expect(self.page.locator('[data-workbench-card]:visible')).to_have_count(3)
        self.page.locator('[data-workbench-open="shot-stable-2"]').click()
        field = self.page.locator('[data-script-shot-key="shot-stable-2"][data-script-field="sound"]')
        expect(field).to_be_visible()
        field.fill('正式稿现场掌声')
        self.page.locator('#creative-save-progress').click()
        self.page.locator('[data-creative-panel="images"]').click()
        expect(self.page.locator('#creative-image-overview [data-workbench-card]:visible')).to_have_count(3)
        expect(self.page.locator('#formal-image-shot-grid')).not_to_be_visible()
        self.page.locator('[data-workbench-open="shot-stable-2"]').click()
        expect(self.page.locator('[data-image-shot="1"]')).to_be_visible()
        self.page.locator('[data-workbench-image-overview]').click()
        expect(self.page.locator('#formal-image-shot-grid')).not_to_be_visible()
        self.assertFalse(self.flow.helper.errors)

    def test_explicit_generation_recommends_once_and_old_project_open_does_not(self):
        original = self.flow.fixture.response
        posts = []
        def response(method, path):
            route = urlparse(path).path
            if route.endswith('/image-workspace/assistant') and method == 'POST':
                posts.append(deepcopy(self.flow.fixture.body))
                return 200, {'task': {'task_id': 'image-recommend-1', 'status': 'waiting_confirmation'},
                    'response': {'type': 'image_batch_proposal', 'project_id': PROJECT_ID,
                        'workspace_id': 'browser-workspace', 'target_kind': 'creative_block',
                        'proposal_digest': 'f' * 64, 'summary': '推荐已准备', 'shots': []}}
            return original(method, path)
        self.flow.fixture.response = response
        self.open()
        self.assertEqual(posts, [])
        self.flow.draft.value['blocks'] = []
        self.flow.draft.value['draft_id'] = None
        self.flow.draft.value['task'] = None
        self.page.reload()
        self.page.locator('#creative-first-confirmed-start').click()
        expect(self.page.locator('#creative-first-assistant-status')).to_contain_text('正在按需求创作')
        revision = self.flow.draft.value['revision']
        self.flow.draft.complete()
        self.flow.draft.value['revision'] = revision + 1
        expect(self.page.locator('[data-image-batch="image-recommend-1"]')).to_be_visible()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['target_kind'], 'creative_block')
        self.assertEqual(posts[0]['selected_block_ids'], ['block-1', 'block-2', 'block-3'])
        self.assertIn('expected_creative_revision', posts[0])
        self.page.reload()
        expect(self.page.locator('[data-workbench-card]').first).to_be_visible()
        self.assertEqual(len(posts), 1)
        self.assertFalse(self.flow.helper.errors)

    def test_initial_image_batch_polls_and_patches_stable_block_identity(self):
        original = self.flow.fixture.response
        requests = []
        model = {'type': 'image_batch_proposal', 'project_id': PROJECT_ID,
            'workspace_id': 'browser-workspace', 'target_kind': 'creative_block',
            'project_version': self.flow.fixture.project_version, 'proposal_digest': 'b' * 64,
            'summary': '核对初稿参考图', 'shots': [{'target_kind': 'creative_block', 'target_id': 'block-2',
                'block_id': 'block-2', 'shot_no': '2', 'visual': '匿名现场', 'included': True,
                'selected_candidate_key': 'image-1', 'candidates': [
                    {'candidate_key': 'image-1', 'caption': '匿名全景', 'suggestion_eligible': True, 'default_eligible': False},
                    {'candidate_key': 'image-2', 'caption': '匿名近景', 'suggestion_eligible': True, 'default_eligible': False}]}]}
        def response(method, path):
            route = urlparse(path).path
            if route.endswith('/image-workspace/assistant') and method == 'POST':
                requests.append(('start', deepcopy(self.flow.fixture.body)))
                pending = deepcopy(model); pending['shots'] = []; pending['progress'] = {'completed': 0, 'total': 1}
                return 200, {'task': {'task_id': 'initial-images', 'status': 'understanding'}, 'response': pending}
            if route.endswith('/image-workspace/assistant') and 'task_id=' in path:
                requests.append(('poll', {}))
                return 200, {'task': {'task_id': 'initial-images', 'status': 'waiting_confirmation'}, 'response': deepcopy(model)}
            if route.endswith('/image-workspace/assistant/initial-images') and method == 'PATCH':
                requests.append(('patch', deepcopy(self.flow.fixture.body)))
                model['shots'][0]['selected_candidate_key'] = self.flow.fixture.body['updates'][0]['candidate_key']
                return 200, {'task': {'task_id': 'initial-images', 'status': 'waiting_confirmation'}, 'response': deepcopy(model)}
            return original(method, path)
        self.flow.fixture.response = response
        self.open()
        self.page.locator('[data-workbench-select-item="block-2"]').check()
        self.page.locator('[data-workbench-bulk="images"]').click()
        expect(self.page.locator('[data-image-batch-choice]')).to_be_visible()
        expect(self.page.locator('[data-image-batch-save]')).to_be_enabled()
        self.page.locator('[data-image-batch-choice]').select_option('image-2')
        expect(self.page.locator('[data-image-batch-choice]')).to_have_value('image-2')
        self.assertTrue(any(kind == 'poll' for kind, _ in requests))
        update = next(value for kind, value in requests if kind == 'patch')['updates'][0]
        self.assertEqual(update['block_id'], 'block-2')
        self.assertNotIn('shot_id', update)
        self.assertFalse(self.flow.helper.errors)

    def test_parent_timeline_keeps_only_latest_receipt_actionable_after_reload(self):
        fixture = self.flow.fixture
        original = fixture.response
        calls = []
        task = {'task_id': 'parent-receipt', 'status': 'waiting_user'}
        proposal = {'type': 'workbench_proposal', 'task_id': task['task_id'], 'stage': 'creative',
            'summary': '核对本次修改', 'proposal_digest': 'c' * 64,
            'groups': [{'group_id': 'block-2', 'target_id': 'block-2', 'number': 2,
                'selected': True, 'depends_on': [],
                'changes': [{'field': '声音', 'before': '环境声', 'after': '现场掌声'}]}]}
        timeline = [
            {'task_id': task['task_id'], 'sequence_no': 1, 'status': 'executing',
                'response': {'type': 'workbench_progress', 'summary': '正在准备'}},
            {'task_id': task['task_id'], 'sequence_no': 2, 'status': 'waiting_user', 'response': proposal}]

        def response(method, path):
            route = urlparse(path).path
            if route == '/api/agent/sessions/flow-session':
                return 200, {'session': {'agent_session_id': 'flow-session'},
                    'tasks': [deepcopy(task)], 'timeline': deepcopy(timeline)}
            if route == '/api/agent/workbench/parent-receipt/confirm':
                calls.append(deepcopy(fixture.body))
                task['status'] = 'completed'
                timeline.extend([
                    {'task_id': task['task_id'], 'sequence_no': 3, 'status': 'executing', 'response': proposal},
                    {'task_id': task['task_id'], 'sequence_no': 4, 'status': 'completed',
                        'response': {'type': 'workbench_applied', 'summary': '本次已采用', 'can_undo': True}}])
                return 200, {'task': deepcopy(task), 'response': deepcopy(timeline[-1]['response'])}
            if route == '/api/agent/workbench/parent-receipt/undo':
                timeline.append({'task_id': task['task_id'], 'sequence_no': 5, 'status': 'completed',
                    'response': {'type': 'workbench_applied', 'summary': '本次已撤销', 'can_undo': False}})
                return 200, {'task': deepcopy(task), 'response': deepcopy(timeline[-1]['response'])}
            return original(method, path)

        fixture.response = response
        self.open()
        self.page.locator('#workbench-assistant-launcher').click()
        expect(self.page.locator('[data-workbench-proposal]')).to_have_count(1)
        self.page.locator('[data-workbench-confirm]').click()
        expect(self.page.locator('[data-workbench-undo]')).to_be_visible()
        self.assertEqual(calls, [{'proposal_digest': 'c' * 64, 'retained_group_ids': ['block-2']}])
        expect(self.page.locator('[data-workbench-confirm]')).to_have_count(0)
        self.page.reload()
        self.page.locator('#workbench-assistant-launcher').click()
        expect(self.page.locator('[data-workbench-proposal]')).to_have_count(1)
        self.page.locator('[data-workbench-undo]').click()
        expect(self.page.locator('[data-workbench-proposal]')).to_contain_text('本次已撤销')
        self.page.reload()
        self.page.locator('#workbench-assistant-launcher').click()
        expect(self.page.locator('[data-workbench-proposal]')).to_have_count(1)
        expect(self.page.locator('[data-workbench-confirm],[data-workbench-undo]')).to_have_count(0)
        self.assertFalse(self.flow.helper.errors)

    def test_materials_return_uses_stable_identity_and_legacy_shot_number(self):
        self.flow.formal_enabled = True
        for query, expected_id in [
            ('focus_shot=2', 'shot-stable-2'),
            ('shot_id=shot-stable-3&focus_shot=1', 'shot-stable-3'),
        ]:
            with self.subTest(query=query):
                self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/materials?return_to=shot-images&{query}')
                expect(self.page.locator('#page-materials')).to_be_visible()
                expect(self.page.locator('#formal-image-to-plan')).to_have_attribute(
                    'href', re.compile('shot_id=shot-stable-3' if expected_id.endswith('3') else 'focus_shot=2'))
                self.page.locator('[data-creative-close-materials]:visible').click()
                expect(self.page.locator('#page-script')).to_be_visible()
                expect(self.page.locator(f'[data-creative-shot="{expected_id}"]')).to_have_attribute('aria-current', 'true')
                if expected_id.endswith('3'):
                    self.assertNotIn('focus_shot=', self.page.url)
        self.assertFalse(self.flow.helper.errors)

    def test_lost_automatic_recommendation_response_retries_same_persisted_request(self):
        fixture = self.flow.fixture
        original = fixture.response
        requests, server_tasks = [], set()
        result = {'task': {'task_id': 'persisted-recommendation', 'status': 'waiting_confirmation'},
            'response': {'type': 'image_batch_proposal', 'project_id': PROJECT_ID,
                'workspace_id': 'browser-workspace', 'target_kind': 'creative_block',
                'proposal_digest': 'e' * 64, 'summary': '本次推荐已恢复', 'shots': []}}

        def response(method, path):
            route = urlparse(path).path
            if route.endswith('/image-workspace/assistant') and method == 'POST':
                requests.append(deepcopy(fixture.body))
                server_tasks.add(fixture.body['idempotency_key'])
                if len(requests) == 1:
                    return 503, {'detail': {'message': '服务端已接受，本次回执丢失'}}
                return 200, deepcopy(result)
            return original(method, path)

        fixture.response = response
        self.flow.draft.value.update(blocks=[], draft_id=None, task=None)
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script')
        self.page.locator('#creative-first-confirmed-start').click()
        expect(self.page.locator('#creative-first-assistant-status')).to_contain_text('正在按需求创作')
        revision = self.flow.draft.value['revision']
        self.flow.draft.complete()
        self.flow.draft.value['revision'] = revision + 1
        expect(self.page.locator('[data-workbench-retry-recommendation]')).to_be_visible()
        self.assertEqual(len(requests), 1)
        self.page.reload()
        expect(self.page.locator('[data-workbench-retry-recommendation]')).to_be_visible()
        self.assertEqual(len(requests), 1)
        # A retry must not rebuild its scope or optimistic versions from this newer draft.
        self.flow.draft.value['revision'] += 1
        self.page.locator('[data-workbench-retry-recommendation]').click()
        expect(self.page.locator('[data-image-batch="persisted-recommendation"]')).to_be_visible()
        expect(self.page.locator('[data-workbench-retry-recommendation]')).to_have_count(0)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(len(server_tasks), 1)
        self.page.reload()
        expect(self.page.locator('[data-workbench-card]').first).to_be_visible()
        self.assertEqual(len(requests), 2)
        self.assertFalse(self.flow.helper.errors)

    def test_expired_owner_session_shows_explicit_reentry_and_clears_stale_sessions(self):
        fixture = self.flow.fixture
        original = fixture.response
        expired, reissues = True, []

        def response(method, path):
            nonlocal expired
            route = urlparse(path).path
            if route == '/api/formal/workspaces/browser-workspace' and expired:
                return 403, {'detail': {'code': 'AUTH_SESSION_INVALID', 'message': '登录已失效'}}
            if route == '/api/agent/owner/reissue':
                reissues.append(deepcopy(fixture.body))
                expired = False
                return 200, {'auth_token': 'renewed-browser-session', 'workspace_id': 'browser-workspace'}
            return original(method, path)

        fixture.response = response
        self.page.add_init_script("localStorage.setItem('mplus.agent.session.old-project', 'expired-project-session')")
        self.page.goto(f'{self.flow.helper.base}/projects')
        expect(self.page.locator('#formal-owner-onboarding')).to_be_visible()
        expect(self.page.locator('#formal-owner-enable')).to_have_text('重新进入工作区')
        expect(self.page.locator('#formal-owner-enable')).to_be_disabled()
        self.assertEqual(reissues, [])
        self.assertIsNone(self.page.evaluate("localStorage.getItem('mplus.agent.auth')"))
        self.assertIsNone(self.page.evaluate("localStorage.getItem('mplus.agent.session.old-project')"))
        self.page.locator('#formal-owner-confirm').check()
        self.page.locator('#formal-owner-enable').click()
        expect(self.page.locator('#formal-owner-onboarding')).not_to_be_visible()
        expect(self.page.locator('#formal-project-list')).to_contain_text('匿名发布会')
        self.assertEqual(reissues, [{'confirmed': True}])
        self.assertEqual(self.page.evaluate("localStorage.getItem('mplus.agent.auth')"), 'renewed-browser-session')
        self.assertFalse(self.flow.helper.errors)

    def test_workspace_server_error_keeps_owner_session_and_shows_read_retry(self):
        fixture = self.flow.fixture
        original = fixture.response

        def response(method, path):
            if urlparse(path).path == '/api/formal/workspaces/browser-workspace':
                return 500, {'detail': {'code': 'INTERNAL_ERROR', 'message': '读取暂时失败'}}
            return original(method, path)

        fixture.response = response
        self.page.goto(f'{self.flow.helper.base}/projects')
        expect(self.page.locator('[data-projects-retry]')).to_be_visible()
        expect(self.page.locator('#formal-owner-onboarding')).not_to_be_visible()
        self.assertEqual(self.page.evaluate("localStorage.getItem('mplus.agent.auth')"), 'browser-session')
        self.assertFalse(self.flow.helper.errors)
