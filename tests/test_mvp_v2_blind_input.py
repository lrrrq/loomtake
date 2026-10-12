import json
import tempfile
import unittest
from pathlib import Path

from mvp_v2.blind_input import (
    BlindInputError,
    seal_blind_input,
    verify_blind_input,
)
from mvp_v2.blind_input_cli import main as blind_input_cli_main
from mvp_v2.run_contract import build_baseline_manifest, sha256_file


class BlindInputContractTests(unittest.TestCase):
    def test_seal_rejects_unapproved_source_documents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            data = json.loads(qualification.read_text(encoding="utf-8"))
            data["approved_source_documents"] = False
            qualification.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(BlindInputError, "资格声明"):
                seal_blind_input(
                    root, brief, media, qualification, root / "seal.json",
                    source_workbook_path=source,
                )

    def test_sealed_input_verifies_without_exposing_project_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            output = root / "seal.json"

            manifest = seal_blind_input(
                root, brief, None, qualification, output,
                source_workbook_path=source,
            )
            verification = verify_blind_input(root, output, _sha256(output))

            self.assertEqual(verification["status"], "pass")
            self.assertEqual(verification["project_code"], "blind-003")
            self.assertEqual(verification["blind_input_seal_sha256"], _sha256(output))
            self.assertEqual(
                verification["blind_input_baseline_sha256"],
                manifest["input_baseline"]["baseline_sha256"],
            )
            self.assertEqual(manifest["project_code"], "blind-003")
            serialized = json.dumps(manifest, ensure_ascii=False)
            self.assertNotIn("第三品牌", serialized)
            self.assertEqual(manifest["input_file_count"], 2)
            self.assertIsNone(manifest["media_scope"])

    def test_verify_detects_changed_or_added_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            output = root / "seal.json"
            seal_blind_input(
                root, brief, media, qualification, output,
                source_workbook_path=source,
            )

            (media / "01.png").write_bytes(b"changed")
            changed = verify_blind_input(root, output, _sha256(output))
            self.assertEqual(changed["status"], "mismatch")
            self.assertIn("sealed/media/01.png", changed["changed_paths"])

            (media / "01.png").write_bytes(b"image-1")
            (media / "03.png").write_bytes(b"new")
            added = verify_blind_input(root, output, _sha256(output))
            self.assertEqual(added["status"], "mismatch")
            self.assertIn("sealed/media/03.png", added["added_paths"])

    def test_cli_seal_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            output = root / "seal.json"
            self.assertEqual(blind_input_cli_main([
                "seal",
                "--project-root", str(root),
                "--brief", str(brief),
                "--source-workbook", str(source),
                "--media-root", str(media),
                "--qualification", str(qualification),
                "--output", str(output),
            ]), 0)
            self.assertEqual(blind_input_cli_main([
                "verify",
                "--project-root", str(root),
                "--manifest", str(output),
                "--expected-sha256", _sha256(output),
            ]), 0)

    def test_seal_accepts_document_only_input_without_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            output = root / "seal.json"
            manifest = seal_blind_input(
                root, brief, None, qualification, output,
                source_workbook_path=source,
            )
            self.assertEqual(manifest["schema_version"], "launch-blind-input/v2")
            self.assertIsNone(manifest["media_scope"])

    def test_seal_rejects_wrong_path_types(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            with self.assertRaisesRegex(BlindInputError, "Brief"):
                seal_blind_input(
                    root, media, media, qualification, root / "seal.json",
                    source_workbook_path=source,
                )
            with self.assertRaisesRegex(BlindInputError, "素材"):
                seal_blind_input(
                    root, brief, brief, qualification, root / "seal.json",
                    source_workbook_path=source,
                )
            with self.assertRaisesRegex(BlindInputError, "源工作簿"):
                seal_blind_input(
                    root, brief, None, qualification, root / "seal.json",
                    source_workbook_path=media,
                )

    def test_seal_rejects_symlinked_media(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            outside = root.parent / f"{root.name}-outside.png"
            outside.write_bytes(b"outside")
            try:
                (media / "linked.png").symlink_to(outside)
                with self.assertRaisesRegex(BlindInputError, "符号链接"):
                    seal_blind_input(
                        root, brief, media, qualification, root / "seal.json",
                        source_workbook_path=source,
                    )
            finally:
                outside.unlink(missing_ok=True)

    def test_verify_requires_external_manifest_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            output = root / "seal.json"
            seal_blind_input(
                root, brief, media, qualification, output,
                source_workbook_path=source,
            )
            with self.assertRaisesRegex(BlindInputError, "外部摘要"):
                verify_blind_input(root, output, "wrong")

    def test_verify_accepts_legacy_v1_seal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, brief, source, media, qualification = _fixture(Path(tmp))
            baseline = build_baseline_manifest(
                root,
                [],
                [brief, media],
                config={"qualification": {"project_code": "blind-003"}},
                reject_symlinks=True,
            )
            output = root / "legacy-seal.json"
            output.write_text(json.dumps({
                "schema_version": "launch-blind-input/v1",
                "project_code": "blind-003",
                "brief_scope": brief.relative_to(root).as_posix(),
                "media_scope": media.relative_to(root).as_posix(),
                "qualification_path": qualification.relative_to(root).as_posix(),
                "qualification_sha256": sha256_file(qualification),
                "input_file_count": len(baseline["files"]),
                "input_baseline": baseline,
            }), encoding="utf-8")

            report = verify_blind_input(root, output, _sha256(output))

            self.assertEqual(report["status"], "pass")


def _fixture(root: Path):
    sealed = root / "sealed"
    media = sealed / "media"
    media.mkdir(parents=True)
    brief = sealed / "brief.xlsx"
    brief.write_bytes(b"third-brand-hidden-brief")
    source = sealed / "approved-source.xlsx"
    source.write_bytes(b"approved-source-document")
    (media / "01.png").write_bytes(b"image-1")
    (media / "02.png").write_bytes(b"image-2")
    qualification = sealed / "qualification.json"
    qualification.write_text(json.dumps({
        "project_code": "blind-003",
        "custodian": "independent-custodian",
        "confirmed_at": "2026-07-14T12:00:00+08:00",
        "authorized_for_use": True,
        "original_brief": True,
        "approved_source_documents": True,
        "not_used_in_development": True,
    }), encoding="utf-8")
    return root, brief, source, media, qualification


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
