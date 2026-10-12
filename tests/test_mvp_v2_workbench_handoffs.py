"""Persistent handoffs reuse existing revision and image-creation authorities."""
from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

from mvp_v2 import creative_workbench
from tests import test_mvp_v2_creative_workbench as fixtures


class WorkbenchHandoffTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CreativeWorkbenchTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.generated_images = []
        def forbidden_provider(*args, **kwargs):
            self.generated_images.append((args, kwargs))
            raise AssertionError('A handoff must never invoke the image provider')
        self.f.generation.image_provider = forbidden_provider

    def action(self, kind, instruction, *, fields=(), dependencies=(), numbers=()):
        return {'kind': kind, 'instruction': instruction, 'fields': list(fields),
            'depends_on': list(dependencies), 'target_scope': 'numbers' if numbers else 'selected',
            **({'target_numbers': list(numbers)} if numbers else {})}

    def start(self, actions, *, key='workbench-handoff', content='修改所选两段，并保留全部后续动作要求。'):
        self.f.provider.actions = actions
        return creative_workbench.start(self.f.agent, self.f.token, self.f.session['agent_session_id'],
            content=content, context=self.f.context(), operation_key=key)

    def execute(self, actions, **kwargs):
        started = self.start(actions, **kwargs)
        return creative_workbench.execute(self.f.agent, self.f.token, started['task']['task_id'])

    def test_brief_plan_and_remaining_actions_survive_refresh_with_exact_instructions_and_dependencies(self):
        actions = [self.action('revise_brief', '把目标时长改为90秒'),
            self.action('replan', '按新需求重新规划镜数', dependencies=[0]),
            self.action('revise_text', '改写所选画面与声音', fields=['visual', 'sound'], dependencies=[1]),
            self.action('find_images', '按新画面推荐参考图', dependencies=[2]),
            self.action('create_image', '为未找到图片的段落生成新图', dependencies=[3])]
        original = '把目标时长改为90秒，按新需求重新规划镜数，改写所选画面与声音，按新画面推荐参考图，再生成需要的新图。'
        before = self.f.business()
        result = self.execute(actions, content=original)
        response = result['response']
        self.assertEqual(result['task']['status'], 'waiting_user')
        self.assertEqual(response['type'], 'workbench_handoff')
        self.assertEqual(response['original_instruction'], original)
        self.assertEqual([handoff['kind'] for handoff in response['handoffs']], ['brief', 'plan'])
        self.assertEqual(response['handoff'], response['handoffs'][0])
        self.assertEqual([action['instruction'] for action in response['actions']], [action['instruction'] for action in actions])
        self.assertEqual([action['depends_on'] for action in response['actions']], [[], [0], [1], [2], [3]])
        self.assertEqual([action['action_index'] for action in response['pending_actions']], [2, 3, 4])
        for handoff in response['handoffs']:
            self.assertEqual(handoff['task_id'], result['task']['task_id'])
            self.assertEqual(handoff['stage'], 'creative')
            self.assertEqual(handoff['target_ids'], ['block_1', 'block_2'])
            self.assertEqual(handoff['labels'], ['第 1 段', '第 2 段'])
            self.assertIn('workbench_task_id=' + result['task']['task_id'], handoff['next_href'])
        self.assertEqual(self.f.business(), before)
        self.assertFalse(self.f.provider.editor_inputs)
        self.assertEqual(self.generated_images, [])
        self.assertEqual(creative_workbench.read(self.f.new_agent(), self.f.token, result['task']['task_id'])['response'], response)

    def test_pure_multi_image_creation_maps_every_target_to_current_context_without_generation(self):
        before = self.f.business()
        result = self.execute([self.action('create_image', '为这两段分别生成一张无文字的场景参考图')],
            content='为这两段分别生成一张无文字的场景参考图。')
        response = result['response']
        self.assertEqual(response['type'], 'image_creation_requests')
        self.assertEqual(result['task']['status'], 'completed')
        requests = response['image_creation_requests']
        self.assertEqual([item['target_id'] for item in requests], ['block_1', 'block_2'])
        for item in requests:
            self.assertEqual(item['target_kind'], 'creative_block')
            self.assertEqual(item['project_id'], self.f.scope['project_id'])
            self.assertEqual(item['workspace_id'], self.f.scope['workspace_id'])
            self.assertEqual(item['instruction'], '为这两段分别生成一张无文字的场景参考图')
            row = next(row for row in before['shots'] if row['shot_id'] == item['target_id'])
            self.assertEqual(item['original_shot']['画面描述'], row['fields']['画面描述'])
            self.assertTrue(item['target_basis'])
        self.assertNotIn('next_href', response)
        self.assertEqual(self.f.business(), before)
        self.assertFalse(self.f.provider.editor_inputs)
        self.assertEqual(self.generated_images, [])

    def test_mixed_text_find_and_create_exposes_only_adopted_targets_from_new_saved_text_then_clears_on_undo(self):
        actions = [self.action('revise_text', '修改所选画面与声音', fields=['visual', 'sound']),
            self.action('find_images', '按新画面推荐参考图', dependencies=[0]),
            self.action('create_image', '依据采用后的新画面生成新图，保留中性背景', dependencies=[0, 1])]
        before = self.f.business()
        proposal = self.execute(actions, content='修改所选两段画面与声音，按新画面推荐参考图，并依据采用后的新画面生成新图。')
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        self.assertNotIn('image_creation_requests', proposal['response'])
        self.assertEqual([action['action_index'] for action in proposal['response']['pending_actions']], [2])
        self.assertEqual(self.f.business(), before)
        saved = self.f.confirm(proposal, ['block_1'])
        requests = saved['response']['image_creation_requests']
        self.assertEqual([item['target_id'] for item in requests], ['block_1'])
        self.assertEqual(requests[0]['depends_on'], [0, 1])
        after = self.f.business()
        self.assertEqual(requests[0]['original_shot']['画面描述'], after['shots'][0]['fields']['画面描述'])
        self.assertNotEqual(requests[0]['original_shot']['画面描述'], before['shots'][0]['fields']['画面描述'])
        self.assertEqual(after['shots'][1], before['shots'][1])
        self.assertEqual(saved['response']['pending_actions'][0]['target_ids'], ['block_2'])
        database = self.f.database()
        self.assertEqual(self.f.confirm(proposal, ['block_1'])['response'], saved['response'])
        self.assertEqual(self.f.database(), database)
        self.assertEqual(creative_workbench.read(self.f.new_agent(), self.f.token, proposal['task']['task_id'])['response'], saved['response'])
        undone = creative_workbench.undo(self.f.new_agent(), self.f.token, proposal['task']['task_id'])
        self.assertEqual(undone['response']['image_creation_requests'], [])
        restored = self.f.business()
        self.assertTrue(restored['shots'][0].pop('image_reconfirmation_required'))
        before['shots'][0].pop('image_reconfirmation_required', None)
        self.assertEqual(restored, before)
        self.assertEqual(self.generated_images, [])

    def test_independent_creation_target_has_selectable_group_and_can_be_confirmed_without_editing_the_draft(self):
        actions = [self.action('revise_text', '修改第1段声音', fields=['sound'], numbers=[1]),
            self.action('create_image', '为第2段当前画面生成无文字参考图', numbers=[2])]
        before = self.f.business()
        proposal = self.execute(actions, content='修改第1段声音，并为第2段当前画面生成无文字参考图。')
        groups = {group['target_id']: group for group in proposal['response']['groups']}
        self.assertEqual(set(groups), {'block_1', 'block_2'})
        self.assertTrue(groups['block_2']['action_only'])
        self.assertEqual(groups['block_2']['changes'], [])
        self.assertIsNone(groups['block_2']['image'])
        self.assertEqual(groups['block_2']['pending_actions'][0]['action_index'], 1)
        self.assertEqual(groups['block_2']['depends_on'], [])
        draft_before = deepcopy(self.f.repo.read()['body']['draft'])
        saved = self.f.confirm(proposal, ['block_2'])
        self.assertEqual(saved['task']['status'], 'completed')
        self.assertEqual([item['target_id'] for item in saved['response']['image_creation_requests']], ['block_2'])
        self.assertEqual(saved['response']['image_creation_requests'][0]['original_shot']['画面描述'], before['shots'][1]['fields']['画面描述'])
        self.assertIn('当前稿未修改', saved['response']['summary'])
        self.assertEqual(saved['response']['pending_actions'], [])
        self.assertEqual(self.f.business(), before)
        self.assertEqual(self.f.repo.read()['body']['draft'], draft_before)
        stable = self.f.database()
        self.assertEqual(self.f.confirm(proposal, ['block_2'])['response'], saved['response'])
        self.assertEqual(self.f.database(), stable)
        self.assertEqual(creative_workbench.read(self.f.new_agent(), self.f.token, proposal['task']['task_id'])['response'], saved['response'])
        state_after = self.f.repo.read()
        undone = creative_workbench.undo(self.f.new_agent(), self.f.token, proposal['task']['task_id'])
        self.assertEqual(undone['response']['image_creation_requests'], [])
        self.assertEqual(self.f.repo.read(), state_after, 'Withdrawing a creation handoff must not write the draft repository')
        self.assertEqual(self.f.business(), before)
        self.assertEqual(self.generated_images, [])

    def test_unselected_independent_creation_target_remains_pending_after_text_adoption(self):
        actions = [self.action('revise_text', '修改第1段声音', fields=['sound'], numbers=[1]),
            self.action('create_image', '为第2段生成无文字参考图', numbers=[2])]
        before = self.f.business()
        proposal = self.execute(actions, content='修改第1段声音，并为第2段生成无文字参考图。')
        saved = self.f.confirm(proposal, ['block_1'])
        self.assertEqual(saved['response']['image_creation_requests'], [])
        self.assertEqual(saved['response']['pending_actions'][0]['target_ids'], ['block_2'])
        self.assertNotEqual(self.f.business()['shots'][0]['fields']['声音'], before['shots'][0]['fields']['声音'])
        self.assertEqual(self.f.business()['shots'][1], before['shots'][1])

    def test_creation_only_group_with_unadopted_dependency_does_not_claim_adoption(self):
        actions = [self.action('revise_text', '修改第1段声音', fields=['sound'], numbers=[1]),
            self.action('create_image', '采用第1段声音后，为第2段生成新图', numbers=[2], dependencies=[0])]
        before = self.f.business()
        proposal = self.execute(actions, content='修改第1段声音，采用后为第2段生成新图。')
        group = next(group for group in proposal['response']['groups'] if group['target_id'] == 'block_2')
        self.assertEqual(group['depends_on'], ['block_1'])
        saved = self.f.confirm(proposal, ['block_2'])
        self.assertEqual(saved['task']['status'], 'cancelled')
        self.assertEqual(saved['response']['image_creation_requests'], [])
        self.assertEqual(saved['response']['pending_actions'][0]['target_ids'], ['block_2'])
        self.assertFalse(saved['response']['can_undo'])
        self.assertEqual(self.f.business(), before)

    def test_successful_search_gap_allows_dependent_creation_without_claiming_an_image_was_saved(self):
        actions = [self.action('find_images', '为所选段落推荐参考图'),
            self.action('create_image', '没有合适参考图时按当前画面生成新图', dependencies=[0])]
        before = self.f.business()
        proposal = self.execute(actions, content='为所选两段推荐参考图，没有合适参考图时按当前画面生成新图。')
        self.assertTrue(all(group['image']['state'] in {'reference', 'gap'}
            and not group['image']['included'] for group in proposal['response']['groups']))
        saved = self.f.confirm(proposal, ['block_1'])
        self.assertEqual(saved['task']['status'], 'completed')
        self.assertEqual([item['target_id'] for item in saved['response']['image_creation_requests']], ['block_1'])
        self.assertIn('当前稿未修改', saved['response']['summary'])
        self.assertEqual(saved['response']['pending_actions'][0]['target_ids'], ['block_2'])
        self.assertEqual(self.f.business(), before)
        self.assertEqual(self.generated_images, [])

    def test_search_read_failure_does_not_satisfy_a_creation_dependency(self):
        actions = [self.action('find_images', '为所选段落推荐参考图'),
            self.action('create_image', '找图完成后为缺图段落生成新图', dependencies=[0])]
        before = self.f.business()
        with patch.object(self.f.library, 'candidates', side_effect=OSError('anonymous isolated read failure')):
            proposal = self.execute(actions, content='为所选两段推荐参考图，找图完成后为缺图段落生成新图。')
        self.assertTrue(all(group['image']['reason_code'] == 'read_failed' for group in proposal['response']['groups']))
        saved = self.f.confirm(proposal, ['block_1'])
        self.assertEqual(saved['task']['status'], 'cancelled')
        self.assertEqual(saved['response']['image_creation_requests'], [])
        self.assertEqual(saved['response']['pending_actions'][0]['target_ids'], ['block_1', 'block_2'])
        self.assertEqual(self.f.business(), before)
        self.assertEqual(self.generated_images, [])

    def test_cross_target_dependency_not_adopted_keeps_creation_pending(self):
        actions = [self.action('revise_text', '修改第1段画面', fields=['visual'], numbers=[1]),
            self.action('revise_text', '修改第2段声音', fields=['sound'], numbers=[2]),
            self.action('create_image', '参照第1段采用后的画面，为第2段生成新图', numbers=[2], dependencies=[0])]
        proposal = self.execute(actions, content='修改第1段画面和第2段声音，再参照第1段采用后的画面为第2段生成新图。')
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        saved = self.f.confirm(proposal, ['block_2'])
        self.assertEqual(saved['response']['image_creation_requests'], [])
        self.assertEqual(saved['response']['pending_actions'][0]['target_ids'], ['block_2'])
        self.assertEqual(saved['response']['pending_actions'][0]['depends_on'], [0])
        self.assertEqual(self.generated_images, [])

    def test_execute_reloads_state_after_acquiring_lock_and_does_not_repeat_models(self):
        started = self.start([self.action('revise_text', '修改所选声音', fields=['sound'])], key='lock-state-race')
        @contextmanager
        def acquired_after_another_worker_finished(*_args, **_kwargs):
            task, data, response = creative_workbench._load(self.f.agent, self.f.token, started['task']['task_id'])
            creative_workbench._record(self.f.agent, self.f.token, task, data, response, 'executing')
            creative_workbench._record(self.f.agent, self.f.token, task, data,
                {'type': 'read_response', 'summary': '另一个执行器已经完成'}, 'completed')
            yield True
        before = self.f.business()
        with patch.object(creative_workbench, 'try_exclusive_lock', acquired_after_another_worker_finished):
            result = creative_workbench.execute(self.f.agent, self.f.token, started['task']['task_id'])
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['response']['summary'], '另一个执行器已经完成')
        self.assertEqual(self.f.provider.advisor_inputs, [])
        self.assertEqual(self.f.provider.editor_inputs, [])
        self.assertEqual(self.f.business(), before)


