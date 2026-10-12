"""Library adoption uses the canonical working draft and original task receipts."""
import unittest
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_image_workspace as image_fixture
from tests.test_mvp_v2_library_browse import build_anonymous_library_package


class DefaultImageWorkingDraftTests(unittest.TestCase):
    def setUp(self):
        self.f = image_fixture.FormalImageWorkspaceTests('runTest')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        f = self.f
        self.token = f.token
        self.scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        self.current = f.workspace.read_current(self.token, **self.scope)
        self.script = f.base.workspace.read_current(self.token, **self.scope)
        package = build_anonymous_library_package(f.base.generated.root / 'working-adoption-library')
        self.library = image_fixture.FormalDefaultImageService(
            projects=f.projects, tasks=image_fixture.TaskService(f.store), delivery=f.delivery,
            artifacts=f.resolver, image_pool=f.workspace.services.images, image_workspace=f.workspace,
            asset_package_resolver=lambda _: package, writes_enabled=True)
        self.project = f.projects.get_project(self.token, f.project_id)

    def edit(self, revision=0):
        return self.f.base.workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=self.current['project_version'], expected_revision=revision,
            shot_no='3', shot_id=self.script['shots'][2]['shot_id'], field='visual',
            value=f'外场入口人群在展板前交流，画面调整 {revision}', idempotency_key=f'edit-{revision}')

    def propose(self, revision, key='working-proposal', shot_no='3', shot_id=None):
        choices = self.library.candidates(self.token, **self.scope,
            expected_project_version=self.current['project_version'], shot_no=shot_no)
        candidate = choices['shots'][0]['candidates'][0]
        self.request = dict(**self.scope, agent_session_id='', expected_project_version=self.current['project_version'],
            expected_script_revision=revision, allowed_shot_ids=[shot_id or self.script['shots'][2]['shot_id']],
            selections=[dict(shot_no=shot_no, candidate_key=candidate['candidate_key'])], idempotency_key=key)
        proposal = self.library.propose(self.token, **self.request)
        self.parameters = next(event['details']['internal_formal_parameters'] for event in self.library.tasks.list_task_events(self.token, task_id=proposal['task_id']) if 'internal_formal_parameters' in event['details'])
        return proposal

    def test_dirty_working_draft_adopts_then_saves_only_progress_with_replay(self):
        revised = self.edit()
        before_versions = self.f.projects.list_project_versions(self.token, self.f.project_id)
        proposal = self.propose(revised['revision'])
        replay = self.library.propose(self.token, **self.request)
        self.assertEqual(replay, proposal)
        adopted = self.library.execute(self.token, project=self.project, parameters=self.parameters)
        self.assertEqual(adopted['adopted_selections'][0]['shot_id'], self.script['shots'][2]['shot_id'])
        self.assertEqual(self.f.workspace.read_progress(self.token, **self.scope)['selections'], [])
        saved = self.f.workspace.save_progress(self.token, **self.scope, base_version_id=self.current['base_version_id'],
            expected_project_version=self.current['project_version'], expected_revision=0,
            expected_script_revision=revised['revision'], selections=[dict(shot_id=item['shot_id'], image_key=item['image_key'], checked=False, selection_active=True) for item in adopted['adopted_selections']], idempotency_key='adopt-progress')
        self.assertEqual(len(saved['selections']), 1)
        self.assertFalse(saved['selections'][0]['checked'])
        self.assertEqual(self.f.projects.list_project_versions(self.token, self.f.project_id), before_versions)
        self.assertTrue(self.f.base.workspace.read_current(self.token, **self.scope)['can_save_version'])

    def test_new_draft_revision_replays_original_proposal_but_rejects_execution(self):
        revised = self.edit(); proposal = self.propose(revised['revision'])
        self.edit(revised['revision'])
        self.assertEqual(self.library.propose(self.token, **self.request), proposal)
        with self.assertRaisesRegex(WorkspaceServiceError, 'DRAFT_REVISION_CONFLICT'):
            self.library.execute(self.token, project=self.project, parameters=self.parameters)
        with self.assertRaisesRegex(WorkspaceServiceError, 'IDEMPOTENCY_CONFLICT'):
            self.library.propose(self.token, **{**self.request, 'allowed_shot_ids':[self.script['shots'][1]['shot_id']]})

    def test_old_mode_still_rejects_unsaved_draft(self):
        revised = self.edit(); self.propose(revised['revision'])
        request = {k:v for k,v in self.request.items() if k != 'expected_script_revision'}
        request['idempotency_key'] = 'old-mode'
        with self.assertRaises(WorkspaceServiceError) as raised:
            self.library.propose(self.token, **request)
        self.assertIn('DRAFT', str(raised.exception))

    def test_deleted_shot_renumbering_uses_final_stable_identity(self):
        revised = self.f.base.workspace.apply_revision_operation(self.token, **self.scope,
            expected_project_version=self.current['project_version'], expected_revision=0,
            operation={'operation':'delete_shot', 'shot_no':'1', 'shot_id':self.script['shots'][0]['shot_id']}, idempotency_key='delete-first')
        target = revised['shots'][1]
        self.assertEqual(target['shot_id'], self.script['shots'][2]['shot_id'])
        self.propose(revised['revision'], shot_no=str(target['shot_no']), shot_id=target['shot_id'])
        adopted = self.library.execute(self.token, project=self.project, parameters=self.parameters)
        self.assertEqual(adopted['adopted_selections'][0]['shot_id'], target['shot_id'])
        self.assertEqual(adopted['adopted_selections'][0]['shot_no'], str(target['shot_no']))

    def test_bulk_library_confirmation_recovers_after_pool_adoption_receipt_loss(self):
        from tests.test_mvp_v2_library_browse import library_confirmation_agent
        revised = self.edit()
        candidate = self.library.candidates(self.token, **self.scope,
            expected_project_version=self.current['project_version'], shot_no='3')['shots'][0]['candidates'][0]
        selected = [{'shot_id': self.script['shots'][index]['shot_id'], 'library_candidate_key': candidate['candidate_key'],
                     'checked': False, 'selection_active': True, 'reuse_reason': '两镜从不同叙事角度引用同一现场'} for index in (1, 2)]
        staged = self.f.workspace.save_progress(self.token, **self.scope, base_version_id=self.current['base_version_id'],
            expected_project_version=self.current['project_version'], expected_revision=0,
            expected_script_revision=revised['revision'], selections=selected, idempotency_key='stage-library', library_service=self.library)
        scope = dict(**self.scope, base_version_id=self.current['base_version_id'], expected_project_version=self.current['project_version'],
                     expected_revision=staged['revision'], expected_script_revision=revised['revision'], selections=selected, library_service=self.library)
        preview = self.f.workspace.preview_progress_confirmation(self.token, **scope)
        self.assertEqual(preview['processable_count'], 2)
        agent = library_confirmation_agent(root=self.f.base.generated.root, store=self.f.store, projects=self.f.projects, service=self.library)
        lost = [True]
        if getattr(self, 'lose_inside_execute', False):
            original_execute = self.library.execute
            def interrupted_execute(*args, **kwargs):
                result = original_execute(*args, **kwargs)
                if lost[0]:
                    lost[0] = False
                    raise SystemExit('RECEIPT_LOST after exact task receipt')
                return result
            self.library.execute = interrupted_execute
        def confirm(*args, **kwargs):
            result = agent.confirm_action(*args, **kwargs)
            if lost[0]:
                lost[0] = False
                self.assertEqual(result['response']['status'], 'completed', result)
                raise WorkspaceServiceError('RECEIPT_LOST', '采用已完成但回执未送达')
            return result
        request = dict(**scope, preview_digest=preview['preview_digest'], accepted_shot_ids=[item['shot_id'] for item in selected],
                       idempotency_key='bulk-library-confirm', confirm_action=confirm)
        before_versions = self.f.projects.list_project_versions(self.token, self.f.project_id)
        interruption = SystemExit if getattr(self, 'lose_inside_execute', False) else WorkspaceServiceError
        with self.assertRaisesRegex(interruption, 'RECEIPT_LOST'):
            self.f.workspace.confirm_progress(self.token, **request)
        result = self.f.workspace.confirm_progress(self.token, **request)
        self.assertEqual(result['confirmed_count'], 2)
        saved = self.f.workspace.read_progress(self.token, **self.scope)
        self.assertEqual(len(saved['selections']), 2)
        self.assertTrue(all(item['checked'] and item['image_key'] and not item.get('library_candidate_key') for item in saved['selections']))
        replay = self.f.workspace.confirm_progress(self.token, **request)
        self.assertEqual(replay, result)
        with self.f.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM tasks WHERE task_type='formal_default_image_adoption'").fetchone()[0], 1)
        self.assertEqual(self.f.projects.list_project_versions(self.token, self.f.project_id), before_versions)

    def test_bulk_library_confirmation_recovers_task_receipt_before_execution_returns(self):
        self.lose_inside_execute = True
        self.test_bulk_library_confirmation_recovers_after_pool_adoption_receipt_loss()

    def test_sixteen_library_references_share_one_verified_context_and_catalog(self):
        from unittest.mock import patch
        _project, _context, plan, _draft, _binding = self.library._context(self.token, **dict(
            workspace_id=self.scope['workspace_id'], project_id=self.scope['project_id'], expected_project_version=self.current['project_version']))
        _, media = self.library._package_media(_project, plan.brief, (getattr(plan, 'constraints', {}) or {}).get('formal_asset_package'))
        keys = [item['media_id'] for item in media[:16]]
        self.assertEqual(len(keys), 16)
        args = dict(**self.scope, expected_project_version=self.current['project_version'])
        single = self.library.progress_reference(self.token, **args, candidate_key=keys[0])
        with patch.object(self.library, '_context', wraps=self.library._context) as context, patch.object(self.library, '_package_media', wraps=self.library._package_media) as catalog:
            result = self.library.progress_references(self.token, **args, candidate_keys=[*keys, keys[0], 'missing-candidate'])
            self.assertEqual(context.call_count, 1)
            self.assertEqual(catalog.call_count, 1)
        self.assertEqual(set(result), set(keys))
        self.assertEqual(result[keys[0]], single)
        with self.assertRaisesRegex(WorkspaceServiceError, 'IMAGE_CANDIDATE_INVALID'):
            self.library.progress_reference(self.token, **args, candidate_key='missing-candidate')
