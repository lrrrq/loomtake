from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp_v2.acceptance import (
    AcceptanceError,
    build_dual_project_acceptance,
    build_project_acceptance,
)
from mvp_v2.acceptance_cli import main as acceptance_cli_main
from mvp_v2.excel_io import write_template_output_workbook
from mvp_v2.pipeline_runtime import open_pipeline_run, publish_pipeline_run, resolve_current_release, resolve_pipeline_run
from mvp_v2.review_package import ReviewPackageError, export_review_workbook
from mvp_v2.review_package_cli import main as review_package_cli_main
from mvp_v2.run_contract import sha256_file


class DualProjectAcceptanceTests(unittest.TestCase):
    def test_completed_formal_run_can_export_a_traceable_review_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _published_release(root, "匿名 Formal 完成态项目")
            manifest_path = run / "run_manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "completed"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8",
            )

            review = export_review_workbook(
                run, root / "reviews" / "completed-formal.xlsx",
            )

            self.assertTrue(review.is_file())

    def test_single_project_acceptance_exposes_machine_and_human_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = _published_release(root, "盲测项目")
            review = export_review_workbook(cache, root / "reviews" / "blind.xlsx")

            report = build_project_acceptance(cache, review)

            self.assertEqual(report["status"], "ready_for_review")
            self.assertEqual(report["review_contract"], {
                "shot_count": 18,
                "adjacent_edge_count": 17,
                "full_film_count": 1,
            })
            self.assertEqual(report["project"]["machine_status"], "pass")
            self.assertEqual(report["project"]["human_review_status"], "pending")

    def test_two_current_machine_pass_releases_are_ready_for_complete_human_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            report = build_dual_project_acceptance([
                (first, first_review),
                (second, second_review),
            ])

            self.assertEqual(report["status"], "ready_for_review")
            self.assertEqual(report["project_count"], 2)
            self.assertEqual(report["machine_status"], "pass")
            self.assertEqual(report["human_review_status"], "pending")
            self.assertEqual(report["release_decision"], "blocked")
            self.assertEqual(report["review_contract"], {
                "shot_count": 36,
                "adjacent_edge_count": 34,
                "full_film_count": 2,
            })
            self.assertEqual(len({row["run_id"] for row in report["projects"]}), 2)
            self.assertTrue(all(row["artifact_hashes"] for row in report["projects"]))
            self.assertTrue(all(row["manifest_sha256"] for row in report["projects"]))
            self.assertTrue(all(row["analysis_sha256"] for row in report["projects"]))
            self.assertTrue(all(row["human_review_status"] == "pending" for row in report["projects"]))
            self.assertTrue(all(row["review_coverage"] == 0 for row in report["projects"]))
            self.assertTrue(all("quality_metrics" in row for row in report["projects"]))

    def test_machine_failure_blocks_human_review_readiness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目", machine_status="fail", hard_fail_count=1)
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            report = build_dual_project_acceptance([
                (first, first_review),
                (second, second_review),
            ])

            self.assertEqual(report["status"], "machine_blocked")
            self.assertEqual(report["machine_status"], "fail")
            self.assertEqual(report["release_decision"], "blocked")

    def test_report_run_id_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目", report_run_id="foreign-run")
            second = _published_release(root, "林氏项目")
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            with self.assertRaisesRegex(AcceptanceError, "run_id"):
                build_dual_project_acceptance([
                    (first, first_review),
                    (second, second_review),
                ])

    def test_cli_writes_joint_evidence_and_returns_three_while_review_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")
            output = root / "dual-acceptance.json"

            code = acceptance_cli_main([
                "--case", str(first), str(first_review),
                "--case", str(second), str(second_review),
                "--output", str(output),
            ])

            self.assertEqual(code, 3)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "ready_for_review")

    def test_review_copy_does_not_mutate_explicit_run_and_rejects_script_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = _published_release(root, "美的项目")
            review = export_review_workbook(cache, root / "reviews" / "美的.xlsx")
            release_before = resolve_pipeline_run(
                cache.parent.parent, cache.name, allowed_statuses={"blocked"}
            )

            from openpyxl import load_workbook
            workbook = load_workbook(review)
            workbook["剪辑复核"]["J2"] = "通过"
            workbook.save(review)
            self.assertFalse((cache.parent.parent / "CURRENT").exists())
            self.assertEqual(
                release_before,
                resolve_pipeline_run(cache.parent.parent, cache.name, allowed_statuses={"blocked"}),
            )

            workbook = load_workbook(review)
            workbook["脚本"]["I2"] = "篡改脚本文案"
            workbook.save(review)
            with self.assertRaisesRegex(ReviewPackageError, "不可编辑内容"):
                from mvp_v2.review_package import validate_review_workbook
                validate_review_workbook(cache, review)

    def test_review_copy_stays_bound_to_explicit_run_after_new_run_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run = _published_release(root, "美的项目")
            review = export_review_workbook(run, root / "reviews" / "美的.xlsx")
            _published_release(root, "美的项目")

            self.assertFalse((run.parent.parent / "CURRENT").exists())
            from mvp_v2.review_package import validate_review_workbook
            self.assertEqual(validate_review_workbook(run, review)["source_run_id"], run.name)

    def test_review_copy_rejects_project_identity_change_inside_review_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = _published_release(root, "美的项目")
            review = export_review_workbook(cache, root / "reviews" / "美的.xlsx")
            from openpyxl import load_workbook
            workbook = load_workbook(review)
            workbook["剪辑复核"]["A2"] = "其他项目"
            workbook.save(review)

            with self.assertRaisesRegex(ReviewPackageError, "项目名称"):
                from mvp_v2.review_package import validate_review_workbook
                validate_review_workbook(cache, review)

    def test_review_copy_rejects_changed_shot_fact_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = _published_release(root, "美的项目")
            review = export_review_workbook(cache, root / "reviews" / "美的.xlsx")
            from openpyxl import load_workbook
            workbook = load_workbook(review)
            sheet = workbook["剪辑复核"]
            headers = {cell.value: cell.column for cell in sheet[1]}
            sheet.cell(2, headers["画面描述"], "被篡改的镜头事实")
            workbook.save(review)
            workbook.close()

            with self.assertRaisesRegex(ReviewPackageError, "不可编辑内容"):
                from mvp_v2.review_package import validate_review_workbook
                validate_review_workbook(cache, review)

    def test_acceptance_rejects_missing_visual_evidence_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            _rewrite_run_report(first, analysis_sha256="")
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            with self.assertRaisesRegex(AcceptanceError, "视觉分析快照.*SHA"):
                build_dual_project_acceptance([
                    (first, first_review),
                    (second, second_review),
                ])

    def test_acceptance_rejects_fake_visual_snapshot_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            _rewrite_run_report(first, analysis_sha256="0" * 64)
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            with self.assertRaisesRegex(AcceptanceError, "视觉分析快照.*SHA"):
                build_dual_project_acceptance([
                    (first, first_review),
                    (second, second_review),
                ])

    def test_acceptance_rejects_replaced_visual_snapshot_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            first.joinpath("image_analysis.minimax.jsonl").write_text(
                '{"image_id":"replaced"}\n', encoding="utf-8"
            )
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            with self.assertRaisesRegex(AcceptanceError, "视觉分析快照.*完整性"):
                build_dual_project_acceptance([
                    (first, first_review),
                    (second, second_review),
                ])

    def test_acceptance_cli_rejects_replaced_visual_snapshot_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")
            first.joinpath("image_analysis.minimax.jsonl").write_text(
                '{"image_id":"forged"}\n', encoding="utf-8"
            )
            output = root / "dual-acceptance.json"

            code = acceptance_cli_main([
                "--case", str(first), str(first_review),
                "--case", str(second), str(second_review),
                "--output", str(output),
            ])

            self.assertEqual(code, 1)
            self.assertFalse(output.exists())

    def test_acceptance_rejects_visual_report_pointing_to_shared_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            shared_manifest = first.parent.parent / "image_manifest.jsonl"
            shared_analysis = first.parent.parent / "image_analysis.minimax.jsonl"
            shared_manifest.write_text('{"image_id":"shared"}\n', encoding="utf-8")
            shared_analysis.write_text('{"image_id":"shared"}\n', encoding="utf-8")
            _rewrite_run_report(
                first,
                manifest_path=str(shared_manifest),
                analysis_path=str(shared_analysis),
                manifest_sha256=sha256_file(shared_manifest),
                analysis_sha256=sha256_file(shared_analysis),
            )
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            with self.assertRaisesRegex(AcceptanceError, "不属于当前 run"):
                build_dual_project_acceptance([
                    (first, first_review),
                    (second, second_review),
                ])

    def test_acceptance_uses_one_current_snapshot_per_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            with patch(
                "mvp_v2.review_package.resolve_current_release",
                side_effect=AssertionError("复核校验不得再次解析 CURRENT"),
            ):
                report = build_dual_project_acceptance([
                    (first, first_review),
                    (second, second_review),
                ])

            self.assertEqual(report["status"], "ready_for_review")

    def test_review_copy_rejects_injected_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = _published_release(root, "美的项目")
            review = export_review_workbook(cache, root / "reviews" / "美的.xlsx")
            from io import BytesIO
            from PIL import Image as PILImage
            image_stream = BytesIO()
            PILImage.new("RGB", (8, 8), "red").save(image_stream, format="PNG")
            image_stream.seek(0)
            from openpyxl import load_workbook
            from openpyxl.drawing.image import Image
            workbook = load_workbook(review)
            workbook["脚本"].add_image(Image(image_stream), "A2")
            workbook.save(review)
            workbook.close()
            image_stream.close()

            with self.assertRaisesRegex(ReviewPackageError, "不可编辑内容"):
                from mvp_v2.review_package import validate_review_workbook
                validate_review_workbook(cache, review)

    def test_acceptance_rejects_run_project_identity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            _rewrite_run_report(first, project_name="伪造项目")
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            with self.assertRaisesRegex(AcceptanceError, "项目名称"):
                build_dual_project_acceptance([
                    (first, first_review),
                    (second, second_review),
                ])

    def test_incomplete_visual_coverage_does_not_block_document_machine_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            _rewrite_run_report(first, visual_evidence_rate=0.9)
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            report = build_dual_project_acceptance([
                (first, first_review),
                (second, second_review),
            ])

            self.assertEqual(report["status"], "ready_for_review")
            self.assertTrue(report["projects"][0]["machine_ready"])

    def test_incomplete_source_coverage_still_blocks_machine_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            _rewrite_run_report(first, source_reference_rate=0.9)
            first_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            second_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")

            report = build_dual_project_acceptance([
                (first, first_review),
                (second, second_review),
            ])

            self.assertEqual(report["status"], "machine_blocked")
            self.assertFalse(report["projects"][0]["machine_ready"])

    def test_joint_human_status_does_not_hide_quality_failure_behind_pending_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            pending_review = export_review_workbook(first, root / "reviews" / "美的.xlsx")
            failed_review = export_review_workbook(second, root / "reviews" / "林氏.xlsx")
            _complete_review(failed_review, fail_first_shot=True)

            report = build_dual_project_acceptance([
                (first, pending_review),
                (second, failed_review),
            ])

            self.assertEqual(report["human_review_status"], "quality_fail")
            self.assertEqual(report["status"], "quality_fail")

    def test_review_package_cli_exports_two_traceable_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _published_release(root, "美的项目")
            second = _published_release(root, "林氏项目")
            first_review = root / "reviews" / "美的.xlsx"
            second_review = root / "reviews" / "林氏.xlsx"

            code = review_package_cli_main([
                "--case", str(first), str(first_review),
                "--case", str(second), str(second_review),
            ])

            self.assertEqual(code, 0)
            from mvp_v2.review_package import validate_review_workbook
            self.assertEqual(validate_review_workbook(first, first_review)["source_run_id"], first.name)
            self.assertEqual(validate_review_workbook(second, second_review)["source_run_id"], second.name)

    def test_review_provenance_values_cannot_become_excel_formulas(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = _published_release(root, "=HYPERLINK(\"https://example.invalid\")")
            review = export_review_workbook(cache, root / "reviews" / "formula.xlsx")
            from openpyxl import load_workbook
            workbook = load_workbook(review, data_only=False)
            sheet = workbook["复核来源"]
            project_cell = next(
                sheet.cell(row, 2)
                for row in range(2, sheet.max_row + 1)
                if sheet.cell(row, 1).value == "project_name"
            )
            self.assertEqual(project_cell.data_type, "s")
            workbook.close()


def _published_release(
    root: Path,
    project_name: str,
    machine_status: str = "pass",
    hard_fail_count: int = 0,
    report_run_id: str | None = None,
) -> Path:
    cache_dir = root / project_name
    source_root = root / "source"
    source_root.mkdir(exist_ok=True)
    brief = {"项目名称": project_name, "脚本类型": "发布会"}
    with open_pipeline_run(brief, cache_dir, source_root, "fingerprint") as state:
        workbook = state.run_dir / "result.xlsx"
        script_rows = [
            {
                "结构": f"结构{index}",
                "镜号": str(index),
                "时长(秒)": 60 if index == 1 else 0,
                "画面描述": f"镜头{index}",
            }
            for index in range(1, 19)
        ]
        details = [
            {"项目名称": project_name, "slot_index": index, "模板镜号": str(index)}
            for index in range(1, 19)
        ]
        write_template_output_workbook(workbook, script_rows, details, [])
        audit = state.run_dir / "result.xlsx.audit.json"
        audit.write_text(json.dumps({
            "status": machine_status,
            "hard_fail_count": hard_fail_count,
            "warning_count": 0,
        }), encoding="utf-8")
        loop_log = state.run_dir / "result.xlsx.loop.json"
        loop_log.write_text("{}", encoding="utf-8")
        visual_review = state.run_dir / "result.visual_review.html"
        visual_review.write_text("<html></html>", encoding="utf-8")
        visual_manifest = state.run_dir / "image_manifest.jsonl"
        visual_manifest.write_text(
            json.dumps({"image_id": f"manifest-{project_name}"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        visual_analysis = state.run_dir / "image_analysis.minimax.jsonl"
        visual_analysis.write_text(
            json.dumps({"image_id": f"analysis-{project_name}"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        run_report = state.run_dir / "run_report.json"
        run_report.write_text(json.dumps({
            "project_name": project_name,
            "run_id": report_run_id or state.run_id,
            "machine_audit_status": machine_status,
            "human_review_status": "pending",
            "release_decision": "blocked",
            "hard_fail_count": hard_fail_count,
            "warning_count": 0,
            "manifest_path": str(visual_manifest),
            "analysis_path": str(visual_analysis),
            "manifest_sha256": sha256_file(visual_manifest),
            "analysis_sha256": sha256_file(visual_analysis),
            "cache_fingerprint": "fingerprint",
            "source_reference_rate": 1.0,
            "visual_analysis_coverage": 1.0,
            "visual_evidence_rate": 1.0,
            "brand_conflict_count": 0,
            "duplicate_image_count": 0,
            "output_path": str(workbook),
            "audit_path": str(audit),
            "visual_review_html": str(visual_review),
        }, ensure_ascii=False), encoding="utf-8")
        artifacts = {
            "workbook": workbook,
            "audit": audit,
            "loop_log": loop_log,
            "visual_review": visual_review,
            "run_report": run_report,
        }
        publish_pipeline_run(
            state,
            "blocked",
            artifacts,
            visual_snapshots={
                "image_manifest": visual_manifest,
                "image_analysis": visual_analysis,
            },
        )
    return state.run_dir


def _complete_review(path: Path, fail_first_shot: bool = False) -> None:
    from openpyxl import load_workbook
    workbook = load_workbook(path)
    try:
        shot = workbook["剪辑复核"]
        shot_headers = {cell.value: cell.column for cell in shot[1]}
        review_columns = ("结构审核", "叙事审核", "节奏审核", "创意审核", "配图审核", "总体审核")
        for row in range(2, 20):
            for column in review_columns:
                shot.cell(row, shot_headers[column], "通过")
            shot.cell(row, shot_headers["重大修改"], "否")
            shot.cell(row, shot_headers["审核人"], "审核员")
            shot.cell(row, shot_headers["审核日期"], "2026-07-14")
        if fail_first_shot:
            shot.cell(2, shot_headers["结构审核"], "不通过")
            shot.cell(2, shot_headers["总体审核"], "不通过")
            shot.cell(2, shot_headers["问题代码"], "STRUCTURE_GAP")
            shot.cell(2, shot_headers["问题严重级别"], "高")
            shot.cell(2, shot_headers["修改意见"], "结构职责不成立")

        for sheet_name, columns in (
            ("相邻镜头复核", ("叙事承接", "动作衔接", "运镜衔接", "色彩衔接", "声音衔接", "总体审核")),
            ("整片复核", ("叙事弧", "节奏曲线", "音乐曲线", "高潮与收束", "总体审核")),
        ):
            sheet = workbook[sheet_name]
            headers = {cell.value: cell.column for cell in sheet[1]}
            for row in range(2, sheet.max_row + 1):
                for column in columns:
                    sheet.cell(row, headers[column], "通过")
                sheet.cell(row, headers["审核人"], "审核员")
                sheet.cell(row, headers["审核日期"], "2026-07-14")
        workbook.save(path)
    finally:
        workbook.close()


def _rewrite_run_report(run_dir: Path, **changes) -> None:
    manifest_path = run_dir / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_record = manifest["artifacts"]["run_report"]
    report_path = Path(report_record["path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.update(changes)
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    report_record["sha256"] = sha256_file(report_path)
    report_record["size_bytes"] = report_path.stat().st_size
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
