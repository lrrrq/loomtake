from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from mvp_v2.alpha_console import AlphaRuntime


_SERVER_PATH = Path(__file__).parent / "support" / "alpha_browser_fixture_server.py"
_SPEC = importlib.util.spec_from_file_location("alpha_browser_fixture_server", _SERVER_PATH)
assert _SPEC and _SPEC.loader
_SERVER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SERVER)


class FixtureRevisionTests(unittest.TestCase):
    def test_revision_reads_parent_artifact_and_persists_only_requested_diff(self):
        with tempfile.TemporaryDirectory(prefix="mplus-alpha-fixture-revision-") as root:
            runtime = AlphaRuntime.isolated(Path(root) / "runtime", port=18991)
            backend = _SERVER.FixtureBackend(runtime)
            first = backend.start_action("start_generation", {"project_id": "project_fixture"}, None)
            second = backend.start_action("fork_revision", {
                "project_id": "project_fixture",
                "parent_run_id": first["run_id"],
                "modification_plan": {"desired_change": "把第 1 镜文案修改为匿名新品开场"},
            }, None)

            parent = backend._read_rows(first["run_id"])
            child = backend._read_rows(second["run_id"])
            parent_bytes = (runtime.runs_root / "fixture" / "runs" / first["run_id"] / "script_rows.json").read_bytes()
            child_bytes = (runtime.runs_root / "fixture" / "runs" / second["run_id"] / "script_rows.json").read_bytes()
            self.assertNotEqual(hashlib.sha256(parent_bytes).hexdigest(), hashlib.sha256(child_bytes).hexdigest())
            self.assertEqual(parent[0]["文案"], "匿名产品开场")
            self.assertEqual(child[0]["文案"], "匿名新品开场")
            self.assertEqual(parent[0]["画面描述"], child[0]["画面描述"])
            self.assertEqual(parent[1], child[1])

            report_path = runtime.runs_root / "fixture" / "runs" / second["run_id"] / "revision_report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["requested_impact"], {"shot_no": "1", "field": "文案", "before": "匿名产品开场", "after": "匿名新品开场"})
            self.assertEqual(report["unauthorized_change_count"], 0)
            self.assertEqual(report["locked_field_violation_count"], 0)
            self.assertTrue(report["locked_field_verification"]["all_preserved"])

            rebuilt_backend = _SERVER.FixtureBackend(runtime)
            self.assertEqual(rebuilt_backend._read_rows(second["run_id"]), child)
