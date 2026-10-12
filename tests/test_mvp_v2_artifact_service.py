import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from mvp_v2.artifact_service import ArtifactResolutionError, RunArtifactResolver


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RunArtifactResolverTests(unittest.TestCase):
    def _fixture(self, root: Path, run_id: str = "1a23456789abcdef") -> tuple[Path, Path]:
        run_dir = root / "匿名项目" / "runs" / run_id
        run_dir.mkdir(parents=True)
        report = run_dir / "run_report.json"
        report.write_text('{"status":"waiting_human_review"}\n', encoding="utf-8")
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "project_name": "匿名项目",
                    "status": "waiting_human_review",
                    "artifacts": {
                        "run_report": {
                            "path": str(report),
                            "sha256": _sha(report),
                            "size_bytes": report.stat().st_size,
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return run_dir, manifest

    def test_resolves_bound_manifest_and_artifact_without_exposing_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, manifest = self._fixture(root)
            resolver = RunArtifactResolver(root)

            loaded = resolver.read_manifest(
                "1a23456789abcdef",
                expected_sha256=_sha(manifest),
            )
            artifact = resolver.resolve_artifact("1a23456789abcdef", "run_report")

            self.assertEqual(loaded["run_id"], "1a23456789abcdef")
            self.assertEqual(artifact.path, (run_dir / "run_report.json").resolve())
            self.assertEqual(
                artifact.ref,
                "artifact://run/1a23456789abcdef/run_report",
            )

    def test_rejects_path_traversal_duplicate_run_and_hash_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, manifest = self._fixture(root)
            resolver = RunArtifactResolver(root)

            with self.assertRaisesRegex(ArtifactResolutionError, "INVALID_INPUT"):
                resolver.read_manifest("../escape")
            with self.assertRaisesRegex(ArtifactResolutionError, "ARTIFACT_HASH_MISMATCH"):
                resolver.read_manifest("1a23456789abcdef", expected_sha256="0" * 64)

            duplicate = root / "另一个项目" / "runs" / "1a23456789abcdef"
            duplicate.mkdir(parents=True)
            (duplicate / "run_manifest.json").write_bytes(manifest.read_bytes())
            with self.assertRaisesRegex(ArtifactResolutionError, "ARTIFACT_AMBIGUOUS"):
                resolver.read_manifest("1a23456789abcdef")

    def test_rejects_manifest_artifact_outside_run_or_changed_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir, manifest = self._fixture(root)
            resolver = RunArtifactResolver(root)
            outside = root / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["artifacts"]["run_report"]["path"] = str(outside)
            payload["artifacts"]["run_report"]["sha256"] = _sha(outside)
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(ArtifactResolutionError, "ARTIFACT_SCOPE_VIOLATION"):
                resolver.resolve_artifact("1a23456789abcdef", "run_report")

            payload["artifacts"]["run_report"]["path"] = str(run_dir / "run_report.json")
            payload["artifacts"]["run_report"]["sha256"] = "f" * 64
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ArtifactResolutionError, "ARTIFACT_HASH_MISMATCH"):
                resolver.resolve_artifact("1a23456789abcdef", "run_report")


if __name__ == "__main__":
    unittest.main()
