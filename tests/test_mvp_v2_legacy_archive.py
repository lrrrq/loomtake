import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from mvp_v2.cloud_storage import CloudObject
from mvp_v2.cloud_runtime import MemoryCloudRunRepository
from mvp_v2.legacy_archive import (
    LegacyArchiveError,
    build_cold_archive_inventory,
    build_legacy_upload_plan,
    read_cold_archive_inventory,
    read_legacy_catalog,
    upload_legacy_plan,
    verify_legacy_upload_plan,
    verify_legacy_upload_receipt,
    write_cold_archive_inventory,
    write_legacy_upload_plan,
)
from mvp_v2.local_ui import ArtifactRegistry, _catalog_history_rows


class MemoryStorage:
    def __init__(self):
        self.objects = {}

    def content_key(self, digest, *, namespace):
        return f"mplus/{namespace}/{digest}"

    def put_file(self, path, *, key, expected_sha256):
        payload = Path(path).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        assert digest == expected_sha256
        self.objects[key] = payload
        return CloudObject(
            storage_ref=f"cos://private-123/{key}?sha256={digest}",
            sha256=digest,
            size_bytes=len(payload),
            bucket="private-123",
            key=key,
        )

    def put_bytes(self, payload, *, key, expected_sha256=None):
        digest = hashlib.sha256(payload).hexdigest()
        assert not expected_sha256 or digest == expected_sha256
        self.objects[key] = payload
        return CloudObject(
            storage_ref=f"cos://private-123/{key}?sha256={digest}",
            sha256=digest,
            size_bytes=len(payload),
            bucket="private-123",
            key=key,
        )

    def verify(self, storage_ref, digest, size_bytes):
        key = storage_ref.split("private-123/", 1)[1].split("?", 1)[0]
        payload = self.objects[key]
        return hashlib.sha256(payload).hexdigest() == digest and len(payload) == size_bytes


class LegacyArchiveTests(unittest.TestCase):
    def _run(self, root: Path, run_id: str, status: str, *, output="delivery.xlsx"):
        run_dir = root / "assets" / f"project-{run_id}" / "runs" / run_id
        run_dir.mkdir(parents=True)
        manifest = {
            "schema_version": "stage1-pipeline-runtime/v3",
            "run_id": run_id,
            "project_name": f"project-{run_id}",
            "category_id": "launch",
            "status": status,
            "started_at": "2026-08-01T00:00:00Z",
            "finished_at": "2026-08-01T00:01:00Z",
        }
        (run_dir / "run_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        delivery = run_dir / output
        delivery.write_bytes(f"xlsx-{run_id}".encode())
        report = {
            "run_id": run_id,
            "project_name": f"project-{run_id}",
            "status": status,
            "output_path": str(delivery),
            "visual_review_html": str(run_dir / "preview.html"),
        }
        (run_dir / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
        (run_dir / "preview.html").write_bytes(b"large-preview")
        (run_dir / "frame.png").write_bytes(b"image")
        return run_dir

    def test_selects_metadata_success_and_explicit_review_allowlist_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "mvp-v2-run"
            self._run(root, "success", "succeeded")
            self._run(root, "waiting-cold", "waiting_human_review")
            self._run(root, "waiting-selected", "waiting_human_review")
            self._run(root, "blocked", "blocked", output="delivery.best_failed.xlsx")
            self._run(root, "failed", "failed", output="delivery.best_failed.xlsx")

            plan = build_legacy_upload_plan(root, review_run_ids=["waiting-selected"])
            by_id = {item["run_id"]: item for item in plan["runs"]}

            self.assertEqual(plan["summary"]["run_count"], 5)
            self.assertEqual(by_id["success"]["artifact_availability"], "delivery_available")
            self.assertEqual(by_id["waiting-selected"]["artifact_availability"], "review_available")
            self.assertEqual(by_id["waiting-cold"]["artifact_availability"], "metadata_only")
            self.assertFalse(by_id["blocked"]["delivery_files"])
            selected_paths = {item["path"] for item in plan["files"]}
            self.assertFalse(any(path.endswith(".html") or path.endswith(".png") for path in selected_paths))
            self.assertTrue(all(item.get("selection_reason") for item in plan["files"]))
            self.assertTrue(all(item.get("exclusion_reason") for item in plan["excluded_files"]))
            self.assertGreater(plan["summary"]["excluded_file_count"], 0)
            self.assertEqual(verify_legacy_upload_plan(plan, verify_files=True)["status"], "pass")
            plan_path = write_legacy_upload_plan(Path(tmp) / "history-plan.json", plan)
            history = _catalog_history_rows(ArtifactRegistry(), plan_path)
            self.assertEqual(len(history), 5)
            self.assertTrue(all(isinstance(item["updated_at"], float) for item in history))
            self.assertEqual(sum(len(item["artifacts"]) for item in history), 2)

    def test_plan_and_receipt_are_immutable_and_sha_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            self._run(root, "success", "succeeded")
            plan = build_legacy_upload_plan(root)
            path = write_legacy_upload_plan(Path(tmp) / "plan.json", plan)
            self.assertEqual(read_legacy_catalog(path, verify_files=True)["plan_sha256"], plan["plan_sha256"])
            with self.assertRaisesRegex(LegacyArchiveError, "拒绝覆盖"):
                write_legacy_upload_plan(path, plan)

            storage = MemoryStorage()
            repository = MemoryCloudRunRepository()
            receipt = upload_legacy_plan(plan, storage, repository=repository)
            self.assertEqual(verify_legacy_upload_receipt(receipt, storage)["status"], "pass")
            imported = repository.list_legacy_runs()
            self.assertEqual(len(imported), 1)
            self.assertEqual(len(imported[0]["delivery_files"]), 1)
            receipt["runs"][0]["status"] = "failed"
            with self.assertRaisesRegex(LegacyArchiveError, "SHA-256"):
                verify_legacy_upload_receipt(receipt)

    def test_cold_archive_inventory_covers_every_file_and_detects_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            run_dir = self._run(root, "success", "succeeded")
            (root / "large-video.mp4").write_bytes(b"video-not-uploaded")
            (root / "CURRENT").symlink_to(run_dir.relative_to(root))
            (root / "external").symlink_to(Path(tmp) / "outside")
            inventory = build_cold_archive_inventory(root)
            self.assertEqual(inventory["summary"]["file_count"], 6)
            self.assertEqual(inventory["summary"]["symlink_count"], 2)
            self.assertEqual(inventory["summary"]["external_symlink_count"], 1)
            self.assertEqual(inventory["summary"]["broken_symlink_count"], 1)
            self.assertIn(
                "large-video.mp4",
                {item["path"] for item in inventory["files"]},
            )
            path = write_cold_archive_inventory(Path(tmp) / "inventory.json", inventory)
            self.assertEqual(
                read_cold_archive_inventory(path, verify_files=True)["inventory_sha256"],
                inventory["inventory_sha256"],
            )
            (run_dir / "frame.png").write_bytes(b"drift")
            with self.assertRaisesRegex(LegacyArchiveError, "已漂移"):
                read_cold_archive_inventory(path, verify_files=True)


if __name__ == "__main__":
    unittest.main()
