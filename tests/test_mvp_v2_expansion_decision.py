from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.expansion_decision import (
    ExpansionDecisionError,
    build_expansion_evidence,
    decide_type_expansion,
)
from mvp_v2.expansion_decision_cli import main as expansion_decision_cli_main
from mvp_v2.blind_input_cli import main as blind_input_cli_main
from mvp_v2.freeze_cli import main as freeze_cli_main
from mvp_v2.review_package import export_review_workbook
from tests.test_mvp_v2_acceptance import (
    _complete_review,
    _published_release,
    _rewrite_run_report,
)
from tests.test_mvp_v2_blind_input import _fixture as _blind_input_fixture


class ExpansionDecisionTests(unittest.TestCase):
    def test_three_clean_release_pass_projects_allow_exhibition_entry(self):
        projects = [_project("midea"), _project("linsy"), _project("blind", passed_iteration=1)]

        result = decide_type_expansion(projects, common_rework_count=0, common_all_three_count=0)

        self.assertEqual(result["decision"], "enter_exhibition")
        self.assertEqual(result["overall_modification_rate"], 0)
        self.assertEqual(result["blocking_reasons"], [])

    def test_nonblocking_quality_gap_continues_launch_improvement(self):
        projects = [
            _project("midea"),
            _project("linsy", release_decision="trial_pass", human_review_status="quality_fail", needs_revision_count=2),
            _project("blind", passed_iteration=2),
        ]

        result = decide_type_expansion(projects, common_rework_count=2, common_all_three_count=0)

        self.assertEqual(result["decision"], "continue_launch")
        self.assertIn("未满足进入展会的全部质量条件", result["reasons"])

    def test_machine_or_evidence_failure_stops_type_expansion(self):
        projects = [_project("midea"), _project("linsy"), _project("blind", machine_status="fail", hard_fail_count=1)]

        result = decide_type_expansion(projects, common_rework_count=0, common_all_three_count=0)

        self.assertEqual(result["decision"], "stop_expansion")
        self.assertTrue(any("机器硬门" in reason for reason in result["blocking_reasons"]))

    def test_exactly_three_distinct_projects_are_required(self):
        with self.assertRaisesRegex(ExpansionDecisionError, "三个"):
            decide_type_expansion([_project("midea"), _project("linsy")], 0, 0)

    def test_missing_required_project_evidence_is_rejected(self):
        projects = [_project("midea"), _project("linsy"), _project("blind")]
        del projects[0]["visual_evidence_rate"]
        with self.assertRaisesRegex(ExpansionDecisionError, "visual_evidence_rate"):
            decide_type_expansion(projects, 0, 0)

    def test_negative_or_inconsistent_review_counts_are_rejected(self):
        projects = [_project("midea"), _project("linsy"), _project("blind")]
        projects[0]["needs_revision_count"] = -1
        with self.assertRaisesRegex(ExpansionDecisionError, "负数"):
            decide_type_expansion(projects, 0, 0)

        projects[0]["needs_revision_count"] = 1
        with self.assertRaisesRegex(ExpansionDecisionError, "N 镜"):
            decide_type_expansion(projects, 0, 0)

    def test_cli_writes_decision_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "decision.json"
            paths = _evidence_files(root)

            with _verified_sources(paths):
                code = expansion_decision_cli_main([
                    *_cli_evidence_args(paths),
                    "--blind-case", str(root / "blind-cache"), str(root / "blind-review.xlsx"),
                    "--output", str(output),
                ])

            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["decision"], "continue_launch")

    def test_evidence_builder_derives_projects_and_common_issue_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _evidence_files(root)
            evidence = _build(paths)

            self.assertEqual(len(evidence["projects"]), 3)
            self.assertEqual(evidence["common_rework_count"], 1)
            self.assertEqual(evidence["common_all_three_count"], 1)
            self.assertTrue(all(row["release_decision"] == "release_pass" for row in evidence["projects"]))

    def test_evidence_builder_verifies_manifests_and_reads_frozen_dual_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _evidence_files(root)
            freeze_manifest = root / "freeze.json"
            blind_seal = root / "blind-seal.json"
            _write(freeze_manifest, {"dual_acceptance_path": paths["dual_acceptance"].name})
            _write(blind_seal, {"schema_version": "launch-blind-input/v1"})
            freeze_verification = _read(paths["freeze_verification"])
            blind_verification = _read(paths["blind_input_verification"])

            with (
                patch(
                    "mvp_v2.expansion_decision.verify_freeze_manifest",
                    return_value=freeze_verification,
                    create=True,
                ) as verify_freeze,
                patch(
                    "mvp_v2.expansion_decision.verify_blind_input",
                    return_value=blind_verification,
                    create=True,
                ) as verify_blind,
                patch(
                    "mvp_v2.expansion_decision.build_project_acceptance",
                    return_value=_formal_blind_acceptance(paths),
                ),
            ):
                evidence = build_expansion_evidence(
                    root,
                    freeze_manifest,
                    _sha256(freeze_manifest),
                    blind_seal,
                    _sha256(blind_seal),
                    (root / "blind-cache", root / "blind-review.xlsx"),
                    paths["timing_evidence"],
                    _sha256(paths["timing_evidence"]),
                )

            verify_freeze.assert_called_once_with(root, freeze_manifest, _sha256(freeze_manifest))
            verify_blind.assert_called_once_with(root, blind_seal, _sha256(blind_seal))
            self.assertEqual(evidence["evidence_binding"]["dual_acceptance_sha256"], _sha256(paths["dual_acceptance"]))

    def test_evidence_builder_rejects_unanchored_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _evidence_files(Path(tmp))
            with self.assertRaisesRegex(ExpansionDecisionError, "外部摘要"):
                _build(paths, timing_sha256="0" * 64)

    def test_evidence_builder_rejects_dual_acceptance_from_another_freeze(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _evidence_files(root)
            freeze = _read(paths["freeze_verification"])
            freeze["dual_acceptance_sha256"] = "0" * 64
            _write(paths["freeze_verification"], freeze)

            with self.assertRaisesRegex(ExpansionDecisionError, "外部摘要"):
                _build(paths)

    def test_evidence_builder_rejects_blind_project_identity_or_run_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _evidence_files(root)
            blind = _read(paths["blind_acceptance"])
            blind["project"]["project_name"] = "benchmark-project"
            _write(paths["blind_acceptance"], blind)

            with self.assertRaisesRegex(ExpansionDecisionError, "盲测项目身份"):
                _build(paths)

            paths = _evidence_files(root)
            blind = _read(paths["blind_acceptance"])
            blind["project"]["run_id"] = ""
            _write(paths["blind_acceptance"], blind)
            with self.assertRaisesRegex(ExpansionDecisionError, "run 缺失"):
                _build(paths)

    def test_evidence_builder_rejects_blind_seal_or_freeze_batch_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _evidence_files(root)
            blind_verification = _read(paths["blind_input_verification"])
            blind_verification["blind_input_seal_sha256"] = "0" * 64
            _write(paths["blind_input_verification"], blind_verification)
            with self.assertRaisesRegex(ExpansionDecisionError, "封存摘要"):
                _build(paths)

            paths = _evidence_files(root)
            freeze = _read(paths["freeze_verification"])
            freeze["freeze_batch_digest"] = "0" * 64
            _write(paths["freeze_verification"], freeze)
            with self.assertRaisesRegex(ExpansionDecisionError, "冻结批次"):
                _build(paths)

    def test_evidence_builder_rejects_benchmark_run_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _evidence_files(Path(tmp))
            freeze = _read(paths["freeze_verification"])
            freeze["benchmark_runs"]["midea"] = "run-swapped"
            _write(paths["freeze_verification"], freeze)
            with self.assertRaisesRegex(ExpansionDecisionError, "基准项目.*run"):
                _build(paths)

    def test_production_cli_rejects_caller_supplied_verification_and_dual_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = _evidence_files(root)
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "mvp_v2.expansion_decision_cli",
                    *_legacy_cli_evidence_args(paths),
                    "--blind-case", str(root / "blind-cache"), str(root / "blind-review.xlsx"),
                    "--output", str(root / "decision.json"),
                ],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 2)
            self.assertIn("--project-root", result.stderr)
            self.assertFalse((root / "decision.json").exists())

    def test_expansion_cli_directly_verifies_real_freeze_and_blind_seal_e2e(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "mvp_v2"
            tests_scope = root / "tests"
            source.mkdir()
            tests_scope.mkdir()
            (source / "core.py").write_text("frozen", encoding="utf-8")
            (tests_scope / "test_gate.py").write_text("frozen", encoding="utf-8")
            public_input = root / "library.xlsx"
            public_input.write_bytes(b"public-input")

            benchmark_cases = []
            for code in ("benchmark-a", "benchmark-b"):
                cache = _published_release(root, code)
                _rewrite_run_report(cache, passed_iteration=1, loop_count=1)
                review = export_review_workbook(cache, root / "reviews" / f"{code}.xlsx")
                _complete_review(review)
                benchmark_cases.append((cache, review))

            freeze_manifest = root / "freeze.json"
            freeze_code = freeze_cli_main([
                "create",
                "--project-root", str(root),
                "--source", str(source),
                "--source", str(tests_scope),
                "--input", str(public_input),
                *[
                    value
                    for cache, review in benchmark_cases
                    for value in ("--case", str(cache), str(review))
                ],
                "--output", str(freeze_manifest),
            ])
            self.assertEqual(freeze_code, 0)

            _, brief, source_workbook, media, qualification = _blind_input_fixture(root)
            blind_seal = root / "blind-seal.json"
            seal_code = blind_input_cli_main([
                "seal",
                "--project-root", str(root),
                "--brief", str(brief),
                "--source-workbook", str(source_workbook),
                "--media-root", str(media),
                "--qualification", str(qualification),
                "--output", str(blind_seal),
            ])
            self.assertEqual(seal_code, 0)

            blind_cache = _published_release(root, "blind-003")
            _rewrite_run_report(blind_cache, passed_iteration=1, loop_count=1)
            blind_review = export_review_workbook(
                blind_cache, root / "reviews" / "blind-003.xlsx"
            )
            _complete_review(blind_review)
            timing = root / "timing.json"
            _write(timing, {
                "schema_version": "launch-timing-evidence/v1",
                "projects": {
                    code: {"manual_active_minutes": 100, "assisted_active_minutes": 50}
                    for code in ("benchmark-a", "benchmark-b", "blind-003")
                },
            })
            output = root / "decision.json"

            code = expansion_decision_cli_main([
                "--project-root", str(root),
                "--freeze-manifest", str(freeze_manifest),
                "--freeze-manifest-sha256", _sha256(freeze_manifest),
                "--blind-input-seal", str(blind_seal),
                "--blind-input-seal-sha256", _sha256(blind_seal),
                "--timing-evidence", str(timing),
                "--timing-evidence-sha256", _sha256(timing),
                "--blind-case", str(blind_cache), str(blind_review),
                "--output", str(output),
            ])

            self.assertEqual(code, 0)
            decision = _read(output)
            self.assertEqual(decision["decision"], "enter_exhibition")
            self.assertEqual(
                {project["project_code"] for project in decision["projects"]},
                {"benchmark-a", "benchmark-b", "blind-003"},
            )


def _project(project_code: str, **overrides) -> dict:
    value = {
        "project_code": project_code,
        "role": "blind" if project_code == "blind" else "benchmark",
        "evidence_status": "pass",
        "machine_status": "pass",
        "hard_fail_count": 0,
        "warning_count": 0,
        "human_review_status": "quality_pass",
        "release_decision": "release_pass",
        "passed_iteration": 1,
        "shot_count": 18,
        "adjacent_edge_count": 17,
        "full_film_count": 1,
        "duration_seconds": 60,
        "approved_count": 18,
        "needs_revision_count": 0,
        "rejected_count": 0,
        "major_change_count": 0,
        "high_severity_rejected_count": 0,
        "source_reference_rate": 1.0,
        "visual_analysis_coverage": 1.0,
        "visual_evidence_rate": 1.0,
        "brand_conflict_count": 0,
        "duplicate_image_count": 0,
    }
    value.update(overrides)
    if "approved_count" not in overrides and (
        "needs_revision_count" in overrides or "rejected_count" in overrides
    ):
        value["approved_count"] = 18 - value["needs_revision_count"] - value["rejected_count"]
    return value


def _evidence_files(root: Path) -> dict[str, Path]:
    issue = {"slot_index": 3, "dimension": "visual", "issue_code": "VISUAL_MISMATCH", "severity": "低", "status": "需修改"}
    def acceptance_project(name: str) -> dict:
        return {
            "project_name": name,
            "run_id": f"run-{name}",
            "machine_status": "pass",
            "machine_ready": True,
            "hard_fail_count": 0,
            "warning_count": 0,
            "human_review_status": "quality_pass",
            "passed_iteration": 1,
            "duration_seconds": 60,
            "source_reference_rate": 1.0,
            "visual_analysis_coverage": 1.0,
            "visual_evidence_rate": 1.0,
            "brand_conflict_count": 0,
            "duplicate_image_count": 0,
            "quality_metrics": {
                "approved_shot_count": 18,
                "needs_revision_shot_count": 0,
                "rejected_shot_count": 0,
                "major_change_count": 0,
                "discussability_rate": 1.0,
            },
            "stage1_editorial_gate": {
                "per_shot": {"row_count": 18},
                "adjacent_pairs": {"edge_count": 17},
                "full_film": {"row_count": 1},
            },
            "issues": [dict(issue)],
        }
    dual = {
        "schema_version": "dual-project-acceptance/v1",
        "status": "quality_pass",
        "machine_status": "pass",
        "human_review_status": "quality_pass",
        "projects": [acceptance_project("midea"), acceptance_project("linsy")],
    }
    blind_project = acceptance_project("blind-003")
    blind = {
        "schema_version": "project-acceptance/v1",
        "status": "quality_pass",
        "machine_status": "pass",
        "human_review_status": "quality_pass",
        "project": blind_project,
    }
    dual_path = root / "dual_acceptance.json"
    _write(dual_path, dual)
    freeze_manifest = root / "freeze.json"
    _write(freeze_manifest, {"dual_acceptance_path": dual_path.name})
    blind_input_seal = root / "blind-seal.json"
    _write(blind_input_seal, {"schema_version": "launch-blind-input/v1"})
    freeze_manifest_sha256 = _sha256(freeze_manifest)
    blind_input_seal_sha256 = _sha256(blind_input_seal)
    freeze_batch_digest = _freeze_batch_digest(freeze_manifest_sha256, _sha256(dual_path))
    blind["evidence_binding"] = {
        "project_code": "blind-003",
        "run_id": "run-blind-003",
        "blind_input_seal_sha256": blind_input_seal_sha256,
        "freeze_batch_digest": freeze_batch_digest,
    }
    payloads = {
        "blind_acceptance": blind,
        "freeze_verification": {
            "schema_version": "launch-blind-freeze-verification/v1",
            "status": "pass",
            "freeze_manifest_sha256": freeze_manifest_sha256,
            "dual_acceptance_sha256": _sha256(dual_path),
            "freeze_batch_digest": freeze_batch_digest,
            "benchmark_runs": {"midea": "run-midea", "linsy": "run-linsy"},
        },
        "blind_input_verification": {
            "schema_version": "launch-blind-input-verification/v1",
            "status": "pass",
            "project_code": "blind-003",
            "blind_input_seal_sha256": blind_input_seal_sha256,
        },
        "timing_evidence": {
            "schema_version": "launch-timing-evidence/v1",
            "projects": {
                "midea": {"manual_active_minutes": 100, "assisted_active_minutes": 50},
                "linsy": {"manual_active_minutes": 100, "assisted_active_minutes": 50},
                "blind-003": {"manual_active_minutes": 100, "assisted_active_minutes": 50},
            },
        },
    }
    paths = {
        "dual_acceptance": dual_path,
        "freeze_manifest": freeze_manifest,
        "blind_input_seal": blind_input_seal,
    }
    for key, value in payloads.items():
        path = root / f"{key}.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        paths[key] = path
    return paths


def _build(paths: dict[str, Path], timing_sha256: str | None = None) -> dict:
    with _verified_sources(paths):
        return build_expansion_evidence(
            paths["freeze_manifest"].parent,
            paths["freeze_manifest"],
            _sha256(paths["freeze_manifest"]),
            paths["blind_input_seal"],
            _sha256(paths["blind_input_seal"]),
            (paths["blind_acceptance"].parent / "blind-cache", paths["blind_acceptance"].parent / "blind-review.xlsx"),
            paths["timing_evidence"],
            timing_sha256 or _sha256(paths["timing_evidence"]),
        )


def _verified_sources(paths: dict[str, Path]):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch(
        "mvp_v2.expansion_decision.verify_freeze_manifest",
        return_value=_read(paths["freeze_verification"]),
    ))
    stack.enter_context(patch(
        "mvp_v2.expansion_decision.verify_blind_input",
        return_value=_read(paths["blind_input_verification"]),
    ))
    stack.enter_context(patch(
        "mvp_v2.expansion_decision.build_project_acceptance",
        return_value=_formal_blind_acceptance(paths),
    ))
    return stack


