"""Creative batch orchestration uses saved paragraphs and the existing image authorities."""
from contextlib import contextmanager
from copy import deepcopy
import unittest
from unittest.mock import patch

from mvp_v2 import formal_image_batch
from mvp_v2.creative_images import save_references
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_application_image_references as reference_fixture


class CreativeImageBatchTests(unittest.TestCase):
    def setUp(self):
        self.base = reference_fixture.ApplicationImageReferenceTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base._enable_library()
        self.generation, self.repo = self.base.service, self.base.repo
        self.library, self.image = self.base.library, self.base.image
        self.library.creative_generation_service = self.generation
        self.token, self.scope = self.base.token, self.base.scope
        self.service = FormalRevisionService(projects=self.generation.services.projects,
            tasks=self.generation.services.tasks, workspace=None,
            image_workspace=self.image, writes_enabled=True)
        self.service.default_images = self.library
        self.generation.creative_revision = self.service
        self.calls = []
        self.original_candidates = self.library.candidates
        self.candidate_patch = patch.object(self.library, 'candidates', side_effect=self.candidates)
        self.candidate_patch.start()
        self.addCleanup(self.candidate_patch.stop)

    def candidates(self, *args, **kwargs):
        self.calls.append(kwargs['block_id'])
        result = self.original_candidates(*args, **kwargs)
        # Canonical identity/pixels are real fixtures; only matcher judgement is
        # controlled to exercise suggestion-vs-review orchestration deterministically.
        for row in result['shots']:
            for candidate in row['candidates']:
                candidate.update(default_eligible=False, suggestion_eligible=True,
                    suggestion_kind='primary_reference', requires_human_review=True)
        return result

    def arguments(self, **extra):
        state = self.repo.read()
        return dict(**self.scope, instruction='帮整份稿配一版图',
            expected_project_version=state['project_version'], expected_creative_revision=state['revision'],
            target_kind='creative_block', idempotency_key='creative-image-batch', **extra)

    def propose(self, **extra):
        return self.service.propose_image_batch(self.token, **self.arguments(**extra))

    def confirm(self, proposal, **extra):
        return self.service.confirm_image_batch(self.token, task_id=proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'], **extra)

    def refs(self):
        return self.repo.read()['body'].get('image_references', [])

    def test_failed_semantic_judgement_is_not_reported_as_content_matches(self):
        def failed(*args, **kwargs):
            result = self.original_candidates(*args, **kwargs)
            for row in result['shots']:
                row['semantic_assessment'] = {'status': 'failed', 'message': '图片语义判断未完成，请重新检索。'}
                for candidate in row['candidates']:
                    candidate.update(default_eligible=False, suggestion_eligible=False,
                                     match_mode='visual', missing=[], conflicts=[])
            return result
        self.library.candidates.side_effect = failed
        arguments = self.arguments(selected_block_ids=['block_1'])
        arguments['instruction'] = '给所选段落配图'
        proposal = self.service.propose_image_batch(self.token, **arguments)
        item = proposal['response']['shots'][0]
        self.assertEqual(item['retrieval']['semantic_status'], 'failed')
        self.assertEqual(item['retrieval']['content_matched_references'], 0)
        self.assertEqual(item['reason_code'], 'semantic_failed')
        self.assertIn('未完成', item['reason'])
        self.assertFalse(item['selected_candidate_key'])
        self.assertFalse(self.refs())

    def test_start_persists_before_retrieval_progress_reloads_and_suggestions_stay_unreviewed(self):
        before = self.repo.read()
        args = self.arguments()
        started = formal_image_batch.start(self.service, self.token, **args)
        self.assertEqual(started['task']['status'], 'understanding')
        self.assertEqual(self.calls, [])
        self.assertEqual(started['response']['progress']['total'], 3)
        self.assertEqual(self.repo.read(), before)
        replay = formal_image_batch.start(self.service, self.token, **args)
        self.assertEqual(replay['task']['task_id'], started['task']['task_id'])
        self.assertEqual(replay['response'], started['response'])
        seen = []
        original = self.library.candidates.side_effect
        def observe(*a, **kw):
            current = self.service.read_image_batch(self.token, **self.scope)
            seen.append(deepcopy(current['response']['progress']))
            self.assertEqual(current['task']['status'], 'executing')
            return original(*a, **kw)
        self.library.candidates.side_effect = observe
        proposal = formal_image_batch.execute(self.service, self.token, task_id=started['task']['task_id'])
        response = proposal['response']
        self.assertEqual([item['completed'] for item in seen], [0, 1, 2])
        self.assertEqual(response['progress'], {'completed': 3, 'total': 3, 'current_target_id': ''})
        self.assertEqual(proposal['task']['status'], 'waiting_user')
        self.assertEqual(response['affected_block_ids'], ['block_1', 'block_2', 'block_3'])
        self.assertEqual(response['affected_shot_ids'], [])
        for item in response['shots']:
            self.assertEqual(item['block_id'], item['target_id'])
            self.assertNotIn('shot_id', item)
            self.assertTrue(item['selected_candidate_key'])
            self.assertTrue(all(not c['default_eligible'] and c['requires_human_review'] for c in item['candidates']))
        self.assertEqual(self.repo.read(), before)
        restored = self.service.read_image_batch(self.token, **self.scope)
        self.assertEqual(restored['response']['shots'], response['shots'])
        self.assertFalse(restored['response']['basis_changed'])
        self.assertEqual(formal_image_batch.execute(self.service, self.token, task_id=started['task']['task_id'])['task']['task_id'], proposal['task']['task_id'])
        self.assertEqual(self.calls, ['block_1', 'block_2', 'block_3'])
        saved = self.confirm(proposal)
        self.assertEqual(saved['target_kind'], 'creative_block')
        self.assertEqual(len(self.refs()), 3)
        self.assertFalse(self.service.read_image_batch(self.token, **self.scope)['response']['basis_changed'])
        self.assertEqual(self.confirm(proposal), saved)
        with self.base.fixture.store.connection() as connection:
            for table in ('project_versions', 'version_review_human_decisions'):
                self.assertEqual(connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_fill_keeps_existing_and_protected_blocks_replace_retains_auxiliary_references(self):
        first = self.base._generated('block_1', 'old-primary')
        second = self.base._generated('block_1', 'old-auxiliary')
        self.base._save([{'block_id': 'block_1', 'task_id': first, 'primary': True},
                         {'block_id': 'block_1', 'task_id': second}], operation='initial-reference-pair')
        state = self.repo.read()
        with self.repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            self.repo.body['assistant_protected_ids'] = ['block_2']
        original_refs = deepcopy(self.refs())
        proposal = self.propose()
        by_id = {row['block_id']: row for row in proposal['response']['shots']}
        self.assertEqual(by_id['block_1']['reason_code'], 'existing_selection')
        self.assertEqual(by_id['block_2']['reason_code'], 'locked')
        self.assertEqual(self.calls, ['block_3'])
        self.confirm(proposal)
        self.assertEqual([r for r in self.refs() if r['block_id'] == 'block_1'], original_refs)
        args = self.arguments(mode='replace_selected')
        args.update(instruction='重新推荐所选段落的主图', selected_block_ids=['block_1'], idempotency_key='replace-first-reference')
        replacement = self.service.propose_image_batch(self.token, **args)
        self.confirm(replacement)
        refs = [r for r in self.refs() if r['block_id'] == 'block_1']
        self.assertEqual(len(refs), 3)
        self.assertEqual({r['reference_id'] for r in original_refs} - {r['reference_id'] for r in refs}, set())
        self.assertEqual(sum(r['primary'] for r in refs), 1)
        self.assertEqual(next(r for r in refs if r['primary'])['kind'], 'library')

    def test_changed_deleted_or_protected_basis_cannot_apply_to_surviving_number(self):
        for change in ('visual', 'delete', 'protect'):
            with self.subTest(change=change):
                args = self.arguments()
                args.update(instruction='第2段配图', selected_block_ids=['block_2'], idempotency_key='stale-' + change)
                proposal = self.service.propose_image_batch(self.token, **args)
                state = self.repo.read()
                with self.repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
                    if change == 'visual':
                        self.repo.body['draft']['shots'][1]['fields']['画面描述'] += '，只拍摄入口空镜'
                    elif change == 'delete':
                        self.repo.body['draft']['shots'].pop(1)
                        self.repo.body['draft']['shots'][1]['shot_no'] = '2'
                    else:
                        self.repo.body['assistant_protected_ids'] = ['block_2']
                before = self.repo.read()
                self.assertTrue(self.service.read_image_batch(self.token, **self.scope)['response']['basis_changed'])
                with self.assertRaises(WorkspaceServiceError):
                    self.confirm(proposal)
                self.assertEqual(self.repo.read(), before)
                # Restore only this isolated fixture so each case retains its own original history.
                latest = self.repo.read()
                with self.repo.transaction(expected_project_version=latest['project_version'], expected_revision=latest['revision']):
                    self.repo.body.update(deepcopy(state['body']))

    def test_lost_success_response_replays_after_project_advance_without_second_write(self):
        proposal = self.propose()
        original = self.service.tasks.transition_task
        def lose_response(*args, **kwargs):
            if kwargs.get('status') == 'completed':
                raise ConnectionError('anonymous lost response after saved references')
            return original(*args, **kwargs)
        with patch.object(self.service.tasks, 'transition_task', side_effect=lose_response):
            with self.assertRaises(ConnectionError):
                self.confirm(proposal)
        saved = self.repo.read()
        self.assertEqual(len(saved['body']['image_references']), 3)
        self.service.projects.rename_project(self.token, project_id=self.scope['project_id'], name='匿名后续项目名称',
            expected_project_version=saved['project_version'])
        before_retry = self.repo.read()
        retried = self.confirm(proposal)
        self.assertEqual(retried['progress']['revision'], saved['revision'])
        self.assertEqual(self.repo.read(), before_retry)
        self.assertEqual(self.confirm(proposal), retried)
        alternate = next(c['candidate_key'] for c in proposal['response']['shots'][0]['candidates']
                         if c['candidate_key'] != proposal['response']['shots'][0]['selected_candidate_key'])
        with self.assertRaisesRegex(WorkspaceServiceError, 'IDEMPOTENCY_CONFLICT'):
            self.confirm(proposal, selected_candidate_keys=[alternate])

    def test_reference_save_rechecks_assistant_protection_inside_transaction_but_allows_manual_save(self):
        generated = self.base._generated('block_1', 'protected-reference')
        args = self.base._arguments([{'block_id': 'block_1', 'task_id': generated}], operation='assistant-guard-save')
        real_transaction = self.repo.transaction
        @contextmanager
        def protected_transaction(**kwargs):
            with real_transaction(**kwargs):
                self.repo.body['assistant_protected_ids'] = ['block_1']
                yield self.repo
        with patch('mvp_v2.creative_images.repository', return_value=self.repo), patch.object(self.repo, 'transaction', side_effect=protected_transaction):
            with self.assertRaisesRegex(WorkspaceServiceError, 'ASSISTANT_LOCKED'):
                save_references(self.generation, self.token, **args, assistant_protection=True)
        self.assertEqual(self.refs(), [])
        state = self.repo.read()
        with self.repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            self.repo.body['assistant_protected_ids'] = ['block_1']
        self.base._save([{'block_id': 'block_1', 'task_id': generated}], operation='manual-protected-save')
        self.assertEqual(len(self.refs()), 1)

    def test_revise_and_cancel_only_change_proposal_and_use_block_identity(self):
        proposal = self.propose()
        before = self.repo.read()
        first = proposal['response']['shots'][0]
        alternate = next(c['candidate_key'] for c in first['candidates'] if c['candidate_key'] != first['selected_candidate_key'])
        revised = self.service.revise_image_batch(self.token, task_id=proposal['task']['task_id'],
            proposal_digest=proposal['response']['proposal_digest'], updates=[{'block_id': 'block_1', 'candidate_key': alternate}])
        self.assertEqual(self.repo.read(), before)
        restored = self.service.read_image_batch(self.token, **self.scope)
        self.assertEqual(restored['response']['shots'][0]['selected_candidate_key'], alternate)
        self.service.cancel_image_batch(self.token, task_id=proposal['task']['task_id'],
            proposal_digest=revised['response']['proposal_digest'])
        self.assertEqual(self.repo.read(), before)
        self.assertEqual(self.service.read_image_batch(self.token, **self.scope)['task']['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
