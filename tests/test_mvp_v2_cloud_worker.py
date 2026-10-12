from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import hashlib
import inspect
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from openpyxl import Workbook
from reportlab.pdfgen import canvas

from mvp_v2 import __version__
from mvp_v2.cloud_runtime import MemoryCloudRunRepository
from mvp_v2.cloud_worker import CloudWorker, _terminal_status
from tests.test_mvp_v2_cloud_runtime import _package


class RecordingStorage:
    def __init__(self):
        self.keys = []

    def key(self, value):
        return f"mplus/{value}"

    def put_file(self, path, *, key, expected_sha256):
        self.keys.append(key)
        return SimpleNamespace(storage_ref=f"cos://private/{key}?sha256={expected_sha256}")

    def put_bytes(self, payload, *, key):
        self.keys.append(key)
        return SimpleNamespace(storage_ref=f"cos://private/{key}", sha256="a" * 64)


class CloudWorkerTests(unittest.TestCase):
    def _worker(self):
        return CloudWorker(
            repository=object(),
            storage=RecordingStorage(),
            worker_id="worker-a",
            package_cache="/tmp/mplus-test-cache",
        )

    def _write_json(self, path, payload):
        path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def _record(self, path):
        return {
            "path": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size_bytes": path.stat().st_size,
        }

    def _delivery_fixture(self, root, *, workbook_kind="valid", reviewed=True, current=True, pdf=None):
        run_id = "run_cloud_001"
        run_dir = root / "data" / "runs" / "anonymous-project" / "runs" / run_id
        run_dir.mkdir(parents=True)
        workbook = run_dir / "delivery.xlsx"
        if workbook_kind in {"valid", "missing"}:
            book = Workbook()
            book.active.title = "脚本"
            book.active["A1"] = "匿名正式交付"
            book.save(workbook)
        elif workbook_kind == "corrupt_zip":
            with ZipFile(workbook, "w") as archive:
                archive.writestr("xl/workbook.xml", b"not a workbook")
            workbook.write_bytes(workbook.read_bytes()[:-1])
        elif workbook_kind == "pseudo":
            workbook.write_text("this is not an xlsx", encoding="utf-8")

        audit = run_dir / "delivery.audit.json"
        self._write_json(audit, {"status": "pass"})
        human = run_dir / "human_review_report.json"
        if reviewed:
            self._write_json(human, {
                "schema_version": "mplus-human-review/v1",
                "status": "complete",
                "human_review_status": "quality_pass",
                "quality_review_status": "quality_pass",
                "reviewer_kind": "human",
                "stage_status": "succeeded",
                "machine_audit_path": str(audit),
                "machine_audit_sha256": hashlib.sha256(audit.read_bytes()).hexdigest(),
                "review_workbook_path": str(workbook),
                "review_workbook_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            })
        feedback = run_dir / "publish_feedback.json"
        self._write_json(feedback, {
            "schema_version": "mplus-publish-feedback/v1",
            "quality_review_status": "quality_pass" if reviewed else "pending",
            "reviewer_kind": "human",
            "quality_review_ref": str(human),
            "release_artifact_refs": {
                "workbook": str(workbook),
                "audit": str(audit),
            },
        })
        report = run_dir / "run_report.json"
        self._write_json(report, {
            "run_id": run_id,
            "output_path": str(workbook),
            "output_sha256": hashlib.sha256(workbook.read_bytes()).hexdigest(),
            "audit_path": str(audit),
            "quality_review_report_path": str(human),
            "machine_audit_status": "pass",
            "human_review_status": "quality_pass" if reviewed else "pending",
            "quality_review_status": "quality_pass" if reviewed else "pending",
            "reviewer_kind": "human",
            "release_decision": "release_pass" if reviewed else "blocked",
            "status": "release_pass" if reviewed else "blocked",
            "exit_code": 0,
        })
        source_commit = "a" * 40
        manifest = {
            "schema_version": "stage1-pipeline-runtime/v3",
            "run_id": run_id,
            "source_commit": source_commit,
            "status": "succeeded",
            "bundle_finalized": True,
            "run_provenance": {
                "cloud_run_id": run_id if current else "run_cloud_old",
                "runner_version": "1.0.0",
                "source_commit": source_commit,
                "package_id": "launch-core",
                "package_version": 4,
                "package_sha256": "b" * 64,
                "package_schema_version": "mplus-asset-package/v3",
                "input_sha256": "c" * 64,
            },
            "artifacts": {
                "workbook": self._record(workbook),
                "audit": self._record(audit),
                "run_report": self._record(report),
            },
            "stages": {
                "human_review": {
                    "status": "succeeded" if reviewed else "waiting_human_review",
                    "output_refs": [str(human), str(workbook)],
                },
                "publish_feedback": {
                    "status": "succeeded" if reviewed else "blocked",
                    "output_refs": [str(feedback)],
                },
            },
        }
        self._write_json(run_dir / "run_manifest.json", manifest)
        if pdf == "valid":
            pdf_path = run_dir / "delivery.pdf"
            document = canvas.Canvas(str(pdf_path))
            document.drawString(72, 720, "Anonymous cloud delivery")
            document.save()
        elif pdf == "invalid":
            (run_dir / "delivery.pdf").write_text("not a pdf", encoding="utf-8")
        return {
            "run_id": run_id,
            "source_commit": source_commit,
            "runner_version": "1.0.0",
            "package_id": "launch-core",
            "package_version": 4,
            "package_sha256": "b" * 64,
            "package_schema_version": "mplus-asset-package/v3",
            "input_sha256": "c" * 64,
        }, root / "output", root / "data"

    def test_valid_reviewed_excel_is_available_without_pdf(self):
        worker = self._worker()
        with tempfile.TemporaryDirectory() as tmp:
            run, output_root, data_root = self._delivery_fixture(Path(tmp))
            self.assertTrue(worker._validate_delivery(run, output_root, data_root))

    def test_delivery_validation_rejects_exit_zero_false_artifacts_and_stale_or_unreviewed_runs(self):
        worker = self._worker()
        cases = {
            "empty": {"workbook_kind": "missing"},
            "pseudo_xlsx": {"workbook_kind": "pseudo"},
            "corrupt_zip": {"workbook_kind": "corrupt_zip"},
            "missing_review_evidence": {"reviewed": False},
            "non_current_snapshot": {"current": False},
            "invalid_pdf": {"pdf": "invalid"},
        }
        for name, options in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                run, output_root, data_root = self._delivery_fixture(Path(tmp), **options)
                if options.get("workbook_kind") == "missing":
                    run_dir = data_root / "runs" / "anonymous-project" / "runs" / run["run_id"]
                    (run_dir / "delivery.xlsx").unlink()
                    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
                    manifest["artifacts"]["workbook"]["sha256"] = "d" * 64
                    self._write_json(run_dir / "run_manifest.json", manifest)
                self.assertFalse(worker._validate_delivery(run, output_root, data_root))
                self.assertEqual(_terminal_status(0, delivery_available=False), ("failed", "metadata_only"))

    def test_valid_pdf_does_not_change_excel_availability(self):
        worker = self._worker()
        with tempfile.TemporaryDirectory() as tmp:
            run, output_root, data_root = self._delivery_fixture(Path(tmp), pdf="valid")
            self.assertTrue(worker._validate_delivery(run, output_root, data_root))
            self.assertEqual(_terminal_status(0, delivery_available=True), ("succeeded", "delivery_available"))

    def test_attempt_outputs_never_overwrite_an_earlier_attempt(self):
        storage = RecordingStorage()
        worker = CloudWorker(
            repository=object(),
            storage=storage,
            worker_id="worker-a",
            package_cache="/tmp/mplus-test-cache",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            data = root / "data"
            output.mkdir()
            data.mkdir()
            (output / "delivery.xlsx").write_bytes(b"xlsx")
            log = root / "worker.log"
            log.write_text("ok", encoding="utf-8")
            run = {
                "run_id": "run_123",
                "runner_version": "1.0.0",
                "source_commit": "a" * 40,
                "package_id": "launch-core",
                "package_version": 4,
                "package_sha256": "b" * 64,
                "run_schema_version": "mplus-cloud-run/v2",
                "input_sha256": "c" * 64,
                "package_schema_version": "mplus-asset-package/v3",
            }
            worker._upload_results({**run, "attempts": 1}, output, data, log, 0)
            first_attempt = set(storage.keys)
            worker._upload_results({**run, "attempts": 2}, output, data, log, 0)
            second_attempt = set(storage.keys) - first_attempt

        self.assertTrue(first_attempt)
        self.assertTrue(second_attempt)
        self.assertTrue(all("runs/run_123/attempts/1/" in key for key in first_attempt))
        self.assertTrue(all("runs/run_123/attempts/2/" in key for key in second_attempt))
        self.assertTrue(first_attempt.isdisjoint(second_attempt))

    def test_worker_starts_a_process_group_and_monitors_heartbeat(self):
        source = inspect.getsource(CloudWorker._execute)
        self.assertIn("start_new_session=True", source)
        self.assertIn("communicate(timeout=0.5)", source)
        self.assertIn("_terminate_process_group", source)

    def test_lease_is_kept_during_materialization_validation_and_upload(self):
        for slow_stage in ("materialization", "validation", "upload"):
            with self.subTest(stage=slow_stage), tempfile.TemporaryDirectory() as tmp:
                clock = [datetime(2026, 9, 7, tzinfo=timezone.utc)]
                repository = MemoryCloudRunRepository(now=lambda: clock[0])
                repository.register_package(_package(4, "a"))
                run = repository.create_run(
                    workspace_id="workspace_anonymous", project_id="project_anonymous",
                    category_id="launch", package_ref="launch-core@4",
                    input_ref="cos://private-123/input?sha256=" + "c" * 64,
                    input_sha256="c" * 64, input_filename="brief.txt",
                    runner_version=__version__, source_commit="d" * 40,
                    container_image_digest="sha256:" + "e" * 64,
                    request={"ai_provider": "deepseek-http", "ai_model": "deepseek-chat",
                             "prompt_version": "v1", "prompt_schema_version": "v1"},
                )
                stopped = threading.Event()
                tick = threading.Event()
                renewed = threading.Event()
                waiting = threading.Event()

                class SteppedHeartbeatEvent:
                    def wait(self, interval):
                        waiting.set()
                        tick.wait()
                        tick.clear()
                        return stopped.is_set()

                    def set(self):
                        stopped.set()
                        tick.set()

                real_heartbeat = repository.heartbeat

                def heartbeat(*args, **kwargs):
                    try:
                        return real_heartbeat(*args, **kwargs)
                    finally:
                        renewed.set()

                def slow_io():
                    # Four 40-second steps exceed the original 120-second lease.
                    # Only the worker's real heartbeat thread can renew it.
                    for _ in range(4):
                        clock[0] += timedelta(seconds=40)
                        if waiting.is_set() and not stopped.is_set():
                            renewed.clear()
                            tick.set()
                            self.assertTrue(renewed.wait(2), "worker did not renew the lease")
                        self.assertIsNone(repository.claim("worker-b"), "another worker reclaimed an active I/O attempt")

                def materialize(*args):
                    # A live thread may not yet have reached its first wait.
                    if not stopped.is_set():
                        waiting.wait(0.1)
                    if slow_stage == "materialization":
                        slow_io()
                    return Path(tmp)

                def download(ref, target, **kwargs):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("anonymous", encoding="utf-8")
                    return target

                def validate(*args):
                    if slow_stage == "validation":
                        slow_io()
                    return True

                def upload(*args):
                    if slow_stage == "upload":
                        slow_io()
                    return {"storage_ref": "cos://private-123/result?sha256=" + "f" * 64, "sha256": "f" * 64}

                worker = CloudWorker(
                    repository, SimpleNamespace(download_file=download),
                    worker_id="worker-a", package_cache=tmp,
                )
                process = SimpleNamespace(returncode=0, communicate=lambda timeout: ("anonymous", None))
                # Patch the module reference, not threading.Event used inside Thread itself.
                with patch("mvp_v2.cloud_worker.threading", SimpleNamespace(
                    Event=SteppedHeartbeatEvent, Thread=threading.Thread,
                )), patch.object(repository, "heartbeat", side_effect=heartbeat), \
                        patch("mvp_v2.cloud_worker.materialize_package", side_effect=materialize), \
                        patch("mvp_v2.cloud_worker.subprocess.Popen", return_value=process), \
                        patch.object(worker, "_validate_delivery", side_effect=validate), \
                        patch.object(worker, "_upload_results", side_effect=upload):
                    result = worker.run_once()
                self.assertEqual(result["status"], "succeeded")
                self.assertEqual(result["attempts"], 1)
                self.assertEqual(repository.get_run(run["run_id"])["result_sha256"], "f" * 64)
                self.assertTrue(stopped.is_set())
                self.assertIsNone(repository.claim("worker-b"))


if __name__ == "__main__":
    unittest.main()
