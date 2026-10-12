"""RV05: working progress cannot revoke a frozen version's review evidence."""
import unittest
from tests import test_mvp_v2_formal_review_workspace as fixture


class FrozenVersionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.FormalReviewWorkspaceTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_unfinished_working_images_do_not_revoke_saved_review(self):
        f = self.f
        approved = f._confirm_current()
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        before = f.review.read_current(f.token, **scope)
        images = f.base.workspace.read_current(f.token, **scope)
        progress = f.base.workspace.save_progress(f.token, **scope,
            base_version_id=images['base_version_id'], expected_project_version=images['project_version'],
            expected_revision=0, expected_script_revision=0, idempotency_key='unfinished-new-images',
            selections=[dict(shot_id=images['shots'][0]['shot_id'], image_key='', checked=False,
                decision_kind='temporary_text_basis', temporary_reason='新稿等待补图', reuse_reason='', query='')])
        self.assertTrue(progress['selections'])
        working = f.base.workspace.read_current(f.token, **scope)
        self.assertFalse(working['review_eligible'])
        after = f.review.read_current(f.token, **scope)
        self.assertEqual(after['state'], before['state'])
        self.assertEqual(after['base_version_id'], before['base_version_id'])
        self.assertEqual(after['final_confirmation'], before['final_confirmation'])

    def _create_child(self):
        f = self.f
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        current = f.base.base.workspace.read_current(f.token, **scope)
        changed = f.base.base.workspace.apply_table_change(f.token, **scope,
            expected_project_version=current['project_version'], expected_revision=0,
            shot_no='1', shot_id=current['shots'][0]['shot_id'], field='subtitle',
            value='新版本字幕等待审核', idempotency_key='historical-child-edit')
        f.base.base.workspace.save_version(f.token, **scope,
            expected_project_version=current['project_version'], expected_revision=changed['revision'],
            reason='历史交付回归', idempotency_key='historical-child-save')
        return scope

    def test_approved_ancestor_remains_readable_and_exportable_after_child(self):
        from mvp_v2.run_contract import sha256_file
        from mvp_v2.workspace_store import WorkspaceServiceError
        f = self.f
        f._confirm_current()
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        old = f.review.read_current(f.token, **scope)
        delivered = f.review.prepare_excel_delivery(f.token, **scope,
            expected_project_version=old['project_version'])
        original_sha = sha256_file(delivered.path)
        self._create_child()
        current = f.review.read_current(f.token, **scope)
        self.assertNotEqual(current['state'], 'confirmed')
        historical = f.review.read_current(f.token, **scope, version_id=old['base_version_id'])
        self.assertEqual(historical['state'], 'confirmed')
        self.assertTrue(historical['read_only'])
        self.assertEqual(historical['final_confirmation'], old['final_confirmation'])
        historical_delivery = f.review.prepare_excel_delivery(f.token, **scope,
            expected_project_version=current['project_version'], version_id=old['base_version_id'])
        self.assertEqual(sha256_file(historical_delivery.path), original_sha)
        with self.assertRaises(WorkspaceServiceError):
            f.review.prepare_excel_delivery(f.token, **scope,
                expected_project_version=current['project_version'])
        with self.assertRaises(WorkspaceServiceError):
            f.review.read_current(f.token, **scope, version_id='unrelated-version')

    def test_first_export_of_ancestor_creates_only_its_frozen_snapshot(self):
        f = self.f
        f._confirm_current()
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        old = f.review.read_current(f.token, **scope)
        self._create_child()
        current = f.review.read_current(f.token, **scope)
        result = f.review.prepare_excel_delivery(f.token, **scope,
            expected_project_version=current['project_version'], version_id=old['base_version_id'])
        self.assertTrue(result.path.is_file())
        with f.store.connection() as connection:
            rows = connection.execute('SELECT version_id FROM delivery_snapshots').fetchall()
        self.assertEqual([row['version_id'] for row in rows], [old['base_version_id']])
        from mvp_v2.workspace_store import WorkspaceServiceError
        from mvp_v2.version_review_repository import WorkspaceVersionReviewRepository
        project = f.base.projects.get_project(f.token, f.project_id)
        context = f.review.services.delivery.resolve(f.token, f.project_id,
            old['base_version_id'], ancestor_of=project['current_version_id'])
        repository = WorkspaceVersionReviewRepository(f.store, auth_token=f.token,
            workspace_id=f.workspace_id, project_id=f.project_id,
            version_id=context.version_id, run_id=context.run_id,
            expected_project_lock_version=project['project_version'],
            read_ancestor_of=project['current_version_id'])
        with f.store.connection() as connection:
            repository._read_context(connection)
            with self.assertRaisesRegex(WorkspaceServiceError, 'VERSION_NOT_CURRENT'):
                repository._write_context(connection, permission='review.record')
        pdf = f.review.prepare_pdf_delivery(f.token, **scope,
            expected_project_version=current['project_version'], version_id=old['base_version_id'])
        self.assertEqual(pdf.status, 'available')
        self.assertTrue(pdf.path.read_bytes().startswith(b'%PDF'))
        self.assertIn(old['version_label'], pdf.filename)