def _formal_blind_acceptance(paths: dict[str, Path]) -> dict:
    value = _read(paths["blind_acceptance"])
    value.pop("evidence_binding", None)
    return value


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _freeze_batch_digest(freeze_manifest_sha256: str, dual_acceptance_sha256: str) -> str:
    import hashlib
    payload = json.dumps({
        "freeze_manifest_sha256": freeze_manifest_sha256,
        "dual_acceptance_sha256": dual_acceptance_sha256,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cli_evidence_args(paths: dict[str, Path]) -> list[str]:
    return [
        "--project-root", str(paths["freeze_manifest"].parent),
        "--freeze-manifest", str(paths["freeze_manifest"]),
        "--freeze-manifest-sha256", _sha256(paths["freeze_manifest"]),
        "--blind-input-seal", str(paths["blind_input_seal"]),
        "--blind-input-seal-sha256", _sha256(paths["blind_input_seal"]),
        "--timing-evidence", str(paths["timing_evidence"]),
        "--timing-evidence-sha256", _sha256(paths["timing_evidence"]),
    ]


def _legacy_cli_evidence_args(paths: dict[str, Path]) -> list[str]:
    values = []
    for key in (
        "dual_acceptance", "blind_acceptance", "freeze_verification",
        "blind_input_verification", "timing_evidence",
    ):
        option = key.replace("_", "-")
        values.extend([f"--{option}", str(paths[key])])
        values.extend([f"--{option}-sha256", _sha256(paths[key])])
    return values


if __name__ == "__main__":
    unittest.main()
