from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
import unittest
from unittest.mock import patch

from PIL import Image

from mvp_v2.image_providers import ImageProviderError, ImageProviderResult, ImageHTTPResponse, zhipu_http_provider
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_generation as creative_fixture


def photo(color):
    out = BytesIO()
    Image.new('RGB', (2560, 1440), color).save(out, format='PNG')
    return out.getvalue()


class ImageCreationTests(unittest.TestCase):
    def setUp(self):
        self.base = creative_fixture.CreativeGenerationTests('runTest')
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.service, self.token, self.scope = self.base.service, self.base.fixture.token, self.base.scope
        self.base.execute(self.base.start())
        self.calls = []
        def provider(prompt, reference_images=(), **kwargs):
            self.calls.append((prompt, reference_images))
            content = photo('blue' if reference_images else 'orange')
            return ImageProviderResult(content, 'image/png', 2560, 1440, 'anonymous-image', None,
                                       '2560x1440', 'anonymous-request', {})
        self.service.image_provider = provider

    def current(self):
        return self.service.read_creative_draft(self.token, **self.scope)

    def start(self, key='image-operation-01', **kwargs):
        current = self.current()
        return self.service.start_image_creation(self.token, **self.scope,
            expected_project_version=current['project_version'], expected_creative_revision=current['revision'],
            target_kind='creative_block', target_id=current['blocks'][1]['block_id'],
            prompt='为当前画面生成一张参考图，不出现虚构品牌文字', operation_key=key,
            start_background=False, **kwargs)

    def execute(self, started):
        self.service.execute_image_creation(self.token, **self.scope, task_id=started['task_id'])
        return self.service.read_image_creation(self.token, **self.scope, task_id=started['task_id'])

    def test_adopted_initial_reference_enters_project_pool_without_formal_version_or_review(self):
        started = self.start()
        first = self.execute(started)
        self.assertEqual(first['status'], 'completed')
        first_bytes, _ = self.service.preview_image_creation(self.token, **self.scope, task_id=first['task_id'])
        second = self.execute(self.start('image-operation-02', purpose='rough'))
        self.assertEqual(second['origin_label'], 'AI 生成图')
        self.assertEqual(self.calls[1][1], ())
        self.assertEqual(self.service.preview_image_creation(self.token, **self.scope, task_id=first['task_id'])[0], first_bytes)
        current = self.current()
        adopted = self.service.adopt_image_creation(self.token, **self.scope, task_id=second['task_id'],
            target_kind='creative_block', target_id=current['blocks'][1]['block_id'],
            expected_project_version=current['project_version'], expected_creative_revision=current['revision'],
            operation_key='adopt-reference-01')
        self.assertEqual(adopted['status'], 'reference_saved')
        self.assertEqual(self.current()['blocks'], current['blocks'])
        self.assertEqual(len(self.current()['image_references']), 1)
        with self.base.fixture.store.connection() as con:
            for table in ('project_versions', 'version_review_human_decisions'):
                self.assertEqual(con.execute('SELECT count(*) FROM '+table).fetchone()[0], 0)
            self.assertEqual(con.execute('SELECT count(*) FROM project_images').fetchone()[0], 1)
            source = con.execute('SELECT task_id, origin, result_ref FROM project_image_generated_sources').fetchone()
            self.assertEqual(source['task_id'], second['task_id'])
            self.assertEqual(source['origin'], 'generated')
            self.assertEqual(source['result_ref'], self.service.services.tasks.get_task(self.token, second['task_id'])['result_ref'])

    def test_uncertain_provider_request_cannot_be_automatically_repeated(self):
        def fail(*args, **kwargs):
            self.calls.append(1)
            raise ImageProviderError('IMAGE_TRANSPORT_FAILED', '结果未知', request_may_have_succeeded=True)
        self.service.image_provider = fail
        started = self.start()
        result = self.execute(started)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['result_uncertain'])
        self.execute(started)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.current()['image_references'], [])

    def test_missing_account_is_explicit_and_does_not_create_tasks(self):
        self.service.image_provider = None
        current = self.current()
        context = self.service.image_creation_context(self.token, **self.scope,
            target_kind='creative_block', target_id=current['blocks'][0]['block_id'])
        self.assertFalse(context['available'])
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, 'IMAGE_PROVIDER_UNAVAILABLE')
        self.assertEqual(self.current()['revision'], current['revision'])

    def test_new_image_requests_reject_reference_editing_before_task_creation(self):
        before = self.current()
        with self.base.fixture.store.connection() as con:
            count = con.execute('SELECT count(*) FROM tasks').fetchone()[0]
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.start(reference_uploads=[('anonymous.png', photo('white'))])
        self.assertEqual(caught.exception.code, 'IMAGE_EDIT_UNAVAILABLE')
        with self.base.fixture.store.connection() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM tasks').fetchone()[0], count)
        self.assertEqual(self.current()['revision'], before['revision'])

    def test_context_declares_text_only_capability_and_generation_purpose(self):
        current = self.current()
        context = self.service.image_creation_context(self.token, **self.scope,
            target_kind='creative_block', target_id=current['blocks'][0]['block_id'])
        self.assertFalse(context['capabilities']['image_edit'])
        self.assertEqual([p['purpose'] for p in context['capabilities']['purposes']], ['storyboard', 'complex', 'rough'])
        self.assertIn(context['suggested_purpose'], ('storyboard', 'complex'))

    def test_original_task_recovers_download_without_repeating_generation(self):
        methods = []
        available = False
        def transport(request, **kwargs):
            methods.append(request.get_method())
            if request.get_method() == 'POST':
                return ImageHTTPResponse(200, {'Content-Type': 'application/json'},
                    json.dumps({'data': [{'url': 'https://sfile.chatglm.cn/anonymous.png'}]}).encode())
            if not available:
                raise OSError('anonymous download outage')
            return ImageHTTPResponse(200, {'Content-Type': 'image/png'}, photo('orange'))
        with patch.dict('os.environ', {'ZHIPU_API_KEY': 'anonymous-zhipu-secret'}):
            self.service.image_provider = zhipu_http_provider(transport=transport)
        started = self.start()
        pending = self.execute(started)
        self.assertEqual(pending['status'], 'waiting_user')
        self.assertTrue(pending['download_pending'])
        available = True
        recovered = self.service.read_image_creation(self.token, **self.scope, task_id=started['task_id'])
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(methods.count('POST'), 1)
        self.assertEqual(self.service.preview_image_creation(self.token, **self.scope, task_id=started['task_id'])[0], photo('orange'))

    def test_historical_edited_result_and_its_original_remain_readable_without_provider(self):
        from mvp_v2 import creative_images
        from mvp_v2.run_contract import sha256_json
        tasks = self.service.services.tasks
        state = self.current()
        target_id = state['blocks'][1]['block_id']
        context = self.service.image_creation_context(self.token, **self.scope,
            target_kind='creative_block', target_id=target_id)
        original, edited = photo('white'), photo('blue')
        original_ref, _, _, _ = creative_images._freeze_image(self.service, original)
        edited_ref, mime, width, height = creative_images._freeze_image(self.service, edited)
        session = tasks.create_agent_session(self.token, project_id=self.scope['project_id'])
        task = tasks.create_task(self.token, agent_session_id=session['agent_session_id'],
            task_type='formal_image_creation', payload_sha256='a' * 64,
            expected_project_version=state['project_version'])
        request = {**self.scope, 'target': context['target'], 'prompt': '历史编辑要求',
            'references': [{'image': original_ref, 'label': '原图'}],
            'target_fields_sha256': context['target_basis'], 'requested_model': 'historical-provider'}
        task = tasks.append_task_event(self.token, task_id=task['task_id'], expected_task_version=task['task_version'],
            status='executing', details={'image_request': request})
        metadata = {'schema': 'mplus-image-creation/v1', 'task_id': task['task_id'],
            'request_sha256': 'a' * 64, 'image': edited_ref, 'mime_type': mime,
            'width': width, 'height': height, 'origin': 'edited', 'parents': request['references']}
        artifact = creative_images._metadata_store(self.service).write(json.dumps(metadata).encode())
        tasks.append_task_event(self.token, task_id=task['task_id'], expected_task_version=task['task_version'],
            status='completed', result_ref=artifact.ref)
        self.service.image_provider = None
        result = self.service.read_image_creation(self.token, **self.scope, task_id=task['task_id'])
        self.assertEqual(result['origin_label'], 'AI 编辑图')
        self.assertEqual(self.service.preview_image_creation(self.token, **self.scope, task_id=task['task_id'])[0], edited)
        self.assertEqual(self.service.preview_image_creation(self.token, **self.scope,
            task_id=task['task_id'], reference_index=0)[0], original)
        self.assertEqual(self.calls, [])
