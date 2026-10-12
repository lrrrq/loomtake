"""Generated references join the existing multi-image Formal delivery journey."""
from hashlib import sha256
import json
import sqlite3
import unittest
from types import SimpleNamespace

from openpyxl import load_workbook

from mvp_v2 import creative_images
from mvp_v2.formal_image_workspace import ImageShotSelection
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.image_providers import ImageProviderResult
from mvp_v2.project_image_authority import (
    ManualImageAnalysisInput, ManualProjectImageAnalysisCommandService,
)
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_image_workspace as workspace_fixture
from tests.test_mvp_v2_image_creation import photo


class GeneratedImageDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.f = workspace_fixture.FormalImageWorkspaceTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.service = self.f.base.generation
        self.service.creative_revision = FormalRevisionService(
            projects=self.f.projects, tasks=TaskService(self.f.store),
            workspace=self.f.base.workspace, image_workspace=self.f.workspace,
            writes_enabled=True,
        )
        self.token = self.f.token
        self.scope = {'workspace_id': self.f.workspace_id, 'project_id': self.f.project_id}
        self.original, self.edited = photo('orange'), photo('blue')
        self.calls = []

        def provider(prompt, reference_images=(), **kwargs):
            self.calls.append(reference_images)
            return ImageProviderResult(self.edited if len(self.calls) > 1 else self.original,
                'image/png', 2560, 1440, 'anonymous-image', None,
                '2560x1440', 'anonymous-request', {})

        self.service.image_provider = provider

    def generate(self, target_id, project_version, operation_key, parents=()):
        started = self.service.start_image_creation(self.token, **self.scope,
            expected_project_version=project_version, target_kind='formal_shot',
            target_id=target_id, prompt='为当前镜头生成一张匿名品牌参考图片',
            operation_key=operation_key, reference_task_ids=list(parents), start_background=False)
        self.service.execute_image_creation(self.token, **self.scope, task_id=started['task_id'])
        result = self.service.read_image_creation(self.token, **self.scope, task_id=started['task_id'])
        self.assertEqual(result['status'], 'completed', result)
        return result

    def test_generated_comparison_stays_pending_until_analysis_then_exports_real_generated_provenance(self):
        initial = self.f.workspace.read_current(self.token, **self.scope)
        self.assertGreater(len(initial['shots']), 3)
        script = self.f.base.workspace.read_current(self.token, **self.scope)
        target = script['shots'][0]
        version = initial['project_version']
        previous_versions = self.f.projects.list_project_versions(self.token, self.f.project_id)
        w = workspace_fixture
        readonly = w.ReadonlyAgentService(w.ReadonlyAgentServices(store=self.f.store,
            identity=w.LocalIdentityAdapter(self.f.store), projects=self.f.projects,
            tasks=TaskService(self.f.store), versions=w.VersionService(self.f.store), artifacts=self.f.resolver))
        agent = w.FormalExecutableAgentService(w.ExecutableAgentServices(readonly=readonly,
            projects=self.f.projects, tasks=TaskService(self.f.store),
            confirmations=w.ActionConfirmationService(self.f.store, b'h' * 32),
            idempotency=w.ToolIdempotencyService(self.f.store),
            brief_store=w.BriefArtifactStore(self.f.base.generated.root / 'data'), backend=w.FormalExecutionBackend()),
            generation=self.service, advisor=None, script_revision=self.service.creative_revision)
        session = agent.services.tasks.create_agent_session(self.token, project_id=self.f.project_id)
        routed = agent.submit_message(self.token, session['agent_session_id'], content='为第1镜生成一张参考图')
        self.assertEqual(routed['response']['type'], 'image_creation_request')
        self.assertEqual(routed['response']['target_id'], target['shot_id'])
        self.assertEqual(self.calls, [])
        ambiguous = agent.submit_message(self.token, session['agent_session_id'], content='生成一张参考图')
        self.assertEqual(ambiguous['response']['type'], 'needs_clarification')
        original = self.generate(target['shot_id'], version, 'generated-original-01')
        edited = self.generate(target['shot_id'], version, 'generated-second-02')
        self.assertEqual(self.calls, [(), ()])
        self.assertEqual(self.service.preview_image_creation(self.token, **self.scope,
            task_id=original['task_id'])[0], self.original)

        _, _, metadata, edited_bytes = creative_images.load_result(self.service, self.token,
            **self.scope, task_id=edited['task_id'])
        self.assertEqual(edited_bytes, self.edited)
        self.assertEqual(metadata['origin'], 'generated')
        self.assertEqual(metadata['parents'], [])

        context = self.service.image_creation_context(self.token, **self.scope,
            target_kind='formal_shot', target_id=target['shot_id'])
        adopted = self.service.adopt_image_creation(self.token, **self.scope,
            task_id=edited['task_id'], target_kind='formal_shot', target_id=target['shot_id'],
            expected_project_version=version, expected_progress_revision=context['progress_revision'],
            expected_script_revision=context['script_revision'], operation_key='generated-adoption-01')
        self.assertEqual(adopted['status'], 'progress_saved')
        self.assertEqual(self.f.projects.list_project_versions(self.token, self.f.project_id), previous_versions)
        progress = self.f.workspace.read_progress(self.token, **self.scope)
        selected = next(row for row in progress['selections'] if row['shot_id'] == target['shot_id'])
        self.assertFalse(selected['checked'])
        image_id = selected['image_key']
        brief_id, snapshot_id = self.f.pool.current_confirmed_brief_binding(self.token,
            **self.scope, expected_project_lock_version=version)
        pool_scope = {**self.scope, 'brief_version_id': brief_id,
            'brief_snapshot_id': snapshot_id, 'expected_project_lock_version': version}
        pending = self.f.pool.resolve_for_planning(self.token, **pool_scope)
        self.assertGreaterEqual(len(pending.images), 37)
        pending_image = next(image for image in pending.images if image.image_id == image_id)
        self.assertIsNone(pending_image.current_analysis)
        self.assertNotIn(image_id, [asset.image_id for asset in
            self.f.pool.resolve_for_delivery(self.token, **pool_scope).assets])
        selections = tuple(ImageShotSelection(row['shot_no'],
            image_id if index == 0 else row['selected_image_key'], True,
            reuse_reason='匿名 fixture 已明确核对同场景参考复用')
            for index, row in enumerate(initial['shots']))
        with self.assertRaises(WorkspaceServiceError) as refused:
            self.f.workspace.save_version(self.token, **self.scope,
                expected_project_version=version, selections=selections,
                idempotency_key='generated-pending-save-01')
        self.assertEqual(refused.exception.code, 'CROSS_PROJECT_DENIED')
        self.assertEqual(self.f.projects.list_project_versions(self.token, self.f.project_id), previous_versions)

        # Use the existing first-shot candidate's human-reviewed semantics for
        # this anonymous controlled-provider fixture; no quality claim is inferred
        # from generating a picture or saving it into the project pool.
        prior = next(asset for asset in self.f.pool.resolve_for_delivery(self.token, **pool_scope).assets
            if asset.image_id == initial['shots'][0]['selected_image_key'])
        summary = prior.business_summary
        ManualProjectImageAnalysisCommandService(self.f.pool).record_analysis(self.token,
            **self.scope, expected_project_lock_version=version,
            submission=ManualImageAnalysisInput(image_id=image_id,
                image_sha256=sha256(self.edited).hexdigest(), brief_version_id=brief_id,
                brief_snapshot_id=snapshot_id, status='pass', caption=summary.caption,
                tags=summary.tags, subjects=summary.subjects, scene=summary.scene,
                purpose=summary.purpose, quality='匿名 fixture 人工已核对', brand=summary.brand,
                evidence=('匿名测试图片人工分析，非真实 Provider 质量验收',)))
        self.assertIn(image_id, [asset.image_id for asset in
            self.f.pool.resolve_for_delivery(self.token, **pool_scope).assets])
        saved = self.f.workspace.save_version(self.token, **self.scope,
            expected_project_version=version, selections=selections,
            idempotency_key='generated-analysed-save-02')
        self.assertEqual(saved['save_status'], 'completed')
        self.assertEqual(saved['shots'][0]['selected_image_key'], image_id)
        versions = self.f.projects.list_project_versions(self.token, self.f.project_id)
        self.assertEqual(len(versions), len(previous_versions) + 1)
        self.assertEqual(versions[-1]['parent_version_id'], previous_versions[-1]['version_id'])
        binding = json.loads(self.f.resolver.resolve_stage_artifact(
            versions[-1]['run_id'], 'visual_binding').path.read_text(encoding='utf-8'))
        generated = next(row for row in binding['bindings'] if row['image_sha256'] == sha256(self.edited).hexdigest())
        self.assertEqual(generated['source_kind'], 'generated')
        self.assertEqual(generated['source_key'], 'generated:' + edited['task_id'])
        self.assertEqual(set(generated['source_location']), {'file', 'task_id', 'image_sha256'})
        self.assertEqual(generated['source_location']['task_id'], edited['task_id'])
        self.assertEqual(generated['source_location']['image_sha256'], sha256(self.edited).hexdigest())
        workbook = load_workbook(self.f.resolver.resolve_artifact(versions[-1]['run_id'], 'workbook').path)
        try:
            self.assertEqual(len(workbook['脚本']._images), len(initial['shots']))
            self.assertEqual(workbook['相邻镜头复核'].max_row - 1, len(initial['shots']) - 1)
            visual_rows = list(workbook['视觉资产'].values)
            headers = visual_rows[0]
            visual = next(dict(zip(headers, row)) for row in visual_rows[1:]
                if row[headers.index('image_id')] == image_id)
            self.assertEqual(visual['来源文件'], 'AI 生成图')
            self.assertIsNone(visual['来源 sheet'])
            self.assertIsNone(visual['来源行'])
            self.assertEqual(visual['content_sha256'], sha256(self.edited).hexdigest())
        finally:
            workbook.close()
        with self.f.store.connection() as con:
            decision = con.execute('SELECT source_kind, source_location_json FROM version_image_decisions_v3 WHERE project_image_id=?',
                (image_id,)).fetchone()
            self.assertEqual(decision['source_kind'], 'generated')
            self.assertEqual(json.loads(decision['source_location_json']), generated['source_location'])
            source = con.execute('SELECT origin, parent_images_json FROM project_image_generated_sources WHERE task_id=?',
                (edited['task_id'],)).fetchone()
            self.assertEqual(source['origin'], 'generated')
            self.assertEqual(json.loads(source['parent_images_json']), metadata['parents'])
        self.assertEqual(self.service.preview_image_creation(self.token, **self.scope,
            task_id=original['task_id'])[0], self.original)

        # Exercise the actual reviewed delivery snapshot, not just the version
        # workbook: generated provenance must not invent an Excel sheet or row.
        from tests.test_mvp_v2_formal_review_workspace import FormalReviewWorkspaceTests
        review = FormalReviewWorkspaceTests._review_service(
            SimpleNamespace(root=self.f.base.generated.root, base=self.f), self.f.store)
        current = review.read_current(self.token, **self.scope)
        started = review.start_review(self.token, **self.scope,
            expected_project_version=current['project_version'], idempotency_key='generated-review-start')
        approved = review.decide(self.token, **self.scope,
            expected_project_version=started['project_version'],
            issue_keys=tuple(item['issue_key'] for item in started['issues']),
            decision='approve', note='匿名受控图片 fixture 核对', idempotency_key='generated-review-all')
        confirmed = review.confirm_final(self.token, **self.scope,
            expected_project_version=approved['project_version'], note='匿名 fixture 整稿确认',
            idempotency_key='generated-review-final')
        delivery = review.prepare_excel_delivery(self.token, **self.scope,
            expected_project_version=confirmed['project_version'])
        with self.f.store.connection() as con:
            snapshot_id = con.execute('SELECT delivery_snapshot_id FROM delivery_snapshots WHERE version_id=?',
                (versions[-1]['version_id'],)).fetchone()[0]
        trace = review.delivery_snapshots.reverse_trace(self.token, project_id=self.f.project_id,
            artifact_ref=f'artifact://delivery/{snapshot_id}/excel')
        generated_trace = next(row for row in trace['source_trace'] if row['project_image_id'] == image_id)
        self.assertEqual(generated_trace['source_task_id'], edited['task_id'])
        self.assertEqual(generated_trace['source_kind'], 'generated')
        self.assertIsNone(generated_trace['source_sheet'])
        self.assertIsNone(generated_trace['source_row'])
        delivered = load_workbook(delivery.path)
        try:
            self.assertEqual(len(delivered['脚本']._images), len(initial['shots']))
            self.assertEqual(delivered['相邻镜头复核'].max_row - 1, len(initial['shots']) - 1)
        finally:
            delivered.close()
        # The persistent authority link cannot be redirected after adoption.
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'IMMUTABLE_RECORD'):
            with self.f.store.transaction() as con:
                con.execute('UPDATE project_image_generated_sources SET project_image_id=? WHERE task_id=?',
                    (initial['shots'][1]['selected_image_key'], edited['task_id']))
        unchanged = review.delivery_snapshots.reverse_trace(self.token, project_id=self.f.project_id,
            artifact_ref=f'artifact://delivery/{snapshot_id}/excel')
        self.assertEqual(unchanged['source_trace'], trace['source_trace'])
