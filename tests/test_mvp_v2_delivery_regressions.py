from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.utils import get_column_letter
from PIL import Image
from pypdf import PdfReader

from mvp_v2.asset_package import create_asset_package, package_media_for_brief, update_asset_package_index
from mvp_v2.excel_io import load_template_slots, write_reviewed_delivery_workbook, write_template_output_workbook
from mvp_v2.v10_image_delivery import create_formal_snapshot_pdf, create_v10_pdf_delivery
from tests.test_mvp_v2_asset_package import _write_source_with_reference_image


class DeliveryRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mplus-delivery-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_reindexed_embedded_replacement_gets_new_content_identity(self):
        source = self.root / "source"
        source.mkdir()
        path = source / "M+发布会类通用脚本库.xlsx"
        _write_source_with_reference_image(path, self.root / "original.png")
        package = create_asset_package(self.root / "package", package_id="launch-core", source_root=source)
        first, _ = package_media_for_brief(package, {"脚本类型": "发布会"})
        original_path = Path(first[0]["原图路径"])
        original_bytes = original_path.read_bytes()
        replacement = BytesIO()
        Image.new("RGB", (1280, 720), "blue").save(replacement, format="PNG")
        replacement.seek(0)
        workbook = load_workbook(path)
        workbook["模版"]._images.clear()
        workbook["模版"].add_image(ExcelImage(replacement), "C7")
        workbook["模版"]["I7"] = "替换后的蓝色场景"
        workbook.save(path)
        workbook.close()
        counts = update_asset_package_index(package)
        all_media, _ = package_media_for_brief(package, {"脚本类型": "发布会"})
        second = [row for row in all_media if row["asset_role"] == "source_reference"]
        self.assertEqual(len(second), 1)
        self.assertTrue(counts["source_reindexed"])
        with Image.open(second[0]["原图路径"]) as extracted:
            self.assertEqual(extracted.getpixel((0, 0)), (0, 0, 255))
        self.assertNotEqual(first[0]["image_id"], second[0]["image_id"])
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertFalse(next(row for row in all_media if row["image_id"] == first[0]["image_id"])["source_links"])
        update_asset_package_index(package)
        third, _ = package_media_for_brief(package, {"脚本类型": "发布会"})
        self.assertEqual(second[0]["image_id"], next(row for row in third if row["asset_role"] == "source_reference")["image_id"])
        extracted_path = Path(second[0]["原图路径"])
        extracted_path.write_bytes(b"corrupted cache")
        reloaded = load_template_slots(source, workbook_path=path)
        self.assertEqual(reloaded[0].reference_image_path, str(extracted_path))
        with Image.open(extracted_path) as recovered:
            self.assertEqual(recovered.getpixel((0, 0)), (0, 0, 255))

    def test_runtime_scrub_removes_hidden_local_links_preserving_business_images(self):
        from mvp_v2.v10_image_delivery import _scrub_runtime_paths
        image_path = self.root / 'anonymous.png'
        Image.new('RGB', (32, 24), 'blue').save(image_path)
        path = self.root / 'delivery.xlsx'
        book = Workbook()
        sheet = book.active
        sheet.append(['画面描述', '画面示意', '公开参考'])
        sheet.append(['来宾走入会场', str(image_path), '公共资料'])
        sheet['A2'].hyperlink = image_path.as_uri()
        sheet['C2'].hyperlink = 'https://example.com/reference'
        sheet.add_image(ExcelImage(image_path), 'B2')
        book.save(path)
        book.close()
        _scrub_runtime_paths(path, {image_path}, self.root)
        result = load_workbook(path)
        try:
            sheet = result.active
            self.assertEqual(sheet['A2'].value, '来宾走入会场')
            self.assertIsNone(sheet['A2'].hyperlink)
            self.assertEqual(sheet['C2'].hyperlink.target, 'https://example.com/reference')
            self.assertIsNone(sheet['B2'].value)
            self.assertEqual(len(sheet._images), 1)
            embedded = sheet._images[0]
            self.assertEqual((embedded.anchor._from.row, embedded.anchor._from.col), (1, 1))
            self.assertEqual(embedded._data(), image_path.read_bytes())
        finally:
            result.close()

    def test_delivery_keeps_images_dimensions_and_validation_on_business_columns(self):
        image = self.root / "preview.png"
        Image.new("RGB", (320, 180), "blue").save(image)
        scripts = [{"镜号": str(n), "时长(秒)": 10, "画面描述": f"匿名场景 {n}", "声音": "现场声", "画面示意": str(image)} for n in (1, 2, 3)]
        details = [{"项目名称": "匿名项目", "slot_index": n, "模板镜号": str(n)} for n in (1, 2, 3)]
        assets = [{"镜号": str(n), "image_id": f"image_{n}", "原图路径": str(image)} for n in (1, 2, 3)]
        source, target = self.root / "confirmed.xlsx", self.root / "delivery.xlsx"
        write_template_output_workbook(source, scripts, details, visual_asset_rows=assets, embed_evidence_previews=True)
        source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        review = {
            "issues": [{"issue_key": f"image:{n}", "shot_no": str(n), "status": "approve"} for n in (1, 2, 3)],
            "decisions": [{"issue_key": f"image:{n}", "decision": "approve", "reviewer_label": "匿名审核员", "decided_at": "2026-09-07"} for n in (1, 2, 3)],
            "final_confirmation": {"decision": "quality_pass"},
        }
        write_reviewed_delivery_workbook(source, target, review_model=review)
        before, after = load_workbook(source), load_workbook(target)
        self.addCleanup(before.close)
        self.addCleanup(after.close)
        for name in ("剪辑复核", "相邻镜头复核", "视觉资产", "配图复核", "抽调明细"):
            old, new = before[name], after[name]
            with self.subTest(sheet=name):
                self.assertEqual(len(new._images), len(old._images))
                for old_image, new_image in zip(old._images, new._images):
                    self.assertEqual(new.cell(1, new_image.anchor._from.col + 1).value, old.cell(1, old_image.anchor._from.col + 1).value)
                    self.assertEqual(new_image.anchor._from.row, old_image.anchor._from.row)
                    self.assertEqual(new_image._data(), old_image._data())
                    width = new.column_dimensions[get_column_letter(new_image.anchor._from.col + 1)].width
                    self.assertLessEqual(new_image.anchor._from.colOff + new_image.anchor.ext.cx, int(width * 7 + 5) * 9525, "预览图不得遮挡相邻审核列")
                    self.assertAlmostEqual(new_image.anchor.ext.cx / new_image.anchor.ext.cy, old_image.anchor.ext.cx / old_image.anchor.ext.cy, places=5)
                old_headers = {cell.value: cell.column for cell in old[1]}
                for cell in new[1]:
                    previous = old.column_dimensions[get_column_letter(old_headers[cell.value])]
                    current = new.column_dimensions[cell.column_letter]
                    self.assertEqual((current.width, current.hidden), (previous.width, previous.hidden), cell.value)
                self.assertEqual(_validation_headers(new), _validation_headers(old))
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), source_sha)

    def _formal_pdf(self, visual):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "脚本"
        sheet.append(["镜号", "画面描述", "声音"])
        scripts, traces = [], []
        for n in (1, 2):
            row = {"镜号": str(n), "画面描述": visual if n == 1 else "末镜画面", "声音": f"第{n}镜现场声音", "旁白": "独立旁白", "字幕": "独立字幕"}
            scripts.append(row)
            sheet.append([row["镜号"], row["画面描述"], row["声音"]])
            data = BytesIO()
            Image.new("RGB", (320, 180), "blue").save(data, format="PNG")
            data.seek(0)
            sheet.add_image(ExcelImage(data), f"D{n + 1}")
            traces.append({"shot_no": str(n), "source_file": "anonymous.xlsx", "source_sheet": "模版", "source_row": n + 6, "image_sha256": "b" * 64})
        path = self.root / "input.xlsx"
        workbook.save(path)
        workbook.close()
        artifact = create_formal_snapshot_pdf(path, workbook_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), snapshot_digest="a" * 64, project_id="project_example", version_id="version_example", run_id="run_example", source_trace=traces, script_rows=scripts, output_path=self.root / "output.pdf")
        return artifact, PdfReader(artifact.pdf_path)

    def _assert_readable_pdf(self, reader, visual):
        extracted = []
        body = []
        for page in reader.pages:
            positions = []
            def visit(text, cm, tm, font, size):
                if text.strip() and size == 10:
                    body.append(text)
                    positions.append((round(tm[4], 2), round(tm[5], 2)))
                    self.assertGreaterEqual(tm[5], 60, text)
            extracted.append(page.extract_text(visitor_text=visit))
            self.assertEqual(len(positions), len(set(positions)), "正文在同一坐标叠印")
            self.assertIn("image_sha256=", extracted[-1])
        all_text = "".join("".join(body).split())
        self.assertIn("".join(visual.split()), all_text)
        for text in ("第1镜现场声音", "第2镜现场声音", "独立旁白", "独立字幕", "末镜画面"):
            self.assertIn(text, all_text)

    def test_formal_pdf_multiline_fields_do_not_overlap(self):
        visual = "镜头跟随人物走入活动现场，依次拍摄舞台灯光、观众互动和现场品牌装置。" * 4
        artifact, reader = self._formal_pdf(visual)
        self.assertEqual(artifact.shot_count, 2)
        self._assert_readable_pdf(reader, visual)

    def test_formal_pdf_long_field_continues_without_clipping(self):
        visual = "长篇画面内容需要完整保留，逐项描述动作与场景。" * 180 + "\n最后一段必须可读。"
        artifact, reader = self._formal_pdf(visual)
        self.assertEqual(artifact.shot_count, 2)
        self.assertGreater(len(reader.pages), 2)
        self._assert_readable_pdf(reader, visual)

    def test_v10_pdf_long_field_keeps_same_version_and_continues(self):
        from tests.test_mvp_v2_v10_image_delivery import V10ImageDeliveryTests
        import json
        case = V10ImageDeliveryTests("runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        visual = "长篇画面内容需要完整保留，逐项描述动作与场景。" * 180
        binding = case.artifacts["artifact://run/run-002/visual_binding"]
        data = json.loads(binding.read_text())
        data["script_rows"][0].update({"画面描述": visual, "声音": "第1镜现场声音", "旁白": "独立旁白", "字幕": "独立字幕"})
        data["script_rows"][1].update({"画面描述": "末镜画面", "声音": "第2镜现场声音"})
        binding.write_text(json.dumps(data, ensure_ascii=False))
        case.context, case.pool, case.snapshot, case.quality = case._authority_objects()
        excel = case._deliver()
        pdf = create_v10_pdf_delivery(excel, output_root=case.output)
        reader = PdfReader(pdf.pdf_path)
        self.assertGreater(len(reader.pages), 2)
        self.assertIn(excel.workbook_sha256, reader.metadata["/Subject"])
        self._assert_readable_pdf(reader, visual)


def _validation_headers(sheet):
    return sorted((validation.formula1, tuple(sorted(sheet.cell(1, col).value for area in validation.sqref.ranges for col in range(area.min_col, area.max_col + 1)))) for validation in sheet.data_validations.dataValidation)
