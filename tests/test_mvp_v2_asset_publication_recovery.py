"""Anonymous immutable package publication across asset/workspace transaction boundaries."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2.asset_center_tools import AssetPackageResolver
from mvp_v2.asset_package import bundle_asset_package_v3
from mvp_v2.manual_asset_commands import ManualAssetPublishCommandService, ManualAssetPublishCommandServices
from mvp_v2.package_registry import build_package_record
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_asset_center as asset_fixture
from tests.test_mvp_v2_ui_workbench import UiWorkbenchFixture


class AssetPublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.f = UiWorkbenchFixture(Path(temporary.name)); self.addCleanup(self.f.client.close)
        self.asset = asset_fixture.AssetCenterServiceTests('runTest')
        self.asset.setUp(); self.addCleanup(self.asset.tearDown)
        self.center, self.registry, self.base = self.asset._publication_service()
        self.original_sha = build_package_record(self.base)['package_sha256']
        actor = self.f.identity.validate_session(self.f.auth_token)['actor_id']
        image = self.asset._stored_file_for(self.center, 'receipt.png', 'image', asset_fixture._new_image_bytes('cyan'))
        staged = self.center.stage_import(import_id='import_receipt_anonymous', category_id='launch',
            scope='shared', files=[image], requested_by_actor_id=actor, expected_package_version=3)
        reviewed = self.center.review_import(staged['import_id'], reviewed_by_actor_id=actor,
            expected_revision=staged['revision'])
        self.parameters = {'package_id':'launch-core', 'expected_active_package_version':3,
            'idempotency_key':'publish-receipt-anonymous', 'import_id':reviewed['import_id'],
            'expected_import_revision':reviewed['revision']}
        self.service = ManualAssetPublishCommandService(ManualAssetPublishCommandServices(
            store=self.f.store, projects=self.f.projects, packages=AssetPackageResolver([self.center])))
        self.prepared = self.service.prepare(self.f.auth_token, project_id=self.f.project['project_id'], parameters=self.parameters)

    def execute(self):
        return self.service.execute(self.f.auth_token, project_id=self.f.project['project_id'],
            parameters=self.parameters, command_token=self.prepared['command_token'])

    def assert_once(self):
        events = self.center.list_events(self.parameters['import_id'])
        self.assertEqual(sum(event['event_type']=='asset_publication.completed' for event in events), 1)
        self.assertEqual(sum(event['event_type']=='asset_import.published' for event in events), 1)
        self.assertEqual(sum(event.get('command_id')==self.prepared['command_id']
            for event in self.registry.list()['alias_history']), 1)
        with self.f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM manual_command_results').fetchone()[0], 1)
        self.assertEqual(build_package_record(self.base)['package_sha256'], self.original_sha)

    def test_workspace_receipt_failure_recovers_domain_result_after_later_publication(self):
        with patch('mvp_v2.manual_asset_commands._save_confirmation_result', side_effect=RuntimeError('receipt unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'receipt unavailable'):
                self.execute()
        self.assertEqual(self.center.get_import(self.parameters['import_id'])['state'], 'published')
        current = self.registry.resolve_formal('launch')
        later = bundle_asset_package_v3(current['local_path'], self.asset.root/'later-v5', package_version=5)
        self.registry.register(later)
        self.registry.promote('launch', 'launch-core', 5, expected_current_ref='launch-core@4')
        registry_before = self.registry.path.read_bytes()
        with patch.object(self.center.package_publication, 'publish', side_effect=AssertionError('must not republish')):
            recovered = self.execute()
        self.assertEqual(recovered['result']['package_version'], 4)
        self.assertEqual(self.registry.path.read_bytes(), registry_before)
        self.assertEqual(self.execute(), recovered)
        self.assert_once()

    def test_alias_committed_response_loss_resumes_same_target(self):
        registry = self.center.package_publication.registry
        original = registry.promote
        def lose(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('alias response lost')
        with patch.object(registry, 'promote', side_effect=lose):
            with self.assertRaisesRegex(WorkspaceServiceError, 'PACKAGE_ALIAS_PROMOTE_FAILED'):
                self.execute()
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 4)
        self.assertEqual(self.center.get_import(self.parameters['import_id'])['state'], 'reviewed')
        current = self.registry.resolve_formal('launch')
        later = bundle_asset_package_v3(current['local_path'], self.asset.root/'later-alias-v5', package_version=5)
        self.registry.register(later)
        self.registry.promote('launch', 'launch-core', 5, expected_current_ref='launch-core@4')
        registry_before = self.registry.path.read_bytes()
        with patch.object(self.center.package_publication, '_bundler', side_effect=AssertionError('must reuse target')):
            result = self.execute()
        self.assertEqual(result['result']['package_version'], 4)
        self.assertEqual(self.registry.path.read_bytes(), registry_before)
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 5)
        self.assert_once()

    def test_completed_package_before_built_event_recovers_without_rebuild(self):
        original = self.center.record_publication_stage
        def lose(import_id, command, stage, data):
            if stage == 'built':
                raise RuntimeError('built event unavailable')
            return original(import_id, command, stage, data)
        with patch.object(self.center, 'record_publication_stage', side_effect=lose):
            with self.assertRaisesRegex(WorkspaceServiceError, 'PACKAGE_BUILD_FAILED'):
                self.execute()
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 3)
        with patch.object(self.center.package_publication, '_bundler', side_effect=AssertionError('must reuse target')):
            self.execute()
        self.assert_once()

    def test_asset_result_event_failure_rolls_back_domain_then_resumes_promoted_package(self):
        original = self.center._append_publication_stage
        def fail(connection, import_id, command, stage, data):
            if stage == 'completed':
                raise RuntimeError('domain receipt unavailable')
            return original(connection, import_id, command, stage, data)
        with patch.object(self.center, '_append_publication_stage', side_effect=fail):
            with self.assertRaisesRegex(WorkspaceServiceError, 'PACKAGE_FINALIZE_FAILED'):
                self.execute()
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 4)
        self.assertEqual(self.center.get_import(self.parameters['import_id'])['state'], 'reviewed')
        self.execute()
        self.assert_once()

    def test_domain_commit_then_response_loss_does_not_compensate_alias(self):
        original = self.center._transition
        def lose(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('domain response lost')
        with patch.object(self.center, '_transition', side_effect=lose):
            result = self.execute()
        self.assertEqual(result['result']['package_version'], 4)
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 4)
        self.assertFalse(any(event.get('reason') == 'publication_compensation'
            for event in self.registry.list()['alias_history']))
        self.assert_once()

    def test_same_command_concurrency_and_retry_keep_one_domain_effect(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        start = Barrier(2)
        def execute(_):
            start.wait(timeout=10)
            try:
                return self.execute()
            except WorkspaceServiceError as exc:
                self.assertIn(exc.code, {'PUBLICATION_IN_PROGRESS', 'PACKAGE_VERSION_CONFLICT'})
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(execute, (1, 2)))
        result = next(value for value in values if value is not None)
        self.assertEqual(self.execute(), result)
        self.assert_once()

    def test_recovery_still_rejects_revoked_identity(self):
        with patch('mvp_v2.manual_asset_commands._save_confirmation_result', side_effect=RuntimeError('receipt unavailable')):
            with self.assertRaises(RuntimeError):
                self.execute()
        with self.f.store.transaction() as connection:
            connection.execute("UPDATE auth_sessions SET revoked_at='2026-09-18T00:00:00Z'")
        with self.assertRaisesRegex(WorkspaceServiceError, 'AUTH_SESSION_INVALID'):
            self.execute()
        with self.f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM manual_command_results').fetchone()[0], 0)
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 4)

    def test_shared_command_cannot_be_reused_for_project_scoped_import(self):
        actor = self.f.identity.validate_session(self.f.auth_token)['actor_id']
        image = self.asset._stored_file_for(self.center, 'private.png', 'image', asset_fixture._new_image_bytes('yellow'))
        staged = self.center.stage_import(import_id='import_receipt_private', category_id='launch', scope='project',
            project_id=self.f.project['project_id'], files=[image], requested_by_actor_id=actor,
            expected_package_version=3)
        reviewed = self.center.review_import(staged['import_id'], reviewed_by_actor_id=actor,
            expected_revision=staged['revision'])
        parameters = {**self.parameters, 'import_id': reviewed['import_id'],
            'expected_import_revision': reviewed['revision']}
        with self.assertRaisesRegex(WorkspaceServiceError, 'PERMISSION_DENIED'):
            self.service.execute(self.f.auth_token, project_id=self.f.project['project_id'], parameters=parameters,
                command_token=self.prepared['command_token'])
        self.assertEqual(self.center.get_import(reviewed['import_id'])['state'], 'reviewed')
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 3)

    def test_partial_target_without_completion_proof_is_preserved_for_reconciliation(self):
        def interrupted(source, target, **kwargs):
            target.mkdir(parents=True)
            (target/'partial.fixture').write_bytes(b'owned partial data')
            raise OSError('interrupted build')
        with patch.object(self.center.package_publication, '_bundler', side_effect=interrupted):
            with self.assertRaisesRegex(WorkspaceServiceError, 'PACKAGE_BUILD_FAILED'):
                self.execute()
        partial = self.center.package_publication.packages_root/'launch-core'/'v4'/'partial.fixture'
        with self.assertRaisesRegex(WorkspaceServiceError, 'PUBLICATION_RECONCILIATION_REQUIRED'):
            self.execute()
        self.assertEqual(partial.read_bytes(), b'owned partial data')
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 3)

    def test_legacy_consumed_without_binding_and_changed_request_are_not_reexecuted(self):
        with self.f.store.transaction() as connection:
            connection.execute("UPDATE manual_command_confirmations SET status='consumed' WHERE command_id=?",
                (self.prepared['command_id'],))
        with self.assertRaisesRegex(WorkspaceServiceError, 'MANUAL_RESULT_RECONCILIATION_REQUIRED'):
            self.execute()
        self.parameters['expected_import_revision'] += 1
        with self.assertRaisesRegex(WorkspaceServiceError, 'PERMISSION_DENIED'):
            self.execute()
        self.assertEqual(self.registry.resolve_formal('launch')['package_version'], 3)
        self.assertFalse(any(event['event_type'].startswith('asset_publication.')
            for event in self.center.list_events(self.parameters['import_id'])))
