from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts.audit_formal_uat_controls import EvidenceError, audit, validate_control_record
from scripts.run_formal_uat_readiness_evidence import COMMANDS
from tests.formal_uat_control_manifest import controls


class FormalControlEvidenceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-control-evidence-audit-")
        self.root = Path(self.temporary.name)
        self.code_sha = "a" * 40

    def tearDown(self):
        self.temporary.cleanup()

    def _file(self, name: str) -> dict[str, str]:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"anonymous:{name}".encode("utf-8"))
        return {
            "path": name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def _record(self, spec: dict) -> dict:
        fingerprint = "b" * 64
        return {
            "schema_version": 1,
            "control_id": spec["control_id"],
            "title": spec["title"],
            "selector": spec["selector"],
            "code_sha": self.code_sha,
            "journey_id": "anonymous-control-proof",
            "observed_at_utc": "2026-08-27T00:00:00Z",
            "test_command": "python -m unittest anonymous",
            "test_exit_code": 0,
            "test_log": self._file(f"logs/{spec['control_id']}.log"),
            "terminal": {
                "status": "PASS",
                "result": "达到真实业务终局",
                "url": "http://127.0.0.1/projects",
                "api": {"status": "PASS", "result": "HTTP 200"},
                "database": {"status": "PASS", "result": "权威行已核验"},
                "artifact": {
                    "status": "NOT_APPLICABLE",
                    "result": "不产生 artifact",
                    "reason": "只读控件",
                },
            },
            "screenshots": {
                "main": self._file(f"screens/{spec['control_id']}-main.png"),
                "viewports": [
                    {
                        "width": width,
                        "height": 812 if width == 375 else 900 if width == 1024 else 1000,
                        "status": "PASS",
                        "horizontal_overflow": False,
                        **self._file(f"screens/{spec['control_id']}-{width}.png"),
                    }
                    for width in (375, 1024, 1440)
                ],
            },
            "keyboard": {
                "status": "PASS",
                "result": "Tab 焦点已验证",
                "selector": spec["selector"],
                "used_fallback": False,
            },
            "double_click": {
                "status": "PASS",
                "result": "没有第二次权威写入",
                "selector": spec["selector"],
                "authority_write_count_before": 7,
                "authority_write_count_after": 7,
                "authority_sha256_before": fingerprint,
                "authority_sha256_after": fingerprint,
            },
            "failure_recovery": {
                "status": "PASS",
                "result": "离线失败闭合并可恢复",
                "selector": spec["selector"],
            },
        }

    def test_valid_record_requires_exact_sha_files_viewports_and_authority_snapshot(self):
        spec = controls()[0]
        record = self._record(spec)
        result = validate_control_record(
            record, spec=spec, evidence_root=self.root, expected_sha=self.code_sha,
        )
        self.assertEqual(result["control_id"], "M01")

    def test_changed_authority_snapshot_is_rejected_even_when_row_count_is_equal(self):
        spec = controls()[0]
        record = self._record(spec)
        record["double_click"]["authority_sha256_after"] = "c" * 64
        with self.assertRaisesRegex(EvidenceError, "逻辑快照不同"):
            validate_control_record(
                record, spec=spec, evidence_root=self.root, expected_sha=self.code_sha,
            )

    def test_audit_fails_closed_when_any_of_sixty_records_is_missing(self):
        (self.root / "controls").mkdir()
        first = controls()[0]
        (self.root / "controls" / "M01.json").write_text(
            json.dumps(self._record(first), ensure_ascii=False), encoding="utf-8",
        )
        with self.assertRaisesRegex(EvidenceError, "缺少 M02"):
            audit(self.root, self.code_sha)

    def test_readiness_runner_stops_each_test_stage_on_first_failure(self):
        for stage in ("focused", "browser", "zero-images", "controls", "full"):
            self.assertIn("-f", COMMANDS[stage], stage)
        self.assertIn("-x", COMMANDS["pytest"])
        self.assertLess(
            COMMANDS["full"].index("discover"),
            COMMANDS["full"].index("-s"),
        )


if __name__ == "__main__":
    unittest.main()
