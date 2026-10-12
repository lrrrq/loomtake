"""Persisted workbench proposals over existing, edited creative and Formal drafts.

Only provider responses are controlled. Text, image, task and transaction writes
use their production services; these tests are scoped service regressions.
"""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from mvp_v2 import creative_workbench
from mvp_v2.formal_advisor import FormalAdvisorPolicy, FormalAdvisorService
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.formal_script_assistant import analyze_formal_script_instruction
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_application_image_references as reference_fixture
from tests import test_mvp_v2_image_assistant_batch as formal_fixture
from tests.test_mvp_v2_library_browse import library_confirmation_agent


class _WorkbenchProvider:
    def __init__(self, *, mixed, stage):
        self.mixed, self.stage = mixed, stage
        self.advisor_inputs, self.editor_inputs = [], []
        self.fields = ['visual', 'sound'] if mixed else ['sound']
        self.actions = None
        self.constraints = {'preserve': ['保留未选段落'], 'prohibit': [], 'unknown': []}

    def advisor(self, prompt, _model, _tokens):
        context, message = prompt.split('\nCONTEXT:', 1)[1].rsplit('\nUSER:', 1)
        self.advisor_inputs.append(json.loads(context))
        actions = [{'kind': 'revise_text', 'source_clause': message,
            'target_scope': 'selected', 'fields': self.fields,
            'instruction': '调整所选画面与声音' if self.mixed else '让所选声音自然衔接', 'depends_on': []}]
        if self.mixed:
            actions.append({'kind': 'find_images', 'source_clause': message,
                'target_scope': 'selected', 'fields': [], 'instruction': '按提议画面重新推荐参考图', 'depends_on': [0]})
        if self.actions is not None:
            actions = [{'source_clause': message, 'depends_on': [], **action} for action in self.actions]
        return json.dumps({'intent': 'workbench_revision', 'summary': '调整所选段落，并保留其他内容。',
            'actions': actions, 'constraints': self.constraints}, ensure_ascii=False)

    def editor(self, prompt, _model, _tokens):
        data = json.loads(prompt.split('\nDATA:', 1)[1])
        self.editor_inputs.append(data)
        values = {'visual': '外场入口人群在展板前交流，固定中景记录现场互动。',
                  'sound': '低声音乐自然衔接现场交流与掌声。',
                  'duration_text': '4',
                  'camera_move': '环绕',
                  'shot_size': '特写',
                  'voiceover': '观众在现场自然交流，活动逐渐展开。'}
        return json.dumps({'schema_version': 'mplus-formal-script-assistant-editor/v1',
            'operations': [{'shot_id': target['shot_id'], 'field': target['field'],
                'value': values[target['field']]} for target in data['targets']]}, ensure_ascii=False)

    def install(self, agent):
        agent.advisor = FormalAdvisorService(FormalAdvisorPolicy(
            invoke=self.advisor, provider='anonymous-workbench', model='anonymous-advisor'))
        agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=self.editor, provider='anonymous-workbench', model='anonymous-editor')


class CreativeWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.base = reference_fixture.ApplicationImageReferenceTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        candidates = self.base._enable_library()
        self.generation, self.repo = self.base.service, self.base.repo
        self.library, self.image = self.base.library, self.base.image
        self.fixture = self.base.fixture
        self.scope, self.token = self.base.scope, self.base.token
        self.library.creative_generation_service = self.generation
        adapter = FormalBriefInputAdapter(self.generation.services.briefs, data_root=self.fixture.root / 'runtime')
        self.revision = FormalRevisionService(projects=self.generation.services.projects,
            tasks=self.generation.services.tasks, workspace=None, image_workspace=self.image,
            writes_enabled=True, brief_adapter=adapter)
        self.revision.default_images = self.library
        self.generation.creative_revision = self.revision
        self.generation.brief_input_adapter = adapter
        self.agent = self.new_agent()
        self.provider = _WorkbenchProvider(mixed=True, stage='creative')
        self.provider.install(self.agent)
        self.session = self.agent.services.tasks.create_agent_session(self.token, project_id=self.scope['project_id'])
        # This is an existing edited three-block project, with a primary and an
        # auxiliary reference on an untouched block, not an empty happy path.
        self.base._save([{'block_id': 'block_3', 'library_candidate_key': c['candidate_key'], 'primary': index == 0}
            for index, c in enumerate(candidates[:2])], operation='existing-reference-pair')
        self.manual('block_3', '声音', '已有人工剪辑备注与环境声', 'existing-manual-third')
        self.initial = self.business()
        self.assertEqual(len(self.initial['shots']), 3)
        self.assertEqual(len(self.initial['refs']), 2)
        self.assertTrue(self.repo.read()['body']['patches'])

    def new_agent(self):
        agent = library_confirmation_agent(root=self.fixture.root, store=self.fixture.store,
            projects=self.generation.services.projects, service=self.library)
        agent.script_revision = self.revision
        agent.generation = self.generation
        return agent

    def manual(self, block_id, field, value, operation):
        state = self.repo.read()
        self.revision.apply_creative_draft_change(self.token, **self.scope,
            expected_project_version=state['project_version'], expected_revision=state['revision'],
            expected_draft_revision=state['body']['draft']['revision'], action='patch',
            block_id=block_id, field=field, value=value, operation_key=operation)

    def business(self):
        body = self.repo.read()['body']
        return {'shots': deepcopy(body['draft']['shots']), 'refs': deepcopy(body.get('image_references', []))}

    def database(self):
        with self.fixture.store.connection() as connection:
            return tuple(connection.iterdump())

    def context(self):
        state = self.repo.read()
        return {'stage': 'creative', 'scope': 'selected', 'selected_target_ids': ['block_1', 'block_2'],
            'current_target_id': 'block_1', 'expected_project_version': state['project_version'],
            'expected_revision': state['revision']}

    def propose(self, key='workbench-mixed-preview'):
        before = self.business()
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content='修改第1、2段画面和声音，再给这两段重新推荐配图，保留其他内容。',
            context=self.context(), operation_key=key)
        self.assertEqual(started['task']['status'], 'understanding')
        self.assertEqual(self.business(), before)
        result = creative_workbench.execute(self.agent, self.token, started['task']['task_id'])
        self.assertEqual(result['task']['status'], 'waiting_user', result['response'])
        self.assertEqual(result['response']['type'], 'workbench_proposal')
        self.assertEqual(self.business(), before, 'Preview must leave saved text and references intact')
        self.assertEqual(len(result['response']['groups']), 2)
        self.assertEqual({g['target_id'] for g in result['response']['groups']}, {'block_1', 'block_2'})
        self.assertTrue(all(g['changes'] and g.get('image', {}).get('selected_candidate_key') for g in result['response']['groups']), result['response'])
        return result

    def confirm(self, proposal, groups=('block_1',), digest=None):
        return creative_workbench.confirm(self.agent, self.token, proposal['task']['task_id'],
            proposal_digest=digest or proposal['response']['proposal_digest'], retained_group_ids=list(groups))

    def test_mixed_preview_partial_adoption_fresh_read_and_undo_preserve_later_other_block_edit(self):
        proposal = self.propose()
        self.assertEqual(len(self.provider.advisor_inputs), 1)
        self.assertEqual(len(self.provider.editor_inputs), 1)
        self.assertEqual(self.provider.advisor_inputs[0]['mode'], 'creative_workbench')
        saved = self.confirm(proposal)
        self.assertEqual(saved['response']['type'], 'workbench_applied')
        after = self.business()
        before_by_id = {r['shot_id']: r for r in self.initial['shots']}
        after_by_id = {r['shot_id']: r for r in after['shots']}
        self.assertNotEqual(after_by_id['block_1']['fields']['画面描述'], before_by_id['block_1']['fields']['画面描述'])
        self.assertNotEqual(after_by_id['block_1']['fields']['声音'], before_by_id['block_1']['fields']['声音'])
        self.assertEqual(after_by_id['block_2'], before_by_id['block_2'])
        self.assertEqual(after_by_id['block_3'], before_by_id['block_3'])
        self.assertEqual([r for r in after['refs'] if r['block_id'] != 'block_1'], self.initial['refs'])
        self.assertEqual(sum(r['block_id'] == 'block_1' for r in after['refs']), 1)
        reread = creative_workbench.read(self.new_agent(), self.token, proposal['task']['task_id'])
        self.assertEqual(reread['response'], saved['response'])
        self.assertEqual(reread['task']['status'], 'completed')
        before_replay = self.database()
        self.assertEqual(self.confirm(proposal)['response'], saved['response'])
        self.assertEqual(self.database(), before_replay)
        with self.assertRaisesRegex(WorkspaceServiceError, 'IDEMPOTENCY_CONFLICT'):
            self.confirm(proposal, ['block_2'])
        self.manual('block_2', '声音', '采用建议后用户另写的环境声', 'later-manual-second')
        undone = creative_workbench.undo(self.agent, self.token, proposal['task']['task_id'])
        self.assertTrue(undone['response']['undone'])
        final = self.business()
        final_by_id = {r['shot_id']: r for r in final['shots']}
        self.assertEqual(final_by_id['block_1']['fields'], before_by_id['block_1']['fields'])
        self.assertEqual(final_by_id['block_2']['fields']['声音'], '采用建议后用户另写的环境声')
        self.assertEqual(final_by_id['block_3'], before_by_id['block_3'])
        self.assertEqual(final['refs'], self.initial['refs'])
        stable = self.database()
        creative_workbench.undo(self.new_agent(), self.token, proposal['task']['task_id'])
        self.assertEqual(self.database(), stable)

    def test_stale_protected_and_wrong_digest_reject_entire_batch_without_task_or_business_change(self):
        for cause in ('digest', 'edit', 'protect'):
            with self.subTest(cause=cause):
                proposal = self.propose('workbench-stale-' + cause)
                if cause == 'edit':
                    self.manual('block_1', '声音', '用户在预览后补写声音', 'concurrent-first-sound')
                elif cause == 'protect':
                    state = self.repo.read()
                    self.revision.set_creative_protection(self.token, **self.scope,
                        expected_project_version=state['project_version'], expected_revision=state['revision'],
                        protected_block_ids=['block_1'], operation_key='protect-after-mixed-preview')
                if cause != 'digest':
                    self.assertTrue(creative_workbench.read(self.agent, self.token, proposal['task']['task_id'])['response']['stale'])
                before, database = self.business(), self.database()
                with self.assertRaisesRegex(WorkspaceServiceError, 'PROPOSAL_DIGEST_MISMATCH' if cause == 'digest' else 'DRAFT_REVISION_CONFLICT'):
                    self.confirm(proposal, ['block_1', 'block_2'], digest='0' * 64 if cause == 'digest' else None)
                self.assertEqual(self.business(), before)
                self.assertEqual(self.database(), database)

    def test_real_image_hash_failure_rolls_back_text_references_and_all_task_events(self):
        proposal = self.propose('workbench-image-rollback')
        picture = proposal['response']['groups'][0]['image']
        selected = next(c for c in picture['candidates'] if c['candidate_key'] == picture['selected_candidate_key'])
        project = self.agent.services.projects.get_project(self.token, self.scope['project_id'])
        _, _, plan, _, _ = self.library._context(self.token, self.scope['workspace_id'], self.scope['project_id'], project['project_version'])
        _, media = self.library._package_media(project, plan.brief)
        path = Path(next(row['path'] for row in media if row['media_id'] == selected['media_id']))
        content, mode = path.read_bytes(), path.stat().st_mode
        before, database = self.business(), self.database()
        try:
            path.chmod(mode | 0o200)
            path.write_bytes(content + b'anonymous-fixture-image-drift')
            with self.assertRaises(WorkspaceServiceError):
                self.confirm(proposal, ['block_1', 'block_2'])
        finally:
            path.write_bytes(content)
            path.chmod(mode)
        self.assertEqual(self.business(), before)
        self.assertEqual(self.database(), database)
        self.assertEqual(creative_workbench.read(self.agent, self.token, proposal['task']['task_id'])['task']['status'], 'waiting_user')
        self.confirm(proposal)
        self.assertEqual(sum(r['block_id'] == 'block_1' for r in self.business()['refs']), 1)

    def test_undo_compare_and_swap_preserves_later_edit_on_adopted_field(self):
        proposal = self.propose('workbench-undo-cas')
        self.confirm(proposal)
        self.manual('block_1', '声音', '用户继续修改已采用段落的声音', 'manual-adopted-first')
        before, database = self.business(), self.database()
        with self.assertRaisesRegex(WorkspaceServiceError, 'DRAFT_REVISION_CONFLICT'):
            creative_workbench.undo(self.agent, self.token, proposal['task']['task_id'])
        self.assertEqual(self.business(), before)
        self.assertEqual(self.database(), database)


    def test_creative_combined_duration_sound_partial_is_atomic_and_full_adoption_undo(self):
        self.provider.mixed = False
        self.provider.actions = [
            {'kind': 'revise_text', 'target_scope': 'selected', 'fields': ['sound'], 'instruction': '让所选声音自然衔接'},
            {'kind': 'align_duration', 'target_scope': 'selected', 'fields': ['duration_text'], 'instruction': '对齐总时长60秒'}]
        self.provider.constraints['total_seconds'] = 60
        before = self.business()
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content='调整所选段落的声音，并仅调整所选时长使全片总时长60秒。',
            context=self.context(), operation_key='creative-mixed-duration')
        proposal = creative_workbench.execute(self.agent, self.token, started['task']['task_id'])
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        self.assertEqual(proposal['response']['duration_status']['actual_seconds'], 60)
        self.assertEqual({c['field'] for g in proposal['response']['groups'] for c in g['changes']}, {'声音', '时长'})
        self.assertEqual(self.business(), before)
        database = self.database()
        with self.assertRaisesRegex(WorkspaceServiceError, 'DURATION_CONSTRAINT_MISMATCH'):
            self.confirm(proposal, ['block_1'])
        self.assertEqual(self.database(), database)
        saved = self.confirm(proposal, ['block_1', 'block_2'])
        self.assertEqual(saved['response']['duration_status']['actual_seconds'], 60)
        self.assertEqual(self.business()['shots'][2], before['shots'][2])
        creative_workbench.undo(self.new_agent(), self.token, proposal['task']['task_id'])
        self.assertEqual(self.business(), before)

    def test_image_only_confirm_and_undo_enforce_write_switches_and_later_protection(self):
        self.manual('block_1', '画面描述', '外场入口人群在展板前交流，固定中景记录现场互动。', 'image-only-matching-scene')
        self.provider.actions = [{'kind': 'find_images', 'target_scope': 'selected', 'fields': [], 'instruction': '为所选段落推荐参考图'}]
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content='为所选两段推荐参考图。', context=self.context(), operation_key='image-only-write-protection')
        proposal = creative_workbench.execute(self.agent, self.token, started['task']['task_id'])
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        self.assertTrue(all(not group['changes'] for group in proposal['response']['groups']))
        self.assertTrue(proposal['response']['groups'][0]['image']['included'])
        for owner, field, value in ((self.agent, 'writes_enabled', False), (self.revision, 'writes_enabled', False),
                                    (self.agent, 'script_revision', None)):
            old, database = getattr(owner, field), self.database()
            setattr(owner, field, value)
            try:
                with self.assertRaisesRegex(WorkspaceServiceError, 'FORMAL_SCRIPT_READ_ONLY'):
                    self.confirm(proposal)
            finally:
                setattr(owner, field, old)
            self.assertEqual(self.database(), database)
        self.confirm(proposal)
        self.assertTrue(any(ref['block_id'] == 'block_1' for ref in self.business()['refs']))
        for owner, field, value in ((self.agent, 'writes_enabled', False), (self.revision, 'writes_enabled', False),
                                    (self.agent, 'script_revision', None)):
            old, database = getattr(owner, field), self.database()
            setattr(owner, field, value)
            try:
                with self.assertRaisesRegex(WorkspaceServiceError, 'FORMAL_SCRIPT_READ_ONLY'):
                    creative_workbench.undo(self.agent, self.token, proposal['task']['task_id'])
            finally:
                setattr(owner, field, old)
            self.assertEqual(self.database(), database)
        state = self.repo.read()
        self.revision.set_creative_protection(self.token, **self.scope,
            expected_project_version=state['project_version'], expected_revision=state['revision'],
            protected_block_ids=['block_1'], operation_key='protect-image-only-adoption')
        before, database = self.business(), self.database()
        with self.assertRaisesRegex(WorkspaceServiceError, 'DRAFT_REVISION_CONFLICT'):
            creative_workbench.undo(self.agent, self.token, proposal['task']['task_id'])
        self.assertEqual(self.business(), before)
        self.assertEqual(self.database(), database)

    def test_selected_two_blocks_is_authoritative_and_not_reparsed_as_block_two(self):
        before = self.business()
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content='修改选中两段画面与声音，并按新画面推荐配图。',
            context=self.context(), operation_key='selected-two-not-ordinal-two')
        proposal = creative_workbench.execute(self.agent, self.token, started['task']['task_id'])
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        self.assertEqual({target['shot_id'] for target in self.provider.editor_inputs[-1]['targets']}, {'block_1', 'block_2'})
        self.assertEqual({group['target_id'] for group in proposal['response']['groups'] if group['changes']}, {'block_1', 'block_2'})
        self.assertEqual(self.business(), before)
        self.confirm(proposal, ['block_1', 'block_2'])
        after = self.business()
        self.assertNotEqual(after['shots'][0]['fields'], before['shots'][0]['fields'])
        self.assertNotEqual(after['shots'][1]['fields'], before['shots'][1]['fields'])
        self.assertEqual(after['shots'][2], before['shots'][2])

    def test_selecting_an_image_gap_does_not_report_adoption_or_enable_undo(self):
        self.provider.actions = [{'kind': 'find_images', 'target_scope': 'selected', 'fields': [], 'instruction': '为所选段落推荐参考图'}]
        before = self.business()
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content='为所选两段推荐参考图。', context=self.context(), operation_key='image-gap-does-not-apply')
        proposal = creative_workbench.execute(self.agent, self.token, started['task']['task_id'])
        self.assertTrue(all(not group['changes'] and not group['image']['included'] for group in proposal['response']['groups']))
        saved = self.confirm(proposal, ['block_1'])
        self.assertEqual(saved['task']['status'], 'cancelled')
        self.assertFalse(saved['response']['can_undo'])
        self.assertEqual(saved['response']['summary'], '未采用本次建议，当前稿已保留。')
        self.assertEqual(self.business(), before)
        database = self.database()
        self.assertEqual(self.confirm(proposal, ['block_1'])['response'], saved['response'])
        self.assertEqual(self.database(), database)


class FormalWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.base = formal_fixture.ImageAssistantBatchTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.f, self.revision = self.base.f, self.base.revision
        self.token, self.scope = self.f.token, self.base.base.scope
        self.workspace, self.images = self.f.base.workspace, self.f.workspace
        self.agent = self.new_agent()
        self.provider = _WorkbenchProvider(mixed=False, stage='formal')
        self.provider.install(self.agent)
        self.session = self.agent.services.tasks.create_agent_session(self.token, project_id=self.scope['project_id'])
        self.original = self.workspace.read_current(self.token, **self.scope)
        self.targets = [row['shot_id'] for row in self.original['shots'][:2]]
        # Preserve real image progress and earlier manual work on another shot.
        self.base.base.patch([self.base.base.selection(2)], key='existing-third-image')
        self.manual(self.original['shots'][2], '已有人工声音设计', 'existing-formal-third')
        self.before = self.business()

    def new_agent(self):
        agent = library_confirmation_agent(root=self.f.base.generated.root, store=self.f.store,
            projects=self.f.projects, service=None)
        agent.script_revision = self.revision
        agent.generation = self.f.base.generation
        return agent

    def business(self):
        current = self.workspace.read_current(self.token, **self.scope)
        return {'shots': deepcopy(current['shots']), 'images': self.images.read_progress(self.token, **self.scope)}

    def database(self):
        with self.f.store.connection() as connection:
            return tuple(connection.iterdump())

    def manual(self, row, value, operation):
        current = self.workspace.read_current(self.token, **self.scope)
        self.workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_revision=current['revision'],
            shot_no=row['shot_no'], shot_id=row['shot_id'], field='sound', value=value, idempotency_key=operation)

    def propose(self, key='formal-workbench-preview'):
        current = self.workspace.read_current(self.token, **self.scope)
        progress = self.images.read_progress(self.token, **self.scope)
        original = self.business()
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content='修改第1、2镜声音，让现场交流声自然衔接音乐，其他内容保持。',
            context={'stage': 'formal', 'scope': 'selected', 'selected_target_ids': self.targets,
                'current_target_id': self.targets[0], 'expected_project_version': current['project_version'],
                'expected_revision': current['revision'], 'expected_progress_revision': progress['revision']}, operation_key=key)
        proposal = creative_workbench.execute(self.agent, self.token, started['task']['task_id'])
        self.assertEqual(proposal['task']['status'], 'waiting_user', proposal['response'])
        self.assertEqual(self.business(), original)
        self.assertEqual({g['target_id'] for g in proposal['response']['groups']}, set(self.targets))
        return proposal

    def confirm(self, proposal):
        return creative_workbench.confirm(self.agent, self.token, proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'], retained_group_ids=[self.targets[0]])

    def test_formal_partial_text_group_fresh_read_and_undo_keep_existing_images_and_other_shot_edit(self):
        proposal = self.propose()
        saved = self.confirm(proposal)
        after = self.business()
        before_by_id = {r['shot_id']: r for r in self.before['shots']}
        after_by_id = {r['shot_id']: r for r in after['shots']}
        self.assertNotEqual(after_by_id[self.targets[0]]['sound'], before_by_id[self.targets[0]]['sound'])
        self.assertEqual(after_by_id[self.targets[1]], before_by_id[self.targets[1]])
        self.assertEqual(after['images']['selections'], self.before['images']['selections'])
        self.assertEqual(creative_workbench.read(self.new_agent(), self.token, proposal['task']['task_id'])['response'], saved['response'])
        stable = self.database()
        self.assertEqual(self.confirm(proposal)['response'], saved['response'])
        self.assertEqual(self.database(), stable)
        self.manual(after_by_id[self.targets[1]], '用户后来修改另一镜声音', 'later-formal-second')
        undone = creative_workbench.undo(self.agent, self.token, proposal['task']['task_id'])
        self.assertTrue(undone['response']['undone'])
        final = self.business()
        by_id = {r['shot_id']: r for r in final['shots']}
        self.assertEqual(by_id[self.targets[0]]['sound'], before_by_id[self.targets[0]]['sound'])
        self.assertEqual(by_id[self.targets[1]]['sound'], '用户后来修改另一镜声音')
        self.assertEqual(final['images']['selections'], self.before['images']['selections'])
        self.assertEqual(by_id[self.original['shots'][2]['shot_id']]['sound'], '已有人工声音设计')

    def test_formal_later_protection_rejects_group_atomically(self):
        proposal = self.propose('formal-workbench-protection')
        current = self.workspace.read_current(self.token, **self.scope)
        self.workspace.set_assistant_locks(self.token, **self.scope,
            expected_project_version=current['project_version'], locked_shot_ids=[self.targets[0]],
            idempotency_key='protect-formal-after-preview')
        before, database = self.business(), self.database()
        with self.assertRaisesRegex(WorkspaceServiceError, 'DRAFT_REVISION_CONFLICT'):
            self.confirm(proposal)
        self.assertEqual(self.business(), before)
        self.assertEqual(self.database(), database)

    def duration_proposal(self, key, *, target=60, combined=False, selected=None, scope='selected'):
        self.provider.actions = ([{'kind': 'revise_text', 'target_scope': scope,
            'fields': ['sound'], 'instruction': '让所选声音自然衔接音乐'}] if combined else []) + [
            {'kind': 'align_duration', 'target_scope': scope, 'fields': ['duration_text'], 'instruction': '对齐已确认全片总时长'}]
        self.provider.constraints['total_seconds'] = target
        current = self.workspace.read_current(self.token, **self.scope)
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content=('调整所选镜头的声音与时长，保留其他镜头，全片总时长60秒。' if combined else
                f'只调整所选时长，对齐全片总时长{target}秒。' if target is not None else '后半段更紧凑，但保留已确认的全片总时长。'),
            context={'stage': 'formal', 'scope': 'all' if scope == 'second_half' else 'selected',
                'selected_target_ids': selected or self.targets, 'current_target_id': self.targets[0],
                'expected_project_version': current['project_version'], 'expected_revision': current['revision']},
            operation_key=key)
        return creative_workbench.execute(self.agent, self.token, started['task']['task_id'])

    def test_formal_duration_sound_uses_one_proposal_preserves_protection_and_partial_constraint(self):
        # Existing durations have drifted in two selected shots; the third is
        # protected and all other shots must remain byte-for-byte equivalent.
        for row in self.original['shots'][:2]:
            current = self.workspace.read_current(self.token, **self.scope)
            self.workspace.apply_table_change(self.token, **self.scope,
                expected_project_version=current['project_version'], expected_revision=current['revision'],
                shot_no=row['shot_no'], shot_id=row['shot_id'], field='duration_seconds',
                value=row['duration_seconds'] + 4, idempotency_key='drift-duration-' + row['shot_no'])
        current = self.workspace.read_current(self.token, **self.scope)
        protected = self.original['shots'][2]['shot_id']
        self.workspace.set_assistant_locks(self.token, **self.scope, expected_project_version=current['project_version'],
            locked_shot_ids=[protected], idempotency_key='duration-protected-third')
        before = self.business()
        proposal = self.duration_proposal('formal-combined-duration', combined=True, selected=self.targets + [protected])
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        groups = proposal['response']['groups']
        self.assertEqual({group['target_id'] for group in groups}, set(self.targets))
        self.assertTrue(all(group['depends_on'] for group in groups))
        self.assertEqual({c['field'] for group in groups for c in group['changes']}, {'sound', 'duration_seconds'})
        self.assertEqual(len(self.provider.editor_inputs), 1)
        self.assertEqual(self.business(), before)
        database = self.database()
        with self.assertRaisesRegex(WorkspaceServiceError, 'DURATION_CONSTRAINT_MISMATCH'):
            self.confirm(proposal)
        self.assertEqual(self.database(), database)
        result = creative_workbench.confirm(self.agent, self.token, proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'], retained_group_ids=self.targets)
        self.assertEqual(result['response']['duration_status']['actual_seconds'], 60)
        after = self.business()
        self.assertEqual(after['shots'][2:], before['shots'][2:])
        self.assertEqual(after['images']['selections'], before['images']['selections'])
        creative_workbench.undo(self.new_agent(), self.token, proposal['task']['task_id'])
        self.assertEqual(self.business()['shots'], before['shots'])

    def test_formal_duration_unknown_conflict_infeasible_and_already_matched(self):
        before = self.business()
        mismatch = self.duration_proposal('formal-duration-conflicting-brief', target=90)
        self.assertEqual(mismatch['response']['type'], 'needs_clarification', mismatch['response'])
        self.assertIn('/brief', mismatch['response']['next_href'])
        matched = self.duration_proposal('formal-duration-matched', target=None, scope='second_half')
        self.assertEqual(matched['response']['type'], 'no_changes', matched['response'])
        self.assertEqual(self.provider.editor_inputs, [])
        self.assertEqual(self.business(), before)
        # The helper uses the same frozen evidence as production; missing a
        # total cannot silently use the current arithmetic as a new requirement.
        project = self.agent.services.projects.get_project(self.token, self.scope['project_id'])
        snapshot = creative_workbench.snapshot(self.agent, self.token, project, 'formal')
        snapshot['evidence']['brief'].pop('视频时长', None)
        snapshot['evidence']['brief'].pop('目标时长', None)
        target, notice = creative_workbench._duration_target(snapshot, {'constraints': {}}, project)
        self.assertIsNone(target)
        self.assertEqual(notice['type'], 'needs_clarification')
        last = before['shots'][-1]
        current = self.workspace.read_current(self.token, **self.scope)
        self.workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_revision=current['revision'],
            shot_no=last['shot_no'], shot_id=last['shot_id'], field='duration_seconds', value=60,
            idempotency_key='duration-fixed-total-infeasible')
        drifted = self.business()
        impossible = self.duration_proposal('formal-duration-infeasible')
        self.assertEqual(impossible['response']['type'], 'needs_clarification', impossible['response'])
        self.assertEqual(self.business(), drifted)

    def test_formal_technical_undo_reuses_canonical_source_preflight(self):
        self.provider.actions = [{'kind': 'revise_text', 'target_scope': 'selected', 'fields': ['camera', 'visual'],
            'instruction': '将所选镜头的景别改为特写，并改写画面描述'}]
        current = self.workspace.read_current(self.token, **self.scope)
        original = deepcopy(next(row for row in current['shots'][1:] if row['shot_size'] not in {'特写', '大特写'}))
        index = current['shots'].index(original)
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content='将所选镜头的景别改为特写，并改写画面描述，保留其他字段。',
            context={'stage': 'formal', 'scope': 'selected', 'selected_target_ids': [original['shot_id']],
                'current_target_id': original['shot_id'], 'expected_project_version': current['project_version'],
                'expected_revision': current['revision']}, operation_key='technical-undo-source')
        proposal = creative_workbench.execute(self.agent, self.token, started['task']['task_id'])
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        _, data, _ = creative_workbench._load(self.agent, self.token, proposal['task']['task_id'])
        self.assertTrue(any(op.get('operation') == 'source_rebind' for op in data['text_subject']['operations']))
        creative_workbench.confirm(self.agent, self.token, proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'], retained_group_ids=[original['shot_id']])
        current = self.workspace.read_current(self.token, **self.scope)
        self.assertEqual(current['shots'][index]['shot_size'], '特写')
        expected = self.workspace.preflight_source_rematches(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_revision=current['revision'],
            operations=[{'operation': 'replace_field', 'shot_no': original['shot_no'], 'shot_id': original['shot_id'],
                'field': field, 'value': original[field]} for field in ('shot_size', 'visual')], allow_original_adaptation=True)
        self.assertTrue(expected['ready'])
        self.assertTrue(any(op.get('operation') == 'source_rebind' for op in expected['operations']))
        creative_workbench.undo(self.new_agent(), self.token, proposal['task']['task_id'])
        restored = self.workspace.read_current(self.token, **self.scope)['shots'][index]
        self.assertEqual(restored['shot_size'], original['shot_size'])
        self.assertEqual(restored['visual'], original['visual'])
        rebound = next(op for op in expected['operations'] if op.get('operation') == 'source_rebind')
        source = rebound['value']['source_refs'][0]
        self.assertEqual({key: restored['source'][key] for key in ('file', 'sheet', 'row')},
            {'file': source['source_file'], 'sheet': source['source_sheet'], 'row': source['source_row']})

    def test_formal_movement_only_scope_valid_values_and_source_rebind_undo(self):
        current = self.workspace.read_current(self.token, **self.scope)
        original = deepcopy(current['shots'][0])
        context = self.workspace.assistant_context(self.token, **self.scope)
        for field, value, accepted in [('camera_move', '环绕', True), ('camera_move', '非法技术值', False),
                                      ('visual', '模型擅自改写画面', False)]:
            with self.subTest(field=field, value=value):
                seen = []
                def editor(prompt, _model, _tokens):
                    data = json.loads(prompt.split('\nDATA:', 1)[1])
                    seen.extend(data['targets'])
                    return json.dumps({'schema_version': 'mplus-formal-script-assistant-editor/v1',
                        'operations': [{'shot_id': original['shot_id'], 'field': field, 'value': value}]}, ensure_ascii=False)
                analysis = analyze_formal_script_instruction(instruction='只将所选镜头的运镜改为环绕，保留画面和声音不变。',
                    shots=current['shots'], assistant_locked_shot_ids=[], selected_shot_ids=[original['shot_id']],
                    allowed_fields_by_shot={original['shot_id']: ['camera_type', 'shot_size', 'camera_move']},
                    editor_policy=FormalScriptAssistantEditorPolicy(invoke=editor, provider='anonymous', model='anonymous'),
                    context=context)
                self.assertEqual({item['field'] for item in seen}, {'camera_move'})
                self.assertIn('环绕', seen[0]['allowed_values'])
                self.assertEqual(analysis['type'], 'rewrite_operations' if accepted else 'needs_clarification', analysis)
        self.assertEqual(self.workspace.read_current(self.token, **self.scope), current)
        self.provider.actions = [{'kind': 'revise_text', 'target_scope': 'selected', 'fields': ['camera'],
            'instruction': '只将所选镜头的运镜改为环绕，保留画面和声音不变'}]
        started = creative_workbench.start(self.agent, self.token, self.session['agent_session_id'],
            content='只将所选镜头的运镜改为环绕，保留画面和声音不变。',
            context={'stage': 'formal', 'scope': 'selected', 'selected_target_ids': [original['shot_id']],
                'current_target_id': original['shot_id'], 'expected_project_version': current['project_version'],
                'expected_revision': current['revision']}, operation_key='movement-only-source-undo')
        proposal = creative_workbench.execute(self.agent, self.token, started['task']['task_id'])
        self.assertEqual(proposal['response']['type'], 'workbench_proposal', proposal['response'])
        self.assertEqual({change['field'] for group in proposal['response']['groups'] for change in group['changes']}, {'camera_move'})
        _, data, _ = creative_workbench._load(self.agent, self.token, proposal['task']['task_id'])
        self.assertTrue(any(op.get('operation') == 'source_rebind' for op in data['text_subject']['operations']))
        self.confirm(proposal)
        saved = self.workspace.read_current(self.token, **self.scope)['shots'][0]
        self.assertEqual(saved['camera_move'], '环绕')
        self.assertEqual((saved['visual'], saved['sound']), (original['visual'], original['sound']))
        creative_workbench.undo(self.new_agent(), self.token, proposal['task']['task_id'])
        restored = self.workspace.read_current(self.token, **self.scope)['shots'][0]
        self.assertEqual(restored['camera_move'], original['camera_move'])
        self.assertEqual((restored['visual'], restored['sound']), (original['visual'], original['sound']))


if __name__ == '__main__':
    unittest.main()
