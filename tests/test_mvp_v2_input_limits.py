from __future__ import annotations

from io import BytesIO
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from starlette.datastructures import UploadFile

from mvp_v2 import cloud_runtime, command, input_limits
from mvp_v2.formal_brief_input_adapter import FormalBriefInputError, _read_stream_to_temp


class FormalInputLimitTests(unittest.TestCase):
    def setUp(self):
        from tests.test_mvp_v2_formal_console import FormalConsoleTests
        FormalConsoleTests.setUp(self)

    def project(self):
        workspace = self.projects.get_workspace(self.token, self.workspace_id)
        return self.projects.create_project(self.token, workspace_id=self.workspace_id, name="匿名容量项目", category_id="launch", expected_workspace_version=workspace["workspace_version"])

    def path(self, project, version=0):
        return f"/api/formal/projects/{project['project_id']}/brief/files?workspace_id={self.workspace_id}&expected_project_version={version}"

    def count(self):
        with self.store.connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0]

    def test_http_oversized_file_is_rejected_before_unbounded_read_or_business_write(self):
        self.adapter.max_file_bytes = 24
        project = self.project()
        reads = []
        original = UploadFile.read
        async def read(upload, size=-1):
            reads.append(size)
            return await original(upload, size)
        with patch.object(UploadFile, "read", read):
            response = self.client.post(self.path(project), headers=self.headers, files=[("files", ("brief.txt", b"a" * 25, "text/plain"))])
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "FILE_TOO_LARGE")
        self.assertTrue(all(0 < size <= 25 for size in reads), reads)
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.count(), 0)
        self.assertFalse(any(self.adapter.store.root.rglob("*")))

    def test_http_exact_file_limit_and_current_dto_use_actual_adapter_configuration(self):
        self.adapter.max_file_bytes = 24
        project = self.project()
        empty = self.client.get(f"/api/formal/projects/{project['project_id']}/brief/current?workspace_id={self.workspace_id}", headers=self.headers).json()
        self.assertEqual(empty["upload_limits"], input_limits.formal_upload_limits(max_file_bytes=24))
        reads = []
        original = UploadFile.read
        async def read(upload, size=-1):
            reads.append(size)
            return await original(upload, size)
        with patch.object(UploadFile, "read", read):
            response = self.client.post(self.path(project), headers=self.headers, files=[("files", ("brief.txt", b"Project: " + b"a" * 15, "text/plain"))])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["upload_limits"], empty["upload_limits"])
        self.assertTrue(all(0 < size <= 25 for size in reads), reads)

    def test_twenty_documents_include_pasted_text_and_twenty_one_read_nothing(self):
        def files(number):
            return [("files", (f"note-{index}.txt", f"note {index}".encode(), "text/plain")) for index in range(number)]
        project = self.project()
        response = self.client.post(self.path(project), headers=self.headers, files=files(19), data={"text": "补充说明"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["brief"]["documents"]), 20)
        before, calls = self.count(), len(self.factory_calls)
        project = self.project()
        with patch.object(UploadFile, "read", side_effect=AssertionError("oversized batch must not read files")):
            response = self.client.post(self.path(project), headers=self.headers, files=files(20), data={"text": "补充说明"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "UPLOAD_LIMIT_EXCEEDED")
        self.assertEqual(self.count(), before)
        self.assertEqual(len(self.factory_calls), calls)

    def test_multibyte_text_boundary_is_utf8_bytes(self):
        project = self.project()
        with patch.object(input_limits, "FORMAL_PASTED_TEXT_BYTES", 12):
            accepted = self.client.post(self.path(project), headers=self.headers, data={"text": "中" * 4})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            rejected = self.client.post(self.path(project, 1), headers=self.headers, data={"text": "中" * 4 + "a"})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(rejected.json()["detail"]["code"], "FILE_TOO_LARGE")
        self.assertEqual(self.count(), 1)

    def test_adapter_stream_only_reads_remaining_limit_plus_one(self):
        class Stream(BytesIO):
            def __init__(self):
                super().__init__(b"a" * 100)
                self.reads = []
            def read(self, size=-1):
                self.reads.append(size)
                return super().read(size)
        stream = Stream()
        with self.assertRaisesRegex(FormalBriefInputError, "FILE_TOO_LARGE"):
            _read_stream_to_temp(stream, 9)
        self.assertEqual(stream.reads, [10])


class CloudInputLimitTests(unittest.TestCase):
    def test_cloud_manifest_uses_shared_file_and_total_limits(self):
        def bundle(sizes):
            files = [{"filename": f"input-{index}.txt", "storage_ref": f"cos://anonymous/{index}", "sha256": str(index) * 64, "size_bytes": size} for index, size in enumerate(sizes)]
            first = files[0]
            return cloud_runtime._input_bundle(input_ref=first["storage_ref"], input_sha256=first["sha256"], input_filename=first["filename"], input_size_bytes=first["size_bytes"], input_files=files)
        with patch.object(input_limits, "CLOUD_INPUT_FILE_BYTES", 8), patch.object(input_limits, "CLOUD_RUN_INPUT_BYTES", 12):
            self.assertEqual(bundle([8, 4])["size_bytes"], 12)
            with self.assertRaises(cloud_runtime.CloudRuntimeError):
                bundle([9])
            with self.assertRaises(cloud_runtime.CloudRuntimeError):
                bundle([8, 5])

    def test_cloud_cli_rejects_oversized_file_before_storage_creation_and_upload(self):
        with tempfile.TemporaryDirectory(prefix="mplus-cloud-capacity-") as directory:
            source = Path(directory) / "brief.txt"
            source.write_bytes(b"a" * 9)
            args = command.parser().parse_args(["run", "cloud", "--project-id", "project_12345678", "--brief", str(source), "--package", "launch-core", "--async"])
            storage = MagicMock()
            storage.put_file.return_value.storage_ref = "cos://anonymous/input"
            with patch.object(input_limits, "CLOUD_INPUT_FILE_BYTES", 8), patch.object(command, "_session", return_value=("anonymous-token", "workspace_12345678")), patch.object(command, "_project", return_value={"category_id": "launch"}), patch.object(command, "_request", return_value={"status": "queued"}) as request, patch.object(command, "_print"), patch("mvp_v2.cloud_storage.CosObjectStorage.from_environment", return_value=storage) as factory:
                with self.assertRaisesRegex(RuntimeError, "上限"):
                    command._run_cloud(args)
                factory.assert_not_called()
                request.assert_not_called()
                source.write_bytes(b"a" * 8)
                self.assertEqual(command._run_cloud(args), 0)
                storage.put_file.assert_called_once()
                self.assertEqual(request.call_args.kwargs["json_body"]["input_size_bytes"], 8)


class FormalInputLimitFrontendTests(unittest.TestCase):
    def test_browser_functions_preflight_bytes_and_allow_noncritical_failure(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(["node", str(root / "tests/support/formal_input_limits_probe.cjs")], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
