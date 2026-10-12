from collections import defaultdict
import unittest

from mvp_v2.generation_stages import read_script_plan
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_image_workspace as image_fixture


class ImageProgressNearDuplicateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = image_fixture.FormalImageWorkspaceTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_preview_requires_reasons_for_distinct_content_in_same_visual_cluster(self):
        f = self.fixture
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        current = f.workspace.read_current(f.token, **scope)
        progress = f.workspace.read_progress(
            f.token, **scope, expected_project_version=current['project_version'],
        )
        candidates = f.workspace.search_shot(
            f.token, **scope, base_version_id=current['base_version_id'],
            expected_project_version=current['project_version'],
            shot_no=current['shots'][0]['shot_no'], query='',
        )['candidates']
        clusters = defaultdict(list)
        for candidate in candidates:
            clusters[candidate['perceptual_cluster_id']].append(candidate['image_key'])
        pair = next(keys[:2] for keys in clusters.values() if len(keys) > 1)
        # Explicitly clear other shots so prior selections cannot account for reuse.
        selections = [dict(shot_id=shot['shot_id'], image_key='', reuse_reason='')
                      for shot in current['shots']]
        for selection, key in zip(selections, pair):
            selection['image_key'] = key
        args = dict(
            **scope, base_version_id=current['base_version_id'],
            expected_project_version=current['project_version'],
            expected_revision=progress['revision'],
            expected_script_revision=progress['script_revision'], selections=selections,
        )
        preview = f.workspace.preview_progress_confirmation(f.token, **args)
        selected = preview['items'][:2]
        self.assertNotEqual(selected[0]['reference']['content_sha256'],
                            selected[1]['reference']['content_sha256'])
        self.assertEqual([item['processable'] for item in selected], [False, False])
        self.assertTrue(all('复用说明' in item['reason'] for item in selected))
        _, context = f.workspace._current_context(f.token, f.workspace_id, f.project_id, current['project_version'])
        plan = read_script_plan(f.workspace.services.artifacts.resolve_stage_artifact(context.run_id, 'script_plan').path)
        brief_id, brief_snapshot = f.workspace._brief_binding(context, plan.confirmed_brief_input)
        pool = f.workspace.services.images.resolve_for_delivery(f.token, **scope,
            brief_version_id=brief_id, brief_snapshot_id=brief_snapshot,
            expected_project_lock_version=current['project_version'])
        saved_choices = [dict(selection, shot_no=shot['shot_no'])
                         for selection, shot in zip(selections, current['shots'])]
        with self.assertRaises(WorkspaceServiceError) as blocked:
            f.workspace._require_reuse_reasons(pool, saved_choices)
        self.assertEqual(blocked.exception.code, 'IMAGE_REUSE_REASON_REQUIRED')
        self.assertEqual([item['shot_id'] for item in blocked.exception.issues],
                         [shot['shot_id'] for shot in current['shots'][:2]])
        for selection in selections[:2]:
            selection['reuse_reason'] = '用相近画面连接两个不同叙事段落，已人工核对'
        with_reasons = f.workspace.preview_progress_confirmation(f.token, **args)
        self.assertEqual([item['processable'] for item in with_reasons['items'][:2]],
                         [True, True])
