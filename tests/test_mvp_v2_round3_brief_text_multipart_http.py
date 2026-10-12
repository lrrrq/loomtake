"""Anonymous real HTTP multipart boundaries; browser newline transport included."""

import hashlib
import socket
import threading
import time
import unittest

import httpx
import uvicorn

from mvp_v2 import input_limits
from mvp_v2.formal_brief_domain import ArtifactPayload
from tests import test_mvp_v2_formal_console as console_fixture


class Round3BriefTextMultipartHttpTests(unittest.TestCase):
    def setUp(self):
        self.fixture = console_fixture.FormalConsoleTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.client.close)
        self.addCleanup(self.fixture.shell_client.close)
        self.project = self.fixture._project("匿名真实HTTP文字边界")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
        # These are the same installed Formal routes, backed only by a fresh
        # TemporaryDirectory/anonymous SQLite fixture. No running app is called.
        server = uvicorn.Server(uvicorn.Config(self.fixture.client.app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()

        def stop():
            server.should_exit = True
            thread.join(10)
            sock.close()

        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(server.started, "anonymous HTTP fixture did not start")
        self.http = httpx.Client(base_url=self.origin, timeout=15)
        self.addCleanup(self.http.close)

    @staticmethod
    def text_at_limit(extra=0, *, multiline=True):
        prefix = ("项目名称：匿名容量资料\n风格调性：克制\n视频目标：说明产品\n补充说明："
                  if multiline else "匿名产品说明：")
        target = input_limits.FORMAL_PASTED_TEXT_BYTES + extra
        text = prefix + "a" * (target - len(prefix.encode("utf-8")))
        assert len(text.encode("utf-8")) == target
        assert text == text.strip()
        return text

    def post_text(self, wire_text, *, expected_version=0, token=None):
        boundary = "mplus-anonymous-round3-text-boundary"
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="text"\r\n\r\n'
            + wire_text
            + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="critical"\r\n\r\ntrue'
            + f'\r\n--{boundary}--\r\n'
        ).encode("utf-8")
        return self.http.post(
            f"/api/formal/projects/{self.project['project_id']}/brief/files",
            params={"workspace_id": self.fixture.workspace_id,
                    "expected_project_version": expected_version},
            headers={"X-MPlus-Agent-Auth": self.fixture.token if token is None else token,
                     "Content-Type": "multipart/form-data; boundary=" + boundary},
            content=body,
        )

    def snapshot(self):
        with self.fixture.store.connection() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in ("projects", "brief_versions", "brief_confirmations", "tasks")
            }

    def assert_frozen_text(self, canonical):
        sources = self.fixture.adapter.service.list_imported_sources(
            self.fixture.token, workspace_id=self.fixture.workspace_id,
            project_id=self.project["project_id"],
        )
        self.assertEqual(len(sources), 1)
        source = sources[0]
        expected = canonical.encode("utf-8")
        self.assertEqual(source["sha256"], hashlib.sha256(expected).hexdigest())
        content = self.fixture.adapter.store.read(
            ArtifactPayload(source["artifact_ref"], source["sha256"]), suffix=".txt",
        )
        self.assertEqual(content, expected)
        self.assertEqual(len(content), input_limits.FORMAL_PASTED_TEXT_BYTES)
        self.assertEqual(self.fixture.provider_calls, [])

    def assert_business_byte_rejection(self, response, before, factory_count):
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "FILE_TOO_LARGE")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.fixture.factory_calls), factory_count)
        self.assertEqual(self.fixture.provider_calls, [])

    def test_no_newline_exact_200000_accepted_and_plus_one_still_rejected(self):
        canonical = self.text_at_limit(multiline=False)
        response = self.post_text(canonical)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_frozen_text(canonical)
        before = self.snapshot()
        factory_count = len(self.fixture.factory_calls)
        response = self.post_text(self.text_at_limit(1, multiline=False), expected_version=1)
        self.assert_business_byte_rejection(response, before, factory_count)

    def test_browser_crlf_transport_of_exact_lf_200000_freezes_canonical_lf(self):
        canonical = self.text_at_limit()
        wire = canonical.replace("\n", "\r\n")
        self.assertGreater(len(wire.encode("utf-8")), input_limits.FORMAL_PASTED_TEXT_BYTES + 1)
        response = self.post_text(wire)
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_frozen_text(canonical)

    def test_browser_crlf_transport_of_lf_200001_rejected_before_business_write(self):
        canonical = self.text_at_limit(1)
        wire = canonical.replace("\n", "\r\n")
        before = self.snapshot()
        factory_count = len(self.fixture.factory_calls)
        response = self.post_text(wire)
        self.assert_business_byte_rejection(response, before, factory_count)

    def test_lone_cr_transport_is_normalized_before_frozen_byte_check(self):
        canonical = self.text_at_limit()
        response = self.post_text(canonical.replace("\n", "\r"))
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_frozen_text(canonical)

    def test_oversized_raw_multipart_part_remains_bounded_and_actionable(self):
        wire = "a" * (2 * (input_limits.FORMAL_PASTED_TEXT_BYTES + 1) + 1)
        before = self.snapshot()
        response = self.post_text(wire)
        self.assert_business_byte_rejection(response, before, 0)
        self.assertNotIn("Part exceeded maximum size", response.text)

    def test_identity_preflight_still_precedes_multipart_parse(self):
        before = self.snapshot()
        response = self.post_text("a" * 500_000, token="invalid-anonymous-token")
        self.assertIn(response.status_code, {401, 403}, response.text)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fixture.factory_calls, [])
        self.assertEqual(self.fixture.provider_calls, [])

    def test_default_five_mib_uploaded_file_keeps_raw_crlf_and_plus_one_rejects(self):
        prefix = "项目名称：匿名文件资料\r\n补充说明：".encode("utf-8")
        content = prefix + b"a" * (input_limits.FORMAL_UPLOAD_FILE_BYTES - len(prefix))
        self.assertEqual(len(content), 5 * 1024 * 1024)
        path = f"/api/formal/projects/{self.project['project_id']}/brief/files"
        headers = {"X-MPlus-Agent-Auth": self.fixture.token}
        response = self.http.post(path,
            params={"workspace_id": self.fixture.workspace_id, "expected_project_version": 0},
            headers=headers, files={"files": ("anonymous-exact-5mib.txt", content, "text/plain")})
        self.assertEqual(response.status_code, 200, response.text)
        sources = self.fixture.adapter.service.list_imported_sources(
            self.fixture.token, workspace_id=self.fixture.workspace_id,
            project_id=self.project["project_id"],
        )
        self.assertEqual(len(sources), 1)
        source = sources[0]
        self.assertEqual(source["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(self.fixture.adapter.store.read(
            ArtifactPayload(source["artifact_ref"], source["sha256"]), suffix=".txt"), content)
        before = self.snapshot()
        factory_count = len(self.fixture.factory_calls)
        response = self.http.post(path,
            params={"workspace_id": self.fixture.workspace_id, "expected_project_version": 1},
            headers=headers, files={"files": ("anonymous-over-5mib.txt", content + b"a", "text/plain")})
        self.assert_business_byte_rejection(response, before, factory_count)


if __name__ == "__main__":
    unittest.main()
