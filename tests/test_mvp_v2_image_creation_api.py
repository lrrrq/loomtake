"""Real HTTP/database image creation journeys with anonymous, local providers."""
from hashlib import sha256
import json
import threading
import time
import unittest
from unittest.mock import patch

from mvp_v2.image_providers import ImageProviderError, ImageProviderResult
from tests import test_mvp_v2_creative_draft_api as creative_api_fixture
from tests.test_mvp_v2_image_creation import photo


class ImageCreationApiTests(unittest.TestCase):
    def test_prepared_prompt_is_persisted_and_http_submission_must_match_visible_draft(self):
        from dataclasses import replace
        current = self.context()
        self.f.generation.provider_policy = replace(self.f.generation.provider_policy, writer=lambda *_: json.dumps({
            'subject': '匿名产品', 'moment': '静置', 'scene': '桌面', 'composition': '居中', 'lighting': '自然光',
            'prompt': '匿名产品静置桌面，自然光，保留后期文字留白。', 'postproduction_text': ['片尾标语'], 'conversion_notes': ['取静态时刻']}))
        target = {'target_kind': current['target']['kind'], 'target_id': current['target']['id']}
        prepared = self.f.client.post(self.path + '/prepare', params=self.params, headers=self.f.headers,
            json={**target, 'additional_instruction': '给第2镜生图'})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        value = prepared.json()
        self.assertEqual(self.context()['prepared_prompt'], value['prepared_prompt'])
        bad = self.f.client.post(self.path, params=self.params, headers=self.f.headers, json={**target,
            'expected_project_version': value['project_version'], 'expected_creative_revision': value['creative_revision'],
            'prompt': '未保存的另一段提示词', 'expected_prompt_sha256': value['prepared_prompt']['sha256'],
            'operation_key': 'unmatched-prepared-prompt'})
        self.assertEqual(bad.status_code, 409, bad.text)
        self.assertEqual(self.calls, [])

    def setUp(self):
        self.base = creative_api_fixture.CreativeDraftApiTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.f = self.base.fixture
        self.addCleanup(self.f.client.close)
        self.addCleanup(self.f.shell_client.close)
        self.path = f"/api/formal/projects/{self.base.project['project_id']}/image-creations"
        self.params = self.base.params
        self.calls = []
        self.original = photo('orange')
        self.edited = photo('blue')
        self.upload_a, self.upload_b = photo('green'), photo('purple')
        self.draft = None

        def provider(prompt, reference_images=(), **kwargs):
            self.calls.append({'prompt': prompt, 'references': reference_images, 'options': kwargs})
            return ImageProviderResult(self.edited if len(self.calls) > 1 else self.original,
                'image/png', 2560, 1440, 'anonymous-image-provider', None,
                '2560x1440', 'anonymous-request-id', {})

        self.f.generation.image_provider = provider

    def ensure_draft(self):
        if self.draft is None:
            self.draft = self.base.generate()
        return self.base.request().json()

    def context(self):
        current = self.ensure_draft()
        result = self.f.client.get(self.path + '/context', headers=self.f.headers,
            params={**self.params, 'target_kind': 'creative_block', 'target_id': current['blocks'][1]['block_id']})
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def payload(self, key='anonymous-create-01', **changes):
        value = self.context()
        return {'expected_project_version': value['project_version'],
                'expected_creative_revision': value['creative_revision'],
                'target_kind': value['target']['kind'], 'target_id': value['target']['id'],
                'prompt': '生成一张保留真实空间关系的活动参考图', 'operation_key': key,
                'reference_image_ids': [], 'reference_task_ids': [], **changes}

    def start(self, body):
        result = self.f.client.post(self.path, params=self.params, headers=self.f.headers, json=body)
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()

    def wait(self, started):
        until = time.monotonic() + 8
        while time.monotonic() < until:
            response = self.f.client.get(self.path + '/' + started['task_id'], params=self.params, headers=self.f.headers)
            self.assertEqual(response.status_code, 200, response.text)
            item = response.json()
            if item['status'] in {'completed', 'failed'}:
                return item
            time.sleep(.01)
        self.fail('图片任务未在本地测试时限内结束')

    def preview(self, url):
        response = self.f.client.get(url, headers=self.f.headers)
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(response.headers['content-type'], 'image/png')
        self.assertIn('no-store', response.headers['cache-control'])
        self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
        return response.content

    def adoption(self, context):
        return {'expected_project_version': context['project_version'],
                'expected_creative_revision': context['creative_revision'],
                'expected_progress_revision': None, 'expected_script_revision': None,
                'target_kind': context['target']['kind'], 'target_id': context['target']['id'],
                'operation_key': 'anonymous-adoption-01'}

    def test_unconfigured_image_account_is_explicit_while_text_draft_still_completes(self):
        self.f.generation.image_provider = None
        context = self.context()
        self.assertFalse(context['available'])
        self.assertIn('账号', context['unavailable_reason'])
        self.assertEqual(len(self.ensure_draft()['blocks']), 3)
        self.assertEqual(self.base.calls, 1)
        before = self.f._table_count('tasks')
        rejected = self.f.client.post(self.path, params=self.params, headers=self.f.headers, json=self.payload())
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()['detail']['code'], 'IMAGE_PROVIDER_UNAVAILABLE')
        self.assertEqual(self.f._table_count('tasks'), before)
        self.assertEqual(self.calls, [])

    def test_json_generate_compare_adopt_and_idempotent_replay_preserve_original(self):
        first_body = self.payload()
        first = self.wait(self.start(first_body))
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['origin_label'], 'AI 生成图')
        self.assertEqual(self.preview(first['preview_url']), self.original)
        self.assertEqual(self.start(first_body)['task_id'], first['task_id'])
        self.assertEqual(len(self.calls), 1)
        mismatch = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
            json={**first_body, 'prompt': '同一操作标识不能换成另一个生成要求'})
        self.assertEqual(mismatch.status_code, 409, mismatch.text)
        self.assertEqual(mismatch.json()['detail']['code'], 'IDEMPOTENCY_CONFLICT')
        self.assertEqual(len(self.calls), 1)
        second_body = self.payload('anonymous-create-02', purpose='rough',
            prompt='生成新的粗构图，人物左右位置明确')
        second = self.wait(self.start(second_body))
        self.assertEqual(second['origin_label'], 'AI 生成图')
        self.assertEqual(self.calls[1]['references'], ())
        self.assertEqual(second['purpose'], 'rough')
        self.assertEqual(self.preview(second['preview_url']), self.edited)
        self.assertEqual(second['reference_previews'], [])
        self.assertEqual(self.preview(first['preview_url']), self.original)
        original_draft = self.ensure_draft()
        adoption = self.adoption(self.context())
        route = self.path + '/' + second['task_id'] + '/adopt'
        wrong_target = self.f.client.post(route, params=self.params, headers=self.f.headers,
            json={**adoption, 'target_id': original_draft['blocks'][0]['block_id']})
        self.assertEqual(wrong_target.status_code, 409, wrong_target.text)
        self.assertEqual(wrong_target.json()['detail']['code'], 'IMAGE_SCOPE_MISMATCH')
        adopted = self.f.client.post(route, params=self.params, headers=self.f.headers, json=adoption)
        self.assertEqual(adopted.status_code, 200, adopted.text)
        self.assertEqual(adopted.json()['status'], 'reference_saved')
        self.assertEqual(self.f.client.post(route, params=self.params, headers=self.f.headers, json=adoption).json(), adopted.json())
        after = self.ensure_draft()
        self.assertEqual(after['blocks'], original_draft['blocks'])
        self.assertEqual(len(after['image_references']), 1)
        self.assertEqual(len(self.calls), 2)
        for table in ('project_versions', 'project_images', 'version_review_human_decisions'):
            self.assertEqual(self.f._table_count(table), 0)
        public = json.dumps(self.context(), ensure_ascii=False)
        for internal in (str(self.f.root), self.f.token, 'artifact://', 'anonymous-request-id', 'anonymous-image-provider'):
            self.assertNotIn(internal, public)

    def test_new_multipart_reference_edit_is_rejected_before_task_or_provider(self):
        body = self.payload()
        count = self.f._table_count('tasks')
        files = [('reference_files', ('original-a.png', self.upload_a, 'image/png')),
                 ('reference_files', ('original-b.png', self.upload_b, 'image/png'))]
        response = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
            data={'payload': json.dumps(body)}, files=files)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()['detail']['code'], 'IMAGE_EDIT_UNAVAILABLE')
        self.assertEqual(self.f._table_count('tasks'), count)
        self.assertEqual(self.calls, [])

    def test_strict_authentication_fields_workspace_and_targets_reject_before_provider(self):
        body = self.payload()
        self.assertEqual(self.f.client.post(self.path, params=self.params, json=body).status_code, 401)
        self.assertEqual(self.f.client.get(self.path + '/context', params={**self.params,
            'target_kind': body['target_kind'], 'target_id': body['target_id']}).status_code, 401)
        for modified in ({'model': 'browser-model'}, {'expected_project_version': True},
                         {'target_kind': 'unknown'}, {'prompt': 17}):
            with self.subTest(modified=modified):
                rejected = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
                    json={**body, **modified})
                self.assertEqual(rejected.status_code, 422, rejected.text)
        wrong_workspace = self.f.client.post(self.path, params={'workspace_id': 'foreign-workspace'},
            headers=self.f.headers, json=body)
        self.assertEqual(wrong_workspace.status_code, 403, wrong_workspace.text)
        invalid_target = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
            json={**body, 'target_id': 'foreign-block'})
        self.assertEqual(invalid_target.status_code, 409, invalid_target.text)
        duplicate = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
            json={**body, 'reference_task_ids': ['same-task', 'same-task']})
        self.assertEqual(duplicate.status_code, 409, duplicate.text)
        malformed = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
            data={'payload': json.dumps({**body, 'provider': 'browser-provider'})},
            files=[('reference_files', ('bad.png', b'not-a-png', 'image/png'))])
        self.assertEqual(malformed.status_code, 422, malformed.text)
        invalid_png = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
            data={'payload': json.dumps(body)}, files=[('reference_files', ('bad.png', b'not-a-png', 'image/png'))])
        self.assertEqual(invalid_png.status_code, 409, invalid_png.text)
        self.assertEqual(self.calls, [])

    def test_foreign_project_task_cannot_be_read_previewed_referenced_or_adopted(self):
        item = self.wait(self.start(self.payload()))
        self.assertEqual(self.f.client.get(item['preview_url']).status_code, 401)
        other = self.f._project('另一个匿名项目')
        other_path = f"/api/formal/projects/{other['project_id']}/image-creations/{item['task_id']}"
        for suffix in ('', '/preview', '/references/0'):
            result = self.f.client.get(other_path + suffix, params=self.params, headers=self.f.headers)
            self.assertEqual(result.status_code, 403, result.text)
            self.assertEqual(result.json()['detail']['code'], 'CROSS_PROJECT_DENIED')
        denied = self.f.client.post(other_path + '/adopt', params=self.params,
            headers=self.f.headers, json=self.adoption(self.context()))
        self.assertEqual(denied.status_code, 403, denied.text)
        other_task = self.f.agent.services.tasks.create_agent_session(self.f.token, project_id=other['project_id'])
        foreign = self.f.agent.services.tasks.create_task(self.f.token,
            agent_session_id=other_task['agent_session_id'], task_type='formal_image_creation',
            payload_sha256='c' * 64, expected_project_version=0)
        denied = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
            json=self.payload('foreign-reference-01', reference_task_ids=[foreign['task_id']]))
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(denied.json()['detail']['code'], 'IMAGE_EDIT_UNAVAILABLE')
        self.assertEqual(len(self.calls), 1)

    def test_blob_sha_drift_rejects_preview_and_edit_reference_without_new_generation(self):
        item = self.wait(self.start(self.payload()))
        digest = sha256(self.original).hexdigest()
        target = self.f.generation.services.images.artifacts.root / 'formal-images' / digest[:2] / digest
        self.assertEqual(target.read_bytes(), self.original)
        target.write_bytes(self.original + b'anonymous-drift')
        try:
            preview = self.f.client.get(item['preview_url'], headers=self.f.headers)
            self.assertEqual(preview.status_code, 409, preview.text)
            self.assertEqual(preview.json()['detail']['code'], 'IMAGE_ARTIFACT_DRIFT')
            # Freeze the valid initial request's scope; context itself properly
            # refuses to publish a completed item whose bytes no longer match.
            draft = self.ensure_draft()
            edited = self.f.client.post(self.path, params=self.params, headers=self.f.headers,
                json={'expected_project_version': draft['project_version'], 'expected_creative_revision': draft['revision'],
                    'target_kind': 'creative_block', 'target_id': draft['blocks'][1]['block_id'],
                    'prompt': '仅调整原图光线', 'operation_key': 'tampered-reference-01', 'reference_task_ids': [item['task_id']]})
            self.assertEqual(edited.status_code, 409, edited.text)
            self.assertEqual(edited.json()['detail']['code'], 'IMAGE_EDIT_UNAVAILABLE')
            self.assertEqual(len(self.calls), 1)
        finally:
            target.write_bytes(self.original)

    def test_uncertain_provider_result_is_never_repeated_by_post_replay_or_status_reads(self):
        def failed(*args, **kwargs):
            self.calls.append('attempted')
            raise ImageProviderError('IMAGE_TRANSPORT_FAILED', 'anonymous-error', request_may_have_succeeded=True)
        self.f.generation.image_provider = failed
        body = self.payload()
        item = self.wait(self.start(body))
        self.assertEqual(item['status'], 'failed')
        self.assertTrue(item['result_uncertain'])
        self.assertFalse(item['preview_url'])
        self.assertEqual(self.start(body)['task_id'], item['task_id'])
        self.assertTrue(self.wait(item)['result_uncertain'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ensure_draft()['image_references'], [])

    def test_status_read_during_worker_completion_returns_completed_instead_of_stale_write_conflict(self):
        gate = threading.Event()
        provider = self.f.generation.image_provider
        def held(*args, **kwargs):
            if not gate.wait(5):
                raise AssertionError('anonymous provider release missing')
            return provider(*args, **kwargs)
        self.f.generation.image_provider = held
        started = self.start(self.payload())
        from mvp_v2 import creative_images
        original_is_held = creative_images.is_lock_held
        def complete_between_status_and_lock(path):
            gate.set()
            until = time.monotonic() + 5
            while original_is_held(path) and time.monotonic() < until:
                time.sleep(.005)
            return original_is_held(path)
        try:
            with patch.object(creative_images, 'is_lock_held', side_effect=complete_between_status_and_lock):
                response = self.f.client.get(self.path + '/' + started['task_id'],
                    params=self.params, headers=self.f.headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['status'], 'completed')
        finally:
            gate.set()
        self.assertEqual(len(self.calls), 1)
