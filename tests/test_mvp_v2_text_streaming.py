import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from mvp_v2 import text_providers


class TextStreamingTests(unittest.TestCase):
    def run_stream(self, *, finish="stop", done=True, callback=None):
        first_seen = threading.Event()
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(value):
                    self.wfile.write(("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode())
                    self.wfile.flush()

                self.wfile.write(b": keepalive\n\n")
                send({"choices": [{"delta": {"content": '{"text":"开场'}, "finish_reason": None}]})
                if not first_seen.wait(2):
                    return
                send({"choices": [{"delta": {"content": '与结尾"}'}, "finish_reason": None}]})
                send({"choices": [{"delta": {}, "finish_reason": finish}]})
                send({"choices": [], "usage": {"completion_tokens": 12}})
                if done:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        deltas = []

        def on_delta(value):
            deltas.append(value)
            first_seen.set()
            if callback:
                callback(value)

        try:
            with patch.dict(os.environ, {
                "DEEPSEEK_API_KEY": "anonymous-stream-key",
                "DEEPSEEK_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            }):
                result = text_providers.deepseek_http_stream_provider()(
                    "return JSON", "deepseek-v4-flash", 2048, on_delta,
                )
            return result, deltas, requests
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_real_http_first_delta_arrives_before_server_releases_final_chunk(self):
        result, deltas, requests = self.run_stream()
        self.assertEqual(deltas, ['{"text":"开场', '与结尾"}'])
        self.assertEqual(json.loads(result), {"text": "开场与结尾"})
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["stream"])
        self.assertEqual(result.diagnostics["response"]["usage"]["completion_tokens"], 12)
        self.assertNotIn("anonymous-stream-key", str(result.diagnostics))

    def test_truncated_or_unterminated_stream_is_not_a_complete_result(self):
        for options in ({"finish": "length"}, {"done": False}):
            with self.subTest(options=options), self.assertRaises(text_providers.TextProviderError):
                self.run_stream(**options)

    def test_callback_conflict_propagates_without_transport_retry(self):
        def stale(_):
            raise ValueError("draft revision changed")
        with self.assertRaisesRegex(ValueError, "draft revision changed"):
            self.run_stream(callback=stale)


if __name__ == "__main__":
    unittest.main()
