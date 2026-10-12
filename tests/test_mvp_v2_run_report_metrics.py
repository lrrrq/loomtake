import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

from mvp_v2.auto_pipeline import _workbook_metrics


class RunReportMetricsTests(unittest.TestCase):
    def test_workbook_metrics_report_dynamic_review_and_source_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "result.xlsx"
            workbook = Workbook()
            detail = workbook.active
            detail.title = "抽调明细"
            detail.append([
                "采用来源文件", "采用来源 sheet", "采用来源行", "时长(秒)",
            ])
            detail.append(["library.xlsx", "模版", 7, 20])
            detail.append(["library.xlsx", "创意库", 9, 40])
            visuals = workbook.create_sheet("视觉资产")
            visuals.append([
                "image_id", "原图路径", "来源文件", "来源 sheet", "来源行",
            ])
            visuals.append([
                "media_complete", "/tmp/reference.png", "library.xlsx", "创意库", 8,
            ])
            visuals.append([
                "project_complete", "/tmp/project.png", "/tmp/project.png", "项目图片池", "",
            ])
            visuals.append(["", "", "library.xlsx", "创意库", 9])
            adjacent = workbook.create_sheet("相邻镜头复核")
            adjacent.append(["边"])
            adjacent.append(["1->2"])
            full = workbook.create_sheet("整片复核")
            full.append(["项目名称"])
            full.append(["测试发布会"])
            workbook.save(path)
            workbook.close()

            metrics = _workbook_metrics(path, {"品牌或客户": "测试品牌"})

        self.assertEqual(metrics["shot_count"], 2)
        self.assertEqual(metrics["total_duration_seconds"], 60)
        self.assertEqual(metrics["adjacent_edge_count"], 1)
        self.assertEqual(metrics["full_film_review_count"], 1)
        self.assertEqual(metrics["source_triplet_count"], 2)
        self.assertEqual(metrics["source_reference_rate"], 1.0)
        self.assertEqual(metrics["visual_asset_count"], 2)
        self.assertEqual(metrics["visual_slot_count"], 3)
        self.assertEqual(metrics["visual_gap_count"], 1)
        self.assertEqual(metrics["visual_traceable_asset_count"], 1)
        self.assertEqual(metrics["visual_evidence_rate"], 0.6667)
        self.assertEqual(metrics["visual_traceability_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
