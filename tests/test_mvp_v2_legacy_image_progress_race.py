"""Old image-save entry points must respect newer canonical image progress."""
import unittest
from unittest.mock import patch

from mvp_v2.formal_image_workspace import ImageShotSelection
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_image_workspace as image_fixture


class LegacyImageProgressRaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = image_fixture.FormalImageWorkspaceTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.workspace, self.token = f.workspace, f.token
        self.scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        self.current = self.workspace.read_current(self.token, **self.scope)
        self.selections = tuple(ImageShotSelection(
            shot['shot_no'], shot['selected_image_key'], True,
            candidate_snapshot_digest=shot['candidate_snapshot_digest']) for shot in self.current['shots'])
        shot = self.current['shots'][0]
        self.replacement = next(candidate['image_key'] for candidate in shot['candidates']
                                if candidate['image_key'] != shot['selected_image_key'])
        self.versions_before = f.projects.list_project_versions(self.token, f.project_id)

    def save_other_window_choice(self):
        return self.workspace.save_progress(self.token, **self.scope,
            base_version_id=self.current['base_version_id'], expected_project_version=self.current['project_version'],
            expected_revision=0, expected_script_revision=0,
            selections=[{'shot_id': self.current['shots'][0]['shot_id'], 'image_key': self.replacement,
                         'checked': False, 'selection_active': True}], idempotency_key='other-window-choice')

    def assert_new_choice_preserved(self):
        progress = self.workspace.read_progress(self.token, **self.scope)
        self.assertEqual(progress['base_version_id'], self.current['base_version_id'])
        self.assertEqual(progress['revision'], 1)
        self.assertEqual(len(progress['selections']), 1)
        self.assertEqual(progress['selections'][0]['image_key'], self.replacement)
        self.assertFalse(progress['selections'][0]['checked'])
        self.assertEqual(self.fixture.projects.list_project_versions(self.token, self.fixture.project_id), self.versions_before)

    def test_old_proposal_cannot_replace_choice_saved_after_preview(self):
        proposal = self.workspace.preview_version(self.token, **self.scope,
            base_version_id=self.current['base_version_id'], expected_project_version=self.current['project_version'],
            selections=self.selections, idempotency_key='old-image-preview')
        self.save_other_window_choice()
        with self.assertRaisesRegex(WorkspaceServiceError, 'IMAGE_PROGRESS_CONFLICT'):
            self.workspace.confirm_version(self.token, proposal_id=proposal['proposal_id'], proposal_digest=proposal['proposal_digest'])
        self.assert_new_choice_preserved()

    def test_direct_old_save_checks_progress_again_inside_final_commit(self):
        original = self.workspace.materializer.materialize
        def concurrent_choice(**kwargs):
            materialized = original(**kwargs)
            self.save_other_window_choice()
            return materialized
        with patch.object(self.workspace.materializer, 'materialize', side_effect=concurrent_choice) as materializer:
            with self.assertRaisesRegex(WorkspaceServiceError, 'IMAGE_PROGRESS_CONFLICT'):
                self.workspace.save_version(self.token, **self.scope,
                    expected_project_version=self.current['project_version'], selections=self.selections,
                    idempotency_key='old-direct-image-save')
            self.assertEqual(materializer.call_count, 1)
        self.assert_new_choice_preserved()

    def test_old_proposal_success_receipt_still_replays_with_frozen_progress_revision(self):
        proposal = self.workspace.preview_version(self.token, **self.scope,
            base_version_id=self.current['base_version_id'], expected_project_version=self.current['project_version'],
            selections=self.selections, idempotency_key='old-image-receipt')
        saved = self.workspace.confirm_version(self.token, proposal_id=proposal['proposal_id'], proposal_digest=proposal['proposal_digest'])
        replay = self.workspace.confirm_version(self.token, proposal_id=proposal['proposal_id'], proposal_digest=proposal['proposal_digest'])
        self.assertEqual(replay['base_version_id'], saved['base_version_id'])
        self.assertEqual(len(self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)), len(self.versions_before) + 1)
