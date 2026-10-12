"""Special-action handoffs through the real UI with anonymous HTTP fixtures.

These cover frontend contracts and persistence of local drafts, not provider
execution or real-service acceptance.
"""
from copy import deepcopy
from urllib.parse import parse_qs, urlparse
import unittest

from playwright.sync_api import expect
from tests import test_mvp_v2_application_flow_browser as application_flow
from tests.test_mvp_v2_image_creation_browser import _ImageCreationFixture
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class WorkbenchHandoffBrowserTests(unittest.TestCase):
    def setUp(self):
        self.flow = application_flow.ApplicationFlowBrowserTests('runTest')
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        self.page = self.flow.page
        self.page.set_viewport_size({'width': 1440, 'height': 1000})
        self.calls = []
        self.task_id = 'handoff-parent'
        self.model = {}
        self.extra_timeline = []
        self.extra_tasks = []
        self.image = _ImageCreationFixture()
        self.image.target_kind = 'creative_block'
        self.on_confirm = None
        original = self.flow.fixture.response

        def response(method, path):
            route = urlparse(path).path
            body = deepcopy(self.flow.fixture.body)
            self.calls.append((method, path, body))
            if route == '/api/agent/sessions/flow-session':
                task = {'task_id': self.task_id, 'status': 'waiting_user'}
                return 200, {'session': {'agent_session_id': 'flow-session'},
                    'tasks': [task, *deepcopy(self.extra_tasks)],
                    'timeline': [*deepcopy(self.extra_timeline),
                        {'task_id': self.task_id, 'sequence_no': 99, 'status': 'waiting_user',
                            'response': deepcopy(self.model)}]}
            if route == f'/api/agent/workbench/{self.task_id}':
                return 200, {'task': {'task_id': self.task_id}, 'response': deepcopy(self.model)}
            if route == f'/api/agent/workbench/{self.task_id}/confirm':
                self.on_confirm(body)
                return 200, {'task': {'task_id': self.task_id}, 'response': deepcopy(self.model)}
            if route == f'/api/agent/workbench/{self.task_id}/undo':
                self.model.update(undone=True, can_undo=False, image_creation_requests=[], summary='本次已撤销')
                return 200, {'task': {'task_id': self.task_id}, 'response': deepcopy(self.model)}
            if '/image-creations' in route:
                self.image.body = body
                status, value = self.image.response(method, path)
                target_id = value.get('target', {}).get('id') if isinstance(value, dict) else None
                block = next((item for item in self.flow.draft.value['blocks'] if item['block_id'] == target_id), None)
                if block and 'original_shot' in value:
                    value['original_shot'] = {'visual': block['fields']['画面描述']}
                    value['creative_revision'] = self.flow.draft.value['revision']
                return status, value
            if '/images/' in route and '/reference-' in route:
                return self.image.response(method, path)
            if route.endswith('/messages'):
                return 200, {'task': {'task_id': self.task_id, 'status': 'waiting_user'},
                    'response': deepcopy(self.model)}
            return original(method, path)

        self.flow.fixture.response = response

    def open_assistant(self):
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script')
        expect(self.page.locator('[data-workbench-card]').first).to_be_visible()
        self.page.locator('#workbench-assistant-launcher').click()

    def handoff(self):
        raw = '需求改为面向合作伙伴；规划突出现场交流；为第二段生成新图，保留客户原话，不加入数字。'
        actions = [
            {'kind': 'revise_brief', 'action_index': 0, 'instruction': '需求改为面向合作伙伴'},
            {'kind': 'replan', 'action_index': 1, 'instruction': '规划突出现场交流'},
            {'kind': 'create_image', 'action_index': 2, 'instruction': '为第二段生成新图',
                'target_ids': ['block-2'], 'target_labels': ['第 2 段'], 'depends_on': [0, 1]}]
        self.model = {'type': 'workbench_handoff', 'task_id': self.task_id,
            'project_id': PROJECT_ID, 'workspace_id': 'browser-workspace', 'stage': 'creative',
            'summary': '原要求已保留', 'original_instruction': raw, 'actions': actions,
            'constraints': {'preserve': ['客户原话'], 'prohibit': ['加入数字'], 'total_seconds': 60},
            'pending_actions': [actions[2]], 'handoffs': [
                {'kind': kind, 'task_id': self.task_id, 'instruction': actions[index]['instruction'],
                    'action_index': index,
                    'next_href': f'/projects/{PROJECT_ID}/{kind}?workbench_task_id={self.task_id}'}
                for index, kind in enumerate(('brief', 'plan'))]}
        return raw

    def creation_request(self, block_id, action_index=0):
        return {'type': 'image_creation_request', 'task_id': self.task_id, 'action_index': action_index,
            'project_id': PROJECT_ID, 'workspace_id': 'browser-workspace', 'stage': 'creative',
            'target_kind': 'creative_block', 'target_id': block_id,
            'target_label': f'第 {block_id[-1]} 段', 'instruction': f'为 {block_id} 准备自然光画面',
            'available': True, 'constraints': {}, 'depends_on': []}

    def business_posts(self):
        return [path for method, path, _ in self.calls if method == 'POST' and any(
            segment in path for segment in ('/brief/files', '/brief-analysis', '/brief/confirm', '/plan', '/image-creations'))]

    def test_brief_handoff_preserves_raw_actions_and_existing_draft_until_explicit_send(self):
        raw = self.handoff()
        self.extra_timeline = deepcopy(self.flow.timeline)
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/brief')
        self.page.locator('#brief-source-panel > summary').click()
        self.page.locator('#formal-brief-text').fill('已有但尚未提交的需求。')
        self.open_assistant()
        expect(self.page.locator('[data-workbench-handoff-task] [data-workbench-actions] li')).to_have_count(3)
        expect(self.page.locator('[data-workbench-handoff-link]')).to_have_count(2)
        self.page.locator('[data-workbench-handoff-link]').first.click()
        host = self.page.locator('#workbench-brief-handoff')
        expect(host).to_contain_text(raw)
        expect(host.locator('[data-workbench-actions] li')).to_have_count(3)
        expect(self.page.locator('#formal-brief-text')).to_have_value('已有但尚未提交的需求。')
        self.assertEqual(self.business_posts(), [])
        self.page.locator('[data-brief-reply="analysis-1:0"]').first.click()
        self.page.locator('#formal-brief-text').fill('已写好的问题回复。')
        host.locator('[data-workbench-handoff-add]').click()
        value = self.page.locator('#formal-brief-text').input_value()
        self.assertIn(raw, value)
        self.assertTrue(value.startswith('已有但尚未提交的需求。'))
        self.assertIn('保留：客户原话', value)
        self.assertIn('不改动：加入数字', value)
        expect(self.page.locator('#brief-answer-drafts')).to_contain_text('已写好的问题回复。')
        host.locator('[data-workbench-handoff-add]').click()
        expect(self.page.locator('#formal-brief-text')).to_have_value(value)
        expect(self.page.locator('#brief-answer-drafts')).to_contain_text('已写好的问题回复。')
        self.page.reload()
        expect(host).to_contain_text(raw)
        expect(self.page.locator('#formal-brief-text')).to_have_value(value)
        self.assertEqual(self.business_posts(), [])
        self.assertFalse(self.flow.helper.errors)

    def test_image_request_removed_after_render_never_opens_generation(self):
        self.model = {'type': 'image_creation_requests', 'task_id': self.task_id,
            'project_id': PROJECT_ID, 'workspace_id': 'browser-workspace',
            'image_creation_requests': [self.creation_request('block-2')]}
        self.open_assistant()
        expect(self.page.locator('[data-agent-image-create]')).to_be_visible()
        self.model.update(undone=True, image_creation_requests=[])
        self.page.locator('[data-agent-image-create]').click()
        expect(self.page.get_by_text('本次生图要求已变化，请重新读取创作助手中的最新结果。', exact=True)).to_be_visible()
        expect(self.page.get_by_role('dialog', name='生成新图')).to_have_count(0)
        self.assertEqual(self.image.context_reads, 0)
        self.assertEqual(self.image.creations, [])
        self.assertFalse(self.flow.helper.errors)

    def test_plan_handoff_uses_original_brief_entry_with_full_instruction(self):
        raw = self.handoff()
        self.open_assistant()
        self.page.locator('[data-workbench-handoff-link]').nth(1).click()
        host = self.page.locator('#workbench-plan-handoff')
        expect(host).to_contain_text('没有直接提交修改文字的入口')
        expect(host).to_contain_text(raw)
        host.locator('[data-workbench-handoff-brief]').click()
        expect(self.page.locator('#workbench-brief-handoff')).to_contain_text(raw)
        self.assertIn('workbench_task_id=handoff-parent', self.page.url)
        self.assertIn('return_to=plan', self.page.url)
        expect(self.page.locator('#formal-brief-text')).to_have_value('')
        self.assertEqual(self.business_posts(), [])
        self.assertFalse(self.flow.helper.errors)

    def test_foreign_handoff_never_enters_current_brief(self):
        self.handoff()
        self.model['project_id'] = 'foreign-project'
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/brief?workbench_task_id={self.task_id}')
        expect(self.page.locator('#workbench-brief-handoff')).to_contain_text('不属于当前项目')
        expect(self.page.locator('[data-workbench-handoff-add]')).to_have_count(0)
        expect(self.page.locator('#formal-brief-text')).to_have_value('')
        self.assertEqual(self.business_posts(), [])
        self.assertFalse(self.flow.helper.errors)

    def test_each_image_target_rechecks_receipt_and_opens_original_generation_dialog(self):
        self.model = {'type': 'image_creation_requests', 'task_id': self.task_id,
            'project_id': PROJECT_ID, 'workspace_id': 'browser-workspace',
            'image_creation_requests': [self.creation_request(f'block-{i}') for i in (1, 2)]}
        self.open_assistant()
        expect(self.page.locator('[data-agent-image-create]')).to_have_count(2)
        keys = self.page.locator('[data-agent-image-create]').evaluate_all('(nodes) => nodes.map(node => node.dataset.agentImageCreate)')
        self.assertEqual(len(set(keys)), 2)
        for index in (0, 1):
            self.page.locator('[data-agent-image-create]').nth(index).click()
            expect(self.page.get_by_role('dialog', name='生成新图')).to_be_visible()
            expect(self.page.locator('[data-creation-prompt]')).not_to_have_value('')
            self.page.locator('[data-creation-close]').click()
        receipts = [path for method, path, _ in self.calls if method == 'GET' and path == f'/api/agent/workbench/{self.task_id}']
        self.assertEqual(len(receipts), 2)
        contexts = [parse_qs(urlparse(path).query)['target_id'][0] for _, path, _ in self.calls if '/image-creations/context?' in path]
        self.assertEqual(contexts, ['block-1', 'block-2'])
        self.assertEqual(self.image.creations, [])
        self.assertFalse(self.flow.helper.errors)

    def test_mixed_adoption_only_exposes_retained_image_targets_using_updated_shot(self):
        actions = [{'kind': 'revise_text', 'action_index': 0, 'instruction': '第二段改为交流全景'},
            {'kind': 'create_image', 'action_index': 1, 'instruction': '采用后为第二段生成新图', 'depends_on': [0]}]
        self.model = {'type': 'workbench_proposal', 'task_id': self.task_id, 'proposal_digest': 'd' * 64,
            'actions': actions, 'pending_actions': [actions[1]], 'groups': [
                {'group_id': 'block-2', 'target_id': 'block-2', 'label': '第 2 段', 'selected': True,
                    'changes': [{'field': '画面描述', 'before': '匿名现场', 'after': '最新采用的交流全景'}]}]}

        def confirm(body):
            self.assertEqual(body['retained_group_ids'], ['block-2'])
            self.flow.draft.value['blocks'][1]['fields']['画面描述'] = '最新采用的交流全景'
            self.flow.draft.value['revision'] += 1
            self.model = {'type': 'workbench_applied', 'task_id': self.task_id, 'can_undo': True,
                'project_id': PROJECT_ID, 'workspace_id': 'browser-workspace',
                'image_creation_requests': [self.creation_request('block-2', 1)]}

        self.on_confirm = confirm
        self.open_assistant()
        expect(self.page.locator('[data-agent-image-create]')).to_have_count(0)
        self.page.locator('[data-workbench-confirm]').click()
        expect(self.page.locator('[data-agent-image-create]')).to_have_count(1)
        self.page.locator('[data-agent-image-create]').click()
        expect(self.page.get_by_role('dialog', name='生成新图')).to_contain_text('最新采用的交流全景')
        self.page.locator('[data-creation-close]').click()
        self.assertEqual(self.image.creations, [])
        self.page.locator('[data-workbench-undo]').click()
        expect(self.page.locator('[data-agent-image-create]')).to_have_count(0)
        self.page.reload()
        self.page.locator('#workbench-assistant-launcher').click()
        expect(self.page.locator('[data-agent-image-create]')).to_have_count(0)
        self.assertFalse(self.flow.helper.errors)

    def test_independent_image_action_remains_selectable_without_text_changes(self):
        action = {'kind': 'create_image', 'action_index': 1, 'instruction': '第三段生成新图', 'target_ids': ['block-3']}
        self.model = {'type': 'workbench_proposal', 'task_id': self.task_id, 'proposal_digest': 'a' * 64,
            'actions': [{'kind': 'revise_text', 'instruction': '第二段声音改为掌声'}, action],
            'pending_actions': [action], 'groups': [
                {'group_id': 'block-2', 'target_id': 'block-2', 'label': '第 2 段', 'selected': True,
                    'changes': [{'field': '声音', 'before': '环境声', 'after': '掌声'}]},
                {'group_id': 'block-3', 'target_id': 'block-3', 'label': '第 3 段', 'selected': True,
                    'action_only': True, 'changes': [], 'pending_actions': [action]}]}

        def confirm(body):
            self.assertEqual(body['retained_group_ids'], ['block-3'])
            self.model = {'type': 'workbench_applied', 'task_id': self.task_id,
                'project_id': PROJECT_ID, 'workspace_id': 'browser-workspace',
                'image_creation_requests': [self.creation_request('block-3', 1)]}

        self.on_confirm = confirm
        before = deepcopy(self.flow.draft.value)
        self.open_assistant()
        expect(self.page.locator('[data-workbench-group-actions]')).to_contain_text('生成图片需求')
        self.page.locator('[data-workbench-retain="block-2"]').uncheck()
        self.page.locator('[data-workbench-confirm]').click()
        expect(self.page.locator('[data-agent-image-create]')).to_have_count(1)
        expect(self.page.locator('[data-agent-image-create]')).to_contain_text('第 3 段')
        self.assertEqual(self.flow.draft.value, before)
        self.assertFalse(self.flow.helper.errors)

    def test_failed_choice_keeps_user_draft_and_does_not_resubmit(self):
        raw = '第一段改全景，第二段生成新图；保留客户原话。'
        self.model = {'type': 'workbench_failed', 'task_id': self.task_id, 'summary': '原要求已保留，请核对后重试',
            'original_instruction': raw, 'choices': [{'label': '保留原要求重试', 'instruction': raw}]}
        self.open_assistant()
        self.page.locator('#agent-message').fill('额外保留结尾。')
        self.page.locator('[data-agent-clarification]').click()
        expect(self.page.locator('#agent-message')).to_have_value('额外保留结尾。\n\n' + raw)
        self.page.locator('[data-agent-clarification]').click()
        expect(self.page.locator('#agent-message')).to_have_value('额外保留结尾。\n\n' + raw)
        self.assertFalse(any(path.endswith('/messages') for _, path, _ in self.calls))
        self.page.locator('#agent-message-submit').click()
        expect(self.page.locator('#agent-message')).to_have_value('')
        messages = [body for method, path, body in self.calls if method == 'POST' and path.endswith('/messages')]
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]['content'], '额外保留结尾。\n\n' + raw)
        self.assertFalse(self.flow.helper.errors)

    def test_internal_text_proposal_only_opens_combined_parent_review(self):
        self.model = {'type': 'workbench_proposal', 'task_id': self.task_id, 'proposal_digest': 'c' * 64,
            'groups': [{'group_id': 'block-2', 'label': '第 2 段', 'changes': [
                {'field': '声音', 'before': '环境声', 'after': '掌声'}]}]}
        self.flow.draft.value['proposal'] = {'digest': 'e' * 64, 'revision': self.flow.draft.value['revision'],
            'workbench_parent_task_id': self.task_id, 'summary': '内部文字建议', 'groups': [
                {'block_id': 'block-2', 'changes': [{'field': '声音', 'before': '环境声', 'after': '掌声'}]}]}
        self.page.goto(f'{self.flow.helper.base}/projects/{PROJECT_ID}/script')
        expect(self.page.locator('[data-workbench-parent-open]')).to_be_visible()
        expect(self.page.locator('#creative-first-apply,#creative-first-decline,[data-first-retain]')).to_have_count(0)
        self.page.locator('[data-workbench-parent-open]').click()
        expect(self.page.locator('#workbench-assistant')).to_be_visible()
        expect(self.page.locator('[data-workbench-confirm]')).to_be_visible()
        self.assertEqual(self.flow.draft.confirmations, [])
        self.page.reload()
        expect(self.page.locator('[data-workbench-parent-open]')).to_be_visible()
        expect(self.page.locator('#creative-first-apply,#creative-first-decline,[data-first-retain]')).to_have_count(0)
        self.assertFalse(self.flow.helper.errors)

    def test_feed_hides_internal_children_and_only_latest_standalone_image_event(self):
        self.model = {'type': 'workbench_progress', 'summary': '父任务最新进度'}
        self.extra_tasks = [{'task_id': 'internal-image', 'status': 'waiting_confirmation', 'workbench_parent_task_id': self.task_id},
            {'task_id': 'standalone-image', 'status': 'waiting_confirmation'}]
        self.extra_timeline = [
            {'task_id': self.task_id, 'sequence_no': 1, 'response': {'type': 'workbench_progress', 'summary': '旧父任务进度'}},
            {'task_id': 'internal-image', 'sequence_no': 2, 'response': {'type': 'image_batch_proposal', 'summary': '内部候选', 'shots': []}},
            {'task_id': 'standalone-image', 'sequence_no': 3, 'response': {'type': 'image_batch_proposal', 'summary': '独立旧候选', 'shots': []}},
            {'task_id': 'standalone-image', 'sequence_no': 4, 'response': {'type': 'image_batch_proposal', 'summary': '独立最新候选', 'shots': []}}]
        self.open_assistant()
        expect(self.page.locator('[data-workbench-proposal]')).to_have_count(1)
        expect(self.page.locator('[data-image-batch="internal-image"]')).to_have_count(0)
        expect(self.page.locator('[data-image-batch="standalone-image"]')).to_have_count(1)
        expect(self.page.locator('#agent-feed')).not_to_contain_text('旧父任务进度')
        expect(self.page.locator('#agent-feed')).not_to_contain_text('独立旧候选')
        expect(self.page.locator('#agent-feed')).to_contain_text('独立最新候选')
        self.assertFalse(self.flow.helper.errors)


if __name__ == '__main__':
    unittest.main()
