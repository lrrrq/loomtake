from __future__ import annotations

from copy import copy
from hashlib import sha256
from io import BytesIO
import unittest
from unittest.mock import patch
from xml.etree.ElementTree import tostring

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as SpreadsheetImage
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.worksheet.pagebreak import Break
from PIL import Image

from mvp_v2 import loop_runner
from tests import test_mvp_v2_formal_generation_service as generation_fixtures
from tests import test_mvp_v2_formal_script_workspace as workspace_fixtures


class ReviewWorkbookLayoutTests(unittest.TestCase):
    def _saved_version(self):
        write_library = generation_fixtures.write_launch_library
        render_workbook = loop_runner.write_template_output_workbook

        def styled_library(path, template_rows, creative_rows):
            write_library(path, template_rows, creative_rows)
            workbook = load_workbook(path)
            try:
                sheet = workbook["模版"]
                edge = Side(style="thin", color="406080")
                for row in sheet:
                    for cell in row:
                        cell.font = Font(name="Arial", size=11, color="203040")
                        cell.border = Border(left=edge, right=edge, top=edge, bottom=edge)
                        cell.alignment = Alignment(vertical="top", wrap_text=True)
                sheet["A1"].font = Font(name="Arial", size=18, bold=True, color="102030")
                sheet["A1"].fill = PatternFill("solid", fgColor="DCEAF2")
                sheet.row_dimensions[1].height = 34
                sheet.row_dimensions[7].height = 234
                sheet.column_dimensions["F"].width = 17.5
                sheet.column_dimensions["G"].width = 19.5
                sheet.column_dimensions["H"].width = 21.5
                # The final template rows deliberately have no reference image.
                for row in range(7, 7 + len(template_rows) - 2):
                    content = BytesIO()
                    with Image.new("RGB", (128, 72), ((row * 13) % 256, 80, (row * 29) % 256)) as image:
                        image.save(content, format="PNG")
                    content.seek(0)
                    sheet.add_image(SpreadsheetImage(content), f"C{row}")
                workbook.save(path)
            finally:
                workbook.close()

        def saved_layout(path, *args, **kwargs):
            render_workbook(path, *args, **kwargs)
            workbook = load_workbook(path)
            try:
                sheet = workbook["脚本"]
                sheet.freeze_panes = "C7"
                sheet.sheet_view.showGridLines = False
                sheet.sheet_view.zoomScale = 85
                sheet.page_setup.orientation = "landscape"
                sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
                sheet.page_setup.fitToWidth = 1
                sheet.page_setup.fitToHeight = 0
                sheet.sheet_properties.pageSetUpPr.fitToPage = True
                sheet.page_margins.left = 0.25
                sheet.page_margins.right = 0.35
                sheet.print_options.horizontalCentered = True
                sheet.print_title_rows = "1:6"
                sheet.print_area = f"A1:M{sheet.max_row}"
                sheet.oddFooter.center.text = "第 &P 页 / 共 &N 页"
                sheet.row_breaks.append(Break(id=15))
                notes = workbook.create_sheet("制作备注")
                notes.append(["事项", "制作说明", "原图路径", "内部artifact", "内部SHA256"])
                notes.append(["拍摄准备", "保留现场自然光，参考图待人工核对", "/private/anonymous/reference.png",
                              "artifact://anonymous/private", "b" * 64])
                notes.column_dimensions["B"].width = 42
                workbook.save(path)
            finally:
                workbook.close()

        fixture = workspace_fixtures.FormalScriptWorkspaceTests("runTest")
        self.addCleanup(fixture.doCleanups)
        with patch.object(generation_fixtures, "write_launch_library", side_effect=styled_library), patch.object(
            loop_runner, "write_template_output_workbook", side_effect=saved_layout,
        ):
            fixture.setUp()
        return fixture

    @staticmethod
    def _database_rows(store):
        with store.connection() as connection:
            tables = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )]
            return {table: sorted(
                [tuple(row) for row in connection.execute('SELECT * FROM "' + table.replace('"', '""') + '"')],
                key=repr,
            ) for table in tables}

    @staticmethod
    def _images(sheet):
        result = []
        for image in sheet._images:
            anchor = image.anchor
            markers = tuple(
                (marker.col, marker.row, marker.colOff, marker.rowOff) if marker is not None else None
                for marker in (getattr(anchor, "_from", None), getattr(anchor, "to", None))
            )
            extent = getattr(anchor, "ext", None)
            with Image.open(BytesIO(image._data())) as pixels:
                result.append((type(anchor).__name__, getattr(anchor, "editAs", None), markers,
                               (extent.cx, extent.cy) if extent else None,
                               pixels.size, sha256(pixels.convert("RGBA").tobytes()).hexdigest()))
        return result

    def test_review_copy_preserves_saved_layout_reference_images_and_business_sheets(self):
        fixture = self._saved_version()
        generated = fixture.generated
        scope = {"workspace_id": generated.workspace_id, "project_id": generated.project["project_id"]}
        current = fixture.workspace.read_current(generated.token, **scope)
        self.assertEqual(len(current["shots"]), 17)
        _, context = fixture.workspace._current_context(generated.token, **scope)
        source = fixture.resolver.resolve_artifact(context.run_id, "workbook").path
        source_bytes = source.read_bytes()
        before = load_workbook(BytesIO(source_bytes))
        self.addCleanup(before.close)
        script = before["脚本"]
        self.assertEqual(len(before.sheetnames), 9)
        self.assertEqual(script.max_row - 6, 17)
        self.assertGreaterEqual(len(script._images), 3)
        self.assertLess(len(script._images), 17)
        self.assertTrue(all(str(script.cell(row, column).value or "").strip()
                            for row in range(7, 24) for column in (6, 7, 8)))
        before_rows = self._database_rows(fixture.store)

        result = fixture.workspace.build_review_workbook(generated.token, **scope, version_no=1)

        self.assertEqual(self._database_rows(fixture.store), before_rows)
        self.assertEqual(source.read_bytes(), source_bytes)
        self.assertTrue(result["filename"].endswith("-脚本复核稿-V1.xlsx"))
        after = load_workbook(BytesIO(result["content"]))
        self.addCleanup(after.close)
        self.assertEqual(after.sheetnames, before.sheetnames)
        exported = after["脚本"]
        self.assertIn("复核稿", exported["A1"].value)
        self.assertIn("非正式交付", exported["A1"].value)
        self.assertTrue("V1" in exported["A1"].value or "版本 1" in exported["A1"].value)
        self.assertIn(script["A1"].value, exported["A1"].value)
        self.assertEqual((exported.max_row, exported.max_column), (script.max_row, script.max_column))
        self.assertEqual(sorted(map(str, exported.merged_cells.ranges)), sorted(map(str, script.merged_cells.ranges)))
        for row in script:
            for original in row:
                copied = exported[original.coordinate]
                if original.coordinate != "A1":
                    self.assertEqual(copied.value, original.value, original.coordinate)
                for attribute in ("font", "fill", "border", "alignment", "protection", "number_format"):
                    self.assertEqual(copy(getattr(copied, attribute)), copy(getattr(original, attribute)),
                                     (original.coordinate, attribute))
        for attribute in ("column_dimensions", "row_dimensions"):
            original_dimensions = getattr(script, attribute)
            copied_dimensions = getattr(exported, attribute)
            self.assertEqual(set(copied_dimensions), set(original_dimensions))
            for key, original in original_dimensions.items():
                self.assertEqual(dict(copied_dimensions[key]), dict(original), (attribute, key))
        self.assertEqual(exported.freeze_panes, script.freeze_panes)
        self.assertEqual(str(exported.print_area), str(script.print_area))
        self.assertEqual(exported.print_title_rows, script.print_title_rows)
        for attribute in ("page_setup", "page_margins", "print_options", "sheet_properties", "sheet_view", "row_breaks", "HeaderFooter"):
            self.assertEqual(tostring(getattr(exported, attribute).to_tree()),
                             tostring(getattr(script, attribute).to_tree()), attribute)
        self.assertEqual(self._images(exported), self._images(script))

        for original in before.worksheets[1:]:
            copied = after[original.title]
            self.assertEqual([image[-2:] for image in self._images(copied)],
                             [image[-2:] for image in self._images(original)], original.title)
            original_headers = [cell.value for cell in original[1]]
            copied_headers = [cell.value for cell in copied[1]]
            for column, header in enumerate(original_headers, start=1):
                technical = any(word in str(header).casefold() for word in ("路径", "sha256", "artifact", "规划事件id"))
                if technical and str(header).casefold() != "content_sha256":
                    self.assertNotIn(header, copied_headers, (original.title, header))
                    continue
                self.assertIn(header, copied_headers, (original.title, header))
                target_column = copied_headers.index(header) + 1
                for row in range(2, original.max_row + 1):
                    value = original.cell(row, column).value
                    if isinstance(value, str) and (value.startswith(("/", "~/", "file://"))
                                                  or "artifact://" in value or "sha256=" in value):
                        value = None
                    self.assertEqual(copied.cell(row, target_column).value, value,
                                     (original.title, row, header))
        details = after["抽调明细"]
        headers = [cell.value for cell in details[1]]
        for field in ("模板来源文件", "模板来源 sheet", "模板来源行"):
            column = headers.index(field) + 1
            self.assertTrue(all(details.cell(row, column).value for row in range(2, 19)), field)
        exported_text = "\n".join(str(cell.value or "") for sheet in after for row in sheet for cell in row)
        for private in (str(generated.root), generated.token, "artifact://", "sha256=", "b" * 64, context.manifest.sha256):
            self.assertNotIn(private, exported_text)
        self.assertEqual(after["制作备注"]["B2"].value, "保留现场自然光，参考图待人工核对")


if __name__ == "__main__":
    unittest.main()
