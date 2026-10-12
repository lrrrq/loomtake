import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from mvp_v2.project_migration import (
    execute_cutover,
    inventory_targets,
    run_rollback_drill,
)
from mvp_v2.runtime_config import load_legacy_mounts, remap_legacy_path


class ProjectMigrationTests(unittest.TestCase):
    def test_cutover_writes_classified_approved_manifest_and_dynamic_observation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            data = root / "data"
            run = project / "方案/mvp-v2-run/assets/demo/runs/run-1"
            source = project / "整理后脚本库"
            export = project / "网页输出"
            run.mkdir(parents=True)
            source.mkdir(parents=True)
            export.mkdir(parents=True)
            (run / "out.xlsx").write_bytes(b"run")
            (source / "source.xlsx").write_bytes(b"source")
            (export / "index.html").write_text("ok", encoding="utf-8")

            summary = execute_cutover(
                project,
                data,
                "test-cutover",
                targets=(Path("方案/mvp-v2-run"), Path("整理后脚本库"), Path("网页输出")),
                approved_by="migration-reviewer",
                cutover_at=datetime(2027, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                observation_days=14,
            )
            config = {"legacy_mounts_path": str(data / "legacy-mounts.json")}
            mounts = load_legacy_mounts(config)
            self.assertEqual(summary["status"], "complete")
            self.assertEqual(summary["unclassified_count"], 0)
            self.assertEqual(summary["approval_status"], "approved")
            self.assertEqual(summary["observation_started_at"], "2027-01-02T03:04:05Z")
            self.assertEqual(summary["observation_until"], "2027-01-16T03:04:05Z")
            self.assertFalse((project / "整理后脚本库").exists())
            self.assertTrue((data / "legacy-archive/test-cutover/整理后脚本库/source.xlsx").is_file())
            self.assertEqual({item["mode"] for item in mounts}, {"read_only"})
            remapped = remap_legacy_path(
                project / "方案/mvp-v2-run/assets/demo/runs/run-1/out.xlsx",
                config,
            )
            self.assertEqual(remapped.read_bytes(), b"run")

            manifest = data / "logs/governance/test-cutover/source-manifest.jsonl"
            records = [json.loads(line) for line in manifest.read_text().splitlines()]
            required = {
                "source_path",
                "target_path",
                "target_ref",
                "mtime_ns",
                "disposition",
                "approval_status",
                "approved_by",
                "approved_at",
            }
            self.assertTrue(records)
            self.assertTrue(all(required <= set(record) for record in records))
            self.assertTrue(all(record["disposition"] != "unclassified" for record in records))
            self.assertTrue(all(record["approval_status"] == "approved" for record in records))
            self.assertTrue(any(record["sha256"] for record in records))

    def test_cutover_refuses_unclassified_or_unapproved_records_before_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            data = root / "data"
            custom = project / "custom-data"
            custom.mkdir(parents=True)
            (custom / "record.bin").write_bytes(b"preserve")

            with self.assertRaisesRegex(ValueError, "unclassified"):
                execute_cutover(
                    project,
                    data,
                    "unclassified-cutover",
                    targets=(Path("custom-data"),),
                    approved_by="reviewer",
                )
            self.assertEqual((custom / "record.bin").read_bytes(), b"preserve")

            with self.assertRaisesRegex(ValueError, "批准人"):
                execute_cutover(
                    project,
                    data,
                    "unapproved-cutover",
                    targets=(Path("custom-data"),),
                    dispositions={"custom-data": "retain_readonly"},
                )
            self.assertEqual((custom / "record.bin").read_bytes(), b"preserve")

    def test_rollback_drill_is_read_only_and_verifies_rpo_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            data = root / "data"
            source = project / "整理后脚本库"
            source.mkdir(parents=True)
            (source / "source.xlsx").write_bytes(b"source")
            execute_cutover(
                project,
                data,
                "rollback-cutover",
                targets=(Path("整理后脚本库"),),
                approved_by="migration-reviewer",
            )
            evidence = data / "logs/governance/rollback-cutover"
            incremental_root = data / "runs"
            incremental = incremental_root / "run-new/result.xlsx"
            incremental.parent.mkdir(parents=True)
            incremental.write_bytes(b"new-run")
            incremental_records = inventory_targets(
                data,
                data,
                targets=(Path("runs/run-new"),),
                target_root=data,
                cutover_id="rollback-cutover",
                dispositions={"runs/run-new": "retain_readonly"},
                approved_by="migration-reviewer",
            )
            incremental_manifest = evidence / "incremental-manifest.jsonl"
            incremental_manifest.write_text(
                "".join(
                    json.dumps(record.__dict__, ensure_ascii=False, sort_keys=True) + "\n"
                    for record in incremental_records
                ),
                encoding="utf-8",
            )
            watched = [
                evidence / "source-manifest.jsonl",
                evidence / "target-manifest.jsonl",
                incremental_manifest,
                data / "legacy-archive/rollback-cutover/整理后脚本库/source.xlsx",
                incremental,
            ]
            before = {str(path): self._fingerprint(path) for path in watched}

            report = run_rollback_drill(
                evidence / "source-manifest.jsonl",
                evidence / "target-manifest.jsonl",
                incremental_manifest,
                rto_limit_seconds=3600,
            )

            after = {str(path): self._fingerprint(path) for path in watched}
            self.assertEqual(before, after)
            self.assertTrue(report["read_only"])
            self.assertTrue(report["rpo_zero_data_ready"])
            self.assertTrue(report["rto_within_limit"])
            self.assertEqual(report["status"], "ready_for_live_switch_drill")
            self.assertEqual(report["missing_or_changed_records"], [])
            self.assertEqual(report["unclassified_count"], 0)
            self.assertEqual(report["unapproved_count"], 0)
            self.assertEqual(report["rto_evidence_scope"], "read_only_manifest_drill")
            self.assertFalse(report["live_switch_verified"])
            self.assertGreaterEqual(report["duration_seconds"], 0)

            incremental.write_bytes(b"tampered")
            blocked = run_rollback_drill(
                evidence / "source-manifest.jsonl",
                evidence / "target-manifest.jsonl",
                incremental_manifest,
            )
            self.assertFalse(blocked["rpo_zero_data_ready"])
            self.assertIn(str(data / "runs/run-new/result.xlsx"), blocked["missing_or_changed_records"])

    def test_rollback_drill_blocks_rpo_claim_when_increment_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            data = root / "data"
            source = project / "整理后脚本库"
            source.mkdir(parents=True)
            (source / "source.xlsx").write_bytes(b"source")
            execute_cutover(
                project,
                data,
                "rollback-blocked",
                targets=(Path("整理后脚本库"),),
                approved_by="migration-reviewer",
            )
            evidence = data / "logs/governance/rollback-blocked"

            report = run_rollback_drill(
                evidence / "source-manifest.jsonl",
                evidence / "target-manifest.jsonl",
                None,
            )

            self.assertFalse(report["rpo_zero_data_ready"])
            self.assertIn("缺少切换后增量 manifest", report["blockers"])

    def test_rollback_drill_blocks_ready_status_when_read_only_rto_is_exceeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            data = root / "data"
            source = project / "整理后脚本库"
            source.mkdir(parents=True)
            (source / "source.xlsx").write_bytes(b"source")
            execute_cutover(
                project,
                data,
                "rollback-slow",
                targets=(Path("整理后脚本库"),),
                approved_by="migration-reviewer",
            )
            evidence = data / "logs/governance/rollback-slow"
            incremental_manifest = evidence / "incremental-manifest.jsonl"
            incremental_manifest.write_text("", encoding="utf-8")

            report = run_rollback_drill(
                evidence / "source-manifest.jsonl",
                evidence / "target-manifest.jsonl",
                incremental_manifest,
                rto_limit_seconds=1e-12,
            )

            self.assertTrue(report["rpo_zero_data_ready"])
            self.assertFalse(report["rto_within_limit"])
            self.assertEqual(report["status"], "blocked")
            self.assertFalse(report["live_switch_verified"])
            self.assertIn("只读 manifest 核对耗时超过 RTO 上限", report["blockers"])

    @staticmethod
    def _fingerprint(path: Path) -> tuple[int, int, str]:
        stat = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        return stat.st_size, stat.st_mtime_ns, digest


if __name__ == "__main__":
    unittest.main()
