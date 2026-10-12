"""Regressions found by the anonymous safe-retry browser delivery journey."""

from pathlib import Path
import tempfile
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook

from mvp_v2.excel_io import scrub_workbook_paths


class RetriedDeliveryWorkbookTests(unittest.TestCase):
    def test_scrub_removes_parent_package_file_hyperlinks_and_keeps_external_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            workbook_path = root / "reviewed-script.xlsx"
            old_package_image = root / "old-package" / "reference.png"
            current_project_image = root / "project-images" / "current.png"
            workbook = Workbook()
            workbook.active.title = "脚本"
            workbook.active["A1"] = "用户保留说明"
            for title, coordinate in (("剪辑复核", "J2"), ("相邻镜头复核", "G2")):
                sheet = workbook.create_sheet(title)
                cell = sheet[coordinate]
                cell.value = str(current_project_image)
                cell.hyperlink = old_package_image.as_uri()
                cell.style = "Hyperlink"
                sheet["A1"] = "保留审核意见"
                sheet["B2"] = "品牌公开资料"
                sheet["B2"].hyperlink = "https://example.com/brand"
            workbook.save(workbook_path)
            workbook.close()
            retained_member = "customXml/anonymous-preserved.xml"
            retained_bytes = b"<business-note>unchanged</business-note>"
            with ZipFile(workbook_path, "a", compression=ZIP_DEFLATED) as archive:
                archive.writestr(retained_member, retained_bytes)

            # A child image version supplies its current project-image paths;
            # inherited package paths are intentionally absent from that set.
            scrub_workbook_paths(workbook_path, {current_project_image})

            checked = load_workbook(workbook_path)
            try:
                for title, coordinate in (("剪辑复核", "J2"), ("相邻镜头复核", "G2")):
                    with self.subTest(sheet=title):
                        self.assertIsNone(checked[title][coordinate].value)
                        self.assertIsNone(checked[title][coordinate].hyperlink)
                        self.assertEqual(checked[title]["A1"].value, "保留审核意见")
                        self.assertEqual(checked[title]["B2"].hyperlink.target, "https://example.com/brand")
                self.assertEqual(checked["脚本"]["A1"].value, "用户保留说明")
            finally:
                checked.close()
            with ZipFile(workbook_path) as archive:
                self.assertEqual(archive.read(retained_member), retained_bytes)
