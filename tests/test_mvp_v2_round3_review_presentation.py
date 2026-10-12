from __future__ import annotations

from copy import copy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import posixpath
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

from mvp_v2.audit import audit_generation
from mvp_v2.excel_io import build_script_review_workbook
from mvp_v2.models import SourceShot, TemplateDecision


MANAGED_SHEETS = (
    "脚本", "抽调明细", "视觉资产", "配图复核", "剪辑复核",
    "相邻镜头复核", "整片复核", "质量审计", "审核记录",
)
OLD_ISSUE = "参考来源命中 Brief 禁忌，但仅作为已审核镜头依据: 烟酒"
OLD_SUGGESTION = "允许保留来源；人工复核最终文案和画面描述已完成当前项目适配"


class ReviewPresentationRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def _fixture(self, configure=None):
        workbook = Workbook()
        script = workbook.active
        script.title = "脚本"
        script["A1"] = "匿名厨房用品介绍片"
        script.merge_cells("A1:L1")
        headers = ("镜号", "时长(秒)", "画面描述", "声音", "备注", "来源文件",
                   "来源Sheet", "来源行", "人工审核", "审核人", "审核意见", "审核时间")
        for column, title in enumerate(headers, 1):
            script.cell(6, column, title)
        for index in range(1, 8):
            row = index + 6
            values = (str(index), 8, f"匿名产品动作{index}，厨房台面与产品均可见。",
                      "轻音乐及现场声，无旁白。", "保留真实构图、来源与手部动作；待人工核对。" * 8,
                      "匿名产品模板V1.xlsx", "模版", index + 6, None, None, None, None)
            for column, value in enumerate(values, 1):
                cell = script.cell(row, column, value)
                cell.font = Font(name="Arial", size=11)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            script.row_dimensions[row].height = 18
        script.column_dimensions["C"].width = 24
        script.column_dimensions["D"].width = 20
        script.column_dimensions["E"].width = 24
        script.column_dimensions["F"].width = 28
        script.freeze_panes = "C7"
        for name in MANAGED_SHEETS[1:]:
            sheet = workbook.create_sheet(name)
            if name == "质量审计":
                sheet.append(["检查项", "级别", "状态", "镜号", "问题", "建议"])
                sheet.append(["brief_forbidden_source_hit", "warning", "warning", "1",
                              OLD_ISSUE, OLD_SUGGESTION])
            else:
                sheet.append(["镜号", "核对说明", "来源文件", "来源Sheet", "来源行",
                              "人工审核", "审核人", "审核意见", "审核时间"])
                count = 6 if name == "相邻镜头复核" else 1 if name == "整片复核" else 7
                for index in range(1, count + 1):
                    sheet.append([str(index), "匿名说明；保留来源并逐项人工核对。" * 6,
                                  "匿名产品模板V1.xlsx", "模版", index + 6,
                                  None, None, None, None])
            sheet.column_dimensions["B"].width = 20
            for row in sheet.iter_rows(min_row=2):
                sheet.row_dimensions[row[0].row].height = 18
                for cell in row:
                    cell.font = Font(name="Arial", size=11)
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
        notes = workbook.create_sheet("客户制作备注")
        notes.append(["事项", "原始备注", "原始状态"])
        notes.append(["参考保留", OLD_SUGGESTION, "待审批"])
        notes.row_dimensions[2].height = 22
        notes.column_dimensions["B"].width = 36
        if configure:
            configure(workbook)
        source = self.root / "anonymous-source.xlsx"
        workbook.save(source)
        workbook.close()
        with ZipFile(source) as archive:
            package = {name: archive.read(name) for name in archive.namelist()}
        package["customXml/anonymous.xml"] = b"<anonymous>preserve-review-evidence</anonymous>"
        package["xl/embeddings/anonymous.bin"] = b"anonymous-opaque-business-data"
        with ZipFile(source, "w", ZIP_DEFLATED) as archive:
            for name, value in package.items():
                archive.writestr(name, value)
        return source

    def _export(self, source):
        before = source.read_bytes()
        source_sha = sha256(before).hexdigest()
        target = self.root / "anonymous-review.xlsx"
        target.write_bytes(build_script_review_workbook(source, version_no=3))
        self.assertTrue(target.is_file())
        self.assertGreater(target.stat().st_size, 0)
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(sha256(source.read_bytes()).hexdigest(), source_sha)
        workbook = load_workbook(target)
        self.addCleanup(workbook.close)
        return workbook, target

    @staticmethod
    def _sheet_xml(path, title):
        with ZipFile(path) as archive:
            namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
            relation_namespace = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            sheet = next(item for item in workbook.find(f"{{{namespace}}}sheets")
                         if item.get("name") == title)
            relation_id = sheet.get(f"{{{relation_namespace}}}id")
            relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            target = next(item.get("Target") for item in relations if item.get("Id") == relation_id)
            part = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
            return archive.read(part)

    def test_multishot_copy_keeps_values_sources_and_empty_human_review(self):
        source = self._fixture()
        original = load_workbook(source)
        self.addCleanup(original.close)
        exported, _ = self._export(source)
        self.assertIn("复核稿，非正式交付", exported["脚本"]["A1"].value)
        self.assertEqual(exported["脚本"].max_row - 6, 7)
        self.assertEqual(exported["相邻镜头复核"].max_row - 1, 6)
        for name in MANAGED_SHEETS:
            sheet = exported[name]
            self.assertTrue(sheet.print_area, name)
            self.assertEqual(sheet.page_setup.orientation, "landscape", name)
            self.assertEqual(sheet.page_setup.paperSize, int(sheet.PAPERSIZE_A3), name)
            self.assertEqual(sheet.page_setup.scale, 100, name)
            self.assertIsNone(sheet.page_setup.fitToWidth, name)
            if name == "质量审计":
                continue
            for row in original[name]:
                for cell in row:
                    if name == "脚本" and cell.coordinate == "A1":
                        continue
                    copied = sheet[cell.coordinate]
                    self.assertEqual(copied.value, cell.value, (name, cell.coordinate))
                    self.assertEqual(copy(copied.alignment), copy(cell.alignment))
        script = exported["脚本"]
        self.assertEqual(script.print_title_rows, "$1:$6")
        self.assertEqual(script.print_title_cols, "$A:$B")
        self.assertIn("$A$1:$L$13", str(script.print_area))
        for row in range(7, 14):
            self.assertGreater(script.row_dimensions[row].height, 96)
            self.assertLessEqual(script.row_dimensions[row].height, 409.5)
            self.assertTrue(all(script.cell(row, column).value is None for column in range(9, 13)))

    def test_unknown_business_sheet_xml_and_opaque_package_members_survive(self):
        source = self._fixture()
        original_xml = self._sheet_xml(source, "客户制作备注")
        _, target = self._export(source)
        self.assertEqual(self._sheet_xml(target, "客户制作备注"), original_xml)
        with ZipFile(source) as original, ZipFile(target) as exported:
            for member in ("customXml/anonymous.xml", "xl/embeddings/anonymous.bin"):
                self.assertEqual(exported.read(member), original.read(member))
        reopened = load_workbook(target)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened["客户制作备注"].print_area)
        self.assertEqual(reopened["客户制作备注"]["B2"].value, OLD_SUGGESTION)

    def test_existing_print_area_keeps_entire_configured_layout(self):
        def configure(workbook):
            sheet = workbook["脚本"]
            sheet.print_area = "A1:K12"
            sheet.print_title_rows = "2:6"
            sheet.print_title_cols = "C:D"
            sheet.page_setup.orientation = "portrait"
            sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
            sheet.page_setup.scale = 75
            sheet.page_setup.fitToWidth = 2
            sheet.page_setup.fitToHeight = 0
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.row_dimensions[7].height = 27
        source = self._fixture(configure)
        original = load_workbook(source)
        self.addCleanup(original.close)
        exported, _ = self._export(source)
        before, after = original["脚本"], exported["脚本"]
        for attribute in ("print_area", "print_title_rows", "print_title_cols"):
            self.assertEqual(str(getattr(after, attribute)), str(getattr(before, attribute)))
        self.assertEqual(ET.tostring(after.page_setup.to_tree()), ET.tostring(before.page_setup.to_tree()))
        self.assertEqual(ET.tostring(after.sheet_properties.to_tree()), ET.tostring(before.sheet_properties.to_tree()))
        self.assertEqual(after.row_dimensions[7].height, 27)

    def test_partial_title_configuration_is_preserved_and_only_missing_axis_is_filled(self):
        for existing_axis in ("rows", "cols"):
            with self.subTest(existing_axis=existing_axis):
                def configure(workbook):
                    sheet = workbook["脚本"]
                    if existing_axis == "rows":
                        sheet.print_title_rows = "3:6"
                    else:
                        sheet.print_title_cols = "C:D"
                    sheet.page_setup.orientation = "portrait"
                    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
                    sheet.page_setup.scale = 80
                source = self._fixture(configure)
                exported, _ = self._export(source)
                script = exported["脚本"]
                self.assertEqual(script.print_title_rows, "$3:$6" if existing_axis == "rows" else "$1:$6")
                self.assertEqual(script.print_title_cols, "$C:$D" if existing_axis == "cols" else "$A:$B")
                self.assertEqual(script.page_setup.orientation, "portrait")
                self.assertEqual(script.page_setup.paperSize, int(script.PAPERSIZE_A4))
                self.assertEqual(script.page_setup.scale, 80)
                self.assertTrue(script.print_area)

    def test_partial_fit_strategy_without_print_area_is_not_replaced_with_scale(self):
        def configure(workbook):
            sheet = workbook["脚本"]
            sheet.page_setup.fitToWidth = 2
            sheet.page_setup.fitToHeight = 0
            sheet.sheet_properties.pageSetUpPr.fitToPage = True
            sheet.print_title_rows = "4:6"
            sheet.print_title_cols = "B:C"
        source = self._fixture(configure)
        exported, _ = self._export(source)
        script = exported["脚本"]
        self.assertTrue(script.print_area)
        self.assertIsNone(script.page_setup.scale)
        self.assertEqual((script.page_setup.fitToWidth, script.page_setup.fitToHeight), (2, 0))
        self.assertTrue(script.sheet_properties.pageSetUpPr.fitToPage)
        self.assertEqual(script.print_title_rows, "$4:$6")
        self.assertEqual(script.print_title_cols, "$B:$C")

    def test_cjk_wrap_grows_more_than_ascii_and_extreme_text_is_preserved_at_excel_height_limit(self):
        def configure(workbook):
            sheet = workbook["脚本"]
            sheet.column_dimensions["C"].width = 10
            for row, text in ((7, "A" * 100), (8, "字" * 100), (9, "长备注" * 1000)):
                sheet.cell(row, 3, text)
                sheet.cell(row, 5, "短备注")
        source = self._fixture(configure)
        exported, _ = self._export(source)
        script = exported["脚本"]
        self.assertGreater(script.row_dimensions[8].height, script.row_dimensions[7].height)
        self.assertEqual(script.row_dimensions[9].height, 409.5)
        self.assertEqual(script["C9"].value, "长备注" * 1000)

    def test_old_audit_warning_copy_does_not_claim_human_review_or_change_severity(self):
        source = self._fixture()
        original = load_workbook(source)
        original["质量审计"].append(["missing_image", "warning", "warning", "2",
            "该镜头没有可用嵌入图，但已审核文字和来源三元组仍可作为脚本文档依据",
            "脚本文档可继续审核；成片制作前再补充当前项目图片或视频"])
        original.save(source)
        original.close()
        exported, _ = self._export(source)
        audit = exported["质量审计"]
        self.assertEqual([audit.cell(2, column).value for column in range(1, 5)],
                         ["brief_forbidden_source_hit", "warning", "warning", "1"])
        self.assertNotIn("已审核镜头", audit["E2"].value)
        self.assertNotIn("人工复核最终文案和画面描述已完成", audit["F2"].value)
        self.assertIn("需核对最终镜头表达", audit["E2"].value)
        self.assertIn("请人工核对", audit["F2"].value)
        self.assertIn("烟酒", audit["E2"].value)
        self.assertEqual([audit.cell(3, column).value for column in range(1, 5)],
                         ["missing_image", "warning", "warning", "2"])
        self.assertNotIn("已审核", audit["E3"].value)
        self.assertIn("供人工核对", audit["E3"].value)
        self.assertIn("配图仍待确认", audit["E3"].value)

    def test_new_audit_warning_also_requires_human_check_without_excluding_reference(self):
        slots = [SourceShot(script_type="产品介绍", source_file="匿名产品模板V1.xlsx",
                            source_sheet="模版", source_row=index + 6, shot_no=str(index),
                            structure="产品展示", visual_desc="烟酒陈列参考" if index == 1 else "厨房产品参考",
                            slot_index=index) for index in range(1, 8)]
        decisions = [TemplateDecision(slot=slot, variant=None, mode="KEEP", score=0,
                                      match_reason="保留参考") for slot in slots]
        scripts = [{"镜号": str(index), "结构": "产品展示", "时长(秒)": 8,
                    "画面描述": "匿名厨房用品正常展示", "声音": "现场声"} for index in range(1, 8)]
        details = [{"slot_index": slot.slot_index, "模板镜号": slot.shot_no,
                    "模板来源文件": slot.source_file, "模板来源 sheet": slot.source_sheet,
                    "模板来源行": slot.source_row, "采用来源文件": slot.source_file,
                    "采用来源 sheet": slot.source_sheet, "采用来源行": slot.source_row,
                    "采用来源类型": "模板", "替换方式": "KEEP"} for slot in slots]
        result = audit_generation({"项目名称": "匿名用品介绍", "脚本类型": "产品介绍",
                                   "品牌或客户": "匿名", "视频时长": "56s",
                                   "禁忌或不能出现的内容": "烟酒"}, slots, decisions, scripts, details)
        warning = next(item for item in result["checks"] if item["检查项"] == "brief_forbidden_source_hit")
        self.assertEqual((warning["级别"], warning["状态"]), ("warning", "warning"))
        self.assertNotIn("已审核镜头", warning["详情"])
        self.assertIn("请人工核对", warning["修复建议"])
        self.assertNotIn("已完成当前项目适配", warning["修复建议"])
        self.assertNotIn("brief_forbidden_source_hit", result["blocking_items"])
        self.assertEqual(result["next_constraints"]["global_excludes"], [])
        missing = [item for item in result["checks"] if item["检查项"] == "missing_image"]
        self.assertEqual(len(missing), 7)
        self.assertTrue(all(item["级别"] == item["状态"] == "warning" for item in missing))
        self.assertTrue(all("已审核" not in item["详情"] and "配图仍待确认" in item["详情"]
                            for item in missing))


if __name__ == "__main__":
    unittest.main()
