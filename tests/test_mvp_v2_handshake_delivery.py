"""Isolated service journey: image progress through immutable save and formal Excel.

Set MPLUS_HANDSHAKE_EVIDENCE_DIR to retain the anonymous delivery and evidence.
Human decisions below are explicit test fixtures, never production approvals.
"""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import unittest

from openpyxl import load_workbook

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentServices
from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from mvp_v2.formal_image_workspace import FormalImageWorkspaceService
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_image_authority import ProjectImagePoolService
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceStore
from tests.test_mvp_v2_formal_agent import FakeFormalGeneration
from tests import test_mvp_v2_formal_review_workspace as review_fixture


class HandshakeDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = review_fixture.FormalReviewWorkspaceTests('runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.images = self.fixture.base.workspace
        self.script = self.fixture.base.base.workspace
        self.store = self.fixture.store
        self.token = self.fixture.token
        self.scope = {'workspace_id': self.fixture.workspace_id, 'project_id': self.fixture.project_id}
        self.projects = ProjectService(self.store)
        tasks = TaskService(self.store)
        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store, identity=LocalIdentityAdapter(self.store), projects=self.projects,
            tasks=tasks, versions=VersionService(self.store), artifacts=self.fixture.base.resolver))
        self.revision = FormalRevisionService(projects=self.projects, tasks=tasks,
            workspace=self.script, image_workspace=self.images, writes_enabled=True)
        self.agent = FormalExecutableAgentService(ExecutableAgentServices(
            readonly=readonly, projects=self.projects, tasks=tasks,
            confirmations=ActionConfirmationService(self.store, b'h' * 32),
            idempotency=ToolIdempotencyService(self.store),
            brief_store=BriefArtifactStore(self.fixture.root / 'runtime'), backend=FormalExecutionBackend()),
            generation=FakeFormalGeneration(), advisor=None, writes_enabled=True, script_revision=self.revision)

    def _versions(self):
        return self.projects.list_project_versions(self.token, self.scope['project_id'])

    def _human_count(self):
        with self.store.connection() as connection:
            return connection.execute('SELECT count(*) FROM version_review_human_decisions').fetchone()[0]

    def _confirm(self, current, progress, selections, key):
        args = dict(**self.scope, base_version_id=current['base_version_id'],
                    expected_project_version=current['project_version'], expected_revision=progress['revision'],
                    expected_script_revision=current['revision'], selections=selections)
        preview = self.images.preview_progress_confirmation(self.token, **args)
        self.assertEqual(preview['pending_count'], 0)
        args.update(preview_digest=preview['preview_digest'], accepted_shot_ids=[item['shot_id'] for item in selections], idempotency_key=key)
        result = self.images.confirm_progress(self.token, **args)
        self.assertEqual(result['confirmed_count'], len(selections))
        self.assertEqual(self.images.confirm_progress(self.token, **args), result)
        return result

    def test_existing_version_subset_then_all_confirmation_save_review_export_restart(self):
        current = self.script.read_current(self.token, **self.scope)
        old_images = self.images.read_current(self.token, **self.scope)
        count = len(current['shots'])
        self.assertGreaterEqual(count, 3)
        versions = self._versions()
        self.assertEqual(self._human_count(), 0)
        _, old_context = self.script._current_context(self.token, **self.scope)
        parent_paths = [self.fixture.base.resolver.resolve_stage_artifact(old_context.run_id, stage).path
                        for stage in ('script_plan', 'script_draft', 'visual_binding')]
        parent_hashes = {str(path): sha256(path.read_bytes()).hexdigest() for path in parent_paths}
        old_keys = {shot['shot_id']: shot['selected_image_key'] for shot in old_images['shots']}
        old_shas = {shot['shot_id']: shot['current_image']['content_sha256'] for shot in old_images['shots']}
        selections = []
        replacements = {}
        for i, shot in enumerate(old_images['shots']):
            key = shot['selected_image_key']
            if i in (0, 2):
                candidates = self.images.search_shot(self.token, **self.scope,
                    expected_project_version=current['project_version'], base_version_id=current['base_version_id'],
                    shot_no=shot['shot_no'], query='')['candidates']
                candidate = next(item for item in candidates if item['image_key'] != key)
                key = candidate['image_key']
                replacements[shot['shot_id']] = candidate['content_sha256']
                selections.append({'shot_id': shot['shot_id'], 'image_key': key, 'checked': False,
                                   'reuse_reason': '隔离回归：按镜头用途核对同场景跨镜承接'})
        progress = self.images.read_progress(self.token, **self.scope)
        progress = self.images.save_progress(self.token, **self.scope, base_version_id=current['base_version_id'],
            expected_project_version=current['project_version'], expected_revision=progress['revision'],
            expected_script_revision=current['revision'], selections=selections, idempotency_key='handshake-unchecked-progress')
        self.assertEqual(len(progress['selections']), 2)
        self.assertTrue(all(not item['checked'] for item in progress['selections']))
        subset = self._confirm(current, progress, selections[:1], 'handshake-subset-confirm')
        self.assertTrue(next(item for item in subset['selections'] if item['shot_id'] == selections[0]['shot_id'])['checked'])
        self.assertTrue(all(not item['checked'] for item in subset['selections'] if item['shot_id'] != selections[0]['shot_id']))
        all_confirmed = self._confirm(current, subset, selections, 'handshake-all-confirm')
        self.assertTrue(all(item['checked'] for item in all_confirmed['selections']))
        self.assertEqual(self._versions(), versions)
        self.assertEqual(self._human_count(), 0)
        readback = self.images.read_progress(self.token, **self.scope)
        self.assertTrue(all(item['checked'] for item in readback['selections']))
        prepared = self.images.prepare_working_version(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_script_revision=current['revision'],
            expected_progress_revision=readback['revision'])
        self.assertEqual(set(prepared['changed_shot_ids']), set(replacements))
        for decision in prepared['decisions']:
            self.assertEqual(sha256(Path(decision['controlled_path']).read_bytes()).hexdigest(), decision['image_sha256'])
            self.assertTrue(decision['source_location']['file'])
        proposal = self.revision.propose_draft_save(self.token, project_id=self.scope['project_id'],
            expected_revision=current['revision'], expected_project_version=current['project_version'],
            expected_progress_revision=readback['revision'], reason='隔离回归：两镜换图，其余保留',
            idempotency_key='handshake-save-proposal')
        self.assertEqual(len(proposal['response']['preview']['image_changes']), 2)
        result = self.agent.confirm_action(self.token, proposal['task_id'], proposal_digest=proposal['response']['proposal_digest'])
        self.assertEqual(result['task']['status'], 'completed', result)
        replay = self.agent.confirm_action(self.token, proposal['task_id'], proposal_digest=proposal['response']['proposal_digest'])
        self.assertEqual(replay['task']['status'], 'completed')
        self.assertEqual(len(self._versions()), len(versions) + 1)
        self.assertEqual(self._human_count(), 0)
        saved = self.script.read_current(self.token, **self.scope)
        after_images = self.images.read_current(self.token, **self.scope)
        self.assertNotEqual(saved['base_version_id'], current['base_version_id'])
        self.assertEqual(saved['shots'], current['shots'])
        for shot in after_images['shots']:
            identity = shot['shot_id']
            self.assertEqual(shot['current_image']['content_sha256'], replacements.get(identity, old_shas[identity]))
            if identity not in replacements:
                self.assertEqual(shot['selected_image_key'], old_keys[identity])
        self.assertEqual(self.images.read_progress(self.token, **self.scope)['selections'], [])
        for path, digest in parent_hashes.items():
            self.assertEqual(sha256(Path(path).read_bytes()).hexdigest(), digest)
        preview = self.fixture.review.read_current(self.token, **self.scope)
        self.assertFalse(preview['review_started'])
        self.assertNotEqual(preview['state'], 'confirmed')
        confirmed = self.fixture._confirm_current()
        self.assertEqual(confirmed['state'], 'confirmed')
        delivery = self.fixture.review.prepare_excel_delivery(self.token, **self.scope,
            expected_project_version=confirmed['project_version'])
        digest = sha256(delivery.path.read_bytes()).hexdigest()
        workbook = load_workbook(delivery.path)
        try:
            self.assertEqual(len(workbook['脚本']._images), count)
            edges = [row for row in workbook['相邻镜头复核'].iter_rows(min_row=2, values_only=True) if any(value is not None for value in row)]
            self.assertEqual(len(edges), count - 1)
            self.assertIn('审核记录', workbook.sheetnames)
        finally:
            workbook.close()
        with self.store.connection() as connection:
            excel_ref = connection.execute("SELECT artifact_ref FROM delivery_artifact_bindings WHERE artifact_kind='excel' AND availability='available'").fetchone()[0]
        trace = self.fixture.review.delivery_snapshots.reverse_trace(self.token, project_id=self.scope['project_id'], artifact_ref=excel_ref)
        self.assertEqual(trace['status'], 'verified')
        self.assertEqual(trace['image_decision_count'], count)
        self.assertTrue(all(item['source_file'] and item['source_sheet'] and int(item['source_row']) > 0 for item in trace['source_trace']))

        reopened_store = WorkspaceStore(self.store.database_path)
        reopened_images = FormalImageWorkspaceService(replace(self.images.services, store=reopened_store,
            projects=ProjectService(reopened_store), delivery=VersionDeliveryService(reopened_store, self.fixture.base.resolver),
            images=ProjectImagePoolService(reopened_store, data_root=self.fixture.root / 'runtime')),
            runs_root=self.fixture.root / 'runs', source_root=self.fixture.base.base.generated.source_root)
        recovered_images = reopened_images.read_current(self.token, **self.scope)
        self.assertEqual([(item['shot_id'], item['selected_image_key'], item['confirmed']) for item in recovered_images['shots']],
                         [(item['shot_id'], item['selected_image_key'], item['confirmed']) for item in after_images['shots']])
        reopened_review = self.fixture._review_service(reopened_store)
        recovered = reopened_review.read_current(self.token, **self.scope)
        self.assertEqual(recovered['state'], 'confirmed')
        recovered_delivery = reopened_review.prepare_excel_delivery(self.token, **self.scope,
            expected_project_version=recovered['project_version'])
        self.assertEqual(sha256(recovered_delivery.path.read_bytes()).hexdigest(), digest)
        if os.environ.get('MPLUS_HANDSHAKE_EVIDENCE_DIR'):
            output = Path(os.environ['MPLUS_HANDSHAKE_EVIDENCE_DIR']).resolve()
            output.mkdir(parents=True, exist_ok=True)
            target = output / 'M+隔离路径B正式交付-v1.xlsx'
            shutil.copy2(delivery.path, target)
            evidence = {'mode': 'anonymous_real_service_fixture_with_simulated_human_decisions', 'shot_count': count,
                'adjacent_review_count': len(edges), 'changed_shot_ids': list(replacements), 'preserved_image_count': count - 2,
                'image_sha_verified_count': len(prepared['decisions']), 'parent_artifacts_unchanged': parent_hashes,
                'new_confirmed_progress_before_save': 2, 'total_confirmed_images_after_save': count, 'human_decisions_before_explicit_review': 0,
                'old_version_id': current['base_version_id'], 'new_version_id': saved['base_version_id'],
                'version_count_before': len(versions), 'version_count_after': len(self._versions()),
                'final_review_state': recovered['state'], 'restart_readback': True,
                'delivery': {'path': str(target), 'sha256': digest}, 'reverse_trace': trace}
            (output / 'evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')


    def test_all_shots_confirmation_preserves_unchanged_images_and_text(self):
        current = self.script.read_current(self.token, **self.scope)
        before = self.images.read_current(self.token, **self.scope)
        count = len(before['shots'])
        _, context = self.script._current_context(self.token, **self.scope)
        parent_paths = [self.fixture.base.resolver.resolve_stage_artifact(context.run_id, stage).path
                        for stage in ('script_plan', 'script_draft', 'visual_binding')]
        hashes = {str(path): sha256(path.read_bytes()).hexdigest() for path in parent_paths}
        expected_shas = {item['shot_id']: item['current_image']['content_sha256'] for item in before['shots']}
        selections = []
        changed_ids = []
        for index, item in enumerate(before['shots']):
            key = item['selected_image_key']
            if index in (0, 2):
                found = self.images.search_shot(self.token, **self.scope,
                    expected_project_version=current['project_version'], base_version_id=current['base_version_id'],
                    shot_no=item['shot_no'], query='')
                chosen = next(candidate for candidate in found['candidates'] if candidate['image_key'] != key)
                key = chosen['image_key']
                expected_shas[item['shot_id']] = chosen['content_sha256']
                changed_ids.append(item['shot_id'])
            selections.append({'shot_id': item['shot_id'], 'image_key': key, 'checked': False,
                               'reuse_reason': '隔离全量确认：已核对各镜的同场景复用用途'})
        progress = self.images.read_progress(self.token, **self.scope)
        progress = self.images.save_progress(self.token, **self.scope, base_version_id=current['base_version_id'],
            expected_project_version=current['project_version'], expected_revision=progress['revision'],
            expected_script_revision=current['revision'], selections=selections, idempotency_key='all-branch-unchecked')
        progress = self._confirm(current, progress, selections, 'all-branch-confirm')
        self.assertEqual(progress['confirmed_count'], count)
        self.assertTrue(all(item['checked'] for item in progress['selections']))
        # Whole-project confirmation is an explicit new image decision for every shot,
        # even when its image bytes are retained. Text and source content stay frozen.
        prepared = self.images.prepare_working_version(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_script_revision=current['revision'],
            expected_progress_revision=progress['revision'])
        self.assertEqual(len(prepared['changed_shot_ids']), count)
        self.assertEqual(len(prepared['comparisons']), count)
        self.assertEqual(sum(item['before_image_key'] != item['after_image_key'] for item in prepared['comparisons']), 2)
        versions = self._versions()
        proposal = self.revision.propose_draft_save(self.token, project_id=self.scope['project_id'],
            expected_revision=current['revision'], expected_project_version=current['project_version'],
            expected_progress_revision=progress['revision'], reason='隔离回归：两镜换图后全稿重新确认',
            idempotency_key='all-branch-version')
        result = self.agent.confirm_action(self.token, proposal['task_id'], proposal_digest=proposal['response']['proposal_digest'])
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(len(self._versions()), len(versions) + 1)
        saved = self.script.read_current(self.token, **self.scope)
        images = self.images.read_current(self.token, **self.scope)
        self.assertEqual(saved['shots'], current['shots'])
        self.assertEqual({item['shot_id']: item['current_image']['content_sha256'] for item in images['shots']}, expected_shas)
        self.assertTrue(all(item['confirmed'] for item in images['shots']))
        for path, digest in hashes.items():
            self.assertEqual(sha256(Path(path).read_bytes()).hexdigest(), digest)
        self.assertEqual(self._human_count(), 0)
        review = self.fixture.review.read_current(self.token, **self.scope)
        self.assertFalse(review['review_started'])
        self.assertNotEqual(review['state'], 'confirmed')
        self.assertEqual(sum(item['kind'] == 'image' for item in review['issues']), count)
        if os.environ.get('MPLUS_HANDSHAKE_EVIDENCE_DIR'):
            output = Path(os.environ['MPLUS_HANDSHAKE_EVIDENCE_DIR']).resolve()
            output.mkdir(parents=True, exist_ok=True)
            evidence = {'scope': 'all_project_shots', 'shot_count': count, 'confirmed_count': progress['confirmed_count'],
                'actual_image_changes': 2, 'unchanged_image_sha_count': count - 2,
                'text_unchanged': True, 'parent_artifacts_unchanged': hashes,
                'new_decision_count': len(prepared['changed_shot_ids']), 'review_image_issue_count': count,
                'human_decisions_before_review': 0, 'version_count_delta': 1,
                'semantics': '全量确认形成全稿新图片决定并重新核对，未换图镜头的图片内容SHA与文字仍保留；仅子集确认时其余镜头保留原决定'}
            (output / 'all-confirmation-evidence.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')


if __name__ == '__main__':
    unittest.main()
