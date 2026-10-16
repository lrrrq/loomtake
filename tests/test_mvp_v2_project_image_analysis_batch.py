from dataclasses import asdict, replace
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from PIL import Image

from mvp_v2 import project_image_analysis_batch as batch
from mvp_v2.formal_generation_service import FormalVisionProviderPolicy
from mvp_v2.image_understanding import ProjectImageVisionResult, VisionProviderError
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_image_authority import (
    ManualProjectImageAnalysisCommandService, ProviderProjectImageAnalysisCommandService,
    ProviderImageAnalysisInput,
)
from mvp_v2.workspace_store import WorkspaceServiceError
from mvp_v2.task_service import TaskService
from tests import test_mvp_v2_formal_generation_service as generation_fixture
from tests.test_mvp_v2_formal_agent import FormalAgentFixture
from tests.test_mvp_v2_project_image_authority import _submission


class ProjectImageAnalysisBatchTests(unittest.TestCase):
    setUp = generation_fixture.FormalGenerationServiceTests.setUp
    _confirmed_brief = generation_fixture.FormalGenerationServiceTests._confirmed_brief
    _service = generation_fixture.FormalGenerationServiceTests._service

    def _prepare(self, count=3, invoke=None):
        self.calls = []
        def provider(path):
            self.calls.append(path)
            if invoke:
                return invoke(path)
            return self._vision_result()
        self.service = self._service(vision=FormalVisionProviderPolicy(
            invoke=provider, provider='anonymous-test', model='deterministic-vision',
            prompt_version='anonymous-batch-v1', max_batch_size=2))
        self.scope = {'workspace_id': self.workspace_id, 'project_id': self.project['project_id']}
        self._add_images(count)
        self.started = batch.start(self.service, self.token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-batch-request')
        self.task_id = self.started['task']['task_id']

    def _vision_result(self):
        return ProjectImageVisionResult('pass', '匿名舞台画面', ('舞台',), ('舞台',),
            '活动空间', '舞台环境参考', '画面清晰', '未见品牌', ('画面可见舞台',))

    def _version(self):
        return self.projects.get_project(self.token, self.project['project_id'])['project_version']

    def _binding(self):
        paired = self.briefs.resolve_current_paired(self.token, **self.scope)
        return {'brief_version_id': paired.brief.brief_version_id,
                'brief_snapshot_id': paired.brief.brief_snapshot_id}

    def _add_images(self, count, offset=0):
        for index in range(offset, count + offset):
            image = Image.new('RGB', (160, 90), (20 + index * 17, 80, 140))
            stream = BytesIO()
            image.save(stream, format='PNG')
            self.service.services.images.adopt_local_upload(self.token, **self.scope,
                **self._binding(), expected_project_lock_version=self._version(),
                filename=f'anonymous-{index}.png', content=stream.getvalue())

    def _read(self):
        return batch.read(self.service, self.token, **self.scope, task_id=self.task_id)

    def _reviewer(self):
        identity = LocalIdentityAdapter(self.store)
        workspace = self.projects.get_workspace(self.token, self.workspace_id)
        actor = identity.create_actor(self.token, workspace_id=self.workspace_id,
            display_name='匿名项目同事', auth_source='local', role='reviewer',
            expected_workspace_version=workspace['workspace_version'], project_id=self.project['project_id'])
        return identity.issue_auth_session(actor_id=actor['actor_id'])['auth_token']

    def test_all_frozen_images_exceed_old_limit_and_restart_replays_one_task(self):
        self._prepare(5)
        queued = self._read()
        self.assertEqual(queued['task']['status'], 'executing')
        self.assertFalse(batch._load(self.service, self.token, self.task_id)[1]['worker_started'])
        finished = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(finished['task']['status'], 'completed')
        self.assertEqual(finished['response']['progress']['processed'], 5)
        self.assertEqual(len(self.calls), 5)
        repeated = batch.start(self.service, self.token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-batch-request')
        self.assertEqual(repeated['task']['task_id'], self.task_id)
        batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(len(self.calls), 5)
        restarted = self._service(vision=self.service.vision_provider_policy)
        restored = batch.read(restarted, self.token, **self.scope)
        self.assertEqual(restored['response']['items'], finished['response']['items'])
        self.assertNotIn('image_sha256', json.dumps(restored['response']))
        ready = self.service.services.images.resolve_for_planning(self.token, **self.scope,
            **self._binding(), expected_project_lock_version=self._version())
        for image in ready.images:
            evidence = self.service.services.images.artifacts.read_analysis(image.current_analysis.evidence)
            self.assertEqual(evidence['analysis_origin'], 'provider')
            self.assertNotIn('reviewed_by_actor_id', evidence)

    def test_pause_waits_for_inflight_result_resume_skips_success_and_new_images(self):
        def pause_first(_path):
            if len(self.calls) == 1:
                stopping = batch.pause(self.service, self.token, task_id=self.task_id)
                self.assertEqual(stopping['task']['status'], 'executing')
                self.assertIn('当前图片返回', stopping['response']['summary'])
            return self._vision_result()
        self._prepare(4, pause_first)
        stopped = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(stopped['task']['status'], 'waiting_user')
        self.assertEqual(stopped['response']['progress']['processed'], 1)
        first_receipt = stopped['response']['items'][0]['analysis_key']
        self._add_images(1, offset=8)
        self.assertEqual(self._read()['response']['new_pending_count'], 1)
        resumed = batch.resume(self.service, self.token, task_id=self.task_id)
        self.assertEqual(resumed['task']['task_id'], self.task_id)
        completed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(completed['response']['progress']['processed'], 4)
        self.assertEqual(completed['response']['items'][0]['analysis_key'], first_receipt)
        self.assertEqual(len(self.calls), 4)
        next_batch = batch.start(self.service, self.token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-next-batch')
        self.assertEqual(next_batch['response']['progress']['total'], 1)
        batch.execute(self.service, self.token, task_id=next_batch['task']['task_id'])
        self.assertEqual(len(self.calls), 5)

    def test_returned_receipt_survives_write_failure_without_second_provider_call(self):
        self._prepare(1)
        with patch.object(ProviderProjectImageAnalysisCommandService, 'record_returned_analysis',
                side_effect=WorkspaceServiceError('TEMPORARY_WRITE_FAILURE', '暂未完成写入')):
            failed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(failed['task']['status'], 'waiting_user')
        self.assertEqual(failed['response']['items'][0]['status'], 'returned')
        self.service = self._service(vision=self.service.vision_provider_policy)
        batch.resume(self.service, self.token, task_id=self.task_id)
        completed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(completed['task']['status'], 'completed')
        self.assertEqual(len(self.calls), 1)

    def test_crashed_request_is_unknown_and_never_automatically_sent_again(self):
        def crashed(_path):
            raise SystemExit('simulate worker exit after request dispatch')
        self._prepare(2, crashed)
        with self.assertRaises(SystemExit):
            batch.execute(self.service, self.token, task_id=self.task_id)
        restored = self._read()
        self.assertEqual(restored['task']['status'], 'waiting_user')
        self.assertEqual(restored['response']['progress']['unknown'], 1)
        self.service.vision_provider_policy = replace(self.service.vision_provider_policy,
            invoke=lambda path: self.calls.append(path) or self._vision_result())
        batch.resume(self.service, self.token, task_id=self.task_id)
        completed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(completed['task']['status'], 'partially_completed')
        self.assertEqual(completed['response']['progress']['unknown'], 1)
        self.assertEqual(completed['response']['progress']['processed'], 1)
        self.assertEqual(len(self.calls), 2)
        next_batch = batch.start(self.service, self.token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-next-batch')
        self.assertEqual(next_batch['response']['progress']['unknown'], 1)
        batch.execute(self.service, self.token, task_id=next_batch['task']['task_id'])
        self.assertEqual(len(self.calls), 2)

    def test_brief_changes_during_provider_then_resume_rebinds_same_returned_observation(self):
        def reconfirm(_path):
            if len(self.calls) == 1:
                self._confirmed_brief(expected_project_version=self._version(), required_content='舞台空间')
            return self._vision_result()
        self._prepare(2, reconfirm)
        original = self._binding()
        paused = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(paused['task']['status'], 'waiting_user')
        self.assertTrue(paused['response']['basis_changed'])
        self.assertEqual(paused['response']['resume_label'], '按当前需求继续')
        self.assertEqual(paused['response']['items'][0]['status'], 'returned')
        batch.resume(self.service, self.token, task_id=self.task_id)
        completed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(completed['task']['status'], 'completed')
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(completed['response']['original_binding'], original)
        self.assertEqual(completed['response']['current_binding'], self._binding())
        task, data = batch._load(self.service, self.token, self.task_id)
        self.assertEqual(len(data['binding_history']), 1)
        first = data['items'][0]
        self.assertEqual(first['receipt']['brief_version_id'], original['brief_version_id'])
        self.assertNotEqual(first['analysis_status'], 'pass')

    def test_timeout_is_unknown_even_when_wrapped_by_provider(self):
        def lost_response(_path):
            try:
                raise TimeoutError('response timed out')
            except TimeoutError as exc:
                raise RuntimeError('provider transport failed') from exc
        self._prepare(1, lost_response)
        result = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(result['task']['status'], 'partially_completed')
        self.assertEqual(result['response']['progress']['unknown'], 1)
        self.assertEqual(result['response']['progress']['failed'], 0)
        self.assertEqual(len(self.calls), 1)

    def test_new_brief_reuses_manual_and_human_confirmed_observations_without_inheriting_pass(self):
        self._prepare(2)
        _, data = batch._load(self.service, self.token, self.task_id)
        manual = ManualProjectImageAnalysisCommandService(self.service.services.images)
        first = data['items'][0]
        manual.record_analysis(self.token, **self.scope, expected_project_lock_version=self._version(),
            submission=_submission(first['image_key'], first['image_sha256'], **self._binding()))
        completed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(len(self.calls), 1)
        selections = [{'image_key': item['image_key'], 'analysis_key': item['analysis_key']}
                      for item in completed['response']['items']]
        reviewed = manual.confirm_existing_analyses(self.token, **self.scope,
            expected_project_lock_version=self._version(), selections=selections,
            note='已核对本次需求', idempotency_key='anonymous-confirm-original')
        old_ids = {analysis.analysis_version_id for analysis in reviewed}
        self._confirmed_brief(expected_project_version=self._version(), required_content='产品细节')
        new = batch.start(self.service, self.token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-rebound-batch')
        completed = batch.execute(self.service, self.token, task_id=new['task']['task_id'])
        self.assertEqual(completed['task']['status'], 'completed')
        self.assertEqual(completed['response']['progress']['needs_review'], 2)
        self.assertEqual(len(self.calls), 1, '同内容换需求不得再次调用识图')
        ready = self.service.services.images.resolve_for_planning(self.token, **self.scope,
            **self._binding(), expected_project_lock_version=self._version())
        for image in ready.images:
            value = self.service.services.images.artifacts.read_analysis(image.current_analysis.evidence)
            self.assertEqual(value['analysis_origin'], 'reused_observation')
            self.assertEqual(value['status'], 'low_confidence')
            self.assertIn(value['observation_source']['analysis_key'], old_ids)
            self.assertNotIn('human_confirmation', value)
            self.assertNotIn('reviewed_by_actor_id', value)
        new_selections = [{'image_key': item['image_key'], 'analysis_key': item['analysis_key']}
                          for item in completed['response']['items']]
        manual.confirm_existing_analyses(self.token, **self.scope,
            expected_project_lock_version=self._version(), selections=new_selections,
            note='已按新需求重新核对用途', idempotency_key='anonymous-confirm-new')
        ready = self.service.services.images.resolve_for_planning(self.token, **self.scope,
            **self._binding(), expected_project_lock_version=self._version())
        self.assertEqual(ready.passed, 2)
        self.assertTrue(all(image.business_summary.evidence_status == 'verified' for image in ready.images))

    def test_returned_command_rejects_changed_payload_before_any_analysis_write(self):
        self._prepare(1)
        with patch.object(ProviderProjectImageAnalysisCommandService, 'record_returned_analysis',
                side_effect=WorkspaceServiceError('TEMPORARY_WRITE_FAILURE', '暂未完成写入')):
            batch.execute(self.service, self.token, task_id=self.task_id)
        _, data = batch._load(self.service, self.token, self.task_id)
        item = data['items'][0]
        command = ProviderProjectImageAnalysisCommandService(self.service.services.images)
        submission = ProviderImageAnalysisInput(image_id=item['image_key'], image_sha256=item['image_sha256'],
            **self._binding(), **asdict(self._vision_result()), provider='anonymous-test',
            model='deterministic-vision', prompt_version='anonymous-batch-v1')
        with self.assertRaises(WorkspaceServiceError) as caught:
            command.record_returned_analysis(self.token, **self.scope,
                expected_project_lock_version=self._version(),
                submission=replace(submission, caption='这段内容不在原返回中'),
                observation_brief_version_id=data['brief_version_id'],
                observation_brief_snapshot_id=data['brief_snapshot_id'], source_task_id=self.task_id)
        self.assertEqual(caught.exception.code, 'IMAGE_ANALYSIS_RECEIPT_MISMATCH')
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM project_image_analysis_versions').fetchone()[0], 0)
        batch.resume(self.service, self.token, task_id=self.task_id)
        result = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(len(self.calls), 1)

    def test_old_unknown_receipt_does_not_hide_later_manual_observation_after_brief_change(self):
        self._prepare(1, lambda _path: (_ for _ in ()).throw(TimeoutError('lost response')))
        failed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(failed['response']['progress']['unknown'], 1)
        _, data = batch._load(self.service, self.token, self.task_id)
        item = data['items'][0]
        ManualProjectImageAnalysisCommandService(self.service.services.images).record_analysis(
            self.token, **self.scope, expected_project_lock_version=self._version(),
            submission=_submission(item['image_key'], item['image_sha256'], **self._binding()))
        self._confirmed_brief(expected_project_version=self._version(), required_content='舞台空间')
        new = batch.start(self.service, self.token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-after-manual')
        result = batch.execute(self.service, self.token, task_id=new['task']['task_id'])
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['response']['progress']['unknown'], 0)
        self.assertEqual(result['response']['progress']['needs_review'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_failed_old_analysis_is_not_downgraded_into_a_confirmable_observation(self):
        self._prepare(1)
        _, original = batch._load(self.service, self.token, self.task_id)
        image = original['items'][0]
        ManualProjectImageAnalysisCommandService(self.service.services.images).record_analysis(
            self.token, **self.scope, expected_project_lock_version=self._version(),
            submission=replace(_submission(image['image_key'], image['image_sha256'], **self._binding()),
                               status='failed'))
        batch.execute(self.service, self.token, task_id=self.task_id)
        self._confirmed_brief(expected_project_version=self._version(), required_content='产品细节')
        _, data = batch._load(self.service, self.token, self.task_id)
        item = data['items'][0]
        reused = self.service.services.images.reuse_analysis_for_current_brief(self.token, **self.scope,
            image_id=item['image_key'], image_sha256=item['image_sha256'], **self._binding(),
            expected_project_lock_version=self._version())
        self.assertIsNone(reused)
        new = batch.start(self.service, self.token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-after-failed')
        result = batch.execute(self.service, self.token, task_id=new['task']['task_id'])
        self.assertEqual(result['response']['items'][0]['analysis_status'], 'pass')
        self.assertEqual(len(self.calls), 1)

    def test_material_task_projections_keep_every_state_in_the_material_workspace(self):
        for status in ('received', 'executing', 'waiting_user', 'completed', 'partially_completed', 'failed', 'blocked'):
            with self.subTest(status=status):
                view = TaskService.employee_task_fields({'task_type': 'formal_image_analysis', 'status': status}, 'anonymous')
                self.assertEqual(view['title'], '素材分析')
                self.assertEqual(view['action_href'], '/projects/anonymous/materials')
                self.assertNotIn('规划', view['next_action'])

    def test_single_image_candidate_sync_keeps_its_original_terminal_receipt(self):
        fixture = FormalAgentFixture(self.root / 'single-image-agent')
        task = fixture.tasks.create_task(fixture.token,
            agent_session_id=fixture.session['agent_session_id'], task_type='formal_image_candidates',
            payload_sha256='a' * 64, expected_project_version=fixture.project['project_version'])
        response = {'type': 'image_candidates', 'summary': '已找到单镜真实候选', 'candidates': []}
        for status in ('executing', 'completed'):
            task = fixture.tasks.transition_task(fixture.token, task_id=task['task_id'],
                expected_task_version=task['task_version'], status=status, details={'response': response})
        result = fixture.agent.sync_task(fixture.token, task['task_id'])
        self.assertEqual(result['task']['task_id'], task['task_id'])
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['response']['summary'], response['summary'])

    def test_two_members_frozen_before_dispatch_share_one_image_request(self):
        def competing_member(_path):
            self.competing = batch.execute(self.service, self.colleague_token, task_id=self.colleague_task)
            return self._vision_result()
        self._prepare(1, competing_member)
        self.colleague_token = self._reviewer()
        colleague = batch.start(self.service, self.colleague_token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-colleague-request')
        self.colleague_task = colleague['task']['task_id']
        completed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(completed['task']['status'], 'completed')
        self.assertEqual(self.competing['task']['status'], 'waiting_user')
        self.assertIn('其他批次', self.competing['response']['summary'])
        batch.resume(self.service, self.colleague_token, task_id=self.colleague_task)
        other_result = batch.execute(self.service, self.colleague_token, task_id=self.colleague_task)
        self.assertEqual(other_result['task']['status'], 'completed')
        self.assertEqual(other_result['response']['items'][0]['analysis_key'],
                         completed['response']['items'][0]['analysis_key'])
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(WorkspaceServiceError) as caught:
            batch.read(self.service, self.colleague_token, **self.scope, task_id=self.task_id)
        self.assertEqual(caught.exception.code, 'PERMISSION_DENIED')

    def test_second_member_does_not_resend_first_members_unknown_request(self):
        self._prepare(1, lambda _path: (_ for _ in ()).throw(TimeoutError('lost response')))
        colleague_token = self._reviewer()
        colleague = batch.start(self.service, colleague_token, **self.scope,
            expected_project_version=self._version(), idempotency_key='anonymous-colleague-request')
        batch.execute(self.service, self.token, task_id=self.task_id)
        other = batch.execute(self.service, colleague_token, task_id=colleague['task']['task_id'])
        self.assertEqual(other['response']['progress']['unknown'], 1)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('source_task_id', other['response']['items'][0])

    def test_restart_before_worker_starts_returns_same_batch_to_resumable_state(self):
        self._prepare(2)
        with patch.object(batch, '_PROCESS_OWNER', {'pid': 99999999, 'instance_id': 'new-test-process'}):
            recovered = self._read()
            self.assertEqual(recovered['task']['status'], 'waiting_user')
            self.assertIn('执行尚未开始', recovered['response']['summary'])
            self.assertEqual(len(self.calls), 0)
            resumed = batch.resume(self.service, self.token, task_id=self.task_id)
            self.assertEqual(resumed['task']['task_id'], self.task_id)
            finished = batch.execute(self.service, self.token, task_id=self.task_id)
            self.assertEqual(finished['task']['status'], 'completed')
            self.assertEqual(len(self.calls), 2)

    def test_provider_cannot_overwrite_human_confirmation_arriving_during_call(self):
        def human_confirms(_path):
            task, data = batch._load(self.service, self.token, self.task_id)
            item = next(item for item in data['items'] if item['status'] == 'in_flight')
            submission = _submission(item['image_key'], item['image_sha256'],
                data['brief_version_id'], data['brief_snapshot_id'])
            self.human = ManualProjectImageAnalysisCommandService(self.service.services.images).record_analysis(
                self.token, **self.scope, expected_project_lock_version=self._version(), submission=submission)
            return self._vision_result()
        self._prepare(1, human_confirms)
        completed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(completed['response']['items'][0]['analysis_key'], self.human.analysis_version_id)
        with self.store.connection() as connection:
            count = connection.execute('SELECT COUNT(*) FROM project_image_analysis_versions').fetchone()[0]
        self.assertEqual(count, 1)

    def test_pause_before_dispatch_does_not_call_provider(self):
        self._prepare(2)
        original = self.service.services.images.resolve_pending_for_provider
        def pause_at_boundary(*args, **kwargs):
            pending = original(*args, **kwargs)
            batch.pause(self.service, self.token, task_id=self.task_id)
            return pending
        with patch.object(self.service.services.images, 'resolve_pending_for_provider', side_effect=pause_at_boundary):
            stopped = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(stopped['task']['status'], 'waiting_user')
        self.assertEqual(stopped['response']['progress']['pending'], 2)
        self.assertEqual(self.calls, [])

    def test_partial_provider_failure_preserves_success_and_task_scope(self):
        def partly_fails(_path):
            if len(self.calls) == 2:
                raise ValueError('anonymous provider parse failure')
            return self._vision_result()
        self._prepare(3, partly_fails)
        completed = batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(completed['task']['status'], 'partially_completed')
        self.assertEqual(completed['response']['progress']['processed'], 2)
        self.assertEqual(completed['response']['progress']['failed'], 1)
        with self.assertRaises(WorkspaceServiceError) as caught:
            batch.read(self.service, self.token, workspace_id=self.workspace_id,
                project_id='another-project', task_id=self.task_id)
        self.assertEqual(caught.exception.code, 'CROSS_PROJECT_DENIED')
        with self.assertRaises(WorkspaceServiceError) as caught:
            batch.start(self.service, self.token, **self.scope,
                expected_project_version=self._version() + 1, idempotency_key='anonymous-batch-request')
        self.assertEqual(caught.exception.code, 'IDEMPOTENCY_CONFLICT')

    def test_invalid_return_stays_failed_and_explicit_new_batch_only_retries_failed_image(self):
        def invalid_second(_path):
            if len(self.calls) == 2:
                error = VisionProviderError('invalid returned JSON')
                error.diagnostics = {'error': {'code': 'VISION_RESPONSE_PARSE_FAILED'},
                    'raw_response': 'private provider output must not enter the public reply'}
                raise error
            return self._vision_result()
        self._prepare(3, invalid_second)
        finished = batch.execute(self.service, self.token, task_id=self.task_id)
        failed = [item for item in finished['response']['items'] if item['status'] == 'failed']
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['error_code'], 'VISION_RESPONSE_PARSE_FAILED')
        self.assertIn('原图与已选图保留', failed[0]['message'])
        self.assertIn('新一批素材分析', failed[0]['message'])
        self.assertNotIn('private provider output', json.dumps(finished))
        batch.execute(self.service, self.token, task_id=self.task_id)
        self.assertEqual(len(self.calls), 3)
        restarted = batch.start(self.service, self.token, **self.scope,
            expected_project_version=self._version(), idempotency_key='explicit-format-retry')
        self.assertEqual(restarted['response']['progress']['total'], 1)
        retried = batch.execute(self.service, self.token, task_id=restarted['task']['task_id'])
        self.assertEqual(retried['task']['status'], 'completed')
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self._read()['response']['items'], finished['response']['items'])


if __name__ == '__main__':
    unittest.main()
