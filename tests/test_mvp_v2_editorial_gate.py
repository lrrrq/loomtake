import tempfile
import unittest
from pathlib import Path
from typing import Optional

from openpyxl import Workbook

from mvp_v2.review_quality import analyze_review_workbooks


LEGACY_EDITORIAL_COLUMNS = [
    "项目名称",
    "slot_index",
    "镜号",
    "结构",
    "时长(秒)",
    "画面描述",
    "声音",
    "image_id",
    "结构审核",
    "叙事审核",
    "节奏审核",
    "创意审核",
    "配图审核",
    "总体审核",
    "修改意见",
    "审核人",
    "审核日期",
]

EDITORIAL_COLUMNS = [
    *LEGACY_EDITORIAL_COLUMNS[:14],
    "重大修改",
    "问题代码",
    "问题严重级别",
    *LEGACY_EDITORIAL_COLUMNS[14:],
]

ADJACENT_COLUMNS = [
    "项目名称",
    "边",
    "叙事承接",
    "动作衔接",
    "运镜衔接",
    "色彩衔接",
    "声音衔接",
    "总体审核",
    "问题代码",
    "问题严重级别",
    "修改意见",
    "审核人",
    "审核日期",
]

FILM_COLUMNS = [
    "项目名称",
    "叙事弧",
    "节奏曲线",
    "音乐曲线",
    "高潮与收束",
    "总体审核",
    "问题代码",
    "问题严重级别",
    "修改意见",
    "审核人",
    "审核日期",
]


