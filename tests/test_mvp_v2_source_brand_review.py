from __future__ import annotations

import tempfile
import unittest
import json
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image as PILImage

from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.cli import main
from mvp_v2.excel_io import load_creative_variants
from mvp_v2.source_brand_review import (
    BrandReviewError,
    load_creative_asset_review,
    load_source_brand_review,
)
from tests.test_mvp_v2 import full_launch_template_rows, write_launch_library


def write_workbook(path: Path, sheet_name: str, rows: list[list]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()


def write_source(root: Path) -> Path:
    path = root / "M+发布会类通用脚本库.xlsx"
    write_workbook(path, "创意库", [
        [], [], [], [],
        ["结构", "镜号", "画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"],
        ["结构", "镜号", "画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"],
        ["开场造势", "1", "", "", "", "地面", "全景", "推进", "品牌logo穿梭入画", "", ""],
        ["", "2", "", "", "", "地面", "中景", "固定", "现场互动", "", ""],
    ])
    return path


REVIEW_HEADERS = [
    "来源文件", "来源sheet", "来源行", "镜号", "结构", "画面描述", "画面示意锚点",
    "来源品牌", "品牌中性", "确认依据", "审核人", "审核日期", "审核状态", "审核备注",
]


def write_review(path: Path, rows: list[list]) -> None:
    write_workbook(path, "创意库审核", [
        ["发布会创意库来源品牌人工审核"],
        ["说明"],
        ["总条数", len(rows)],
        [],
        REVIEW_HEADERS,
        *rows,
    ])


def review_row(source_row: int, **overrides) -> list:
    data = {
        "来源文件": "M+发布会类通用脚本库.xlsx",
        "来源sheet": "创意库",
        "来源行": source_row,
        "镜号": "",
        "结构": "",
        "画面描述": "",
        "画面示意锚点": "",
        "来源品牌": "",
        "品牌中性": "待确认",
        "确认依据": "",
        "审核人": "",
        "审核日期": "",
        "审核状态": "待审核",
        "审核备注": "",
    }
    data.update(overrides)
    return [data[header] for header in REVIEW_HEADERS]


ASSET_REVIEW_HEADERS = [
    "visual_asset_id", "来源文件", "来源sheet", "来源行", "图片序号", "原图路径",
    "图片审核结论", "审核人", "审核日期", "图片审核备注",
]


def append_asset_review(path: Path, rows: list[list]) -> None:
    workbook = load_workbook(path)
    sheet = workbook.create_sheet("图片审核")
    sheet.append(["创意库图片资产逐张审核"])
    sheet.append(["只有可用图片可自动调用"])
    sheet.append(["图片数", len(rows)])
    sheet.append([])
    sheet.append(ASSET_REVIEW_HEADERS)
    for row in rows:
        sheet.append(row)
    workbook.save(path)
    workbook.close()


def asset_review_row(source_row: int, image_path: str, verdict: str, sequence: int = 1) -> list:
    data = {
        "visual_asset_id": f"asset-{source_row}-{sequence}",
        "来源文件": "M+发布会类通用脚本库.xlsx",
        "来源sheet": "创意库",
        "来源行": source_row,
        "图片序号": sequence,
        "原图路径": image_path,
        "图片审核结论": verdict,
        "审核人": "项目负责人",
        "审核日期": "2026-07-14",
        "图片审核备注": "",
    }
    return [data[header] for header in ASSET_REVIEW_HEADERS]


def add_excel_image(sheet, image_path: Path, anchor: str) -> ExcelImage:
    buffer = BytesIO()
    with PILImage.open(image_path) as source:
        source.save(buffer, format="PNG")
    buffer.seek(0)
    image = ExcelImage(buffer)
    sheet.add_image(image, anchor)
    return image


def close_excel_image(image: ExcelImage) -> None:
    if hasattr(image.ref, "close"):
        image.ref.close()


class SourceBrandReviewTests(unittest.TestCase):
    def test_loader_treats_images_anchored_in_form_column_as_visual_references(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "M+发布会类通用脚本库.xlsx"
            write_workbook(source, "创意库", [
                [], [], [], [],
                ["镜号", "结构", "形式", "内容", "", "", "", "", "", "", ""],
                ["", "", "", "画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
                ["1", "开场造势", "", "", "", "地面", "全景", "推进", "品牌logo穿梭入画", "", ""],
            ])
            workbook = load_workbook(source)
            excel_images = []
            for index, anchor in enumerate(("C7", "D7"), start=1):
                image_path = root / f"image-{index}.png"
                PILImage.new("RGB", (1280, 720), (index * 50, 0, 0)).save(image_path)
                excel_images.append(add_excel_image(workbook["创意库"], image_path, anchor))
            workbook.save(source)
            workbook.close()
            for image in excel_images:
                close_excel_image(image)

            variants = load_creative_variants(root)

            paths = [part.strip() for part in variants[0].reference_image_path.split(";") if part.strip()]
            self.assertEqual(len(paths), 2)

    def test_loader_preserves_multiple_images_on_the_same_source_anchor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_source(root)
            image_paths = []
            for index, color in enumerate(((255, 0, 0), (0, 0, 255)), start=1):
                image_path = root / f"image-{index}.png"
                PILImage.new("RGB", (1280, 720), color).save(image_path)
                image_paths.append(image_path)
            workbook = load_workbook(source)
            sheet = workbook["创意库"]
            excel_images = []
            for image_path in image_paths:
                excel_images.append(add_excel_image(sheet, image_path, "C7"))
            workbook.save(source)
            workbook.close()
            for image in excel_images:
                close_excel_image(image)

            variants = load_creative_variants(root)

            paths = [part.strip() for part in variants[0].reference_image_path.split(";") if part.strip()]
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(Path(path).exists() for path in paths))

    def test_asset_review_allows_only_individually_approved_images(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_source(root)
            for index, color in enumerate(((255, 0, 0), (0, 0, 255)), start=1):
                image_path = root / f"image-{index}.png"
                PILImage.new("RGB", (1280, 720), color).save(image_path)
                workbook = load_workbook(source)
                excel_image = add_excel_image(workbook["创意库"], image_path, "C7")
                workbook.save(source)
                workbook.close()
                close_excel_image(excel_image)
            extracted = load_creative_variants(root)[0].reference_image_path.split("; ")
            review = root / "review.xlsx"
            write_review(review, [review_row(7), review_row(8)])
            append_asset_review(review, [
                asset_review_row(7, extracted[0], "可用", 1),
                asset_review_row(7, extracted[1], "存疑", 2),
            ])

            variants = load_creative_variants(root, brand_review_path=review)

            self.assertEqual(variants[0].reference_image_path, extracted[0])

    def test_asset_review_sheet_is_an_authoritative_whitelist(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_source(root)
            image_path = root / "image.png"
            PILImage.new("RGB", (1280, 720), (255, 0, 0)).save(image_path)
            workbook = load_workbook(source)
            excel_image = add_excel_image(workbook["创意库"], image_path, "C7")
            workbook.save(source)
            workbook.close()
            close_excel_image(excel_image)
            review = root / "review.xlsx"
            write_review(review, [review_row(7), review_row(8)])
            append_asset_review(review, [])

            variants = load_creative_variants(root, brand_review_path=review)

            self.assertEqual(variants[0].reference_image_path, "")
            self.assertEqual(variants[0].reference_image_evidence, "")

    def test_asset_review_rejects_unknown_verdict(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            review = Path(temp_dir) / "review.xlsx"
            write_review(review, [])
            append_asset_review(review, [asset_review_row(7, "image.png", "差不多")])

            with self.assertRaisesRegex(BrandReviewError, "图片审核结论无效"):
                load_creative_asset_review(review)

    def test_pending_rows_explicitly_clear_source_brand_permission(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            review = Path(temp_dir) / "review.xlsx"
            write_review(review, [
                review_row(7, **{
                    "来源品牌": "林氏家居", "品牌中性": "否", "确认依据": "源项目文件",
                    "审核人": "张三", "审核日期": "2026-07-14", "审核状态": "已确认",
                }),
                review_row(8),
            ])

            loaded = load_source_brand_review(review)

            self.assertEqual(set(loaded), {
                ("M+发布会类通用脚本库.xlsx", "创意库", 7),
                ("M+发布会类通用脚本库.xlsx", "创意库", 8),
            })
            self.assertEqual(loaded[("M+发布会类通用脚本库.xlsx", "创意库", 7)]["source_brand"], "林氏家居")
            self.assertEqual(loaded[("M+发布会类通用脚本库.xlsx", "创意库", 8)]["source_brand"], "")

    def test_duplicate_headers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            review = Path(temp_dir) / "review.xlsx"
            write_review(review, [])
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "创意库审核"
            sheet.append([])
            sheet.append([])
            sheet.append([])
            sheet.append([])
            sheet.append(REVIEW_HEADERS + ["审核状态"])
            workbook.save(review)
            workbook.close()

            with self.assertRaisesRegex(BrandReviewError, "重复字段"):
                load_source_brand_review(review)

    def test_pending_review_overrides_existing_source_brand_to_block_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = write_source(root)
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "创意库"
            for row in range(1, 9):
                for col in range(1, 13):
                    sheet.cell(row, col, "")
            headers = ["结构", "镜号", "画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注", "来源品牌"]
            for index, header in enumerate(headers, start=1):
                sheet.cell(5, index, header)
                sheet.cell(6, index, header)
            sheet.cell(7, 1, "开场造势")
            sheet.cell(7, 2, "1")
            sheet.cell(7, 9, "品牌logo穿梭入画")
            sheet.cell(7, 12, "林氏家居")
            workbook.save(source)
            workbook.close()
            review = root / "review.xlsx"
            write_review(review, [review_row(7)])

            variants = load_creative_variants(root, brand_review_path=review)

            self.assertEqual(variants[0].source_brand, "")
            self.assertFalse(variants[0].brand_neutral)

    def test_confirmed_neutral_requires_human_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            review = Path(temp_dir) / "review.xlsx"
            write_review(review, [review_row(7, **{
                "品牌中性": "是", "审核状态": "已确认", "审核人": "张三", "审核日期": "2026-07-14",
            })])

            with self.assertRaisesRegex(BrandReviewError, "确认依据"):
                load_source_brand_review(review)

    def test_review_metadata_is_applied_by_source_triple(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_source(root)
            review = root / "review.xlsx"
            write_review(review, [
                review_row(7, **{
                    "品牌中性": "是", "确认依据": "画面无品牌元素", "审核人": "张三",
                    "审核日期": "2026-07-14", "审核状态": "已确认",
                }),
                review_row(8),
            ])

            variants = load_creative_variants(root, brand_review_path=review)

            self.assertTrue(variants[0].brand_neutral)
            self.assertEqual(variants[0].brand_neutral_evidence, "人工确认:张三;画面无品牌元素;2026-07-14")
            self.assertFalse(variants[1].brand_neutral)

    def test_cli_forwards_source_brand_review_to_loop(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            brief = Path(temp_dir) / "brief.xlsx"
            write_workbook(brief, "需求", [[
                "项目名称", "项目区域文件名", "脚本类型", "视频时长", "风格调性", "必须出现的内容",
            ], ["测试发布会", "test.xlsx", "发布会", "60s", "科技感", "logo"]])
            review = Path(temp_dir) / "review.xlsx"
            write_review(review, [])
            result = type("Result", (), {
                "exit_code": 2, "iterations": [], "best_failed_audit_path": None,
                "best_failed_output_path": None, "passed_iteration": None,
            })()
            with patch("mvp_v2.cli.run_generation_loop", return_value=result) as loop:
                main(["--input", str(brief), "--output", str(Path(temp_dir) / "out.xlsx"), "--loop", "--source-brand-review", str(review)])

            self.assertEqual(loop.call_args.kwargs["source_brand_review_path"], str(review))

    def test_cli_rejects_invalid_source_brand_review_before_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            brief = Path(temp_dir) / "brief.xlsx"
            write_workbook(brief, "需求", [[
                "项目名称", "项目区域文件名", "脚本类型", "视频时长", "风格调性", "必须出现的内容",
            ], ["测试发布会", "test.xlsx", "发布会", "60s", "科技感", "logo"]])
            review = Path(temp_dir) / "review.xlsx"
            write_workbook(review, "wrong", [["bad"]])
            with patch("mvp_v2.cli.run_generation_loop") as loop:
                exit_code = main([
                    "--input", str(brief), "--output", str(Path(temp_dir) / "out.xlsx"),
                    "--loop", "--source-brand-review", str(review),
                ])

            self.assertEqual(exit_code, 1)
            loop.assert_not_called()

    def test_cli_rejects_invalid_asset_review_before_generation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            brief = Path(temp_dir) / "brief.xlsx"
            write_workbook(brief, "需求", [[
                "项目名称", "项目区域文件名", "脚本类型", "视频时长", "风格调性", "必须出现的内容",
            ], ["测试发布会", "test.xlsx", "发布会", "60s", "科技感", "logo"]])
            review = Path(temp_dir) / "review.xlsx"
            write_review(review, [])
            append_asset_review(review, [asset_review_row(7, "image.png", "差不多")])
            with patch("mvp_v2.cli.run_generation_loop") as loop:
                exit_code = main([
                    "--input", str(brief), "--output", str(Path(temp_dir) / "out.xlsx"),
                    "--loop", "--source-brand-review", str(review),
                ])

            self.assertEqual(exit_code, 1)
            loop.assert_not_called()

    def test_source_brand_review_cli_does_not_adopt_unverified_neutral_candidate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            source_root.mkdir()
            write_launch_library(
                source_root / "M+发布会类通用脚本库.xlsx",
                full_launch_template_rows(),
                [[1, "开场造势", "", "", "", "航拍", "全景", "固定", "品牌logo穿梭入画", "", ""]],
            )
            workbook_path = source_root / "M+发布会类通用脚本库.xlsx"
            workbook = load_workbook(workbook_path)
            creative = workbook["创意库"]
            creative.cell(6, 12, "品牌中性")
            creative.cell(7, 12, "是")
            workbook.save(workbook_path)
            workbook.close()

            review = root / "review.xlsx"
            write_review(review, [review_row(7, **{
                "品牌中性": "是", "审核状态": "待审核",
            })])
            brief = root / "brief.txt"
            brief.write_text(
                "测试发布会\n脚本类型：发布会\n品牌或客户：测试品牌\n视频时长：60s\n"
                "风格调性：科技感\n必须出现的内容：品牌logo\n",
                encoding="utf-8",
            )
            output = root / "output.xlsx"

            code = main([
                "--brief-file", str(brief),
                "--source-root", str(source_root),
                "--source-brand-review", str(review),
                "--output", str(output),
            ])

            self.assertIn(code, {0, 2})
            workbook = load_workbook(output, read_only=True, data_only=True)
            try:
                details = workbook["抽调明细"]
                headers = {cell.value: cell.column for cell in details[1]}
                modes = [
                    details.cell(row, headers["替换方式"]).value
                    for row in range(2, details.max_row + 1)
                ]
            finally:
                workbook.close()
            self.assertNotIn("REPLACE", modes)
            self.assertNotIn("ADAPT", modes)

    def test_loop_cli_brand_verdict_requires_explicit_no_brand_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            source_root.mkdir()
            write_launch_library(
                source_root / "M+发布会类通用脚本库.xlsx",
                full_launch_template_rows(),
                [],
            )
            brief = root / "brief.txt"
            brief.write_text(
                "测试发布会\n脚本类型：发布会\n品牌或客户：测试品牌\n视频时长：60s\n"
                "风格调性：科技感\n必须出现的内容：品牌logo\n",
                encoding="utf-8",
            )
            manifest = root / "manifest.jsonl"
            manifest.write_text('{"image_id":"img-1"}\n', encoding="utf-8")

            for label, brand_names, blocked in (
                ("empty", [], True),
                ("unknown", ["未知"], True),
                ("no-brand", ["无"], False),
            ):
                with self.subTest(label=label):
                    analysis = root / f"analysis-{label}.jsonl"
                    analysis.write_text(json.dumps({
                        "image_id": "img-1",
                        "caption": "发布会主会场",
                        "tags": ["发布会", "主会场"],
                        "brand_names": brand_names,
                        "source": "minimax",
                    }, ensure_ascii=False) + "\n", encoding="utf-8")
                    output = root / f"output-{label}.xlsx"
                    code = main([
                        "--brief-file", str(brief),
                        "--source-root", str(source_root),
                        "--output", str(output),
                        "--loop", "--max-loops", "1",
                        "--image-manifest-jsonl", str(manifest),
                        "--image-analysis-jsonl", str(analysis),
                    ])
                    audit_path = root / f"output-{label}.best_failed.audit.json"
                    if code == 0:
                        audit_path = Path(str(output) + ".audit.json")
                    audit = json.loads(audit_path.read_text(encoding="utf-8"))
                    hard_items = {
                        check["检查项"]
                        for check in audit["checks"]
                        if check["级别"] == "hard" and check["状态"] == "fail"
                    }
                    warning_items = {
                        check["检查项"]
                        for check in audit["checks"]
                        if check["级别"] == "warning"
                    }
                    self.assertEqual(code, 0)
                    self.assertNotIn("missing_bound_visual", hard_items)
                    self.assertIn("missing_bound_visual", warning_items)
                    self.assertNotIn("image_analysis_missing_brand_verdict", hard_items)
                    if blocked:
                        self.assertIn("image_analysis_missing_brand_verdict", warning_items)
                    else:
                        self.assertNotIn("image_analysis_missing_brand_verdict", warning_items)

    def test_auto_pipeline_cli_brand_verdict_requires_explicit_no_brand_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_root = root / "source"
            source_root.mkdir()
            workbook_path = source_root / "M+发布会类通用脚本库.xlsx"
            write_launch_library(
                workbook_path,
                full_launch_template_rows(),
                [],
            )
            refresh_ai_spec(workbook_path)
            brief = root / "brief.txt"
            brief.write_text(
                "测试发布会\n脚本类型：发布会\n品牌或客户：测试品牌\n视频时长：60s\n"
                "风格调性：科技感\n必须出现的内容：品牌logo\n",
                encoding="utf-8",
            )
            manifest = root / "manifest.jsonl"
            manifest.write_text('{"image_id":"img-1"}\n', encoding="utf-8")

            for label, brand_names, blocked in (
                ("empty", [], True),
                ("unknown", ["未知"], True),
                ("no-brand", ["无"], False),
            ):
                with self.subTest(label=label):
                    analysis = root / f"auto-analysis-{label}.jsonl"
                    analysis.write_text(json.dumps({
                        "image_id": "img-1",
                        "caption": "发布会主会场",
                        "tags": ["发布会", "主会场"],
                        "brand_names": brand_names,
                        "source": "minimax",
                    }, ensure_ascii=False) + "\n", encoding="utf-8")
                    cache_root = root / f"assets-{label}"
                    code = main([
                        "--brief-file", str(brief),
                        "--source-root", str(source_root),
                        "--source-view", "ai-spec",
                        "--output", str(root / f"auto-output-{label}.xlsx"),
                        "--auto-pipeline", "--max-loops", "1",
                        "--asset-cache-root", str(cache_root),
                        "--image-manifest-jsonl", str(manifest),
                        "--image-analysis-jsonl", str(analysis),
                    ])
                    report_path = next(cache_root.glob("*/runs/*/run_report.json"))
                    report = json.loads(report_path.read_text(encoding="utf-8"))
                    audit = json.loads(Path(report["audit_path"]).read_text(encoding="utf-8"))
                    hard_items = {
                        check["检查项"]
                        for check in audit["checks"]
                        if check["级别"] == "hard" and check["状态"] == "fail"
                    }
                    warning_items = {
                        check["检查项"]
                        for check in audit["checks"]
                        if check["级别"] == "warning"
                    }
                    self.assertEqual(code, 3)
                    self.assertEqual(report["machine_audit_status"], "pass")
                    self.assertEqual(report["human_review_status"], "pending")
                    self.assertNotIn("missing_bound_visual", hard_items)
                    self.assertIn("missing_bound_visual", warning_items)
                    self.assertNotIn("image_analysis_missing_brand_verdict", hard_items)
                    if blocked:
                        self.assertIn("image_analysis_missing_brand_verdict", warning_items)
                    else:
                        self.assertNotIn("image_analysis_missing_brand_verdict", warning_items)


if __name__ == "__main__":
    unittest.main()
