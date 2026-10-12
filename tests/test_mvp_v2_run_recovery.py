import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.pipeline_runtime import (
    open_pipeline_run,
    publish_pipeline_run,
    resolve_current_release,
    resolve_pipeline_run,
)
from mvp_v2.run_state import exclusive_lock


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class RunRecoveryTests(unittest.TestCase):
    def test_pipeline_lock_records_owner_identity_and_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_root = root / "source"
            source_root.mkdir()

            with open_pipeline_run(
                brief={"项目名称": "锁元数据项目"},
                cache_dir=root / "cache",
                source_root=source_root,
                cache_fingerprint="lock-metadata",
            ) as state:
                metadata = json.loads(state.lock_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["pid"], os.getpid())
                self.assertEqual(metadata["host"], socket.gethostname())
                self.assertEqual(metadata["run_id"], state.run_id)
                self.assertTrue(metadata["process_start"])
                self.assertTrue(metadata["created_at"])

    def test_same_host_dead_process_lock_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".pipeline.lock"
            lock_path.write_text(
                json.dumps({
                    "pid": 99999999,
                    "host": socket.gethostname(),
                    "process_start": "dead-process",
                    "created_at": "2026-07-13T00:00:00Z",
                    "run_id": "dead-run",
                }),
                encoding="utf-8",
            )

            with exclusive_lock(lock_path, run_id="replacement-run"):
                metadata = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["run_id"], "replacement-run")

            self.assertFalse(lock_path.exists())

    def test_same_host_live_process_lock_cannot_be_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".pipeline.lock"
            with exclusive_lock(lock_path, run_id="owner-run"):
                owner_metadata = lock_path.read_text(encoding="utf-8")
            lock_path.write_text(owner_metadata, encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "锁已被占用"):
                with exclusive_lock(lock_path, run_id="contender-run"):
                    self.fail("live process lock must not be stolen")

            self.assertEqual(lock_path.read_text(encoding="utf-8"), owner_metadata)

    def test_same_host_reused_pid_with_different_start_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".pipeline.lock"
            lock_path.write_text(
                json.dumps({
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                    "process_start": "different-process-start",
                    "created_at": "2026-07-13T00:00:00Z",
                    "run_id": "reused-pid-run",
                }),
                encoding="utf-8",
            )

            with exclusive_lock(lock_path, run_id="replacement-run"):
                metadata = json.loads(lock_path.read_text(encoding="utf-8"))
                self.assertEqual(metadata["run_id"], "replacement-run")

    def test_live_process_is_not_stolen_when_process_start_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".pipeline.lock"
            with patch("mvp_v2.run_state._process_start", return_value=None):
                with exclusive_lock(lock_path, run_id="owner-run"):
                    owner_metadata = lock_path.read_text(encoding="utf-8")
                    self.assertTrue(json.loads(owner_metadata)["process_start"])

                lock_path.write_text(owner_metadata, encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "锁已被占用"):
                    with exclusive_lock(lock_path, run_id="contender-run"):
                        self.fail("live process lock must not be stolen")

    def test_remote_host_lock_cannot_be_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".pipeline.lock"
            remote_metadata = {
                "pid": 99999999,
                "host": "remote-build-host",
                "process_start": "unknown",
                "created_at": "2026-07-13T00:00:00Z",
                "run_id": "remote-run",
            }
            lock_path.write_text(json.dumps(remote_metadata), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "远端主机"):
                with exclusive_lock(lock_path, run_id="local-run"):
                    self.fail("remote lock must not be stolen")

            self.assertEqual(
                json.loads(lock_path.read_text(encoding="utf-8")),
                remote_metadata,
            )

    def test_invalid_lock_metadata_fails_closed(self):
        base = {
            "pid": 99999999,
            "host": socket.gethostname(),
            "process_start": "dead-process",
            "created_at": "2026-07-13T00:00:00Z",
            "run_id": "dead-run",
        }
        invalid_cases = {
            "missing_run_id": {key: value for key, value in base.items() if key != "run_id"},
            "missing_created_at": {
                key: value for key, value in base.items() if key != "created_at"
            },
            "pid_string": {**base, "pid": "99999999"},
            "pid_boolean": {**base, "pid": True},
            "host_not_string": {**base, "host": 123},
            "process_start_not_string": {**base, "process_start": {}},
            "run_id_not_string": {**base, "run_id": []},
            "created_at_not_string": {**base, "created_at": {}},
            "created_at_not_timestamp": {**base, "created_at": "not-a-timestamp"},
        }

        for case_name, invalid_metadata in invalid_cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                lock_path = Path(tmp) / ".pipeline.lock"
                original = json.dumps(invalid_metadata, ensure_ascii=False)
                lock_path.write_text(original, encoding="utf-8")

                with self.assertRaisesRegex(RuntimeError, "锁元数据无效"):
                    with exclusive_lock(lock_path, run_id="replacement-run"):
                        self.fail("invalid lock metadata must never be replaced")

                self.assertEqual(lock_path.read_text(encoding="utf-8"), original)

    def test_lock_created_between_check_and_open_cannot_be_stolen(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / ".pipeline.lock"
            remote_metadata = {
                "pid": 99999999,
                "host": "remote-race-host",
                "process_start": "unknown",
                "created_at": "2026-07-13T00:00:00Z",
                "run_id": "remote-race-run",
            }
            original_link = os.link
            injected = False

            def create_competing_lock(source, target):
                nonlocal injected
                if not injected:
                    injected = True
                    Path(target).write_text(json.dumps(remote_metadata), encoding="utf-8")
                return original_link(source, target)

            with patch("mvp_v2.run_state.os.link", side_effect=create_competing_lock):
                with self.assertRaisesRegex(RuntimeError, "远端主机"):
                    with exclusive_lock(lock_path, run_id="local-race-run"):
                        self.fail("racing lock must not be overwritten")

            self.assertEqual(
                json.loads(lock_path.read_text(encoding="utf-8")),
                remote_metadata,
            )

    def test_startup_reconciles_same_project_running_manifest_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            orphan_manifest = cache_dir / "runs" / "orphan-run" / "run_manifest.json"
            other_manifest = cache_dir / "runs" / "other-run" / "run_manifest.json"
            orphan_manifest.parent.mkdir(parents=True)
            other_manifest.parent.mkdir(parents=True)
            orphan_manifest.write_text(
                json.dumps({
                    "schema_version": "stage1-pipeline-runtime/v1",
                    "run_id": "orphan-run",
                    "project_name": "恢复项目",
                    "status": "running",
                    "started_at": "2026-07-13T00:00:00Z",
                }),
                encoding="utf-8",
            )
            other_manifest.write_text(
                json.dumps({
                    "schema_version": "stage1-pipeline-runtime/v1",
                    "run_id": "other-run",
                    "project_name": "其他项目",
                    "status": "running",
                    "started_at": "2026-07-13T00:00:00Z",
                }),
                encoding="utf-8",
            )
            ledger_path = cache_dir / "run_ledger.jsonl"
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger_path.write_text(
                json.dumps({"run_id": "orphan-run", "event": "started"}) + "\n",
                encoding="utf-8",
            )

            for _ in range(2):
                with open_pipeline_run(
                    brief={"项目名称": "恢复项目"},
                    cache_dir=cache_dir,
                    source_root=source_root,
                    cache_fingerprint="recovery",
                ):
                    pass

            recovered = json.loads(orphan_manifest.read_text(encoding="utf-8"))
            untouched = json.loads(other_manifest.read_text(encoding="utf-8"))
            recovery_events = [
                row for row in _read_jsonl(ledger_path)
                if row.get("run_id") == "orphan-run" and row.get("event") == "recovered"
            ]
            self.assertEqual(recovered["status"], "failed")
            self.assertEqual(recovered["recovery_reason"], "orphaned_running_run")
            self.assertTrue(recovered["recovered_at"])
            self.assertEqual(len(recovery_events), 1)
            self.assertEqual(recovery_events[0]["details"]["final_status"], "failed")
            self.assertEqual(untouched["status"], "running")

    def test_startup_recovers_unpublished_running_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            source_root = root / "source"
            source_root.mkdir()
            brief = {"项目名称": "当前发布项目"}

            with open_pipeline_run(
                brief=brief,
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="current-release",
            ) as current_state:
                artifacts = {}
                for name, filename in {
                    "workbook": "result.xlsx",
                    "audit": "result.audit.json",
                    "loop_log": "result.loop.json",
                    "visual_review": "result.visual_review.html",
                    "run_report": "run_report.json",
                }.items():
                    path = current_state.run_dir / filename
                    path.write_text(name, encoding="utf-8")
                    artifacts[name] = path
                publish_pipeline_run(current_state, "succeeded", artifacts)

            current_manifest = current_state.manifest_path
            manifest = json.loads(current_manifest.read_text(encoding="utf-8"))
            manifest["status"] = "running"
            manifest.pop("finished_at", None)
            current_manifest.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            before = current_manifest.read_bytes()
            self.assertEqual(
                next(iter(resolve_pipeline_run(cache_dir, current_state.run_id, allowed_statuses={"running"}).values())).parent,
                current_state.run_dir.resolve(),
            )

            with open_pipeline_run(
                brief=brief,
                cache_dir=cache_dir,
                source_root=source_root,
                cache_fingerprint="next-run",
            ):
                pass

            self.assertNotEqual(current_manifest.read_bytes(), before)
            recovery_events = [
                row for row in _read_jsonl(cache_dir / "run_ledger.jsonl")
                if row.get("run_id") == current_state.run_id
                and row.get("event") == "recovered"
            ]
            self.assertEqual(len(recovery_events), 1)

    def test_control_flow_interruptions_are_finalized_as_failed(self):
        for interruption in (SystemExit("stop"), KeyboardInterrupt()):
            with self.subTest(interruption=type(interruption).__name__):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    source_root = root / "source"
                    source_root.mkdir()

                    with self.assertRaises(type(interruption)):
                        with open_pipeline_run(
                            brief={"项目名称": "中断项目"},
                            cache_dir=root / "cache",
                            source_root=source_root,
                            cache_fingerprint="interrupted",
                        ) as state:
                            raise interruption

                    manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
                    events = [row["event"] for row in _read_jsonl(state.ledger_path)]
                    self.assertEqual(manifest["status"], "failed")
                    self.assertEqual(events, ["started", "failed"])


if __name__ == "__main__":
    unittest.main()
