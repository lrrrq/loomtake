from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfReader, PdfWriter

from mvp_v2 import formal_brief_input_adapter as subject
from tests.test_mvp_v2_formal_brief_input_adapter import (
    _upload,
    _image_bytes, _office_zip, _text_pdf, _xlsx_bytes,
)


class FormalBriefOcrTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-ocr-")
        self.root = Path(self.temporary.name)
        self.calls = []

    def tearDown(self):
        self.temporary.cleanup()

    def policy(self, responses=None, **kwargs):
        responses = iter(responses or [{"status": "succeeded", "text": "项目名称: 匿名扫描活动"}])
        def invoke(path, prompt):
            self.assertTrue(path.is_file())
            self.assertEqual(Image.open(path).format, "PNG")
            self.calls.append((path.read_bytes(), prompt))
            answer = next(responses)
            if isinstance(answer, Exception):
                raise answer
            return json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer
        return subject.FormalBriefOcrPolicy(invoke=invoke, provider="anonymous", model="fixture", **kwargs)

    def extract(self, filename, content, policy=None, critical=True):
        target = self.root / filename
        target.write_bytes(content)
        digest = sha256(content).hexdigest()
        kwargs = {"ocr_policy": policy} if policy is not None else {}
        return subject._extract_all("anonymous-project", [{
            "document_id": "source_doc_" + digest[:20], "filename": filename,
            "suffix": target.suffix, "mime_type": "fixture", "size_bytes": len(content),
            "sha256": digest, "critical": critical, "_stored_path": target,
        }], **kwargs)

    def test_mixed_pdf_requires_each_unread_page_even_when_first_page_has_text(self):
        result = self.extract("mixed.pdf", _pdf_pages(True, False, False))
        row = result["extractions"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error"]["code"], "OCR_REVIEW_REQUIRED")
        self.assertEqual([x["location"]["page"] for x in row["ocr"]], [2, 3])
        self.assertEqual(row["locations"][0]["location"], {"page": 1})
        self.assertEqual(result["facts"], [])

    def test_scanned_mixed_pages_keep_text_locations_and_distinct_ocr_evidence(self):
        policy = self.policy([{"status": "succeeded", "text": "目标时长: 60秒"}, {"status": "no_text", "text": ""}])
        with patch.object(subject, "_rasterize_pdf_page", return_value=_image_bytes("PNG")):
            result = self.extract("mixed.pdf", _pdf_pages(True, False, False), policy)
        row = result["extractions"][0]
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual([x["location"]["page"] for x in row["locations"]], [1, 2])
        self.assertEqual([x["status"] for x in row["ocr"]], ["succeeded", "no_text"])
        self.assertEqual(row["ocr"][0]["text_sha256"], sha256("目标时长: 60秒".encode()).hexdigest())
        self.assertEqual(len(self.calls), 2)

    def test_standalone_formats_recognize_real_text_and_keep_source_sha(self):
        for suffix, kind in ((".jpg", "JPEG"), (".jpeg", "JPEG"), (".png", "PNG"), (".webp", "WEBP")):
            with self.subTest(suffix=suffix):
                content = _image_bytes(kind)
                result = self.extract("text" + suffix, content, self.policy())
                self.assertEqual(result["facts"][0]["value"], "匿名扫描活动")
                self.assertEqual(result["extractions"][0]["ocr"][0]["source_sha256"], sha256(content).hexdigest())
                self.assertEqual(result["images"][0]["content_sha256"], sha256(content).hexdigest())

    def test_no_text_photo_succeeds_but_provider_failure_and_invalid_output_do_not(self):
        for response, status, code in (({"status": "no_text", "text": ""}, "succeeded", None), (RuntimeError("private command failure"), "failed", "OCR_PROVIDER_FAILED"), ({"status": "succeeded"}, "failed", "OCR_RESPONSE_INVALID"), ({"status": "no_text", "text": "contradiction"}, "failed", "OCR_RESPONSE_INVALID")):
            with self.subTest(response=str(response)):
                result = self.extract("photo.png", _image_bytes("PNG"), self.policy([response]))
                row = result["extractions"][0]
                self.assertEqual(row["status"], status)
                self.assertEqual((row["error"] or {}).get("code"), code)
                self.assertEqual(result["facts"], [])
                self.assertNotIn("private command failure", json.dumps(row, default=str))

    def test_office_text_and_image_only_documents_extract_embedded_text(self):
        for filename, content in (
            ("brief.docx", _office_zip("word/document.xml", "<w:document xmlns:w=\"w\"><w:body><w:p/></w:body></w:document>", {"word/media/image1.png": _image_bytes("PNG")})),
            ("brief.pptx", _office_zip("ppt/slides/slide1.xml", "<p:sld xmlns:p=\"p\"/>", {"ppt/media/image1.png": _image_bytes("PNG")})),
            ("brief.xlsx", _xlsx_with_image()),
        ):
            with self.subTest(filename=filename):
                result = self.extract(filename, content, self.policy())
                self.assertEqual(result["extractions"][0]["status"], "succeeded")
                self.assertEqual(result["extractions"][0]["ocr"][0]["location"]["embedded_image"], 1)
                self.assertTrue(any(row["value"] == "匿名扫描活动" for row in result["facts"]))
                if filename.endswith("xlsx"):
                    self.assertEqual(result["extractions"][0]["ocr"][0]["location"]["row"], 5)
                    self.assertEqual(result["extractions"][0]["ocr"][0]["location"]["column"], 3)

    def test_mmx_transport_content_is_unwrapped_without_salvaging_invalid_json(self):
        valid = json.dumps({"status": "succeeded", "text": "品牌: 匿名品牌"}, ensure_ascii=False)
        result = self.extract("photo.png", _image_bytes("PNG"), self.policy([{"status": "ok", "content": "```json\n" + valid + "\n```"}]))
        self.assertEqual(result["facts"][0]["value"], "匿名品牌")
        raw = '{"status":"succeeded","text":"品牌: 匿名品牌'
        result = self.extract("broken.png", _image_bytes("PNG"), self.policy([raw]))
        self.assertEqual(result["extractions"][0]["status"], "failed")
        self.assertEqual(result["extractions"][0]["ocr"][0]["_evidence_payload"]["raw_response"], raw)

    def test_word_relationship_keeps_paragraph_and_actual_picture_reference(self):
        content = _office_zip("word/document.xml", '<w:document xmlns:w="w" xmlns:a="a" xmlns:r="r"><w:body><w:p/><w:p><a:blip r:embed="rId7"/></w:p></w:body></w:document>', {
            "word/_rels/document.xml.rels": '<Relationships><Relationship Id="rId7" Target="media/image1.png"/></Relationships>',
            "word/media/image1.png": _image_bytes("PNG"),
        })
        result = self.extract("position.docx", content, self.policy())
        self.assertEqual(result["extractions"][0]["ocr"][0]["location"], {"embedded_image": 1, "paragraph": 2, "part": "word/document.xml"})

    def test_plain_text_and_markdown_do_not_call_ocr(self):
        for suffix in ("txt", "md"):
            result = self.extract("brief." + suffix, "项目名称: 匿名文字活动".encode(), self.policy())
            self.assertEqual(result["facts"][0]["value"], "匿名文字活动")
        self.assertEqual(self.calls, [])

    def test_encrypted_or_damaged_pdf_rejected_before_provider(self):
        writer = PdfWriter(); writer.add_blank_page(width=10, height=10); writer.encrypt("anonymous")
        output = BytesIO(); writer.write(output)
        for content in (output.getvalue(), b"%PDF-broken"):
            with self.assertRaisesRegex(subject.FormalBriefInputError, "UPLOAD_REJECTED"):
                self.extract("invalid.pdf", content, self.policy())
        self.assertEqual(self.calls, [])

    def test_missing_rasterizer_is_explicit_extraction_failure(self):
        result = self.extract("scan.pdf", _pdf_pages(False), self.policy(pdftoppm_command=str(self.root / "missing")))
        self.assertEqual(result["extractions"][0]["error"]["code"], "OCR_RASTERIZER_UNAVAILABLE")
        self.assertEqual(self.calls, [])

    @unittest.skipUnless(shutil.which("pdftoppm"), "pdftoppm not installed")
    def test_configured_rasterizer_produces_actual_page_pixels(self):
        result = self.extract("scan.pdf", _pdf_pages(False), self.policy(pdftoppm_command=shutil.which("pdftoppm")))
        self.assertEqual(result["extractions"][0]["status"], "succeeded")
        self.assertGreater(len(self.calls[0][0]), 500)

    def test_runtime_uses_existing_vision_route_and_explicit_rasterizer_without_startup_call(self):
        from mvp_v2.formal_runtime import build_formal_runtime
        configured = {"data_root": str(self.root / "runtime-data"), "runs_root": str(self.root / "runtime-runs"), "source_root": str(self.root / "runtime-source")}
        with patch.dict(os.environ, {"MPLUS_PDFTOPPM_COMMAND": "/configured/pdftoppm", "MINIMAX_API_KEY": "anonymous-key"}), patch("mvp_v2.formal_runtime.provider_credentials_available", side_effect=lambda name: name == "minimax-cli"), patch("mvp_v2.formal_runtime._run_minimax_vision", return_value='{"status":"no_text","text":""}') as invoke:
            bundle = build_formal_runtime(configured)
            invoke.assert_not_called()
            policy = bundle.brief_adapter.ocr_policy
            self.assertIsNotNone(policy)
            self.assertEqual(policy.pdftoppm_command, "/configured/pdftoppm")
            path = self.root / "fixture.png"
            path.write_bytes(_image_bytes("PNG"))
            self.assertEqual(json.loads(policy.invoke(path, "OCR prompt"))["status"], "no_text")
            invoke.assert_called_once_with("mmx", path, "anonymous-key", 90, prompt="OCR prompt")


class FormalBriefOcrLifecycleTests(unittest.TestCase):
    def setUp(self):
        from tests.test_mvp_v2_formal_brief_input_adapter import FormalBriefInputAdapterTests
        FormalBriefInputAdapterTests.setUp(self)

    def tearDown(self):
        self.temporary.cleanup()

    def adapter_with_response(self, response):
        def invoke(_path, _prompt):
            if isinstance(response, Exception):
                raise response
            return json.dumps(response, ensure_ascii=False)
        return subject.FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data", ocr_policy=subject.FormalBriefOcrPolicy(invoke=invoke, provider="anonymous", model="fixture"))

    def import_image(self, adapter, *, project=None, critical=True):
        return adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=(project or self.first)["project_id"], expected_project_version=0, uploads=[
            _upload("brief.txt", _complete_brief()),
            subject.UploadedDocument("photo.png", BytesIO(_image_bytes("PNG")), critical=critical),
        ])

    def raw(self, result, project=None):
        return self.adapter.service.read_structured(self.token, workspace_id=self.workspace_id, project_id=(project or self.first)["project_id"], brief_version_id=result["brief_key"])

    def counting_adapter(self, calls, *, response=None, model="fixture"):
        def invoke(_path, _prompt):
            calls.append(model)
            if isinstance(response, Exception):
                raise response
            return json.dumps(response or {"status": "no_text", "text": ""}, ensure_ascii=False)
        return subject.FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data",
            ocr_policy=subject.FormalBriefOcrPolicy(invoke=invoke, provider="anonymous", model=model))

    def append_text(self, adapter, text, version, *, project=None):
        return adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=(project or self.first)["project_id"], expected_project_version=version,
            uploads=[_upload("answer.txt", text)])

    def test_append_text_reuses_pdf_office_image_evidence_after_restart(self):
        calls = []
        adapter = self.counting_adapter(calls)
        uploads = [_upload("brief.txt", _complete_brief()), _upload("scan.pdf", _pdf_pages(False)),
            _upload("photo.png", _image_bytes("PNG")),
            _upload("scan.docx", _office_zip("word/document.xml", '<w:document xmlns:w="w"><w:body><w:p/></w:body></w:document>', {"word/media/image1.png": _image_bytes("PNG")}))]
        with patch.object(subject, "_rasterize_pdf_page", return_value=_image_bytes("PNG")):
            first = adapter.import_uploads(self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], expected_project_version=0, uploads=uploads)
        self.assertEqual(len(calls), 3)
        old = self.raw(first)
        restarted = self.counting_adapter(calls)
        with patch.object(subject, "_extract_bytes", wraps=subject._extract_bytes) as extract:
            second = self.append_text(restarted, "交付要求：横版高清", 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0]["filename"] for call in extract.call_args_list], ["answer.txt"])
        current = self.raw(second)
        old_rows = {r["document_id"]: r for r in old["extractions"]}
        self.assertTrue(all(r == old_rows[r["document_id"]] for r in current["extractions"] if r["document_id"] in old_rows))
        self.assertEqual(current["images"], old["images"])
        sources = adapter.service.latest_import_base(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"]).payload_data["source_documents"]
        self.assertEqual(len(sources), 5)
        self.assertTrue(all(s.get("extraction_reuse_key") for s in sources))

    def test_reuse_invalidates_on_parser_and_ocr_configuration_change(self):
        calls = []
        adapter = self.counting_adapter(calls)
        self.import_image(adapter)
        with patch.object(subject, "EXTRACTION_REUSE_VERSION", "anonymous-next-parser"):
            self.append_text(adapter, "交付要求：高清", 1)
        self.assertEqual(len(calls), 2)
        self.append_text(self.counting_adapter(calls, model="next-model"), "备注：新增资料", 2)
        self.assertEqual(calls, ["fixture", "fixture", "next-model"])

    def test_failed_ocr_append_does_not_retry_but_explicit_source_reimport_does(self):
        calls = []
        failed = self.counting_adapter(calls, response=RuntimeError("offline"))
        self.import_image(failed)
        self.assertEqual(len(calls), 1)
        # Even a newly configured model is not authorization to retry an old
        # failed document when only an unrelated answer was uploaded.
        success = self.counting_adapter(calls, model="new-config")
        second = self.append_text(success, "交付要求：横版", 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(second["has_failed_extractions"])
        third = success.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=2,
            uploads=[_upload("photo.png", _image_bytes("PNG"))])
        self.assertEqual(len(calls), 2)
        self.assertFalse(third["has_failed_extractions"])

    def test_frozen_document_retry_only_calls_selected_failure_and_keeps_other_receipts(self):
        calls = []
        def invoke(path, _prompt):
            with Image.open(path) as image:
                color = image.getpixel((0, 0))
            calls.append(color)
            if color != (0, 128, 0):
                raise RuntimeError("temporary OCR failure")
            return json.dumps({"status": "no_text", "text": ""})
        first_adapter = subject.FormalBriefInputAdapter(self.adapter.service,
            data_root=self.root / "data", ocr_policy=subject.FormalBriefOcrPolicy(
                invoke=invoke, provider="anonymous", model="first"))
        photos = []
        for color in ("blue", "green", "purple"):
            data = BytesIO(); Image.new("RGB", (24, 12), color).save(data, format="PNG")
            photos.append(_upload(color + ".png", data.getvalue()))
        first = first_adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0,
            uploads=[_upload("brief.txt", _complete_brief()), *photos])
        original = self.raw(first)
        ids = {Path(row["relative_path"]).name: row["source_document_id"]
            for row in original["source_manifest"]["documents"]}
        self.assertEqual(len(calls), 3)
        retried_calls = []
        restarted = self.counting_adapter(retried_calls, model="new-configuration")
        with patch.object(subject, "EXTRACTION_REUSE_VERSION", "next-parser"):
            second = restarted.retry_failed_documents(self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], brief_version_id=first["brief_key"],
                document_ids=[ids["blue.png"]], expected_project_version=1)
        current = self.raw(second)
        self.assertEqual(retried_calls, ["new-configuration"])
        self.assertEqual(second["import_status"], "retried")
        self.assertEqual(second["retried_document_ids"], [ids["blue.png"]])
        self.assertEqual(current["parent_brief_version_id"], first["brief_key"])
        self.assertEqual(current["source_manifest"]["documents"], original["source_manifest"]["documents"])
        old_rows = {row["document_id"]: row for row in original["extractions"]}
        new_rows = {row["document_id"]: row for row in current["extractions"]}
        self.assertEqual(new_rows[ids["blue.png"]]["status"], "succeeded")
        self.assertEqual(new_rows[ids["green.png"]], old_rows[ids["green.png"]])
        self.assertEqual(new_rows[ids["purple.png"]], old_rows[ids["purple.png"]])
        self.assertTrue(second["has_failed_extractions"])
        self.assertEqual(self.raw(first), original)
        from mvp_v2.formal_brief_domain import FormalBriefDomainError
        with self.assertRaisesRegex(FormalBriefDomainError, "不可确认"):
            restarted.confirm(self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], brief_version_id=second["brief_key"],
                confirmed_at="2026-10-08T00:00:00Z", expected_project_version=2)

    def test_frozen_retry_rejects_stale_foreign_successful_and_tampered_inputs_without_calls(self):
        calls = []
        failed = self.counting_adapter(calls, response=RuntimeError("offline"))
        first = self.import_image(failed)
        original = self.raw(first)
        documents = original["source_manifest"]["documents"]
        photo = next(row for row in documents if row["relative_path"].endswith("photo.png"))
        text = next(row for row in documents if row["relative_path"].endswith("brief.txt"))
        success = self.counting_adapter(calls)
        arguments = {"workspace_id": self.workspace_id, "project_id": self.first["project_id"],
            "brief_version_id": first["brief_key"], "expected_project_version": 1}
        for selected in ([], ["missing-source"], [text["source_document_id"]],
                         [photo["source_document_id"], photo["source_document_id"]]):
            with self.assertRaisesRegex(subject.FormalBriefInputError, "INVALID_INPUT"):
                success.retry_failed_documents(self.token, **arguments, document_ids=selected)
        with self.assertRaisesRegex(Exception, "CROSS_PROJECT_DENIED"):
            success.retry_failed_documents(self.token, workspace_id=self.workspace_id,
                project_id=self.second["project_id"], brief_version_id=first["brief_key"],
                document_ids=[photo["source_document_id"]], expected_project_version=0)
        self.assertEqual(len(calls), 1)
        appended = self.append_text(success, "交付要求：横版", 1)
        with self.assertRaisesRegex(subject.FormalBriefInputError, "BRIEF_VERSION_CONFLICT"):
            success.retry_failed_documents(self.token, **{**arguments, "expected_project_version": 2},
                document_ids=[photo["source_document_id"]])
        digest = photo["content_sha256"]
        success.store.file_path(digest, ".png").write_bytes(b"tampered")
        with self.assertRaisesRegex(subject.FormalBriefInputError, "INPUT_TAMPERED"):
            success.retry_failed_documents(self.token, **{**arguments,
                "brief_version_id": appended["brief_key"], "expected_project_version": 2},
                document_ids=[photo["source_document_id"]])
        self.assertEqual(len(calls), 1)

    def test_frozen_retry_failure_rolls_back_new_version_and_keeps_original_failure(self):
        calls = []
        first = self.import_image(self.counting_adapter(calls, response=RuntimeError("offline")))
        original = self.raw(first)
        document_id = next(row["document_id"] for row in original["extractions"] if row["status"] == "failed")
        restarted = self.counting_adapter(calls)
        def fail_transaction(_connection, _version, _actor):
            raise RuntimeError("retry transaction failed")
        with self.assertRaisesRegex(RuntimeError, "retry transaction failed"):
            restarted.retry_failed_documents(self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], brief_version_id=first["brief_key"],
                document_ids=[document_id], expected_project_version=1,
                transaction_hook=fail_transaction)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.raw(first), original)
        self.assertEqual(restarted.service.latest_import_base(self.token,
            workspace_id=self.workspace_id, project_id=self.first["project_id"]).brief_version_id, first["brief_key"])

    def test_replacement_changes_only_selected_active_source_and_append_does_not_revive_history(self):
        calls = []
        def invoke(path, _prompt):
            with Image.open(path) as image:
                color = image.getpixel((0, 0))
            calls.append(color)
            if color != (0, 128, 0):
                raise RuntimeError("temporary OCR failure")
            return json.dumps({"status": "no_text", "text": ""})
        original_adapter = subject.FormalBriefInputAdapter(self.adapter.service,
            data_root=self.root / "data", ocr_policy=subject.FormalBriefOcrPolicy(
                invoke=invoke, provider="anonymous", model="original"))
        photos = []
        for color in ("blue", "green", "purple"):
            output = BytesIO(); Image.new("RGB", (24, 12), color).save(output, format="PNG")
            photos.append(_upload(color + ".png", output.getvalue()))
        first = original_adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0,
            uploads=[_upload("brief.txt", _complete_brief()), *photos])
        original = self.raw(first)
        documents = {Path(row["relative_path"]).name: row
            for row in original["source_manifest"]["documents"]}
        blue = documents["blue.png"]["source_document_id"]
        purple = documents["purple.png"]["source_document_id"]
        old_rows = {row["document_id"]: row for row in original["extractions"]}
        replacement_calls = []
        replacement = self.counting_adapter(replacement_calls, model="new-configuration")
        with patch.object(subject, "EXTRACTION_REUSE_VERSION", "next-parser"):
            second = replacement.replace_failed_document(self.token,
                workspace_id=self.workspace_id, project_id=self.first["project_id"],
                brief_version_id=first["brief_key"], document_id=blue,
                upload=subject.UploadedDocument("replacement.txt", BytesIO("交付要求：横版高清".encode()), False),
                expected_project_version=1)
        current = self.raw(second)
        active = {row["source_document_id"]: row for row in current["source_manifest"]["documents"]}
        self.assertEqual(second["import_status"], "replaced")
        self.assertEqual(second["replaced_document_id"], blue)
        self.assertNotIn(blue, active)
        self.assertTrue(active[second["replacement_document_id"]]["critical"])
        self.assertEqual(current["parent_brief_version_id"], first["brief_key"])
        for document in documents.values():
            document_id = document["source_document_id"]
            if document_id != blue:
                self.assertEqual(active[document_id], document)
                self.assertEqual(next(row for row in current["extractions"]
                    if row["document_id"] == document_id), old_rows[document_id])
        self.assertTrue(second["has_failed_extractions"])
        self.assertEqual(replacement_calls, [])
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(link["source_document_id"] != blue
            for image in current["images"] for link in image["source_links"]))
        third = self.append_text(original_adapter, "参考方案：简洁片尾", 2)
        third_raw = self.raw(third)
        self.assertNotIn(blue, {row["source_document_id"]
            for row in third_raw["source_manifest"]["documents"]})
        self.assertEqual(len(calls), 3)
        fourth = original_adapter.replace_failed_document(self.token,
            workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=third["brief_key"], document_id=purple,
            upload=_upload("other-replacement.md", "特殊需求：只使用已确认资料"),
            expected_project_version=3)
        self.assertFalse(fourth["has_failed_extractions"])
        final_ids = {row["source_document_id"]
            for row in self.raw(fourth)["source_manifest"]["documents"]}
        self.assertTrue({blue, purple}.isdisjoint(final_ids))
        confirmed = original_adapter.confirm(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=fourth["brief_key"],
            confirmed_at="2026-10-08T00:00:00Z", expected_project_version=4)
        self.assertEqual(confirmed["confirmation_status"], "confirmed")
        self.assertEqual(self.raw(first), original)
        self.assertTrue(original_adapter.store.file_path(documents["blue.png"]["content_sha256"], ".png").is_file())
        with self.store.connection() as connection:
            historical = {row[0] for row in connection.execute(
                "SELECT source_document_id FROM project_files WHERE project_id=?", (self.first["project_id"],))}
        self.assertTrue({blue, purple}.issubset(historical))

    def test_same_sha_replacement_uses_frozen_retry_and_preserves_source_name_and_manifest(self):
        calls = []
        first = self.import_image(self.counting_adapter(calls, response=RuntimeError("offline")))
        original = self.raw(first)
        document_id = next(row["document_id"] for row in original["extractions"] if row["status"] == "failed")
        restarted = self.counting_adapter(calls, model="recovery")
        second = restarted.replace_failed_document(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=first["brief_key"],
            document_id=document_id, upload=_upload("renamed.png", _image_bytes("PNG")),
            expected_project_version=1)
        self.assertEqual(second["import_status"], "retried")
        self.assertEqual(second["retried_document_ids"], [document_id])
        self.assertFalse(second["has_failed_extractions"])
        self.assertEqual(self.raw(second)["source_manifest"]["documents"], original["source_manifest"]["documents"])
        self.assertEqual(self.raw(second)["parent_brief_version_id"], first["brief_key"])
        self.assertEqual(calls, ["fixture", "recovery"])
        self.assertEqual(self.raw(first), original)

    def test_replacement_rejects_invalid_foreign_successful_and_stale_targets_before_reading_upload(self):
        calls = []
        adapter = self.counting_adapter(calls, response=RuntimeError("offline"))
        first = self.import_image(adapter)
        original = self.raw(first)
        failed = next(row["document_id"] for row in original["extractions"] if row["status"] == "failed")
        succeeded = next(row["document_id"] for row in original["extractions"] if row["status"] == "succeeded")
        class UnreadStream:
            def read(self, _size):
                raise AssertionError("invalid replacement must not read the upload")
        arguments = dict(workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=first["brief_key"], expected_project_version=1,
            upload=subject.UploadedDocument("replacement.txt", UnreadStream()))
        for document_id in ("", "missing-source", succeeded, None, []):
            with self.subTest(document_id=document_id), self.assertRaisesRegex(subject.FormalBriefInputError, "INVALID_INPUT"):
                adapter.replace_failed_document(self.token, **arguments, document_id=document_id)
        with self.assertRaisesRegex(Exception, "CROSS_PROJECT_DENIED"):
            adapter.replace_failed_document(self.token, **{**arguments,
                "project_id": self.second["project_id"], "expected_project_version": 0}, document_id=failed)
        with self.assertRaisesRegex(Exception, "PROJECT_VERSION_CONFLICT"):
            adapter.replace_failed_document(self.token, **{**arguments, "expected_project_version": 0}, document_id=failed)
        with self.assertRaisesRegex(subject.FormalBriefInputError, "INVALID_INPUT"):
            adapter.replace_failed_document(self.token, **{**arguments, "upload": "local-path"}, document_id=failed)
        self.append_text(adapter, "交付要求：横版", 1)
        with self.assertRaisesRegex(subject.FormalBriefInputError, "BRIEF_VERSION_CONFLICT"):
            adapter.replace_failed_document(self.token, **{**arguments, "expected_project_version": 2}, document_id=failed)
        self.assertEqual(calls, ["fixture"])
        self.assertEqual(self.raw(first), original)

    def test_failed_replacement_transaction_preserves_original_version_and_can_retry(self):
        calls = []
        first = self.import_image(self.counting_adapter(calls, response=RuntimeError("offline")))
        original = self.raw(first)
        document_id = next(row["document_id"] for row in original["extractions"] if row["status"] == "failed")
        replacement = self.counting_adapter(calls)
        def fail_transaction(_connection, _version, _actor):
            raise RuntimeError("replacement transaction failed")
        with self.store.connection() as connection:
            before = list(connection.iterdump())
        arguments = dict(workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=first["brief_key"], document_id=document_id, expected_project_version=1)
        with self.assertRaisesRegex(RuntimeError, "replacement transaction failed"):
            replacement.replace_failed_document(self.token, **arguments,
                upload=_upload("replacement.txt", "交付要求：横版高清"), transaction_hook=fail_transaction)
        with self.store.connection() as connection:
            self.assertEqual(list(connection.iterdump()), before)
        self.assertEqual(self.raw(first), original)
        self.assertEqual(replacement.service.latest_import_base(self.token,
            workspace_id=self.workspace_id, project_id=self.first["project_id"]).brief_version_id, first["brief_key"])
        recovered = replacement.replace_failed_document(self.token, **arguments,
            upload=_upload("replacement.txt", "交付要求：横版高清"))
        self.assertFalse(recovered["has_failed_extractions"])
        self.assertEqual(self.raw(recovered)["parent_brief_version_id"], first["brief_key"])
        self.assertEqual(calls, ["fixture"])

    def test_same_content_cross_project_is_not_reused_and_same_name_new_sha_is_new(self):
        calls = []
        adapter = self.counting_adapter(calls)
        first = self.import_image(adapter)
        other = self.import_image(adapter, project=self.second)
        self.assertEqual(len(calls), 2)
        buffer = BytesIO(); Image.new("RGB", (23, 17), "purple").save(buffer, format="PNG")
        result = adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=1,
            uploads=[_upload("photo.png", buffer.getvalue())])
        self.assertEqual(len(calls), 3)
        current = self.raw(result)
        self.assertEqual(len(current["images"]), 2)
        other_payload = adapter.service.latest_import_base(self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"]).payload_data
        self.assertEqual({s["project_id"] for s in other_payload["source_documents"]}, {self.second["project_id"]})
        self.assertNotEqual(self.raw(first)["project_id"], self.raw(other, self.second)["project_id"])
        from mvp_v2.identity_service import LocalIdentityAdapter
        from mvp_v2.workspace_store import WorkspaceServiceError
        identity = LocalIdentityAdapter(self.store)
        actor = identity.create_actor(self.token, workspace_id=self.workspace_id,
            project_id=self.second['project_id'], display_name='仅乙项目查看者',
            auth_source='local', role='viewer',
            expected_workspace_version=self.projects.get_workspace(self.token, self.workspace_id)['workspace_version'])
        viewer = identity.issue_auth_session(actor_id=actor['actor_id'], token='ocr-isolated-viewer')['auth_token']
        with self.store.connection() as connection:
            before = list(connection.iterdump())
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            adapter.service.latest_import_base(viewer, workspace_id=self.workspace_id,
                project_id=self.first['project_id'])
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            adapter.read_structured(viewer, workspace_id=self.workspace_id,
                project_id=self.first['project_id'], brief_version_id=first['brief_key'])
        self.assertEqual(len(calls), 3)
        with self.store.connection() as connection:
            self.assertEqual(list(connection.iterdump()), before)

    def test_legacy_missing_reuse_metadata_falls_back_to_extract(self):
        from dataclasses import replace
        from copy import deepcopy
        calls = []
        adapter = self.counting_adapter(calls)
        self.import_image(adapter)
        original = adapter.service.latest_import_base
        def legacy(*args, **kwargs):
            base = original(*args, **kwargs)
            payload = deepcopy(base.payload_data)
            for source in payload["source_documents"]:
                source.pop("extraction_reuse_key", None)
            return replace(base, payload_data=payload)
        with patch.object(adapter.service, "latest_import_base", side_effect=legacy):
            self.append_text(adapter, "交付要求：横版", 1)
        self.assertEqual(len(calls), 2)

    def test_reused_image_tamper_blocks_before_any_ocr(self):
        calls = []
        adapter = self.counting_adapter(calls)
        first = self.import_image(adapter)
        image = self.raw(first)["images"][0]
        adapter.store.image_path(image["content_sha256"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(subject.FormalBriefInputError, "INPUT_TAMPERED"):
            self.append_text(adapter, "交付要求：横版", 1)
        self.assertEqual(len(calls), 1)

    def test_successful_ocr_freezes_response_and_pixels_and_confirms_after_restart(self):
        response = {"status": "succeeded", "text": "特殊需求: 字幕横版"}
        adapter = self.adapter_with_response(response)
        result = self.import_image(adapter)
        raw = self.raw(result)
        record = next(row for row in raw["extractions"] if row["ocr"])["ocr"][0]
        evidence_path = adapter.store.root / "ocr" / record["evidence"]["sha256"][:2] / record["evidence"]["sha256"]
        evidence = json.loads(evidence_path.read_bytes())
        self.assertEqual(json.loads(evidence["raw_response"]), response)
        self.assertEqual(evidence["text"], response["text"])
        image_path = adapter.store.root / "ocr-images" / record["image_sha256"][:2] / record["image_sha256"]
        self.assertEqual(sha256(image_path.read_bytes()).hexdigest(), record["image_sha256"])
        restarted = subject.FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data")
        confirmed = restarted.confirm(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=result["brief_key"], confirmed_at="2026-09-09T00:00:00Z", expected_project_version=1)
        self.assertEqual(confirmed["confirmation_status"], "confirmed")
        self.assertEqual(confirmed["brief"]["documents"][1]["ocr"][0]["status"], "succeeded")
        self.assertNotIn("raw_response", json.dumps(confirmed))
        self.assertNotIn("artifact://", json.dumps(confirmed))

    def test_critical_provider_failure_blocks_confirm_noncritical_failure_is_warning(self):
        adapter = self.adapter_with_response(RuntimeError("provider unavailable"))
        critical = self.import_image(adapter)
        with self.assertRaisesRegex(Exception, "关键文件提取失败"):
            adapter.confirm(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=critical["brief_key"], confirmed_at="2026-09-09T00:00:00Z", expected_project_version=1)
        optional = self.import_image(adapter, project=self.second, critical=False)
        self.assertTrue(any(row["code"] == "NONCRITICAL_EXTRACTION_FAILED" for row in optional["brief"]["warnings"]))
        confirmed = adapter.confirm(self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"], brief_version_id=optional["brief_key"], confirmed_at="2026-09-09T00:00:00Z", expected_project_version=1)
        self.assertEqual(confirmed["confirmation_status"], "confirmed")

    def test_ocr_artifact_tamper_blocks_current_read_and_confirmation(self):
        adapter = self.adapter_with_response({"status": "no_text", "text": ""})
        result = self.import_image(adapter)
        record = next(row for row in self.raw(result)["extractions"] if row["ocr"])["ocr"][0]
        target = adapter.store.root / "ocr" / record["evidence"]["sha256"][:2] / record["evidence"]["sha256"]
        target.write_text("tampered")
        with self.assertRaisesRegex(subject.FormalBriefInputError, "INPUT_TAMPERED"):
            adapter.read_current_formal(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"])
        with self.assertRaisesRegex(subject.FormalBriefInputError, "INPUT_TAMPERED"):
            adapter.confirm(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=result["brief_key"], confirmed_at="2026-09-09T00:00:00Z", expected_project_version=1)

    def test_ocr_pixel_tamper_and_duplicate_content_cannot_bypass_evidence(self):
        adapter = self.adapter_with_response({"status": "no_text", "text": ""})
        with self.assertRaisesRegex(subject.FormalBriefInputError, "DUPLICATE_UPLOAD"):
            adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[_upload("one.png", _image_bytes("PNG")), _upload("same.png", _image_bytes("PNG"))])
        result = self.import_image(adapter)
        record = next(row for row in self.raw(result)["extractions"] if row["ocr"])["ocr"][0]
        target = adapter.store.root / "ocr-images" / record["image_sha256"][:2] / record["image_sha256"]
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(subject.FormalBriefInputError, "INPUT_TAMPERED"):
            adapter.read_structured(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=result["brief_key"])

    def test_all_declared_text_and_picture_suffixes_import_confirm_and_reopen(self):
        adapter = self.adapter_with_response({"status": "no_text", "text": ""})
        jpeg = BytesIO(); Image.new("RGB", (19, 11), "red").save(jpeg, format="JPEG")
        uploads = [_upload("brief.txt", _complete_brief()), _upload("notes.md", "特殊需求: 使用现场同期声"),
                   _upload("one.png", _image_bytes("PNG")), _upload("two.jpg", _image_bytes("JPEG")),
                   _upload("three.jpeg", jpeg.getvalue()), _upload("four.webp", _image_bytes("WEBP"))]
        result = adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=uploads)
        confirmed = adapter.confirm(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=result["brief_key"], confirmed_at="2026-09-09T00:00:00Z", expected_project_version=1)
        self.assertEqual(confirmed["confirmation_status"], "confirmed")
        reopened = subject.FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data").read_current_formal(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"])
        self.assertEqual(reopened["brief"], confirmed["brief"])
        self.assertEqual({row["status"] for row in reopened["brief"]["documents"]}, {"succeeded"})

    def test_scanned_input_reprocesses_after_configuration_and_successful_retry_reuses_evidence(self):
        content = _pdf_pages(False)
        first = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[_upload("scan.pdf", content)])
        self.assertTrue(first["has_failed_extractions"])
        calls = []
        def invoke(_path, _prompt):
            calls.append("OCR")
            return json.dumps({"status": "succeeded", "text": _complete_brief()}, ensure_ascii=False)
        adapter = subject.FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data", ocr_policy=subject.FormalBriefOcrPolicy(invoke=invoke, provider="anonymous", model="fixture"))
        with patch.object(subject, "_rasterize_pdf_page", return_value=_image_bytes("PNG")):
            second = adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=1, uploads=[_upload("scan.pdf", content)])
            retry = adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=2, uploads=[_upload("scan.pdf", content)])
        self.assertNotEqual(second["brief_key"], first["brief_key"])
        self.assertEqual(retry["brief_key"], second["brief_key"])
        self.assertEqual(retry["import_status"], "already_imported")
        self.assertEqual(calls, ["OCR"])
        original = self.raw(first)
        self.assertEqual(original["extractions"][0]["status"], "failed")

    def test_failed_transaction_rolls_back_authority_and_keeps_shared_pixels(self):
        adapter = self.adapter_with_response({"status": "no_text", "text": ""})
        with self.store.connection() as connection:
            before = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                      for table in ('brief_versions', 'project_files', 'document_images')}
        def fail(_connection, _version, _actor):
            raise RuntimeError("rollback fixture")
        with self.assertRaisesRegex(RuntimeError, "rollback fixture"):
            adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[_upload("photo.png", _image_bytes("PNG"))], transaction_hook=fail)
        with self.store.connection() as connection:
            after = {table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in before}
        self.assertEqual(after, before)
        self.assertFalse(any(path.is_file() for path in (adapter.store.root / '.staging').rglob('*')))

    def test_scanned_pdf_and_office_image_only_documents_survive_confirmation(self):
        adapter = self.adapter_with_response({"status": "succeeded", "text": _complete_brief()})
        uploads = [
            _upload("scan.pdf", _pdf_pages(False)),
            _upload("scan.docx", _office_zip("word/document.xml", '<w:document xmlns:w="w"><w:body><w:p/></w:body></w:document>', {"word/media/image1.png": _image_bytes("PNG")})),
            _upload("scan.pptx", _office_zip("ppt/slides/slide1.xml", '<p:sld xmlns:p="p"/>', {"ppt/media/image1.png": _image_bytes("PNG")})),
            _upload("scan.xlsx", _xlsx_with_image()),
        ]
        with patch.object(subject, "_rasterize_pdf_page", return_value=_image_bytes("PNG")):
            result = adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=uploads)
        raw = self.raw(result)
        self.assertEqual([row["ocr"][0]["status"] for row in raw["extractions"]], ["succeeded"] * 4)
        self.assertEqual(len(raw["images"]), 1)
        self.assertEqual(len(raw["images"][0]["source_links"]), 3)
        confirmed = adapter.confirm(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=result["brief_key"], confirmed_at="2026-09-09T00:00:00Z", expected_project_version=1)
        self.assertEqual(confirmed["confirmation_status"], "confirmed")
        self.assertEqual({row["status"] for row in confirmed["brief"]["documents"]}, {"succeeded"})


def _complete_brief():
    return "项目名称: 匿名活动\n脚本类型: 发布会\n目标时长: 60秒\n视频目标: 回顾发布活动\n必须出现的内容: 产品演示\n关键事件与流程: 嘉宾发布\n风格调性: 克制真实\n禁止或不能出现的内容: 虚构事实"


def _pdf_pages(*has_text):
    writer = PdfWriter()
    for has in has_text:
        if has:
            writer.add_page(PdfReader(BytesIO(_text_pdf("Project: Anonymous"))).pages[0])
        else:
            writer.add_blank_page(width=612, height=792)
    output = BytesIO(); writer.write(output); return output.getvalue()


def _xlsx_with_image():
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as WorkbookImage
    workbook = Workbook(); workbook.active.add_image(WorkbookImage(BytesIO(_image_bytes("PNG"))), "C5")
    output = BytesIO(); workbook.save(output); return output.getvalue()
