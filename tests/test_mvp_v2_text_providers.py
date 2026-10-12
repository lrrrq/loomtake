import json
import os
import unittest
from unittest.mock import MagicMock, patch

from mvp_v2.text_providers import (
    TextProviderError,
    _deepseek_content,
    _extract_text,
    deepseek_http_provider,
    minimax_http_provider,
    provider_credentials_available,
)


class TextProviderTests(unittest.TestCase):
    def test_minimax_cli_cloud_route_requires_service_key_and_command(self):
        with patch.dict(os.environ, {
            "MPLUS_EXECUTION_BACKEND": "cloud",
            "MINIMAX_API_KEY": "",
        }, clear=False), patch(
            "mvp_v2.text_providers._command_available", return_value=True,
        ):
            self.assertFalse(provider_credentials_available("minimax-cli"))

        with patch.dict(os.environ, {
            "MPLUS_EXECUTION_BACKEND": "cloud",
            "MINIMAX_API_KEY": "anonymous-test-key",
        }, clear=False), patch(
            "mvp_v2.text_providers._command_available", return_value=True,
        ):
            self.assertTrue(provider_credentials_available("minimax-cli"))

        with patch.dict(os.environ, {
            "MPLUS_EXECUTION_BACKEND": "cloud",
            "MINIMAX_API_KEY": "anonymous-test-key",
        }, clear=False), patch(
            "mvp_v2.text_providers._command_available", return_value=False,
        ):
            self.assertFalse(provider_credentials_available("minimax-cli"))

    def test_minimax_cli_local_route_keeps_existing_login_compatibility(self):
        with patch.dict(os.environ, {
            "MPLUS_EXECUTION_BACKEND": "local",
            "MINIMAX_API_KEY": "",
        }, clear=False), patch(
            "mvp_v2.text_providers._command_available", return_value=True,
        ):
            self.assertTrue(provider_credentials_available("minimax-cli"))

    def test_extracts_mmx_messages_api_text_block(self):
        raw = json.dumps({
            "model": "MiniMax-M2.7-highspeed",
            "content": [
                {"type": "thinking", "thinking": "internal"},
                {"type": "text", "text": '{"status":"ok"}'},
            ],
            "stop_reason": "end_turn",
            "usage": {"output_tokens": 76},
        })

        self.assertEqual(_extract_text(raw), '{"status":"ok"}')

    def test_thinking_only_response_reports_truncation_evidence(self):
        raw = json.dumps({
            "content": [{"type": "thinking", "thinking": "internal"}],
            "stop_reason": "max_tokens",
            "usage": {"output_tokens": 128},
        })

        with self.assertRaisesRegex(
            TextProviderError,
            "stop_reason=max_tokens.*content_types=\\['thinking'\\].*output_tokens=128",
        ):
            _extract_text(raw)

    def test_deepseek_empty_content_reports_finish_and_usage(self):
        with self.assertRaisesRegex(
            TextProviderError,
            "finish_reason=length.*completion_tokens=8192",
        ):
            _deepseek_content({
                "choices": [{
                    "finish_reason": "length",
                    "message": {"content": "", "reasoning_content": "internal"},
                }],
                "usage": {"completion_tokens": 8192},
            })

    def test_deepseek_json_route_explicitly_disables_thinking(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"status":"ok"}'},
            }],
            "usage": {"completion_tokens": 9},
        }).encode("utf-8")
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "anonymous-test-key"}), patch(
            "mvp_v2.text_providers.urlopen", return_value=response,
        ) as opener:
            result = deepseek_http_provider()("return json", "deepseek-v4-flash", 8192)
        request = opener.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, '{"status":"ok"}')
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_minimax_cloud_route_uses_service_api_key_without_cli_login(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"status":"ok"}'},
            }],
        }).encode("utf-8")
        with patch.dict(os.environ, {"MINIMAX_API_KEY": "anonymous-test-key"}), patch(
            "mvp_v2.text_providers.urlopen", return_value=response,
        ) as opener:
            result = minimax_http_provider()("return json", "MiniMax-M2.7", 4096)
        request = opener.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, '{"status":"ok"}')
        self.assertEqual(payload["model"], "MiniMax-M2.7")
        self.assertEqual(request.full_url, "https://api.minimaxi.com/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
