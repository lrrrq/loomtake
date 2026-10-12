import base64
import json
import socket
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from mvp_v2.image_providers import (
    DEFAULT_SEEDREAM_MODEL, ImageHTTPResponse, ImageProviderError,
    MAX_IMAGE_BYTES, MAX_JSON_BYTES, MAX_REFERENCE_BYTES, MAX_TOTAL_REFERENCE_BYTES,
    SEEDREAM_ENDPOINT, _PinnedHTTPSConnection, _https_transport, _public_address, seedream_http_provider,
)


def picture(size=(64, 32), format="PNG"):
    output = BytesIO()
    Image.new("RGB", size, (29, 87, 151)).save(output, format=format)
    return output.getvalue()


class SeedreamHTTPProviderTests(unittest.TestCase):
    def setUp(self):
        self.key = "anonymous-ark-test-secret"
        self.environment = patch.dict("os.environ", {"ARK_API_KEY": self.key})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.requests = []
        self.content = picture()
        self.payload = {"model": DEFAULT_SEEDREAM_MODEL, "data": [{
            "b64_json": base64.b64encode(self.content).decode(), "size": "64x32", "output_format": "png"}],
            "usage": {"input_images": 0, "generated_images": 1, "output_tokens": 123, "total_tokens": 123}}
        self.responses = []

    def transport(self, request, *, timeout_seconds, max_bytes):
        self.requests.append((request, timeout_seconds, max_bytes))
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response
        return ImageHTTPResponse(200, {"Content-Type": "application/json", "X-Request-Id": "request-fixture"},
            json.dumps(self.payload).encode())

    def provider(self, **kwargs):
        return seedream_http_provider(transport=self.transport, **kwargs)

    def request_body(self):
        return json.loads(self.requests[0][0].data)

    def test_single_generation_has_controlled_payload_and_actual_evidence(self):
        result = self.provider()("生成匿名活动主视觉")
        request, timeout, limit = self.requests[0]
        self.assertEqual(request.full_url, SEEDREAM_ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.key)
        self.assertEqual(timeout, 180)
        self.assertEqual(limit, MAX_JSON_BYTES)
        payload = self.request_body()
        self.assertEqual(payload["model"], DEFAULT_SEEDREAM_MODEL)
        self.assertEqual(payload["size"], "2560x1440")
        self.assertEqual(payload["sequential_image_generation"], "disabled")
        self.assertEqual(payload["response_format"], "b64_json")
        self.assertEqual(payload["output_format"], "png")
        self.assertFalse(payload["stream"])
        self.assertNotIn("image", payload)
        self.assertNotIn("n", payload)
        self.assertNotIn("tools", payload)
        self.assertEqual(result.content, self.content)
        self.assertEqual((result.width, result.height, result.mime_type), (64, 32, "image/png"))
        self.assertEqual(result.requested_model, DEFAULT_SEEDREAM_MODEL)
        self.assertEqual(result.actual_model, DEFAULT_SEEDREAM_MODEL)
        self.assertEqual(result.request_id, "request-fixture")
        self.assertEqual(result.usage, self.payload["usage"])
        self.assertNotIn(self.key, repr(result))

    def test_edit_uses_verified_data_urls_in_reference_order_and_preserves_ratio(self):
        references = (picture((32, 64)), picture((80, 40), "JPEG"))
        result = self.provider()("参考图1构图与图2材质，保持主体位置", references)
        payload = self.request_body()
        self.assertIsInstance(payload["image"], list)
        for value, expected, mime in zip(payload["image"], references, ("png", "jpeg")):
            self.assertTrue(value.startswith("data:image/" + mime + ";base64,"))
            self.assertEqual(base64.b64decode(value.split(",", 1)[1]), expected)
        width, height = map(int, payload["size"].split("x"))
        self.assertAlmostEqual(width / height, .5, delta=.002)
        self.assertGreaterEqual(width * height, 3686400)
        self.assertEqual(result.requested_size, payload["size"])

    def test_single_reference_is_string_and_explicit_size_is_validated(self):
        self.provider()("修改匿名图片", (picture(),), size="2560x1440", aspect_ratio="16:9")
        self.assertIsInstance(self.request_body()["image"], str)
        self.assertEqual(self.request_body()["size"], "2560x1440")
        for size, ratio in (("1024x1024", None), ("9000x9000", None), ("2560x1440", "1:1"),
                            ("automatic", None), ("2K", "unbounded")):
            with self.subTest(size=size, ratio=ratio), self.assertRaises(ImageProviderError):
                self.provider()("匿名图片", size=size, aspect_ratio=ratio)
        self.assertEqual(len(self.requests), 1)

    def test_all_supported_resolution_and_aspect_presets_stay_inside_model_bounds(self):
        for size in ("2K", "3K", "4K"):
            for aspect in ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "21:9"):
                with self.subTest(size=size, aspect=aspect):
                    self.provider()("匿名图片", size=size, aspect_ratio=aspect)
                    width, height = map(int, json.loads(self.requests[-1][0].data)["size"].split("x"))
                    self.assertGreaterEqual(width * height, 3686400)
                    self.assertLessEqual(width * height, 16777216)
        self.provider()("多图参考", tuple(picture() for _ in range(14)))
        self.assertEqual(len(json.loads(self.requests[-1][0].data)["image"]), 14)

    def test_missing_upstream_metadata_is_not_filled_from_request(self):
        self.payload.pop("model")
        self.payload.pop("usage")
        self.responses.append(ImageHTTPResponse(200, {"Content-Type": "application/json"}, json.dumps(self.payload).encode()))
        result = self.provider()("匿名图片")
        self.assertIsNone(result.actual_model)
        self.assertIsNone(result.request_id)
        self.assertEqual(result.usage, {})
        self.assertEqual(result.requested_model, DEFAULT_SEEDREAM_MODEL)

    def test_reference_limits_and_bad_images_fail_before_http(self):
        invalid = [tuple([picture()] * 15), (b"not-an-image",), (picture((14, 30)),),
            (picture((16, 300)),), (picture(format="GIF"),), ("https://127.0.0.1/private",)]
        for references in invalid:
            with self.subTest(references_count=len(references)), self.assertRaises(ImageProviderError) as caught:
                self.provider()("匿名修改", references)
            self.assertFalse(caught.exception.request_may_have_succeeded)
        with patch("mvp_v2.image_providers.MAX_REFERENCE_BYTES", 8), self.assertRaises(ImageProviderError):
            self.provider()("匿名修改", (picture(),))
        with patch("mvp_v2.image_providers.MAX_TOTAL_REFERENCE_BYTES", len(picture()) + 1), self.assertRaises(ImageProviderError):
            self.provider()("匿名修改", (picture(), picture()))
        self.assertEqual(self.requests, [])

    def test_malformed_business_inputs_fail_without_http(self):
        for arguments in ({"prompt": "\ud800"}, {"prompt": "匿名图片", "aspect_ratio": []},
                          {"prompt": "匿名图片", "size": 123}):
            with self.subTest(arguments=list(arguments)), self.assertRaises(ImageProviderError):
                self.provider()(**arguments)
        self.assertEqual(self.requests, [])

    def test_http_errors_and_transport_failures_never_retry_or_expose_secret(self):
        for response in (ImageHTTPResponse(429, {}, b'{"error":{"message":"' + self.key.encode() + b'"}}'),
                         ImageHTTPResponse(503, {}, b"temporary error"), OSError(self.key)):
            self.responses = [response]
            before = len(self.requests)
            with self.assertRaises(ImageProviderError) as caught:
                self.provider()("匿名图片")
            self.assertEqual(len(self.requests), before + 1)
            self.assertNotIn(self.key, str(caught.exception))
            self.assertTrue(caught.exception.request_may_have_succeeded)

    def test_invalid_api_response_is_bounded_and_never_retried(self):
        cases = [ImageHTTPResponse(200, {"Content-Type": "text/html"}, b"<html>bad</html>"),
            ImageHTTPResponse(200, {"Content-Type": "application/json"}, b"[1]"),
            ImageHTTPResponse(200, {"Content-Type": "application/json"}, b"not-json")]
        for response in cases:
            self.responses = [response]
            with self.assertRaises(ImageProviderError):
                self.provider()("匿名图片")
        with patch("mvp_v2.image_providers.MAX_JSON_BYTES", 10), self.assertRaises(ImageProviderError):
            self.provider()("匿名图片")

    def test_bad_image_mismatched_metadata_and_extra_outputs_are_rejected(self):
        original = dict(self.payload["data"][0])
        for change in ({"b64_json": "not-base64"}, {"b64_json": base64.b64encode(b"not an image").decode()},
                       {"size": "2048x2048"}, {"output_format": "jpeg"}, {"error": {"message": self.key}}):
            self.payload["data"] = [{**original, **change}]
            with self.subTest(change=list(change)), self.assertRaises(ImageProviderError):
                self.provider()("匿名图片")
        self.payload["data"] = [original, original]
        with self.assertRaises(ImageProviderError):
            self.provider()("匿名图片")
        self.payload["data"] = [original]
        with patch("mvp_v2.image_providers.MAX_IMAGE_BYTES", 5), self.assertRaises(ImageProviderError):
            self.provider()("匿名图片")

    def test_url_fallback_requires_exact_official_host_and_never_carries_credentials(self):
        host = "anonymous-result.tos-cn-beijing.volces.com"
        url = "https://" + host + "/image.png?X-Tos-Signature=anonymous"
        self.payload["data"] = [{"url": url, "size": "64x32", "output_format": "png"}]
        with self.assertRaises(ImageProviderError):
            self.provider()("匿名图片")
        self.responses = [self.transport_response(), ImageHTTPResponse(200, {"Content-Type": "image/png"}, self.content)]
        result = self.provider(allowed_download_hosts=(host,))("匿名图片")
        request, _, limit = self.requests[-1]
        self.assertEqual(request.full_url, url)
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertIsNone(request.get_header("Referer"))
        self.assertEqual(limit, MAX_IMAGE_BYTES)
        self.assertEqual(result.content, self.content)

    def test_url_fallback_rejects_wrong_mime_and_oversized_body(self):
        host = "anonymous-result.tos-cn-beijing.volces.com"
        self.payload["data"] = [{"url": "https://" + host + "/image.png"}]
        for response in (ImageHTTPResponse(200, {"Content-Type": "text/html"}, self.content),
                         ImageHTTPResponse(200, {"Content-Type": "image/jpeg"}, self.content)):
            self.responses = [self.transport_response(), response]
            with self.assertRaises(ImageProviderError):
                self.provider(allowed_download_hosts=(host,))("匿名图片")
        self.responses = [self.transport_response(), ImageHTTPResponse(200, {"Content-Type": "image/png"}, self.content)]
        with patch("mvp_v2.image_providers.MAX_IMAGE_BYTES", 5), self.assertRaises(ImageProviderError):
            self.provider(allowed_download_hosts=(host,))("匿名图片")

    def transport_response(self):
        return ImageHTTPResponse(200, {"Content-Type": "application/json"}, json.dumps(self.payload).encode())

    def test_unsafe_return_urls_and_redirects_are_not_followed(self):
        host = "anonymous-result.tos-cn-beijing.volces.com"
        urls = ["http://" + host + "/image.png", "https://127.0.0.1/image.png", "https://" + host + ".evil.test/image.png",
            "https://user:pass@" + host + "/image.png", "https://@" + host + "/image.png", "https://" + host + ":8443/image.png",
            "https://" + host + "/image.png#fragment", "file:///private/secret"]
        for url in urls:
            self.payload["data"] = [{"url": url}]
            before = len(self.requests)
            with self.subTest(url=url), self.assertRaises(ImageProviderError):
                self.provider(allowed_download_hosts=(host,))("匿名图片")
            self.assertEqual(len(self.requests), before + 1)
        self.payload["data"] = [{"url": "https://" + host + "/image.png"}]
        self.responses = [self.transport_response(), ImageHTTPResponse(302, {"Location": "http://127.0.0.1"}, b"")]
        before = len(self.requests)
        with self.assertRaises(ImageProviderError):
            self.provider(allowed_download_hosts=(host,))("匿名图片")
        self.assertEqual(len(self.requests), before + 2)

    def test_credentials_and_configuration_are_server_owned(self):
        with patch.dict("os.environ", {"ARK_API_KEY": ""}), self.assertRaises(ImageProviderError):
            self.provider()
        with self.assertRaises(ImageProviderError):
            self.provider(allowed_download_hosts=("attacker.test",))
        with self.assertRaises(TypeError):
            self.provider()("匿名图片", model="client-selected-model")
        with self.assertRaises(TypeError):
            self.provider(base_url="https://attacker.test")
        self.assertEqual(self.requests, [])

    def test_dns_private_or_mixed_answers_are_denied(self):
        def addr(ip):
            return (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
        for ips in (["127.0.0.1"], ["10.0.0.1"], ["169.254.169.254"], ["8.8.8.8", "192.168.1.1"], []):
            with self.subTest(ips=ips), patch("mvp_v2.image_providers.socket.getaddrinfo", return_value=[addr(ip) for ip in ips]), \
                    self.assertRaises(ImageProviderError):
                _public_address("anonymous-result.tos-cn-beijing.volces.com")
        with patch("mvp_v2.image_providers.socket.getaddrinfo", return_value=[addr("8.8.8.8")]):
            self.assertEqual(_public_address("anonymous-result.tos-cn-beijing.volces.com"), "8.8.8.8")

    def test_tls_connection_pins_checked_ip_and_keeps_official_hostname(self):
        with patch("mvp_v2.image_providers.socket.create_connection") as connect:
            connection = _PinnedHTTPSConnection("ark.cn-beijing.volces.com", "8.8.8.8", timeout=12)
            with patch.object(connection._context, "wrap_socket") as wrap:
                connection.connect()
            self.assertEqual(connect.call_args.args[0], ("8.8.8.8", 443))
            self.assertEqual(wrap.call_args.kwargs["server_hostname"], "ark.cn-beijing.volces.com")

    def test_fake_ip_uses_https_dns_then_pins_only_public_address(self):
        synthetic = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('198.18.0.29', 443))]
        with patch('mvp_v2.image_providers.socket.getaddrinfo', return_value=synthetic), \
                patch('mvp_v2.image_providers._public_address_from_https_dns', return_value=['8.8.8.8']) as dns:
            self.assertEqual(_public_address('anonymous.example.com'), '8.8.8.8')
            dns.assert_called_once_with('anonymous.example.com')
        with patch('mvp_v2.image_providers.socket.getaddrinfo', return_value=synthetic), \
                patch('mvp_v2.image_providers._public_address_from_https_dns', return_value=['127.0.0.1']), \
                self.assertRaises(ImageProviderError):
            _public_address('anonymous.example.com')

    def test_https_dns_checks_question_and_all_addresses_without_redirect_or_unbounded_body(self):
        from mvp_v2.image_providers import _public_address_from_https_dns
        for question in ('anonymous.example.com', 'anonymous.example.com.'):
            with self.subTest(question=question), patch('mvp_v2.image_providers._PinnedHTTPSConnection') as factory:
                response = factory.return_value.getresponse.return_value
                response.status = 200
                value = {'Status': 0, 'TC': False, 'Question': [{'name': question, 'type': 1}],
                         'Answer': [{'type': 1, 'data': '8.8.8.8'}]}
                response.read.return_value = json.dumps(value).encode()
                self.assertEqual(_public_address_from_https_dns('anonymous.example.com'), ['8.8.8.8'])
                factory.assert_called_once_with('cloudflare-dns.com', '1.1.1.1', timeout=10)
                response.read.assert_called_once_with(65537)
                value['Answer'].append({'type': 1, 'data': '192.168.1.1'})
                response.read.return_value = json.dumps(value).encode()
                with self.assertRaises(ImageProviderError):
                    _public_address_from_https_dns('anonymous.example.com')
                response.status = 302
                with self.assertRaises(ImageProviderError):
                    _public_address_from_https_dns('anonymous.example.com')

    def test_dns_rejection_before_post_is_certainly_unsent(self):
        with patch('mvp_v2.image_providers._public_address', side_effect=ImageProviderError('IMAGE_DOWNLOAD_DENIED', '地址核对失败')):
            with self.assertRaises(ImageProviderError) as caught:
                seedream_http_provider()('匿名')
        self.assertTrue(caught.exception.request_not_sent)
        self.assertFalse(caught.exception.request_may_have_succeeded)

    def test_default_transport_bounds_body_and_does_not_follow_redirect(self):
        from urllib.request import Request
        with patch("mvp_v2.image_providers._public_address", return_value="8.8.8.8"), \
                patch("mvp_v2.image_providers._PinnedHTTPSConnection") as connection_class:
            connection = connection_class.return_value
            response = connection.getresponse.return_value
            response.status = 302
            response.getheaders.return_value = [("Location", "https://attacker.test")]
            response.getheader.return_value = None
            response.read.return_value = b"redirect"
            result = _https_transport(Request(SEEDREAM_ENDPOINT, data=b"{}", method="POST"),
                timeout_seconds=5, max_bytes=20)
            self.assertEqual(result.status, 302)
            self.assertEqual(connection.request.call_count, 1)
            self.assertEqual(response.read.call_args.args, (21,))
            connection.close.assert_called_once()
            response.getheader.return_value = "999"
            response.read.reset_mock()
            with self.assertRaises(ImageProviderError):
                _https_transport(Request(SEEDREAM_ENDPOINT), timeout_seconds=5, max_bytes=20)
            response.read.assert_not_called()
            response.getheader.return_value = None
            response.read.return_value = b"x" * 21
            with self.assertRaises(ImageProviderError):
                _https_transport(Request(SEEDREAM_ENDPOINT), timeout_seconds=5, max_bytes=20)


if __name__ == "__main__":
    unittest.main()
