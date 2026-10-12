import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.pipeline_runtime import migrate_legacy_release, open_pipeline_run, publish_pipeline_run


class PublicationAuthorityTests(unittest.TestCase):
    def test_legacy_pipeline_never_writes_current_without_explicit_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run(
                {"项目名称": "匿名项目"}, root / "cache", root, "test"
            ) as state:
                artifacts = {}
                for name in ("workbook", "audit", "loop_log", "visual_review", "run_report"):
                    artifact = state.run_dir / f"{name}.fixture"
                    artifact.write_bytes(b"anonymous")
                    artifacts[name] = artifact
                publish_pipeline_run(state, "completed", artifacts)
                self.assertFalse((state.run_dir.parent.parent / "CURRENT").exists())

    def test_publish_api_rejects_legacy_current_adapter_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with open_pipeline_run({"项目名称": "匿名项目"}, root / "cache", root, "test") as state:
                artifacts = {}
                for name in ("workbook", "audit", "loop_log", "visual_review", "run_report"):
                    path = state.run_dir / f"{name}.fixture"
                    path.write_bytes(b"anonymous")
                    artifacts[name] = path
                with self.assertRaises(TypeError):
                    publish_pipeline_run(state, "completed", artifacts, legacy_current_adapter=lambda _state: None)

    def test_legacy_migration_cannot_call_current_writer_or_create_publication_outbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache_dir = root / "cache"
            public_paths = {
                "workbook": root / "formal" / "result.xlsx",
                "audit": root / "formal" / "result.xlsx.audit.json",
                "loop_log": root / "formal" / "result.xlsx.loop.json",
                "visual_review": root / "formal" / "result.visual_review.html",
                "run_report": cache_dir / "run_report.json",
            }
            for name, path in public_paths.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"anonymous legacy {name}".encode("utf-8"))
            artifact_paths = {
                name: root / "new-run" / f"{name}.fixture"
                for name in public_paths
            }

            with patch("mvp_v2.pipeline_runtime._write_current_atomic", create=True) as current_writer:
                migrated = migrate_legacy_release(cache_dir, public_paths, artifact_paths)

            current_writer.assert_not_called()
            self.assertIsNotNone(migrated)
            self.assertFalse((cache_dir / "CURRENT").exists())
            self.assertFalse((cache_dir / "workspace.sqlite3").exists())


if __name__ == "__main__":
    unittest.main()
