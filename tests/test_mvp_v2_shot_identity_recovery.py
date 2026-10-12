"""RV05-01: deletion must preserve identity across unsaved draft operations."""
import unittest
from tests import test_mvp_v2_image_progress as fixture
from mvp_v2.workspace_store import WorkspaceServiceError


class ShotIdentityRecoveryTests(unittest.TestCase):
    setUp = fixture.ImageProgressTests.setUp

    def current_script(self):
        return self.fixture.base.workspace.read_current(self.token, **self.scope)

    def delete(self, index, key):
        model = self.current_script()
        shot = model['shots'][index]
        return self.fixture.base.workspace.apply_revision_operation(self.token, **self.scope,
            expected_project_version=model['project_version'], expected_revision=model['revision'],
            operation={'operation': 'delete_shot', 'shot_no': shot['shot_no'], 'shot_id': shot['shot_id']},
            idempotency_key=key)

    def test_first_delete_edit_consecutive_delete_undo_redo(self):
        original = self.current_script()
        deleted = self.delete(0, 'delete-first')
        self.assertEqual(deleted['shots'][0]['shot_id'], original['shots'][1]['shot_id'])
        edited = self.fixture.base.workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=deleted['project_version'], expected_revision=deleted['revision'],
            shot_no='1', shot_id=original['shots'][1]['shot_id'], field='subtitle', value='幸存镜修改',
            idempotency_key='edit-survivor')
        self.assertEqual(edited['shots'][0]['subtitle'], '幸存镜修改')
        again = self.delete(1, 'delete-middle-after-first')
        self.assertEqual([s['shot_id'] for s in again['shots']],
            [s['shot_id'] for i,s in enumerate(original['shots']) if i not in (0,2)])
        for action, expected in [('undo', edited), ('redo', again)]:
            now = self.current_script()
            result = getattr(self.fixture.base.workspace, action)(self.token, **self.scope,
                expected_project_version=now['project_version'], expected_revision=now['revision'],
                idempotency_key='history-' + action)
            self.assertEqual(result['shots'], expected['shots'])
        before = self.current_script()
        with self.assertRaises(WorkspaceServiceError):
            self.fixture.base.workspace.apply_table_change(self.token, **self.scope,
                expected_project_version=before['project_version'], expected_revision=before['revision'],
                shot_no='1', shot_id=original['shots'][0]['shot_id'], field='subtitle', value='错误实体',
                idempotency_key='deleted-identity')
        self.assertEqual(self.current_script(), before)

    def test_middle_delete_image_page_uses_current_entities(self):
        original = self.current_script()
        deleted = self.delete(1, 'delete-middle')
        page = self.service.read_current(self.token, **self.scope)
        self.assertEqual([s['shot_id'] for s in page['shots']], [s['shot_id'] for s in deleted['shots']])
        self.assertEqual(page['shot_count'], len(original['shots']) - 1)
        self.assertEqual([s['shot_no'] for s in page['shots']], [str(i+1) for i in range(len(deleted['shots']))])
        self.assertEqual(page['shots'][1]['visual'], deleted['shots'][1]['visual'])
        self.assertEqual([s['shot_id'] for s in page['progress']['shots']], [s['shot_id'] for s in deleted['shots']])

    def test_selection_progress_and_sources_follow_identity_through_history(self):
        initial = self.current_script()
        selections = [dict(shot_id=s['shot_id'], image_key=s['selected_image_key'],
            checked=True, decision_kind='image_confirmed', reuse_reason='不同镜头说明同一场景',
            temporary_reason='', query='') for s in self.current['shots']]
        saved = self.service.save_progress(self.token, **self.scope,
            base_version_id=self.current['base_version_id'], expected_project_version=self.current['project_version'],
            expected_revision=0, expected_script_revision=0, selections=selections, idempotency_key='before-delete-images')
        deleted = self.delete(0, 'delete-with-images')
        progress = self.service.read_progress(self.token, **self.scope)
        self.assertEqual([s['shot_id'] for s in progress['shots']], [s['shot_id'] for s in deleted['shots']])
        live = {s['shot_id']: s for s in progress['selections'] if s['selection_active']}
        self.assertNotIn(initial['shots'][0]['shot_id'], live)
        for before, now in zip(initial['shots'][1:], deleted['shots']):
            self.assertEqual(now['source'], before['source'])
            self.assertEqual(live[now['shot_id']]['image_key'],
                next(s['image_key'] for s in saved['selections'] if s['shot_id'] == now['shot_id']))
        restored = self.fixture.base.workspace.undo(self.token, **self.scope,
            expected_project_version=deleted['project_version'], expected_revision=deleted['revision'],
            idempotency_key='restore-images')
        self.assertEqual(restored['shots'], initial['shots'])
        restored_progress = self.service.read_progress(self.token, **self.scope)
        self.assertEqual({s['shot_id'] for s in restored_progress['selections'] if s['selection_active']},
            {s['shot_id'] for s in initial['shots']})

    def test_new_confirmed_brief_keeps_old_draft_readable_and_discardable(self):
        script = self.current_script()
        changed = self.fixture.base.workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=script['project_version'], expected_revision=0,
            shot_no='1', shot_id=script['shots'][0]['shot_id'], field='subtitle', value='保留旧稿输入',
            idempotency_key='before-new-brief')
        self.service.save_progress(self.token, **self.scope,
            base_version_id=self.current['base_version_id'], expected_project_version=self.current['project_version'],
            expected_revision=0, expected_script_revision=changed['revision'],
            selections=[dict(shot_id=script['shots'][0]['shot_id'], image_key='', checked=False,
                decision_kind='temporary_text_basis', temporary_reason='待补', reuse_reason='', query='')],
            idempotency_key='before-new-brief-progress')
        self.fixture.base.generated._confirmed_brief(expected_project_version=script['project_version'], required_content='更新后的流程要求')
        latest = self.fixture.projects.get_project(self.token, self.scope['project_id'])['project_version']
        self.assertEqual(self.current_script()['shots'][0]['subtitle'], '保留旧稿输入')
        self.assertEqual(self.service.read_current(self.token, **self.scope)['base_version_id'], script['base_version_id'])
        progress = self.service.read_progress(self.token, **self.scope)
        with self.assertRaisesRegex(WorkspaceServiceError, 'FORMAL_BRIEF_DRIFT'):
            self.service.save_progress(self.token, **self.scope,
                base_version_id=script['base_version_id'], expected_project_version=latest,
                expected_revision=progress['revision'], expected_script_revision=changed['revision'],
                selections=[], idempotency_key='stale-write')
        discarded = self.fixture.base.workspace.discard_changes(self.token, **self.scope,
            expected_project_version=latest, expected_revision=changed['revision'], idempotency_key='discard-old-text')
        cleared = self.service.save_progress(self.token, **self.scope,
            base_version_id=script['base_version_id'], expected_project_version=latest,
            expected_revision=progress['revision'], expected_script_revision=discarded['revision'],
            selections=[], discard_all=True, idempotency_key='discard-old-images')
        self.assertFalse(any(s['selection_active'] for s in cleared['selections']))
        self.assertFalse(self.current_script()['can_save_version'])

    def test_old_draft_recovery_rejects_tampered_frozen_files_without_discarding_edits(self):
        original = self.current_script()
        workspace = self.fixture.base.workspace
        workspace.apply_table_change(self.token, **self.scope,
            expected_project_version=original['project_version'], expected_revision=original['revision'],
            shot_no='1', shot_id=original['shots'][0]['shot_id'], field='subtitle',
            value='新需求确认后仍须保留的旧稿修改', idempotency_key='before-tampered-recovery')
        self.fixture.base.generated._confirmed_brief(
            expected_project_version=original['project_version'], required_content='新需求流程')
        before = self.current_script()
        context = self.fixture.delivery.resolve(self.token, self.scope['project_id'], before['base_version_id'])
        paths = [self.fixture.resolver.resolve_stage_artifact(context.run_id, 'script_draft').path,
                 self.fixture.resolver.resolve_manifest(context.run_id).path]
        for index, path in enumerate(paths):
            with self.subTest(artifact=path.name):
                contents = path.read_bytes()
                try:
                    path.write_bytes(contents + b' ')
                    with self.assertRaisesRegex(WorkspaceServiceError, 'HASH_MISMATCH'):
                        self.current_script()
                    with self.assertRaisesRegex(WorkspaceServiceError, 'HASH_MISMATCH'):
                        workspace.discard_changes(self.token, **self.scope,
                            expected_project_version=before['project_version'],
                            expected_revision=before['revision'], idempotency_key=f'tampered-discard-{index}')
                finally:
                    path.write_bytes(contents)
                self.assertEqual(self.current_script(), before)
