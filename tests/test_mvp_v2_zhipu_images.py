import json
import unittest
from unittest.mock import patch

from mvp_v2 import image_providers as images
from tests.test_mvp_v2_image_providers import picture


class ZhipuImageTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict('os.environ', {'ZHIPU_API_KEY': 'anonymous-zhipu-secret'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.requests = []
        self.url = 'https://sfile.chatglm.cn/anonymous.png'
        self.content = picture((1792, 1008))

    def transport(self, request, **kwargs):
        self.requests.append(request)
        if request.get_method() == 'POST':
            return images.ImageHTTPResponse(200, {'Content-Type': 'application/json'},
                json.dumps({'created': 123, 'data': [{'url': self.url}]}).encode())
        return images.ImageHTTPResponse(200, {'Content-Type': 'image/png'}, self.content)

    def test_purposes_use_documented_model_parameters_and_download_without_auth(self):
        for purpose, model, size, quality in [('storyboard', 'cogview-4-250304', '1792x1008', 'standard'),
                ('complex', 'glm-image', '2048x1152', 'hd'), ('rough', 'cogview-3-flash', '1792x1008', 'standard')]:
            with self.subTest(purpose=purpose):
                self.requests.clear()
                self.content = picture(tuple(map(int, size.split('x'))))
                result = images.zhipu_http_provider(transport=self.transport)('匿名画面', purpose=purpose)
                payload = json.loads(self.requests[0].data)
                self.assertEqual((payload['model'], payload['size'], payload['quality']), (model, size, quality))
                self.assertEqual(self.requests[0].full_url, images.ZHIPU_ENDPOINT)
                self.assertNotIn('image', payload)
                self.assertNotIn('n', payload)
                self.assertFalse(payload['watermark_enabled'])
                self.assertIsNone(self.requests[1].get_header('Authorization'))
                self.assertEqual(result.content, self.content)
                self.assertEqual(result.requested_model, model)
                self.assertIsNone(result.actual_model)

    def test_unsupported_edits_sizes_and_glm_long_prompt_fail_before_request(self):
        provider = images.zhipu_http_provider(transport=self.transport)
        for kwargs in [{'reference_images': (self.content,)}, {'size': '2K'}, {'purpose': 'unknown'},
                       {'size': '2048x2048'}, {'purpose': 'complex', 'size': '1792x1008'}]:
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(images.ImageProviderError):
                provider('匿名', **kwargs)
        with self.assertRaises(images.ImageProviderError):
            provider('文' * 1001, purpose='complex')
        self.assertEqual(self.requests, [])

    def test_failure_is_not_retried_or_rerouted(self):
        def fail(request, **kwargs):
            self.requests.append(request)
            raise OSError('anonymous-zhipu-secret')
        with self.assertRaises(images.ImageProviderError) as caught:
            images.zhipu_http_provider(transport=fail)('匿名', purpose='complex')
        self.assertTrue(caught.exception.request_may_have_succeeded)
        self.assertNotIn('anonymous-zhipu-secret', str(caught.exception))
        self.assertEqual(len(self.requests), 1)

    def test_rejected_download_address_records_only_safe_shape_and_never_downloads(self):
        self.url = 'http://sfile.chatglm.cn/private.png?signature=anonymous-secret'
        with self.assertRaises(images.ImageProviderError) as caught:
            images.zhipu_http_provider(transport=self.transport)('匿名')
        self.assertEqual(caught.exception.code, 'IMAGE_DOWNLOAD_DENIED')
        issue = caught.exception.download_address_issue
        self.assertEqual(issue['scheme'], 'http')
        self.assertEqual(issue['value_type'], 'string')
        self.assertNotIn('anonymous-secret', json.dumps(issue))
        self.assertNotIn('sfile', json.dumps(issue))
        self.assertEqual([r.get_method() for r in self.requests], ['POST'])

    def test_mislabeled_png_header_uses_decoded_jpeg_as_actual_mime(self):
        # Observed in the real free-model response: image/png carried JPEG.
        self.content = picture((1792, 1008), format='JPEG')
        result = images.zhipu_http_provider(transport=self.transport)('匿名')
        self.assertEqual(result.mime_type, 'image/jpeg')
        self.assertEqual(result.content, self.content)

    def test_receipt_is_available_before_download_failure_and_recovers_without_post(self):
        receipts = []
        def interrupted(request, **kwargs):
            if request.get_method() == 'GET':
                raise OSError('download interrupted')
            return self.transport(request, **kwargs)
        with self.assertRaises(images.ImageProviderError):
            images.zhipu_http_provider(transport=interrupted)('匿名', response_observer=receipts.append)
        self.assertEqual(len(receipts), 1)
        self.requests.clear()
        recovered = images.zhipu_result_from_receipt(receipts[0], transport=self.transport)
        self.assertEqual(recovered.content, self.content)
        self.assertEqual([request.get_method() for request in self.requests], ['GET'])

    def test_return_url_requires_public_https_and_rejects_redirects(self):
        for url in ['http://sfile.chatglm.cn/a', 'https://127.0.0.1/a', 'https://localhost/a',
                    'https://user:password@sfile.chatglm.cn/a', 'https://sfile.chatglm.cn/a#fragment']:
            self.url = url
            self.requests.clear()
            with self.subTest(url=url), self.assertRaises(images.ImageProviderError):
                images.zhipu_http_provider(transport=self.transport)('匿名')
            self.assertEqual(len(self.requests), 1)

    def test_complex_route_never_implicitly_uses_free_model(self):
        self.assertEqual(images.image_creation_purpose({'visual': '三格画面，多主体左右关系，指定文字“向前”'}), 'complex')
        self.assertEqual(images.image_creation_purpose({'visual': '普通会场全景'}), 'storyboard')
