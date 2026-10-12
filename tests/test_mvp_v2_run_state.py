import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.run_state import (
    append_jsonl_ledger,
    cache_fingerprint,
    create_run_dir,
    exclusive_lock,
    generate_run_id,
    write_json_atomic,
)


class RunStateTests(unittest.TestCase):
    def test_generate_run_id_is_unique_and_safe_for_directory_names(self):
        first = generate_run_id()
        second = generate_run_id()

        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[A-Za-z0-9_-]+$")
        self.assertRegex(second, r"^[A-Za-z0-9_-]+$")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = create_run_dir(root, first)

            self.assertEqual(run_dir, root / first)
            self.assertTrue(run_dir.is_dir())

    def test_exclusive_lock_rejects_second_holder_and_releases_on_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "stage1.lock"

            with exclusive_lock(lock_path):
                with self.assertRaisesRegex(RuntimeError, "锁已被占用"):
                    with exclusive_lock(lock_path):
                        self.fail("second lock should not be acquired")

            with exclusive_lock(lock_path):
                self.assertTrue(lock_path.exists())

    def test_write_json_atomic_writes_readable_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"

            write_json_atomic(path, {"run_id": "run-1", "status": "ok"})

            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8")),
                {"run_id": "run-1", "status": "ok"},
            )

    def test_write_json_atomic_leaves_no_half_file_when_replace_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run.json"

            with patch("mvp_v2.run_state.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_json_atomic(path, {"run_id": "run-1"})

            self.assertFalse(path.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_append_jsonl_ledger_keeps_all_events_with_required_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.jsonl"

            append_jsonl_ledger(path, run_id="run-1", event="started", details={"step": 1})
            append_jsonl_ledger(path, run_id="run-1", event="finished", details={"step": 2})

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["event"] for row in rows], ["started", "finished"])
            for row in rows:
                self.assertEqual(row["run_id"], "run-1")
                self.assertIn("timestamp", row)
                self.assertIn("event", row)
            self.assertEqual(rows[0]["details"], {"step": 1})
            self.assertEqual(rows[1]["details"], {"step": 2})

    def test_cache_fingerprint_is_stable_and_changes_when_any_component_changes(self):
        baseline = cache_fingerprint(
            schema={"version": "schema-v1"},
            rule={"version": "rule-v1"},
            prompt={"version": "prompt-v1"},
            provider={"id": "minimax"},
        )

        self.assertEqual(
            baseline,
            cache_fingerprint(
                schema={"version": "schema-v1"},
                rule={"version": "rule-v1"},
                prompt={"version": "prompt-v1"},
                provider={"id": "minimax"},
            ),
        )
        self.assertNotEqual(
            baseline,
            cache_fingerprint(
                schema={"version": "schema-v2"},
                rule={"version": "rule-v1"},
                prompt={"version": "prompt-v1"},
                provider={"id": "minimax"},
            ),
        )
        self.assertNotEqual(
            baseline,
            cache_fingerprint(
                schema={"version": "schema-v1"},
                rule={"version": "rule-v2"},
                prompt={"version": "prompt-v1"},
                provider={"id": "minimax"},
            ),
        )
        self.assertNotEqual(
            baseline,
            cache_fingerprint(
                schema={"version": "schema-v1"},
                rule={"version": "rule-v1"},
                prompt={"version": "prompt-v2"},
                provider={"id": "minimax"},
            ),
        )
        self.assertNotEqual(
            baseline,
            cache_fingerprint(
                schema={"version": "schema-v1"},
                rule={"version": "rule-v1"},
                prompt={"version": "prompt-v1"},
                provider={"id": "openai"},
            ),
        )