class FormalImageHandoffTests(unittest.TestCase):
    def test_formal_independent_creation_target_can_be_adopted_and_withdrawn_without_script_changes(self):
        f = fixtures.FormalWorkbenchTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.agent.generation.creative_revision = f.revision
        f.provider.actions = [
            {'kind': 'revise_text', 'target_scope': 'numbers', 'target_numbers': [1], 'fields': ['sound'],
                'instruction': '修改第1镜声音', 'depends_on': []},
            {'kind': 'create_image', 'target_scope': 'numbers', 'target_numbers': [2], 'fields': [],
                'instruction': '根据第2镜当前画面生成参考图', 'depends_on': []}]
        current = f.workspace.read_current(f.token, **f.scope)
        before = f.business()
        started = creative_workbench.start(f.agent, f.token, f.session['agent_session_id'],
            content='修改第1镜声音，并根据第2镜当前画面生成参考图。',
            context={'stage': 'formal', 'scope': 'selected', 'selected_target_ids': f.targets,
                'current_target_id': f.targets[0], 'expected_project_version': current['project_version'],
                'expected_revision': current['revision']}, operation_key='formal-independent-image-handoff')
        proposal = creative_workbench.execute(f.agent, f.token, started['task']['task_id'])
        self.assertEqual({group['target_id'] for group in proposal['response']['groups']}, set(f.targets))
        saved = creative_workbench.confirm(f.agent, f.token, proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'], retained_group_ids=[f.targets[1]])
        self.assertEqual([item['target_id'] for item in saved['response']['image_creation_requests']], [f.targets[1]])
        self.assertEqual(saved['response']['image_creation_requests'][0]['original_shot']['visual'], before['shots'][1]['visual'])
        self.assertEqual(f.business(), before)
        self.assertEqual(f.workspace.read_current(f.token, **f.scope), current)
        withdrawn = creative_workbench.undo(f.new_agent(), f.token, proposal['task']['task_id'])
        self.assertEqual(withdrawn['response']['image_creation_requests'], [])
        self.assertEqual(f.business(), before)
        self.assertEqual(f.workspace.read_current(f.token, **f.scope), current)

    def test_formal_multi_target_image_handoff_uses_formal_shot_identity(self):
        f = fixtures.FormalWorkbenchTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.agent.generation.creative_revision = f.revision
        f.provider.actions = [{'kind': 'create_image', 'target_scope': 'selected', 'fields': [],
            'instruction': '按当前正式稿为所选两镜分别生成参考图', 'depends_on': []}]
        current = f.workspace.read_current(f.token, **f.scope)
        before = f.business()
        started = creative_workbench.start(f.agent, f.token, f.session['agent_session_id'],
            content='按当前正式稿为所选两镜分别生成参考图。',
            context={'stage': 'formal', 'scope': 'selected', 'selected_target_ids': f.targets,
                'current_target_id': f.targets[0], 'expected_project_version': current['project_version'],
                'expected_revision': current['revision']}, operation_key='formal-multi-image-handoff')
        result = creative_workbench.execute(f.agent, f.token, started['task']['task_id'])
        self.assertEqual(result['response']['type'], 'image_creation_requests', result['response'])
        requests = result['response']['image_creation_requests']
        self.assertEqual([item['target_id'] for item in requests], f.targets)
        self.assertTrue(all(item['target_kind'] == 'formal_shot' for item in requests))
        self.assertEqual([item['original_shot']['visual'] for item in requests], [row['visual'] for row in before['shots'][:2]])
        self.assertEqual(f.business(), before)
        self.assertEqual(f.provider.editor_inputs, [])


if __name__ == '__main__':
    unittest.main()
