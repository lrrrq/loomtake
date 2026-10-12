import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mvp_v2.formal_image_workspace import FormalImageWorkspaceService, FormalImageWorkspaceServices
from mvp_v2.version_delivery import VersionDeliveryService
from tests import test_mvp_v2_creative_formal_binding as binding_fixture
from tests.test_mvp_v2_image_creation import photo
from mvp_v2.image_providers import ImageProviderResult
from mvp_v2.workspace_store import WorkspaceServiceError


class CreativeImageBindingTests(unittest.TestCase):
    def setUp(self):
        self.base = binding_fixture.CreativeFormalBindingTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.service = self.base.service
        self.scope, self.token = self.base.scope, self.base.fixture.token
        self.image = FormalImageWorkspaceService(FormalImageWorkspaceServices(
            store=self.base.fixture.store, projects=self.service.services.projects,
            delivery=VersionDeliveryService(self.base.fixture.store, self.service.services.artifacts),
            artifacts=self.service.services.artifacts, images=self.service.services.images),
            runs_root=self.base.fixture.root / 'runs', source_root=self.base.fixture.source_root)
        self.service.creative_revision = SimpleNamespace(image_workspace=self.image)
        self.calls = 0
        def provider(prompt, **kwargs):
            self.calls += 1
            content = photo('orange' if self.calls == 1 else 'blue')
            return ImageProviderResult(content, 'image/png', 2560, 1440, 'anonymous-image', None,
                                       '2560x1440', 'anonymous-request', {})
        self.service.image_provider = provider

    def _prepared_reference(self, block_id, key):
        state = self.base.repo.read()
        started = self.service.start_image_creation(self.token, **self.scope,
            expected_project_version=state['project_version'], expected_creative_revision=state['revision'],
            target_kind='creative_block', target_id=block_id, prompt='匿名活动参考图',
            operation_key='image-start-' + key, start_background=False)
        self.service.execute_image_creation(self.token, **self.scope, task_id=started['task_id'])
        return dict(**self.scope, task_id=started['task_id'],
            expected_project_version=state['project_version'], expected_creative_revision=state['revision'],
            target_kind='creative_block', target_id=block_id, operation_key='image-adopt-' + key)

    def reference(self, block_id, key):
        args = self._prepared_reference(block_id, key)
        self.service.adopt_image_creation(self.token, **args)
        return args['task_id']

    def generate(self, plan):
        return self.service.generate_from_confirmed_plan(self.token, **self.scope,
            task_id=plan['plan_id'], plan_ref=plan['plan_ref'], plan_sha256=plan['plan_sha256'],
            expected_project_version=self.base.repo.read()['project_version'])

    def test_creative_adoption_registers_pool_before_formal_generation_and_replay_is_stable(self):
        args = self._prepared_reference('block_1', 'immediate-pool')
        saved = self.service.adopt_image_creation(self.token, **args)
        current = self.base.repo.read()
        readiness = self.service.get_image_readiness(self.token, **self.scope,
            expected_project_version=current['project_version'])
        self.assertEqual((readiness['total'], readiness['pending']), (1, 1))
        self.assertEqual(readiness['unmaterialized_source_count'], 0)
        self.assertEqual(readiness['images'][0]['image_key'], saved['image_key'])
        self.assertEqual(len(current['body']['image_references']), 1)
        self.assertEqual(self.service.adopt_image_creation(self.token, **args), saved)
        self.assertEqual(self.base.repo.read(), current)
        with self.base.fixture.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM project_images').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM project_image_generated_sources').fetchone()[0], 1)
            for table in ('project_versions', 'project_image_analysis_versions', 'version_review_human_decisions'):
                self.assertEqual(con.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.calls, 1)

    def test_legacy_references_are_read_only_until_explicit_source_sync_then_do_not_duplicate(self):
        # Model old reference-only adoption; existing generated artifacts and receipts stay intact.
        with patch.object(self.service.services.images, 'current_confirmed_brief_binding',
                side_effect=WorkspaceServiceError('CONFIRMED_BRIEF_REQUIRED', '匿名未确认资料')):
            self.reference('block_1', 'legacy-first')
            self.reference('block_3', 'legacy-third')
        current = self.base.repo.read()
        before = self.service.get_image_readiness(self.token, **self.scope,
            expected_project_version=current['project_version'])
        self.assertEqual(before['total'], 0)
        self.assertEqual(before['unmaterialized_creative_reference_count'], 2)
        self.assertEqual(before['unmaterialized_source_count'], 2)
        with self.base.fixture.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM project_images').fetchone()[0], 0)
        synced = self.service.synchronize_image_sources(self.token, **self.scope,
            expected_project_version=current['project_version'])
        self.assertEqual((synced['total'], synced['pending']), (2, 2))
        self.assertEqual(synced['unmaterialized_creative_reference_count'], 0)
        self.assertEqual(synced['unmaterialized_source_count'], 0)
        self.assertEqual(self.service.synchronize_image_sources(self.token, **self.scope,
            expected_project_version=current['project_version']), synced)
        self.assertEqual(self.base.repo.read(), current)
        with self.base.fixture.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM project_images').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT count(*) FROM project_image_generated_sources').fetchone()[0], 2)
        self.assertEqual(self.calls, 2)

    def test_creative_adoption_does_not_swallow_non_confirmation_binding_failure(self):
        args = self._prepared_reference('block_1', 'binding-failure')
        before = self.base.repo.read()
        with patch.object(self.service.services.images, 'current_confirmed_brief_binding',
                side_effect=WorkspaceServiceError('PROJECT_VERSION_CONFLICT', '匿名并发修改')):
            with self.assertRaises(WorkspaceServiceError) as caught:
                self.service.adopt_image_creation(self.token, **args)
        self.assertEqual(caught.exception.code, 'PROJECT_VERSION_CONFLICT')
        self.assertEqual(self.base.repo.read(), before)
        with self.base.fixture.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM project_images').fetchone()[0], 0)
        self.assertEqual(self.calls, 1)

    def test_pool_registration_survives_reference_save_failure_and_retry_reuses_same_image(self):
        args = self._prepared_reference('block_1', 'reference-save-retry')
        before = self.base.repo.read()
        with patch('mvp_v2.creative_generation.CreativeDraftRepository.transaction',
                side_effect=WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '匿名保存失败')):
            with self.assertRaises(WorkspaceServiceError) as caught:
                self.service.adopt_image_creation(self.token, **args)
        self.assertEqual(caught.exception.code, 'DRAFT_REVISION_CONFLICT')
        self.assertEqual(self.base.repo.read(), before)
        with self.base.fixture.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM project_images').fetchone()[0], 1)
        saved = self.service.adopt_image_creation(self.token, **args)
        self.assertTrue(saved['image_key'])
        self.assertEqual(len(self.base.repo.read()['body']['image_references']), 1)
        with self.base.fixture.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM project_images').fetchone()[0], 1)
            self.assertEqual(con.execute('SELECT count(*) FROM project_image_generated_sources').fetchone()[0], 1)
        self.assertEqual(self.calls, 1)

    def test_two_references_transfer_to_explicit_formal_shots_as_unchecked_progress_and_replay(self):
        tasks = [self.reference('block_1', 'first'), self.reference('block_3', 'third')]
        plan = self.base.plan()
        result = self.generate(plan)
        self.assertEqual(result['creative_images']['status'], 'progress_saved', result.get('creative_images'))
        progress = self.image.read_progress(self.token, **self.scope)
        self.assertEqual(len(progress['selections']), 2)
        expected = [shot['shot_no'] for shot in plan['shots'] if shot.get('creative_block', {}).get('block_id') in {'block_1','block_3'}]
        self.assertEqual([row['shot_no'] for row in progress['selections']], expected)
        self.assertTrue(all(not row['checked'] for row in progress['selections']))
        self.assertFalse(progress['review_eligible'])
        with self.base.fixture.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM project_images').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT count(*) FROM project_image_generated_sources').fetchone()[0], 2)
            self.assertEqual(con.execute('SELECT count(*) FROM project_image_source_links').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM project_image_analysis_versions').fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM version_review_human_decisions').fetchone()[0], 0)
        self.service.inherit_creative_images(self.token, **self.scope, version_id=result['version_id'])
        self.assertEqual(self.image.read_progress(self.token, **self.scope)['revision'], progress['revision'])
        self.assertEqual(self.calls, 2)

    def test_protection_transfers_without_images_and_retry_keeps_later_human_unlock(self):
        from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
        script = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(
            store=self.base.fixture.store, projects=self.service.services.projects,
            delivery=VersionDeliveryService(self.base.fixture.store, self.service.services.artifacts),
            commands=None, artifacts=self.service.services.artifacts, image_workspace=self.image),
            runs_root=self.base.fixture.root / 'runs', source_root=self.base.fixture.source_root)
        self.service.creative_revision.workspace = script
        current = self.base.repo.read()
        with self.base.repo.transaction(expected_project_version=current['project_version'], expected_revision=current['revision']):
            self.base.repo.body['assistant_protected_ids'] = ['block_1', 'block_3']
        plan = self.base.plan()
        result = self.generate(plan)
        current = script.read_current(self.token, **self.scope)
        expected_numbers = {shot['shot_no'] for shot in plan['shots'] if shot.get('creative_block', {}).get('assistant_protected')}
        self.assertEqual({shot['shot_no'] for shot in current['shots'] if shot['assistant_locked']}, expected_numbers)
        self.assertEqual(len(expected_numbers), 2)
        self.assertEqual(self.calls, 0)
        script.set_assistant_locks(self.token, **self.scope, expected_project_version=current['project_version'],
            locked_shot_ids=[], idempotency_key='human-unlock-after-conversion')
        self.service.inherit_creative_images(self.token, **self.scope, version_id=result['version_id'])
        self.assertFalse(script.read_current(self.token, **self.scope)['assistant_locked_shot_ids'])

    def test_new_reference_invalidates_frozen_plan_without_calling_text_provider(self):
        self.reference('block_1', 'first')
        plan = self.base.plan()
        self.reference('block_3', 'third')
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.generate(plan)
        self.assertEqual(caught.exception.code, 'CREATIVE_PLAN_STALE')
        self.assertEqual(self.base.calls, [])

    def test_formal_generation_can_fill_pool_without_applying_image_progress(self):
        from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
        script = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(
            store=self.base.fixture.store, projects=self.service.services.projects,
            delivery=self.image.services.delivery, commands=None, artifacts=self.service.services.artifacts,
            image_workspace=self.image), runs_root=self.base.fixture.root / 'runs', source_root=self.base.fixture.source_root)
        self.service.creative_revision.workspace = script
        self.generate(self.base.plan())
        current = script.read_current(self.token, **self.scope)
        before = self.image.read_progress(self.token, **self.scope)
        target = current['shots'][1]['shot_id']
        started = self.service.start_image_creation(self.token, **self.scope,
            expected_project_version=current['project_version'], target_kind='formal_shot',
            target_id=target, prompt='匿名活动参考图', operation_key='formal-batch-pool-start', start_background=False)
        self.service.execute_image_creation(self.token, **self.scope, task_id=started['task_id'])
        args = dict(**self.scope, task_id=started['task_id'], expected_project_version=current['project_version'],
            target_kind='formal_shot', target_id=target, operation_key='formal-batch-pool-adopt', defer_selection=True)
        saved = self.service.adopt_image_creation(self.token, **args)
        self.assertEqual(saved['status'], 'pool_saved')
        self.assertEqual(self.image.read_progress(self.token, **self.scope), before)
        self.assertEqual(self.service.adopt_image_creation(self.token, **args), saved)
        content, mime = self.service.read_image_preview(self.token, **self.scope,
            expected_project_version=current['project_version'], image_id=saved['image_key'],
            version_id=current['base_version_id'])
        self.assertEqual(mime, 'image/png')
        self.assertTrue(content.startswith(b'\x89PNG'))
        self.assertEqual(self.calls, 1)