class Stage1EditorialGateTests(unittest.TestCase):
    def test_full_stage1_contract_quality_passes_only_when_three_layers_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "stage1-pass.xlsx"
            self._write_workbook(
                workbook,
                editorial_rows=[self._editorial_row(slot) for slot in range(1, 19)],
                adjacent_rows=[self._adjacent_row(edge) for edge in self._all_edges()],
                film_rows=[self._film_row()],
            )

            report = analyze_review_workbooks([workbook])
            project = report["projects"][0]
            gate = project.get("stage1_editorial_gate", {})

            self.assertEqual(report["human_review_status"], "quality_pass")
            self.assertEqual(gate.get("overall_status"), "quality_pass")
            self.assertEqual(gate.get("adjacent_pairs", {}).get("edge_count"), 17)
            self.assertEqual(gate.get("full_film", {}).get("row_count"), 1)

    def test_missing_or_duplicate_adjacent_edges_keep_stage1_non_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_edge_workbook = Path(tmp) / "missing-edge.xlsx"
            duplicate_edge_workbook = Path(tmp) / "duplicate-edge.xlsx"

            self._write_workbook(
                missing_edge_workbook,
                editorial_rows=[self._editorial_row(slot) for slot in range(1, 19)],
                adjacent_rows=[self._adjacent_row(edge) for edge in self._all_edges()[:-1]],
                film_rows=[self._film_row()],
            )
            duplicate_rows = [self._adjacent_row(edge) for edge in self._all_edges()]
            duplicate_rows[-1]["边"] = "16->17"
            self._write_workbook(
                duplicate_edge_workbook,
                editorial_rows=[self._editorial_row(slot) for slot in range(1, 19)],
                adjacent_rows=duplicate_rows,
                film_rows=[self._film_row()],
            )

            missing_report = analyze_review_workbooks([missing_edge_workbook])
            duplicate_report = analyze_review_workbooks([duplicate_edge_workbook])

            self.assertEqual(missing_report["human_review_status"], "pending")
            self.assertEqual(duplicate_report["status"], "invalid")
            self.assertNotEqual(duplicate_report["human_review_status"], "quality_pass")

    def test_missing_identity_is_allowed_but_invalid_status_never_passes_stage1(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_provenance = Path(tmp) / "missing-provenance.xlsx"
            invalid_status = Path(tmp) / "invalid-status.xlsx"

            adjacent_rows = [self._adjacent_row(edge) for edge in self._all_edges()]
            adjacent_rows[0]["审核日期"] = ""
            self._write_workbook(
                missing_provenance,
                editorial_rows=[self._editorial_row(slot) for slot in range(1, 19)],
                adjacent_rows=adjacent_rows,
                film_rows=[self._film_row()],
            )

            bad_adjacent_rows = [self._adjacent_row(edge) for edge in self._all_edges()]
            bad_adjacent_rows[0]["总体审核"] = "待定"
            self._write_workbook(
                invalid_status,
                editorial_rows=[self._editorial_row(slot) for slot in range(1, 19)],
                adjacent_rows=bad_adjacent_rows,
                film_rows=[self._film_row()],
            )

            missing_report = analyze_review_workbooks([missing_provenance])
            invalid_report = analyze_review_workbooks([invalid_status])

            self.assertEqual(missing_report["human_review_status"], "quality_pass")
            self.assertEqual(invalid_report["status"], "invalid")
            self.assertEqual(invalid_report["human_review_status"], "invalid")

    def test_any_adjacent_or_full_film_non_pass_fails_stage1(self):
        with tempfile.TemporaryDirectory() as tmp:
            edge_fail = Path(tmp) / "edge-fail.xlsx"
            film_fail = Path(tmp) / "film-fail.xlsx"

            adjacent_rows = [self._adjacent_row(edge) for edge in self._all_edges()]
            adjacent_rows[3].update({
                "叙事承接": "需修改",
                "总体审核": "需修改",
                "问题代码": "NARRATIVE_BRIDGE_BREAK",
                "问题严重级别": "中",
                "修改意见": "承接不顺，需补转场",
            })
            self._write_workbook(
                edge_fail,
                editorial_rows=[self._editorial_row(slot) for slot in range(1, 19)],
                adjacent_rows=adjacent_rows,
                film_rows=[self._film_row()],
            )

            film_row = self._film_row()
            film_row.update({
                "总体审核": "不通过",
                "问题代码": "SOUND_CURVE_COLLAPSE",
                "问题严重级别": "高",
                "修改意见": "音乐曲线未支撑高潮收束",
            })
            self._write_workbook(
                film_fail,
                editorial_rows=[self._editorial_row(slot) for slot in range(1, 19)],
                adjacent_rows=[self._adjacent_row(edge) for edge in self._all_edges()],
                film_rows=[film_row],
            )

            self.assertEqual(analyze_review_workbooks([edge_fail])["human_review_status"], "quality_fail")
            self.assertEqual(analyze_review_workbooks([film_fail])["human_review_status"], "quality_fail")

    def test_non_pass_editorial_row_without_issue_metadata_keeps_the_entered_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            workbook = Path(tmp) / "editorial-pending.xlsx"
            rows = [self._editorial_row(slot) for slot in range(1, 19)]
            rows[0]["总体审核"] = "需修改"
            rows[0]["重大修改"] = "是"
            rows[0]["修改意见"] = ""
            rows[0]["问题代码"] = ""
            rows[0]["问题严重级别"] = ""
            self._write_workbook(
                workbook,
                editorial_rows=rows,
                adjacent_rows=[self._adjacent_row(edge) for edge in self._all_edges()],
                film_rows=[self._film_row()],
            )

            report = analyze_review_workbooks([workbook])

            self.assertEqual(report["human_review_status"], "quality_fail")
            self.assertEqual(report["projects"][0].get("stage1_editorial_gate", {}).get("per_shot", {}).get("status"), "quality_fail")

    def test_major_change_is_counted_and_legacy_workbook_stays_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            major_change_workbook = Path(tmp) / "major-change.xlsx"
            legacy_workbook = Path(tmp) / "legacy.xlsx"

            rows = [self._editorial_row(slot) for slot in range(1, 19)]
            rows[1]["重大修改"] = "是"
            self._write_workbook(
                major_change_workbook,
                editorial_rows=rows,
                adjacent_rows=[self._adjacent_row(edge) for edge in self._all_edges()],
                film_rows=[self._film_row()],
            )
            self._write_workbook(
                legacy_workbook,
                editorial_rows=[self._legacy_editorial_row(slot) for slot in range(1, 19)],
            )

            major_change_report = analyze_review_workbooks([major_change_workbook])
            legacy_report = analyze_review_workbooks([legacy_workbook])

            self.assertEqual(
                major_change_report["projects"][0]["quality_metrics"]["major_change_count"],
                1,
            )
            self.assertEqual(legacy_report["human_review_status"], "pending")
            self.assertNotEqual(legacy_report["status"], "invalid")

    def _write_workbook(
        self,
        path: Path,
        editorial_rows: list[dict],
        adjacent_rows: Optional[list[dict]] = None,
        film_rows: Optional[list[dict]] = None,
    ) -> None:
        wb = Workbook()
        ws = wb.active
        editorial_columns = list(editorial_rows[0].keys()) if editorial_rows else EDITORIAL_COLUMNS
        ws.title = "剪辑复核"
        ws.append(editorial_columns)
        for row in editorial_rows:
            ws.append([row.get(column, "") for column in editorial_columns])

        if adjacent_rows is not None:
            edge_ws = wb.create_sheet("相邻镜头复核")
            edge_ws.append(ADJACENT_COLUMNS)
            for row in adjacent_rows:
                edge_ws.append([row.get(column, "") for column in ADJACENT_COLUMNS])

        if film_rows is not None:
            film_ws = wb.create_sheet("整片复核")
            film_ws.append(FILM_COLUMNS)
            for row in film_rows:
                film_ws.append([row.get(column, "") for column in FILM_COLUMNS])

        wb.save(path)
        wb.close()

    def _editorial_row(self, slot: int) -> dict:
        row = {
            "项目名称": "项目A",
            "slot_index": slot,
            "镜号": str(slot),
            "结构": "结构段",
            "时长(秒)": 3,
            "画面描述": f"镜头 {slot}",
            "声音": "音乐",
            "image_id": f"img_{slot:02d}",
            "结构审核": "通过",
            "叙事审核": "通过",
            "节奏审核": "通过",
            "创意审核": "通过",
            "配图审核": "通过",
            "总体审核": "通过",
            "重大修改": "否",
            "问题代码": "",
            "问题严重级别": "",
            "修改意见": "",
            "审核人": "审核员",
            "审核日期": "2026-07-13",
        }
        if slot in (2, 4, 5):
            row["总体审核"] = "需修改"
            row["问题代码"] = "NARRATIVE_BRIDGE_BREAK"
            row["问题严重级别"] = "中"
            row["修改意见"] = "需要补一句承接"
        if slot == 5:
            row["配图审核"] = "需修改"
            row["问题代码"] = "VISUAL_MISMATCH"
            row["问题严重级别"] = "中"
            row["修改意见"] = "需替换匹配画面"
        return row

    def _legacy_editorial_row(self, slot: int) -> dict:
        row = self._editorial_row(slot)
        return {column: row[column] for column in LEGACY_EDITORIAL_COLUMNS}

    def _adjacent_row(self, edge: str) -> dict:
        return {
            "项目名称": "项目A",
            "边": edge,
            "叙事承接": "通过",
            "动作衔接": "通过",
            "运镜衔接": "通过",
            "色彩衔接": "通过",
            "声音衔接": "通过",
            "总体审核": "通过",
            "问题代码": "",
            "问题严重级别": "",
            "修改意见": "通过",
            "审核人": "审核员",
            "审核日期": "2026-07-13",
        }

    def _film_row(self) -> dict:
        return {
            "项目名称": "项目A",
            "叙事弧": "通过",
            "节奏曲线": "通过",
            "音乐曲线": "通过",
            "高潮与收束": "通过",
            "总体审核": "通过",
            "问题代码": "",
            "问题严重级别": "",
            "修改意见": "通过",
            "审核人": "审核员",
            "审核日期": "2026-07-13",
        }

    def _all_edges(self) -> list[str]:
        return [f"{index}->{index + 1}" for index in range(1, 18)]


if __name__ == "__main__":
    unittest.main()
