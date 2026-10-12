from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mvp_v2.alpha_brief import AlphaBriefError, AlphaBriefStore
from mvp_v2.models import SourceShot
from mvp_v2.planning_core import compile_plan_artifact


class AlphaBriefTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mplus-alpha-")
        self.root = Path(self.temp.name)
        self.store = AlphaBriefStore(self.root)
        self.inputs = self.root / "inputs"
        self.inputs.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_five_anonymous_formats_produce_traceable_confirmable_brief(self):
        pdf = self.inputs / "anonymous.pdf"
        _text_pdf(pdf, "Project: Alpha")
        image = self.inputs / "anonymous.png"
        Image.new("RGB", (20, 10), "blue").save(image)
        docx = self.inputs / "anonymous.docx"
        _office_zip(
            docx,
            "word/document.xml",
            "<w:document xmlns:w=\"w\"><w:body><w:p><w:r><w:t>品牌: 匿名品牌</w:t></w:r></w:p></w:body></w:document>",
            {"word/media/image1.png": image.read_bytes()},
        )
        xlsx = self.inputs / "anonymous.xlsx"
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "资料"
        sheet["A1"] = "脚本类型: 发布会"
        sheet["A2"] = "目标时长: 60秒"
        sheet["A3"] = "风格调性: 简洁可信"
        sheet["A4"] = "必须出现: 产品演示"
        workbook.save(xlsx)
        pptx = self.inputs / "anonymous.pptx"
        _office_zip(pptx, "ppt/slides/slide1.xml", "<p:sld xmlns:p=\"p\"><p:spTree><p:txBody><a:p xmlns:a=\"a\"><a:r><a:t>关键事件: 产品演示</a:t></a:r></a:p></p:txBody></p:spTree></p:sld>")
        self.store.freeze_documents([pdf, docx, xlsx, pptx, image])
        imported = self.store.import_documents("alpha-project", [pdf, docx, xlsx, pptx, image])

        self.assertEqual([item["status"] for item in imported["extractions"]], ["succeeded"] * 5)
        self.assertFalse(imported["conflicts"])
        self.assertEqual({fact["field"] for fact in imported["facts"]}, {"项目名称", "品牌或客户", "脚本类型", "目标时长", "风格调性", "必须出现的内容", "关键事件与流程"})
        self.assertTrue(all(fact["evidence_refs"] for fact in imported["facts"]))
        self.assertEqual(len(imported["images"]), 1)
        self.assertEqual(len(imported["images"][0]["source_links"]), 2)
        self.assertNotIn(str(self.root), str(imported))
        refs = {location["evidence_ref"] for extraction in imported["extractions"] for location in extraction["locations"]}
        self.assertTrue(any("/page/1" in ref for ref in refs))
        self.assertTrue(any("/paragraph/1" in ref for ref in refs))
        self.assertTrue(any("/sheet/%E8%B5%84%E6%96%99/row/1/column/1" in ref for ref in refs))
        self.assertTrue(any("/slide/1" in ref for ref in refs))
        self.assertTrue(any("/image/1" in ref for ref in refs))

        confirmed = self.store.confirm(imported, confirmed_by="alpha-manager")
        self.assertEqual(confirmed["confirmation_status"], "confirmed")
        self.assertRegex(confirmed["confirmed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.store.read_confirmed("alpha-project", confirmed["brief_ref"], confirmed["brief_sha256"]), confirmed)
        with self.assertRaisesRegex(AlphaBriefError, "不属于当前隔离项目"):
            self.store.read_confirmed("other-project", confirmed["brief_ref"], confirmed["brief_sha256"])
        planner_brief = self.store.planning_input("alpha-project", confirmed["brief_ref"], confirmed["brief_sha256"])
        self.assertEqual(planner_brief["视频时长"], "60秒")
        plan = compile_plan_artifact(
            planner_brief,
            [SourceShot("发布会", "anonymous-template.xlsx", "template", 1, visual_desc="产品演示")],
            source_view="alpha-confirmed",
        )
        self.assertEqual(plan.duration_budget["shot_count"], 17)
        self.assertEqual(plan.duration_budget["planned_seconds"], 60)

    def test_conflict_and_critical_failure_cannot_be_confirmed_but_noncritical_failure_stays_visible(self):
        good = self.inputs / "one.txt"
        good.write_text("项目名称: 匿名甲\n脚本类型: 发布会", encoding="utf-8")
        conflict = self.inputs / "two.md"
        conflict.write_text("项目名称: 匿名乙", encoding="utf-8")
        self.store.freeze_documents([good, conflict])
        imported = self.store.import_documents("alpha-project", [good, conflict])
        self.assertEqual(imported["conflicts"][0]["status"], "unresolved")
        with self.assertRaisesRegex(AlphaBriefError, "未解决冲突"):
            self.store.confirm(imported, confirmed_by="alpha-manager")
        conflict_id = imported["conflicts"][0]["conflict_id"]
        selected = imported["conflicts"][0]["fact_ids"][0]
        resolved = self.store.resolve_conflicts(imported, {conflict_id: selected}, resolved_by="alpha-manager")
        self.assertEqual(self.store.confirm(resolved, confirmed_by="alpha-manager")["confirmation_status"], "confirmed")

        other = AlphaBriefStore(Path(tempfile.mkdtemp(prefix="mplus-alpha-")))
        broken = other.inputs_root / "broken.txt"
        other.inputs_root.mkdir()
        broken.write_text("", encoding="utf-8")
        other.freeze_documents([{"path": broken, "critical": False}])
        visible = other.import_documents("alpha-project", [broken])
        self.assertEqual(visible["extractions"][0]["status"], "failed")
        self.assertEqual(visible["extractions"][0]["error"]["code"], "EXTRACTION_FAILED")
        self.assertEqual(other.confirm(visible, confirmed_by="alpha-manager")["confirmation_status"], "confirmed")

    def test_rejects_nonisolated_store_root(self):
        with self.assertRaisesRegex(AlphaBriefError, "隔离目录"):
            AlphaBriefStore(Path.cwd())

    def test_encrypted_scanned_and_oversized_documents_are_explicit_failures(self):
        encrypted = self.inputs / "encrypted.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        writer.encrypt("anonymous-password")
        with encrypted.open("wb") as output:
            writer.write(output)
        scanned = self.inputs / "scanned.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=612, height=792)
        with scanned.open("wb") as output:
            writer.write(output)
        self.store.freeze_documents([encrypted, scanned])
        failed = self.store.import_documents("alpha-project", [encrypted, scanned])
        self.assertEqual([item["status"] for item in failed["extractions"]], ["failed", "failed"])
        self.assertIn("加密", failed["extractions"][0]["error"]["message"])
        self.assertIn("OCR 已延期", failed["extractions"][1]["error"]["message"])

        oversized = self.inputs / "oversized.txt"
        oversized.write_text("项目名称: 匿名", encoding="utf-8")
        other = AlphaBriefStore(Path(tempfile.mkdtemp(prefix="mplus-alpha-")), max_file_bytes=1)
        other.inputs_root.mkdir()
        oversized = other.inputs_root / "oversized.txt"
        oversized.write_text("项目名称: 匿名", encoding="utf-8")
        other.freeze_documents([oversized])
        limited = other.import_documents("alpha-project", [oversized])
        self.assertEqual(limited["extractions"][0]["error"]["code"], "FILE_TOO_LARGE")

    def test_planning_only_reads_persisted_verified_artifact_and_input_must_be_frozen_inside_root(self):
        outside = self.root / "outside.txt"
        outside.write_text("项目名称: 匿名", encoding="utf-8")
        with self.assertRaisesRegex(AlphaBriefError, "inputs"):
            self.store.freeze_documents([outside])

        text = self.inputs / "brief.txt"
        text.write_text("项目名称: 匿名\n脚本类型: 发布会\n目标时长: 60秒\n风格调性: 简洁\n必须出现: 产品", encoding="utf-8")
        self.store.freeze_documents([text])
        text.write_text("项目名称: 被篡改", encoding="utf-8")
        with self.assertRaisesRegex(AlphaBriefError, "冻结后已变化"):
            self.store.import_documents("alpha-project", [text])

        fresh = AlphaBriefStore(Path(tempfile.mkdtemp(prefix="mplus-alpha-")))
        fresh.inputs_root.mkdir()
        brief = fresh.inputs_root / "brief.txt"
        brief.write_text("项目名称: 匿名\n脚本类型: 发布会\n目标时长: 60秒\n风格调性: 简洁\n必须出现: 产品", encoding="utf-8")
        fresh.freeze_documents([brief])
        confirmed = fresh.confirm(fresh.import_documents("alpha-project", [brief]), confirmed_by="alpha-manager")
        fake = dict(confirmed)
        fake["brief_sha256"] = "0" * 64
        with self.assertRaisesRegex(TypeError, "planning_input"):
            fresh.planning_input("alpha-project", fake)  # type: ignore[arg-type]
        with self.assertRaisesRegex(AlphaBriefError, "SHA-256"):
            fresh.planning_input("alpha-project", confirmed["brief_ref"], "0" * 64)
        artifact = fresh.root / "confirmed" / f"{confirmed['brief_sha256']}.json"
        payload = json.loads(artifact.read_text(encoding="utf-8"))
        payload["confirmed_by"] = "tampered"
        artifact.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(AlphaBriefError, "合同无效"):
            fresh.planning_input("alpha-project", confirmed["brief_ref"], confirmed["brief_sha256"])

    def test_import_submission_must_exactly_match_frozen_manifest_and_rejects_ancestor_links(self):
        one = self.inputs / "one.txt"
        critical_failure = self.inputs / "critical-failure.txt"
        one.write_text("项目名称: 匿名", encoding="utf-8")
        critical_failure.write_text("", encoding="utf-8")
        self.store.freeze_documents([one, critical_failure])
        with self.assertRaisesRegex(AlphaBriefError, "完整一致"):
            self.store.import_documents("alpha-project", [one])
        with self.assertRaisesRegex(AlphaBriefError, "不能重复"):
            self.store.import_documents("alpha-project", [one, one, critical_failure])
        extra = self.inputs / "extra.txt"
        extra.write_text("目标时长: 60秒", encoding="utf-8")
        with self.assertRaisesRegex(AlphaBriefError, "完整一致"):
            self.store.import_documents("alpha-project", [one, critical_failure, extra])

        linked_root = AlphaBriefStore(Path(tempfile.mkdtemp(prefix="mplus-alpha-")))
        linked_root.inputs_root.mkdir()
        external = Path(tempfile.mkdtemp(prefix="mplus-alpha-external-"))
        (external / "brief.txt").write_text("项目名称: 匿名", encoding="utf-8")
        (linked_root.inputs_root / "dir-link").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(AlphaBriefError, "路径不允许符号链接"):
            linked_root.freeze_documents([linked_root.inputs_root / "dir-link" / "brief.txt"])
        (linked_root.inputs_root / "file-link.txt").symlink_to(external / "brief.txt")
        with self.assertRaisesRegex(AlphaBriefError, "路径不允许符号链接"):
            linked_root.freeze_documents([linked_root.inputs_root / "file-link.txt"])


def _office_zip(path: Path, member: str, content: str, extra: dict[str, bytes] | None = None) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
        for name, value in (extra or {}).items():
            archive.writestr(name, value)


def _text_pdf(path: Path, text: str) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as output:
        writer.write(output)


if __name__ == "__main__":
    unittest.main()
