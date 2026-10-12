from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from PIL import Image

from mvp_v2.ai_spec import (
    AI_SPEC_COLUMNS,
    AI_SPEC_SHEET,
    build_ai_spec_rows,
    check_ai_spec,
    load_ai_spec_shots,
    refresh_ai_spec,
)
from mvp_v2.ai_spec_cli import main as ai_spec_main
from mvp_v2.excel_io import load_creative_variants, load_template_slots
from mvp_v2.loop_runner import _generate
from mvp_v2.run_contract import sha256_file
from mvp_v2.template_logic import logical_slot_index


HEADERS = [
    "结构", "镜号", "画面示意", "形式", "效果", "机位", "景别", "运镜",
    "画面描述", "声音", "备注", "可复用等级", "禁用说明", "镜头功能",
    "画面主体", "标签", "来源品牌", "品牌中性", "品牌中性确认依据",
]


class AiSpecTests(unittest.TestCase):
    def test_seventeen_slot_template_is_usable_and_keeps_final_role(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_library(root, template_count=17, creative_count=1)

            report = refresh_ai_spec(path)
            slots = load_template_slots(root)

            self.assertTrue(report.in_sync)
            self.assertTrue(report.usable)
            self.assertEqual(report.template_count, 17)
            self.assertEqual(logical_slot_index(slots[-1], len(slots)), 18)

    def test_refresh_check_idempotency_preservation_and_reverse_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_library(root, creative_count=2, with_images=True)
            path.chmod(0o644)
            before = _human_snapshot(path)

            first = refresh_ai_spec(path)

            self.assertTrue(first.changed)
            self.assertTrue(first.in_sync)
            self.assertTrue(first.usable)
            self.assertEqual((first.template_count, first.creative_count), (18, 2))
            self.assertTrue(Path(f"{path}.ai-spec.bak").exists())
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(_human_snapshot(path), before)
            first_hash = sha256_file(path)

            second = refresh_ai_spec(path)

            self.assertFalse(second.changed)
            self.assertEqual(sha256_file(path), first_hash)
            human_template = load_template_slots(root)
            human_creative = load_creative_variants(root)
            ai_template, ai_creative = load_ai_spec_shots(root)
            self.assertEqual(_shot_contract(ai_template), _shot_contract(human_template))
            self.assertEqual(_shot_contract(ai_creative), _shot_contract(human_creative))
            self.assertEqual(ai_template[0].form, "固定镜头")
            self.assertEqual(len(ai_template[0].reference_image_path.split("; ")), 2)
            self.assertTrue(all(Path(value).exists() for value in ai_template[0].reference_image_path.split("; ")))

    def test_check_detects_human_change_and_ai_tampering(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_library(Path(temp_dir), creative_count=1)
            refresh_ai_spec(path)
            workbook = load_workbook(path)
            workbook["模版"]["I7"] = "人工修改后的画面"
            workbook.save(path)
            workbook.close()

            stale = check_ai_spec(path)

            self.assertFalse(stale.in_sync)
            self.assertIn("ai_spec_out_of_sync", stale.validation_codes)
            refresh_ai_spec(path)
            workbook = load_workbook(path)
            workbook[AI_SPEC_SHEET]["Q2"] = "直接篡改AI表"
            workbook.save(path)
            workbook.close()
            tampered = check_ai_spec(path)
            self.assertFalse(tampered.in_sync)
            self.assertIn("ai_spec_out_of_sync", tampered.validation_codes)

    def test_validation_marks_duplicate_invalid_brand_and_pending_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_library(Path(temp_dir), creative_count=4)
            workbook = load_workbook(path)
            creative = workbook["创意库"]
            creative["B7"] = "1"
            creative["L7"] = "神秘等级"
            creative["M7"] = ""
            creative["B8"] = "1"
            creative["R9"] = "是"
            creative["S9"] = ""
            creative["P10"] = ""
            workbook.save(path)
            workbook.close()

            rows = [row for row in build_ai_spec_rows(path) if row.record_type == "creative"]

            by_row = {row.source_row: row for row in rows}
            self.assertEqual(by_row[7].record_status, "blocked")
            self.assertIn("duplicate_record_id", by_row[7].validation_codes)
            self.assertIn("invalid_reuse_level", by_row[7].validation_codes)
            self.assertEqual(by_row[8].record_status, "blocked")
            self.assertEqual(by_row[9].record_status, "blocked")
            self.assertIn("brand_neutral_evidence_required", by_row[9].validation_codes)
            self.assertEqual(by_row[10].record_status, "pending_review")
            self.assertIn("missing_tags", by_row[10].validation_codes)

    def test_pending_confirmation_text_is_advisory_not_a_generation_gate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_library(Path(temp_dir), creative_count=1)
            workbook = load_workbook(path)
            workbook["创意库"]["K7"] = "执行方式待确认，由项目人员审核"
            workbook.save(path)
            workbook.close()

            row = next(
                row for row in build_ai_spec_rows(path)
                if row.record_type == "creative"
            )

            self.assertEqual(row.record_status, "ready")
            self.assertIn("review_hint_present", row.validation_codes)

    def test_category_workbook_uses_current_file_source_and_category_record_id(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            original = _write_library(root, creative_count=1)
            path = root / "M+赛事类通用脚本库_AI版v20260724.xlsx"
            original.rename(path)

            rows = build_ai_spec_rows(path)

            self.assertTrue(all(row.record_id.startswith("mplus-sports:") for row in rows))
            self.assertTrue(all(row.source_file == path.name for row in rows))
            self.assertEqual(rows[0].source_sheet, "模版")
            self.assertEqual(rows[0].source_row, 7)
            self.assertEqual(rows[0].source_anchor, f"{path.name}#模版!R7C9")

    def test_ai_formula_and_bad_headers_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_library(Path(temp_dir), creative_count=1)
            refresh_ai_spec(path)
            workbook = load_workbook(path)
            workbook[AI_SPEC_SHEET]["Q2"] = "=1+1"
            workbook.save(path)
            workbook.close()
            formula_report = check_ai_spec(path)
            self.assertFalse(formula_report.in_sync)
            self.assertIn("ai_spec_formula_forbidden", formula_report.validation_codes)

            refresh_ai_spec(path)
            workbook = load_workbook(path)
            workbook[AI_SPEC_SHEET]["A1"] = "wrong"
            workbook.save(path)
            workbook.close()
            header_report = check_ai_spec(path)
            self.assertIn("ai_spec_headers_invalid", header_report.validation_codes)

    def test_blank_framework_refreshes_header_only_and_returns_two(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_library(Path(temp_dir), template_count=0, creative_count=0)

            exit_code = ai_spec_main(["refresh", "--workbook", str(path)])

            self.assertEqual(exit_code, 2)
            report = check_ai_spec(path)
            self.assertTrue(report.in_sync)
            self.assertFalse(report.usable)
            workbook = load_workbook(path, read_only=True)
            self.assertEqual([cell.value for cell in workbook[AI_SPEC_SHEET][1]], AI_SPEC_COLUMNS)
            self.assertEqual(workbook[AI_SPEC_SHEET].max_row, 1)
            workbook.close()

    def test_generation_human_and_ai_source_views_select_same_sources(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_library(root, creative_count=2)
            refresh_ai_spec(path)
            workbook_hash = sha256_file(path)
            brief = {
                "项目名称": "同源对照",
                "项目区域文件名": "同源对照",
                "脚本类型": "发布会",
                "品牌或客户": "测试品牌",
                "行业或活动类别": "发布会",
                "视频时长": "60秒",
                "风格调性": "科技",
                "必须出现的内容": "",
                "禁忌或不能出现的内容": "",
            }

            human = _generate([brief], root, {}, None, "human")
            ai_spec = _generate([brief], root, {}, None, "ai-spec")

            human_decisions = human[2]
            ai_decisions = ai_spec[2]
            contract = lambda decisions: [
                (
                    decision.slot.source_file,
                    decision.slot.source_sheet,
                    decision.slot.source_row,
                    decision.variant.source_file if decision.variant else "",
                    decision.variant.source_sheet if decision.variant else "",
                    decision.variant.source_row if decision.variant else 0,
                )
                for decision in decisions
            ]
            self.assertEqual(contract(ai_decisions), contract(human_decisions))
            self.assertEqual(sha256_file(path), workbook_hash)

    def test_generation_views_are_exactly_equal_with_relative_root_and_embedded_images(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            absolute_root = Path(temp_dir)
            path = _write_library(absolute_root, creative_count=2, with_images=True)
            refresh_ai_spec(path)
            relative_root = absolute_root.relative_to(Path.cwd())
            brief = {
                "项目名称": "相对路径同源对照",
                "项目区域文件名": "相对路径同源对照",
                "脚本类型": "发布会",
                "品牌或客户": "测试品牌",
                "行业或活动类别": "发布会",
                "视频时长": "60秒",
                "风格调性": "科技",
                "必须出现的内容": "",
                "禁忌或不能出现的内容": "",
            }

            human = _generate([brief], relative_root, {}, None, "human")
            ai_spec = _generate([brief], relative_root, {}, None, "ai-spec")

            self.assertEqual(ai_spec[2], human[2])

    def test_generation_ai_spec_uses_same_project_number_fallback_as_human(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = _write_library(root, creative_count=2)
            refresh_ai_spec(path)
            media_dir = root / "发布会类" / "同源对照"
            media_dir.mkdir(parents=True)
            for shot_no in ("09", "13"):
                Image.new("RGB", (80, 45), (255, 0, 0)).save(media_dir / f"{shot_no}.png")
            brief = {
                "项目名称": "同源对照",
                "项目区域文件名": "同源对照",
                "脚本类型": "发布会",
                "品牌或客户": "测试品牌",
                "行业或活动类别": "发布会",
                "视频时长": "60秒",
                "风格调性": "科技",
                "必须出现的内容": "",
                "禁忌或不能出现的内容": "",
            }

            human = _generate([brief], root, {}, None, "human")
            ai_spec = _generate([brief], root, {}, None, "ai-spec")
            contract = lambda decisions: [
                (decision.slot.reference_image_path, decision.slot.reference_image_evidence)
                for decision in decisions
            ]

            self.assertEqual(
                [contract(human[2])[index] for index in (8, 12)],
                [
                    (str(media_dir / "09.png"), "项目编号回退"),
                    (str(media_dir / "13.png"), "项目编号回退"),
                ],
            )
            self.assertEqual(contract(ai_spec[2]), contract(human[2]))

    def test_refresh_preserves_unrelated_package_members_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = _write_library(Path(temp_dir), creative_count=1)
            payload = b"opaque-ole-payload\x00\x01"
            with ZipFile(path, "a", compression=ZIP_DEFLATED) as package:
                package.writestr("xl/embeddings/oleObject1.bin", payload)

            refresh_ai_spec(path)

            with ZipFile(path) as package:
                self.assertEqual(package.read("xl/embeddings/oleObject1.bin"), payload)


def _write_library(
    root: Path,
    template_count: int = 18,
    creative_count: int = 1,
    with_images: bool = False,
) -> Path:
    path = root / "M+发布会类通用脚本库.xlsx"
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    _fill_sheet(template, template_count, "模板")
    creative = workbook.create_sheet("创意库")
    _fill_sheet(creative, creative_count, "创意")
    image_handles = []
    if with_images and template_count:
        for index, color in enumerate(((255, 0, 0), (0, 0, 255)), start=1):
            image_path = root / f"ref-{index}.png"
            Image.new("RGB", (80, 45), color).save(image_path)
            image = ExcelImage(Image.open(image_path))
            template.add_image(image, "C7")
            image_handles.append(image)
    workbook.save(path)
    workbook.close()
    for image in image_handles:
        try:
            image.ref.close()
        except AttributeError:
            pass
    return path


def _fill_sheet(sheet, count: int, prefix: str) -> None:
    sheet.append([f"{prefix}库"])
    sheet.append(["保留公式", "=1+1"])
    sheet.append([])
    sheet.append([])
    sheet.append(HEADERS)
    sheet.append([None] * len(HEADERS))
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(HEADERS))
    for index in range(1, count + 1):
        sheet.append([
            "开场" if index == 1 else "",
            str(index),
            "",
            "固定镜头",
            "光效",
            "地面",
            "全景",
            "推进",
            f"{prefix}画面{index}",
            "音乐",
            "",
            "可复用" if prefix == "模板" else "镜头功能可复用",
            "",
            "建立空间",
            "舞台",
            "发布会; 舞台",
            "",
            "否",
            "",
        ])


def _human_snapshot(path: Path):
    workbook = load_workbook(path, data_only=False)
    snapshot = []
    try:
        for name in ("模版", "创意库"):
            sheet = workbook[name]
            values = tuple(tuple(cell.value for cell in row) for row in sheet.iter_rows())
            merges = tuple(sorted(str(value) for value in sheet.merged_cells.ranges))
            images = tuple(
                (image.anchor._from.row + 1, image.anchor._from.col + 1)
                for image in getattr(sheet, "_images", [])
            )
            snapshot.append((name, values, merges, images))
    finally:
        workbook.close()
    return tuple(snapshot)


def _shot_contract(shots):
    fields = (
        "shot_no", "structure", "form", "visual_desc", "reuse_level", "source_file",
        "source_sheet", "source_row", "reference_image_path", "slot_index",
    )
    rows = []
    for shot in shots:
        values = []
        for field in fields:
            value = getattr(shot, field)
            if field == "reference_image_path":
                value = "; ".join(str(Path(item).resolve()) for item in value.split("; ") if item)
            values.append(value)
        rows.append(tuple(values))
    return rows


if __name__ == "__main__":
    unittest.main()
