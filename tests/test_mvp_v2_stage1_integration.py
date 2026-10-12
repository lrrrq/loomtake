import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook, load_workbook

from mvp_v2.editorial_gate import (
    ADJACENT_REVIEW_SHEET_NAME,
    EXPECTED_ADJACENT_EDGES,
    FULL_FILM_REVIEW_COLUMNS,
    FULL_FILM_REVIEW_SHEET_NAME,
)
from mvp_v2.excel_io import (
    ADJACENT_REVIEW_OUTPUT_COLUMNS,
    BriefError,
    read_briefs,
    write_template_output_workbook,
)
from mvp_v2.review_quality import analyze_review_workbooks


class Stage1IntegrationTests(unittest.TestCase):
    def test_excel_brief_rejects_duration_other_than_sixty_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "brief.xlsx"
            workbook = Workbook()
            ws = workbook.active
            ws.append([
                "项目名称",
                "项目区域文件名",
                "脚本类型",
                "视频时长",
                "风格调性",
                "必须出现的内容",
            ])
            ws.append(["测试发布会", "测试", "发布会", "30s", "科技", "品牌logo"])
            workbook.save(path)
            workbook.close()

            with self.assertRaisesRegex(BriefError, "60"):
                read_briefs(path)

    def test_generated_workbook_contains_seventeen_edges_and_full_film_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "output.xlsx"
            script_rows = [
                {
                    "结构": f"结构{index}",
                    "镜号": str(index),
                    "时长(秒)": 60 if index == 1 else 0,
                    "画面描述": f"镜头{index}",
                    "声音": "音乐",
                }
                for index in range(1, 19)
            ]
            detail_rows = [
                {"项目名称": "测试发布会", "slot_index": index, "模板镜号": str(index)}
                for index in range(1, 19)
            ]

            write_template_output_workbook(path, script_rows, detail_rows)

            workbook = load_workbook(path, read_only=True, data_only=True)
            try:
                self.assertIn(ADJACENT_REVIEW_SHEET_NAME, workbook.sheetnames)
                self.assertIn(FULL_FILM_REVIEW_SHEET_NAME, workbook.sheetnames)
                edge_ws = workbook[ADJACENT_REVIEW_SHEET_NAME]
                film_ws = workbook[FULL_FILM_REVIEW_SHEET_NAME]
                self.assertEqual(
                    [cell.value for cell in edge_ws[1]],
                    ADJACENT_REVIEW_OUTPUT_COLUMNS,
                )
                self.assertEqual(
                    [edge_ws.cell(row=index + 2, column=2).value for index in range(17)],
                    list(EXPECTED_ADJACENT_EDGES),
                )
                self.assertEqual(edge_ws.max_row, 18)
                self.assertEqual([cell.value for cell in film_ws[1]], FULL_FILM_REVIEW_COLUMNS)
                self.assertEqual(film_ws.max_row, 2)
            finally:
                workbook.close()

            report = analyze_review_workbooks([path])
            self.assertEqual(report["human_review_status"], "pending")


if __name__ == "__main__":
    unittest.main()
