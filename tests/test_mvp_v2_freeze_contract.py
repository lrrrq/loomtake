import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from mvp_v2.freeze_contract import (
    FreezeContractError,
    create_freeze_manifest,
    verify_freeze_manifest,
)
from mvp_v2.freeze_cli import main as freeze_cli_main
from mvp_v2.review_package import export_review_workbook
from tests.test_mvp_v2_acceptance import _published_release


class FreezeContractTests(unittest.TestCase):
    def test_freeze_refuses_pending_dual_project_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, public_input = _fixture(Path(tmp))

            with patch(
                "mvp_v2.freeze_contract.build_dual_project_acceptance",
                return_value=_acceptance("ready_for_review"),
            ) as build_acceptance:
                with self.assertRaisesRegex(FreezeContractError, "双项目人工质量门"):
                    create_freeze_manifest(
                        root, [source, root / "tests"], [public_input], _cases(root), root / "freeze.json"
                    )
            build_acceptance.assert_called_once_with(_cases(root))

    def test_freeze_records_scopes_and_verifies_unchanged_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, public_input = _fixture(Path(tmp))
            output = root / "freeze.json"

            with patch(
                "mvp_v2.freeze_contract.build_dual_project_acceptance",
                return_value=_acceptance("quality_pass"),
            ):
                manifest = create_freeze_manifest(
                    root, [source, root / "tests"], [public_input], _cases(root), output
                )
            verification = verify_freeze_manifest(root, output, _sha256(output))
            acceptance_path = root / "freeze.dual_acceptance.json"

            self.assertEqual(verification["status"], "pass")
            self.assertEqual(manifest["schema_version"], "launch-blind-freeze/v1")
            self.assertEqual(manifest["dual_acceptance_sha256"], _sha256(acceptance_path))
            self.assertEqual(manifest["source_scopes"], ["mvp_v2", "tests"])
            self.assertTrue(manifest["baseline"]["baseline_sha256"])
            self.assertEqual(verification["freeze_manifest_sha256"], _sha256(output))
            self.assertEqual(verification["dual_acceptance_sha256"], _sha256(acceptance_path))
            self.assertTrue(verification["freeze_batch_digest"])
            self.assertEqual(
                verification["benchmark_runs"],
                {"美的": "run-美的", "林氏": "run-林氏"},
            )

    def test_verify_detects_modified_and_new_source_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, public_input = _fixture(Path(tmp))
            output = root / "freeze.json"
            with patch(
                "mvp_v2.freeze_contract.build_dual_project_acceptance",
                return_value=_acceptance("quality_pass"),
            ):
                create_freeze_manifest(root, [source, root / "tests"], [public_input], _cases(root), output)

            (source / "core.py").write_text("changed", encoding="utf-8")
            modified = verify_freeze_manifest(root, output, _sha256(output))
            self.assertEqual(modified["status"], "mismatch")
            self.assertIn("mvp_v2/core.py", modified["changed_paths"])

            (source / "core.py").write_text("stable", encoding="utf-8")
            (source / "new_rule.py").write_text("new", encoding="utf-8")
            added = verify_freeze_manifest(root, output, _sha256(output))
            self.assertEqual(added["status"], "mismatch")
            self.assertIn("mvp_v2/new_rule.py", added["added_paths"])

    def test_cli_create_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, public_input = _fixture(Path(tmp))
            output = root / "freeze.json"

            with patch(
                "mvp_v2.freeze_contract.build_dual_project_acceptance",
                return_value=_acceptance("quality_pass"),
            ):
                create_code = freeze_cli_main([
                    "create",
                    "--project-root", str(root),
                    "--source", str(source),
                    "--source", str(root / "tests"),
                    "--input", str(public_input),
                    "--case", str(root / "cache-a"), str(root / "review-a.xlsx"),
                    "--case", str(root / "cache-b"), str(root / "review-b.xlsx"),
                    "--output", str(output),
                ])
            verify_code = freeze_cli_main([
                "verify",
                "--project-root", str(root),
                "--manifest", str(output),
                "--expected-sha256", _sha256(output),
            ])

            self.assertEqual(create_code, 0)
            self.assertEqual(verify_code, 0)

    def test_freeze_cli_ignores_forged_acceptance_and_rejects_missing_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, public_input = _fixture(Path(tmp))
            output = root / "freeze.json"
            output.with_name("freeze.dual_acceptance.json").write_text(
                json.dumps(_acceptance("quality_pass"), ensure_ascii=False),
                encoding="utf-8",
            )

            code = freeze_cli_main([
                "create",
                "--project-root", str(root),
                "--source", str(source),
                "--source", str(root / "tests"),
                "--input", str(public_input),
                "--case", str(root / "fake-a"), str(root / "review-a.xlsx"),
                "--case", str(root / "fake-b"), str(root / "review-b.xlsx"),
                "--output", str(output),
            ])

            self.assertEqual(code, 1)
            self.assertFalse(output.exists())

    def test_freeze_cli_rejects_pending_real_current_without_mock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, public_input = _fixture(Path(tmp))
            first = _published_release(root, "benchmark-a")
            second = _published_release(root, "benchmark-b")
            first_review = export_review_workbook(first, root / "reviews" / "a.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "b.xlsx")
            output = root / "freeze.json"

            code = freeze_cli_main([
                "create",
                "--project-root", str(root),
                "--source", str(source),
                "--source", str(root / "tests"),
                "--input", str(public_input),
                "--case", str(first), str(first_review),
                "--case", str(second), str(second_review),
                "--output", str(output),
            ])

            self.assertEqual(code, 1)
            self.assertFalse(output.exists())

    def test_freeze_rejects_empty_public_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _ = _fixture(Path(tmp))

            with self.assertRaisesRegex(FreezeContractError, "公共输入"):
                create_freeze_manifest(
                    root, [source, root / "tests"], [], _cases(root), root / "freeze.json"
                )

    def test_verify_requires_external_manifest_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, public_input = _fixture(Path(tmp))
            output = root / "freeze.json"
            with patch(
                "mvp_v2.freeze_contract.build_dual_project_acceptance",
                return_value=_acceptance("quality_pass"),
            ):
                create_freeze_manifest(root, [source, root / "tests"], [public_input], _cases(root), output)

            with self.assertRaisesRegex(FreezeContractError, "外部摘要"):
                verify_freeze_manifest(root, output, "wrong")

    def test_freeze_rejects_symlink_inside_required_source_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, public_input = _fixture(Path(tmp))
            outside = root.parent / f"{root.name}-outside.py"
            outside.write_text("outside", encoding="utf-8")
            try:
                (source / "linked.py").symlink_to(outside)
                with patch(
                    "mvp_v2.freeze_contract.build_dual_project_acceptance",
                    return_value=_acceptance("quality_pass"),
                ):
                    with self.assertRaisesRegex(FreezeContractError, "符号链接"):
                        create_freeze_manifest(root, [source, root / "tests"], [public_input], _cases(root), root / "freeze.json")
            finally:
                outside.unlink(missing_ok=True)


def _fixture(root: Path):
    source = root / "mvp_v2"
    source.mkdir(parents=True)
    (source / "core.py").write_text("stable", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_gate.py").write_text("stable", encoding="utf-8")
    public_input = root / "library.xlsx"
    public_input.write_bytes(b"library")
    return root, source, public_input


def _cases(root: Path):
    return [
        (root / "cache-a", root / "review-a.xlsx"),
        (root / "cache-b", root / "review-b.xlsx"),
    ]


def _acceptance(acceptance_status: str):
    human_status = "quality_pass" if acceptance_status == "quality_pass" else "pending"
    project = lambda name: {
        "project_name": name,
        "run_id": f"run-{name}",
        "machine_ready": True,
        "human_review_status": human_status,
        "manifest_sha256": f"manifest-{name}",
        "analysis_sha256": f"analysis-{name}",
        "cache_fingerprint": f"cache-{name}",
        "stage1_editorial_gate": {
            "per_shot": {"row_count": 18},
            "adjacent_pairs": {"edge_count": 17},
            "full_film": {"row_count": 1},
        },
    }
    return {
        "schema_version": "dual-project-acceptance/v1",
        "status": acceptance_status,
        "machine_status": "pass",
        "human_review_status": human_status,
        "release_decision": "stage2_pass" if acceptance_status == "quality_pass" else "blocked",
        "project_count": 2,
        "review_contract": {"shot_count": 36, "adjacent_edge_count": 34, "full_film_count": 2},
        "projects": [project("美的"), project("林氏")],
    }


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
