import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook
from PIL import Image

from mvp_v2.editorial_gate import (
    ADJACENT_REVIEW_COLUMNS,
    ADJACENT_REVIEW_DIMENSIONS,
    ADJACENT_REVIEW_SHEET_NAME,
    EXPECTED_ADJACENT_EDGES,
    FULL_FILM_REVIEW_COLUMNS,
    FULL_FILM_REVIEW_DIMENSIONS,
    FULL_FILM_REVIEW_SHEET_NAME,
)
from mvp_v2.excel_io import write_template_output_workbook
from mvp_v2.review_quality import (
    EDITORIAL_REVIEW_COLUMNS,
    analyze_review_workbooks,
    write_review_report,
)
from mvp_v2.review_cli import main as review_cli_main


class ReviewQualityTests(unittest.TestCase):
    def test_review_sheets_include_visible_source_images_for_shot_and_pair_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image_paths = []
            for index, color in enumerate(((30, 80, 160), (160, 80, 30)), start=1):
                image_path = root / f"shot-{index}.png"
                Image.new("RGB", (1280, 720), color).save(image_path)
                image_paths.append(image_path)
            output = root / "review-evidence.xlsx"
            script_rows = [
                {"结构": "开场造势", "镜号": str(index), "画面描述": f"镜头 {index}"}
                for index in range(1, 3)
            ]
            detail_rows = [
                {"项目名称": "测试发布会", "slot_index": index, "模板镜号": str(index)}
                for index in range(1, 3)
            ]
            visual_rows = [
                {
                    "镜号": str(index),
                    "slot_index": index,
                    "image_id": f"img-{index}",
                    "原图路径": str(image_paths[index - 1]),
                }
                for index in range(1, 3)
            ]

            write_template_output_workbook(
                output,
                script_rows,
                detail_rows,
                visual_asset_rows=visual_rows,
            )

            workbook = load_workbook(output)
            shot_review = workbook["剪辑复核"]
            shot_headers = [cell.value for cell in shot_review[1]]
            self.assertIn("原图路径", shot_headers)
            self.assertIn("预览图", shot_headers)
            self.assertEqual(len(shot_review._images), 2)
            path_cell = shot_review.cell(2, shot_headers.index("原图路径") + 1)
            self.assertEqual(path_cell.hyperlink.target, image_paths[0].resolve().as_uri())

            adjacent = workbook["相邻镜头复核"]
            adjacent_headers = [cell.value for cell in adjacent[1]]
            self.assertIn("前镜预览", adjacent_headers)
            self.assertIn("后镜预览", adjacent_headers)
            self.assertEqual(len(adjacent._images), 2)
            workbook.close()

    def test_seventeen_shots_use_sixteen_adjacent_edges(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "seventeen.xlsx"
            script_rows = [
                {"镜号": str(index), "结构": "发布会", "画面描述": f"镜头 {index}"}
                for index in range(1, 18)
            ]
            detail_rows = [
                {"项目名称": "17镜发布会", "slot_index": index, "模板镜号": str(index)}
                for index in range(1, 18)
            ]

            write_template_output_workbook(output, script_rows, detail_rows)

            workbook = load_workbook(output, read_only=True)
            adjacent = workbook[ADJACENT_REVIEW_SHEET_NAME]
            edges = [row[1].value for row in adjacent.iter_rows(min_row=2)]
            workbook.close()
            self.assertEqual(len(edges), 16)
            self.assertEqual(edges[-1], "16->17")

    def test_seventeen_shot_review_can_pass_dynamic_quality_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "seventeen-reviewed.xlsx"
            rows = [self._review_row("17镜发布会", index, "通过") for index in range(1, 18)]
            self._write_review_workbook(workbook, rows, edge_count=16)

            report = analyze_review_workbooks([workbook])

            project = report["projects"][0]
            self.assertEqual(project["human_review_status"], "quality_pass")
            self.assertEqual(project["stage1_editorial_gate"]["adjacent_pairs"]["edge_count"], 16)

    def test_generated_workbook_adds_editorial_review_sheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output.xlsx"
            script_rows = [{
                "结构": "开场造势",
                "镜号": "1",
                "时长(秒)": 3,
                "画面描述": "品牌 logo 入画",
                "声音": "音乐起",
            }]
            detail_rows = [{
                "项目名称": "测试发布会",
                "slot_index": 1,
                "模板镜号": "1",
            }]
            visual_rows = [{"镜号": "1", "image_id": "img_001"}]

            write_template_output_workbook(
                output,
                script_rows,
                detail_rows,
                visual_asset_rows=visual_rows,
            )

            wb = load_workbook(output)
            self.assertIn("剪辑复核", wb.sheetnames)
            ws = wb["剪辑复核"]
            self.assertEqual([cell.value for cell in ws[1]], EDITORIAL_REVIEW_COLUMNS)
            headers = [cell.value for cell in ws[1]]
            row = dict(zip(headers, [cell.value for cell in ws[2]]))
            self.assertEqual(row["项目名称"], "测试发布会")
            self.assertEqual(row["slot_index"], 1)
            self.assertEqual(row["image_id"], "img_001")
            self.assertIsNone(row["总体审核"])
            validations = ws.data_validations.dataValidation
            self.assertEqual(len(validations), 4)
            self.assertEqual(validations[0].formula1, '"通过,需修改,不通过"')
            for hidden_column in ("结构审核", "叙事审核", "节奏审核", "创意审核", "配图审核", "审核人", "审核日期"):
                letter = ws.cell(1, headers.index(hidden_column) + 1).column_letter
                self.assertTrue(ws.column_dimensions[letter].hidden)
            overall_letter = ws.cell(1, headers.index("总体审核") + 1).column_letter
            self.assertFalse(ws.column_dimensions[overall_letter].hidden)
            self.assertIn("只需在本列填写", ws.cell(1, headers.index("总体审核") + 1).comment.text)
            wb.close()

    def test_blank_reviews_are_pending_not_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "pending.xlsx"
            self._write_review_workbook(workbook, [self._review_row("项目A", 1)])

            report = analyze_review_workbooks([workbook])

            project = report["projects"][0]
            self.assertEqual(report["status"], "pending_review")
            self.assertEqual(project["reviewed_shot_count"], 0)
            self.assertEqual(project["pending_shot_count"], 1)
            self.assertEqual(project["overall"]["approved_count"], 0)
            self.assertEqual(project["review_coverage"], 0.0)
            self.assertIn("quality_metrics", project)
            self.assertEqual(project["human_review_status"], "pending")
            self.assertEqual(report["human_review_status"], "pending")

    def test_sparse_overall_only_without_identity_is_a_complete_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "sparse.xlsx"
            rows = [self._review_row("精简审核", slot, "") for slot in range(1, 18)]
            for row in rows:
                row["总体审核"] = "通过"
            self._write_review_workbook(workbook, rows, edge_count=16)
            wb = load_workbook(workbook)
            for sheet_name, keep_column in (
                ("相邻镜头复核", "总体审核"),
                ("整片复核", "总体审核"),
            ):
                sheet = wb[sheet_name]
                headers = {cell.value: cell.column for cell in sheet[1]}
                for row_no in range(2, sheet.max_row + 1):
                    for column in headers:
                        if column not in {"项目名称", "边", keep_column} and column in {
                            *ADJACENT_REVIEW_DIMENSIONS,
                            *FULL_FILM_REVIEW_DIMENSIONS,
                            "审核人", "审核日期",
                        }:
                            sheet.cell(row_no, headers[column], "")
            wb.save(workbook)
            wb.close()

            report = analyze_review_workbooks([workbook])

            project = report["projects"][0]
            self.assertEqual(project["review_coverage"], 1.0)
            self.assertEqual(project["human_review_status"], "quality_pass")
            self.assertEqual(report["human_review_status"], "quality_pass")

    def test_completed_reviews_compute_editorial_quality_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "reviewed.xlsx"
            approved = self._review_row("项目A", 1, "通过")
            needs_revision = self._review_row("项目A", 2, "通过")
            needs_revision.update({
                "叙事审核": "需修改",
                "创意审核": "需修改",
                "配图审核": "不通过",
                "总体审核": "需修改",
                "问题代码": "VISUAL_MISMATCH",
                "问题严重级别": "中",
                "修改意见": "镜头承接生硬，图片不符合画面功能",
            })
            self._write_review_workbook(workbook, [approved, needs_revision])

            report = analyze_review_workbooks([workbook])

            project = report["projects"][0]
            self.assertEqual(report["status"], "pending_review")
            self.assertEqual(project["reviewed_shot_count"], 2)
            self.assertEqual(project["overall"]["approved_count"], 1)
            self.assertEqual(project["overall"]["needs_revision_count"], 0)
            self.assertEqual(project["overall"]["rejected_count"], 1)
            self.assertEqual(project["manual_modification_rate"], 0.5)
            self.assertEqual(project["image_acceptance_rate"], 0.5)
            self.assertEqual(project["structure_adjustment_rate"], 0.0)
            self.assertIn("quality_metrics", project)
            self.assertEqual(project["human_review_status"], "pending")

    def test_invalid_review_status_is_reported_and_not_counted_as_reviewed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "invalid.xlsx"
            row = self._review_row("项目A", 1, "通过")
            row["节奏审核"] = "差不多"
            self._write_review_workbook(workbook, [row])

            report = analyze_review_workbooks([workbook])

            project = report["projects"][0]
            self.assertEqual(report["status"], "invalid")
            self.assertEqual(project["reviewed_shot_count"], 0)
            self.assertEqual(project["invalid_review_count"], 1)
            self.assertEqual(project["invalid_reviews"][0]["column"], "节奏审核")
            self.assertEqual(report["human_review_status"], "invalid")

    def test_rule_change_candidate_requires_same_slot_issue_in_two_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.xlsx"
            second = Path(tmp) / "b.xlsx"
            row_a = self._review_row("项目A", 3, "通过")
            row_b = self._review_row("项目B", 3, "通过")
            for row, comment in ((row_a, "转场缺少叙事承接"), (row_b, "转场与上一镜脱节")):
                row["叙事审核"] = "需修改"
                row["总体审核"] = "需修改"
                row["问题代码"] = "NARRATIVE_BRIDGE_BREAK"
                row["问题严重级别"] = "中"
                row["修改意见"] = comment
            self._write_review_workbook(first, [row_a])
            self._write_review_workbook(second, [row_b])

            report = analyze_review_workbooks([first, second])

            self.assertEqual(len(report["rule_change_candidates"]), 1)
            candidate = report["rule_change_candidates"][0]
            self.assertEqual(candidate["slot_index"], 3)
            self.assertEqual(candidate["dimension"], "narrative")
            self.assertEqual(candidate["project_count"], 2)
            self.assertEqual(candidate["projects"], ["项目A", "项目B"])

    def test_write_review_report_outputs_utf8_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "review_report.json"
            report = {"status": "pending_review", "projects": [{"project_name": "项目A"}]}

            written = write_review_report(output, report)

            self.assertEqual(written, output)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), report)

    def test_review_cli_writes_combined_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "a.xlsx"
            second = Path(tmp) / "b.xlsx"
            output = Path(tmp) / "review_report.json"
            self._write_review_workbook(first, [self._review_row("项目A", 1)])
            self._write_review_workbook(second, [self._review_row("项目B", 1)])

            exit_code = review_cli_main([
                "--input", str(first),
                "--input", str(second),
                "--output", str(output),
            ])

            self.assertEqual(exit_code, 3)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["project_count"], 2)
            self.assertEqual(report["status"], "pending_review")
            self.assertEqual(report["human_review_status"], "pending")

    def test_review_cli_returns_three_for_completed_rejected_eighteen_shot_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "rejected.xlsx"
            output = Path(tmp) / "review_report.json"
            self._write_review_workbook(
                workbook,
                [self._review_row("项目A", slot, "不通过") for slot in range(1, 19)],
            )

            exit_code = review_cli_main([
                "--input", str(workbook),
                "--output", str(output),
            ])

            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 3)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["human_review_status"], "quality_fail")
            self.assertEqual(report["projects"][0]["human_review_status"], "quality_fail")

    def test_review_cli_returns_zero_for_eighteen_shot_quality_boundary_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "quality_pass.xlsx"
            output = Path(tmp) / "review_report.json"
            rows = [self._review_row("项目A", slot, "通过") for slot in range(1, 19)]
            for slot in (2, 4, 5):
                rows[slot - 1]["总体审核"] = "需修改"
                rows[slot - 1]["问题代码"] = "NARRATIVE_BRIDGE_BREAK"
                rows[slot - 1]["问题严重级别"] = "中"
                rows[slot - 1]["修改意见"] = "需要调整镜头承接"
            rows[4]["配图审核"] = "需修改"
            self._write_review_workbook(workbook, rows)

            exit_code = review_cli_main([
                "--input", str(workbook),
                "--output", str(output),
            ])

            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exit_code, 0)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["human_review_status"], "quality_pass")
            self.assertEqual(report["projects"][0]["quality_metrics"]["approved_shot_count"], 15)
            self.assertEqual(report["projects"][0]["quality_metrics"]["accepted_image_count"], 17)

    def test_missing_reviewer_or_date_does_not_repeat_the_review_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            for missing_column in ("审核人", "审核日期"):
                with self.subTest(missing_column=missing_column):
                    workbook = Path(tmp) / f"missing_{missing_column}.xlsx"
                    output = Path(tmp) / f"missing_{missing_column}.json"
                    rows = [self._review_row("项目A", slot, "通过") for slot in range(1, 19)]
                    for slot in (2, 4, 5):
                        rows[slot - 1]["总体审核"] = "需修改"
                        rows[slot - 1]["问题代码"] = "NARRATIVE_BRIDGE_BREAK"
                        rows[slot - 1]["问题严重级别"] = "中"
                        rows[slot - 1]["修改意见"] = "需要调整镜头承接"
                    rows[4]["配图审核"] = "需修改"
                    rows[0][missing_column] = ""
                    self._write_review_workbook(workbook, rows)

                    report = analyze_review_workbooks([workbook])
                    exit_code = review_cli_main([
                        "--input", str(workbook),
                        "--output", str(output),
                    ])

                    project = report["projects"][0]
                    self.assertEqual(report["status"], "complete")
                    self.assertEqual(report["projects"][0]["human_review_status"], "quality_pass")
                    self.assertEqual(report["human_review_status"], "quality_pass")
                    self.assertEqual(project["reviewed_shot_count"], 18)
                    self.assertEqual(project["pending_shot_count"], 0)
                    self.assertEqual(project["review_coverage"], 1.0)
                    self.assertEqual(report["summary"]["reviewed_shot_count"], 18)
                    self.assertEqual(report["summary"]["pending_shot_count"], 0)
                    self.assertEqual(report["summary"]["review_coverage"], 1.0)
                    self.assertEqual(exit_code, 0)

    def test_aggregate_human_review_status_prioritizes_pending_then_quality_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            quality_pass = Path(tmp) / "quality_pass.xlsx"
            missing_provenance = Path(tmp) / "missing_provenance.xlsx"
            quality_fail = Path(tmp) / "quality_fail.xlsx"
            pass_rows = [self._review_row("项目通过", slot, "通过") for slot in range(1, 19)]
            for slot in (2, 4, 5):
                pass_rows[slot - 1]["总体审核"] = "需修改"
                pass_rows[slot - 1]["问题代码"] = "NARRATIVE_BRIDGE_BREAK"
                pass_rows[slot - 1]["问题严重级别"] = "中"
                pass_rows[slot - 1]["修改意见"] = "需要调整镜头承接"
            pass_rows[4]["配图审核"] = "需修改"
            pending_rows = [dict(row) for row in pass_rows]
            for row in pending_rows:
                row["项目名称"] = "项目待补溯源"
            for column in ("结构审核", "叙事审核", "节奏审核", "创意审核", "配图审核", "总体审核"):
                pending_rows[0][column] = ""
            fail_rows = [self._review_row("项目不通过", slot, "不通过") for slot in range(1, 19)]
            self._write_review_workbook(quality_pass, pass_rows)
            self._write_review_workbook(missing_provenance, pending_rows)
            self._write_review_workbook(quality_fail, fail_rows)

            pending_report = analyze_review_workbooks([quality_pass, missing_provenance])
            failed_report = analyze_review_workbooks([quality_pass, quality_fail])

            self.assertEqual(pending_report["human_review_status"], "pending")
            self.assertEqual(failed_report["human_review_status"], "quality_fail")

    @staticmethod
    def _review_row(project_name, slot_index, status=""):
        row = {column: "" for column in EDITORIAL_REVIEW_COLUMNS}
        row.update({
            "项目名称": project_name,
            "slot_index": slot_index,
            "镜号": str(slot_index),
            "结构": "核心宣讲与仪式",
            "时长(秒)": 3,
            "画面描述": "测试画面",
            "声音": "测试声音",
            "image_id": f"img_{slot_index}",
            "结构审核": status,
            "叙事审核": status,
            "节奏审核": status,
            "创意审核": status,
            "配图审核": status,
            "总体审核": status,
            "重大修改": "否" if status else "",
            "问题代码": "NARRATIVE_BRIDGE_BREAK" if status in {"需修改", "不通过"} else "",
            "问题严重级别": "中" if status in {"需修改", "不通过"} else "",
            "修改意见": "需要调整镜头承接" if status in {"需修改", "不通过"} else "",
            "审核人": "审核员" if status else "",
            "审核日期": "2026-07-10" if status else "",
        })
        return row

    @staticmethod
    def _write_review_workbook(path, rows, edge_count=17):
        wb = Workbook()
        ws = wb.active
        ws.title = "剪辑复核"
        ws.append(EDITORIAL_REVIEW_COLUMNS)
        for row in rows:
            ws.append([row.get(column, "") for column in EDITORIAL_REVIEW_COLUMNS])
        project_name = str(rows[0].get("项目名称") or "") if rows else ""
        adjacent_ws = wb.create_sheet(ADJACENT_REVIEW_SHEET_NAME)
        adjacent_ws.append(ADJACENT_REVIEW_COLUMNS)
        edges = tuple(f"{index}->{index + 1}" for index in range(1, edge_count + 1))
        for edge in edges:
            row = {column: "" for column in ADJACENT_REVIEW_COLUMNS}
            row.update({
                "项目名称": project_name,
                "边": edge,
                **{column: "通过" for column in ADJACENT_REVIEW_DIMENSIONS},
                "审核人": "审核员",
                "审核日期": "2026-07-10",
            })
            adjacent_ws.append([row.get(column, "") for column in ADJACENT_REVIEW_COLUMNS])
        film_ws = wb.create_sheet(FULL_FILM_REVIEW_SHEET_NAME)
        film_ws.append(FULL_FILM_REVIEW_COLUMNS)
        film_row = {column: "" for column in FULL_FILM_REVIEW_COLUMNS}
        film_row.update({
            "项目名称": project_name,
            **{column: "通过" for column in FULL_FILM_REVIEW_DIMENSIONS},
            "审核人": "审核员",
            "审核日期": "2026-07-10",
        })
        film_ws.append([film_row.get(column, "") for column in FULL_FILM_REVIEW_COLUMNS])
        wb.save(path)


if __name__ == "__main__":
    unittest.main()
