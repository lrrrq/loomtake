from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
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
from mvp_v2.formal_brief_domain import (
    ArtifactPayload, ConfirmationBinding, ConfirmedBriefInput, ConfirmedBriefResolver,
    FormalBriefDomainError, FormalBriefDomainService, FormalBriefWorkflow, SourceManifest,
    adapt_confirmed_brief_for_t02, formalize_alpha_extraction, legacy_snapshot_view,
    _validate_formal_payload,
)
from mvp_v2.formal_brief_migration import FORMAL_BRIEF_TABLES, authoritative_migration_fragment


class _Artifacts:
    def __init__(self): self.values = {}; self.write_calls = 0
    def write(self, payload):
        self.write_calls += 1
        digest = sha256(payload).hexdigest(); self.values[digest] = payload
        return ArtifactPayload(f"artifact://brief-version/{digest}", digest)
    def read(self, payload):
        return self.values[payload.sha256]


class _Authority:
    def __init__(self, stored, artifacts): self.stored, self.artifacts = stored, artifacts
    def load_confirmed(self, value): return self.stored
    def read_payload_bytes(self, payload): return self.artifacts.read(payload)


class _Access:
    def __init__(self): self.calls = []
    def require_project_permission(self, auth_token, *, workspace_id, project_id, permission):
        self.calls.append((auth_token, workspace_id, project_id, permission))
        if auth_token == "denied" or project_id != "project_alpha": raise FormalBriefDomainError("权限拒绝")
        return "actor_manager"


class FormalBriefDomainTests(unittest.TestCase):
    def setUp(self):
        self.artifacts, self.number = _Artifacts(), 0
        def ids(prefix): self.number += 1; return f"{prefix}_{self.number:04d}"
        self.service = FormalBriefDomainService(self.artifacts, ids)

    def test_formalizes_every_supported_location_with_source_and_content_sha(self):
        imported, manifest = _five_formats()
        formal = formalize_alpha_extraction(imported)
        locations = [location["evidence"] for extraction in formal["extractions"] for location in extraction["locations"]]
        self.assertTrue(all(item["source_document_id"] and len(item["content_sha256"]) == 64 for item in locations))
        self.assertTrue(any(item["location"] == {"page": 1} for item in locations))
        self.assertTrue(any(item["location"] == {"paragraph": 1} for item in locations))
        self.assertTrue(any(item["location"] == {"sheet": "资料", "row": 1, "column": 1} for item in locations))
        self.assertTrue(any(item["location"] == {"slide": 1} for item in locations))
        self.assertTrue(any(item["location"] == {"image_index": 1} for item in locations))
        self.assertTrue(all(len(item["content_sha256"]) == 64 for fact in formal["facts"] for item in fact["evidence_refs"]))
        self.assertTrue(all(item["artifact"]["sha256"] == item["content_sha256"] for item in formal["images"]))
        self.assertEqual(len(formal["images"]), 1)
        self.assertEqual(len(formal["images"][0]["source_links"]), 2)
        self.assertEqual(
            {link["source_document_id"] for link in formal["images"][0]["source_links"]},
            {link["source_document_id"] for link in imported["images"][0]["source_links"]},
        )
        self.assertTrue(all(
            link["evidence"]["source_document_id"] == link["source_document_id"]
            and link["evidence"]["location"] == {"image_index": 1}
            and link["evidence"]["content_sha256"] == formal["images"][0]["content_sha256"]
            for link in formal["images"][0]["source_links"]
        ))
        access = _Access(); workflow = FormalBriefWorkflow(self.service, access)
        pending = workflow.import_frozen("manager", workspace_id="workspace_alpha", project_id="project_alpha", source_manifest=manifest, extracted=imported)
        confirmed = workflow.confirm("manager", version=pending, confirmed_at="2026-08-13T00:00:00Z", binding=ConfirmationBinding("brief_confirmation_bound", "brief_snapshot_bound"))
        resolver = ConfirmedBriefResolver(_Authority(confirmed, self.artifacts))
        self.assertEqual(resolver.resolve_payload(confirmed)["project_id"], "project_alpha")
        t02_input = adapt_confirmed_brief_for_t02(confirmed, resolver)
        self.assertEqual(t02_input.confirmed.payload, confirmed.payload)
        self.assertEqual(t02_input.brief_payload["project_id"], "project_alpha")
        with self.assertRaises(TypeError): adapt_confirmed_brief_for_t02(confirmed.as_mapping(), resolver)  # type: ignore[arg-type]
        self.assertEqual([call[-1] for call in access.calls], ["project.update", "review.record"])

    def test_manifest_is_exact_and_critical_file_cannot_be_omitted(self):
        imported, manifest = _five_formats()
        shortened = SourceManifest.from_mapping({"manifest": manifest.manifest.as_mapping(), "documents": [
            {"source_document_id": item.source_document_id, "relative_path": item.relative_path, "content_sha256": item.content_sha256, "critical": item.critical}
            for item in manifest.documents[:-1]
        ]})
        with self.assertRaisesRegex(FormalBriefDomainError, "完整且精确一致"):
            self.service.imported_version(workspace_id="workspace_alpha", project_id="project_alpha", source_manifest=shortened, extracted=imported, created_by_actor_id="actor")
        failed, critical_manifest = _conflicted_import()
        failed["extractions"][0].update({"status": "failed", "critical": True, "error": {"code": "EXTRACTION_FAILED"}})
        pending = self.service.imported_version(workspace_id="workspace_alpha", project_id="project_alpha", source_manifest=critical_manifest, extracted=failed, created_by_actor_id="actor")
        payload_mutations = (
            lambda value: value["extractions"].pop(),
            lambda value: value["extractions"][0].update({"status": "succeeded"}),
            lambda value: value["extractions"][0].update({"critical": False}),
        )
        for mutate in payload_mutations:
            forged = self.service.imported_version(workspace_id="workspace_alpha", project_id="project_alpha", source_manifest=critical_manifest, extracted=failed, created_by_actor_id="actor")
            mutate(forged.payload_data)
            with self.assertRaisesRegex(FormalBriefDomainError, "关键文件"):
                self.service.confirm(forged, actor_id="actor", confirmed_at="2026-08-13T00:00:00Z", binding=ConfirmationBinding("confirmation", "snapshot"))
        replacement = replace(pending, payload=ArtifactPayload("artifact://brief-version/" + "d" * 64, "d" * 64))
        with self.assertRaisesRegex(FormalBriefDomainError, "artifact"):
            self.service.confirm(replacement, actor_id="actor", confirmed_at="2026-08-13T00:00:00Z", binding=ConfirmationBinding("confirmation", "snapshot"))
        failed["source_documents"][0]["critical"] = False
        failed["extractions"][0]["critical"] = False
        with self.assertRaisesRegex(FormalBriefDomainError, "完整且精确一致"):
            self.service.imported_version(workspace_id="workspace_alpha", project_id="project_alpha", source_manifest=critical_manifest, extracted=failed, created_by_actor_id="actor")
        with tempfile.TemporaryDirectory(prefix="mplus-alpha-") as root:
            alpha = AlphaBriefStore(root); alpha.inputs_root.mkdir(); outside = Path(tempfile.mkdtemp()) / "outside.txt"; outside.write_text("项目名称: 匿名")
            with self.assertRaises(AlphaBriefError): alpha.freeze_documents([outside])

    def test_conflict_confirmation_time_payload_and_bare_dict_are_rejected(self):
        imported, manifest = _conflicted_import()
        pending = self.service.imported_version(workspace_id="workspace_alpha", project_id="project_alpha", source_manifest=manifest, extracted=imported, created_by_actor_id="actor")
        with self.assertRaisesRegex(FormalBriefDomainError, "不可确认"):
            self.service.confirm(pending, actor_id="actor", confirmed_at="2026-08-13T00:00:00Z", binding=ConfirmationBinding("confirmation", "snapshot"))
        evidence = ArtifactPayload("artifact://resolution/" + "a" * 64, "a" * 64)
        conflict = pending.payload_data["conflicts"][0]
        resolved = self.service.resolve_conflicts(
            pending, resolutions={conflict["conflict_id"]: (conflict["fact_ids"][0], evidence)},
            actor_id="actor", resolved_at="2026-08-13T00:00:00Z",
        )
        confirmed = self.service.confirm(resolved, actor_id="actor", confirmed_at="2026-08-13T00:00:00Z", binding=ConfirmationBinding("confirmation", "snapshot"))
        resolver = ConfirmedBriefResolver(_Authority(confirmed, self.artifacts))
        with self.assertRaises(TypeError): resolver.resolve_payload(confirmed.as_mapping())  # type: ignore[arg-type]
        with self.assertRaisesRegex(FormalBriefDomainError, "持久确认"):
            resolver.resolve_payload(replace(confirmed, project_id="other"))
        invalid = confirmed.as_mapping(); invalid["confirmed_at"] = "2026-08-13T00:00:00+08:00"
        with self.assertRaisesRegex(FormalBriefDomainError, "UTC Z"):
            ConfirmedBriefInput.from_mapping(invalid)
        invalid = confirmed.as_mapping(); invalid["unresolved_conflict_count"] = -1
        with self.assertRaisesRegex(FormalBriefDomainError, "非负"):
            ConfirmedBriefInput.from_mapping(invalid)
        self.artifacts.values[confirmed.payload.sha256] = b"tampered"
        with self.assertRaisesRegex(FormalBriefDomainError, "SHA-256"):
            resolver.resolve_payload(confirmed)

    def test_import_rejects_empty_or_duplicate_factory_entity_ids_before_writing_artifact(self):
        imported, manifest = _conflicted_import()
        cases = {
            "empty": lambda _prefix: "",
            "non_string": lambda _prefix: 1,
            "duplicate_fact": lambda _prefix: "duplicate-id",
        }
        for name, factory in cases.items():
            with self.subTest(name=name):
                artifacts = _Artifacts()
                service = FormalBriefDomainService(artifacts, factory)
                with self.assertRaisesRegex(FormalBriefDomainError, "ID factory"):
                    service.imported_version(
                        workspace_id="workspace_alpha", project_id="project_alpha",
                        source_manifest=manifest, extracted=imported, created_by_actor_id="actor",
                    )
                self.assertEqual(artifacts.write_calls, 0)

        counter = 0
        def empty_conflict(prefix):
            nonlocal counter
            counter += 1
            return "" if prefix == "brief_conflict" else f"{prefix}-{counter}"
        artifacts = _Artifacts()
        with self.assertRaisesRegex(FormalBriefDomainError, "ID factory"):
            FormalBriefDomainService(artifacts, empty_conflict).imported_version(
                workspace_id="workspace_alpha", project_id="project_alpha",
                source_manifest=manifest, extracted=imported, created_by_actor_id="actor",
            )
        self.assertEqual(artifacts.write_calls, 0)

        imported["facts"].extend([
            {"fact_id": "fact_goal_a", "field": "视频目标", "value": "匿名目标甲", "confidence": "direct", "evidence_refs": ["artifact://evidence/doc_a/page/1"]},
            {"fact_id": "fact_goal_b", "field": "视频目标", "value": "匿名目标乙", "confidence": "direct", "evidence_refs": ["artifact://evidence/doc_a/page/2"]},
        ])
        imported["conflicts"].append({"conflict_id": "conflict_goal", "field": "视频目标", "fact_ids": ["fact_goal_a", "fact_goal_b"], "status": "unresolved"})
        counter = 0
        def duplicate_conflicts(prefix):
            nonlocal counter
            counter += 1
            return "duplicate-conflict" if prefix == "brief_conflict" else f"{prefix}-{counter}"
        artifacts = _Artifacts()
        with self.assertRaisesRegex(FormalBriefDomainError, "ID factory"):
            FormalBriefDomainService(artifacts, duplicate_conflicts).imported_version(
                workspace_id="workspace_alpha", project_id="project_alpha",
                source_manifest=manifest, extracted=imported, created_by_actor_id="actor",
            )
        self.assertEqual(artifacts.write_calls, 0)

    def test_field_update_provenance_rejects_current_source_outside_manifest(self):
        extracted, manifest = _five_formats()
        payload = formalize_alpha_extraction(extracted)
        payload["field_updates"] = [{
            "field_path": "风格调性",
            "source_document_ids": ["source_doc_not_in_manifest"],
            "retired_source_document_ids": [],
            "literal_source_document_ids": ["source_doc_not_in_manifest"],
        }]
        with self.assertRaisesRegex(FormalBriefDomainError, "未绑定当前字段事实与来源清单"):
            _validate_formal_payload(payload, payload["project_id"], manifest)

    def test_field_update_provenance_rejects_another_fields_source_but_allows_retired_history(self):
        extracted, manifest = _five_formats()
        payload = formalize_alpha_extraction(extracted)
        style = next(item for item in payload["facts"] if item["field_path"] == "风格调性")
        style_sources = {item["source_document_id"] for item in style["evidence_refs"]}
        other_source = next(item.source_document_id for item in manifest.documents
            if item.source_document_id not in style_sources)
        payload["field_updates"] = [{
            "field_path": "风格调性", "source_document_ids": [other_source],
            "retired_source_document_ids": [], "literal_source_document_ids": [other_source],
        }]
        with self.assertRaisesRegex(FormalBriefDomainError, "未绑定当前字段事实与来源清单"):
            _validate_formal_payload(payload, payload["project_id"], manifest)
        actual_source = next(iter(style_sources))
        payload["field_updates"] = [{
            "field_path": "风格调性", "source_document_ids": [actual_source],
            "retired_source_document_ids": ["source_doc_retired_from_old_manifest"],
            "literal_source_document_ids": [actual_source, "source_doc_retired_from_old_manifest"],
        }]
        _validate_formal_payload(payload, payload["project_id"], manifest)

    def test_legacy_is_read_only_rbac_blocks_and_migration_is_immutable_same_schema_contract(self):
        legacy = legacy_snapshot_view(workspace_id="workspace_alpha", project_id="project_alpha", brief_snapshot_id="legacy", payload={"ref": "artifact://legacy/" + "b" * 64, "sha256": "b" * 64})
        self.assertEqual(legacy.source_kind, "legacy_single_source")
        with self.assertRaises(TypeError): ConfirmedBriefResolver(_Authority(None, self.artifacts)).resolve_payload(legacy)  # type: ignore[arg-type]
        with self.assertRaisesRegex(FormalBriefDomainError, "权限拒绝"):
            FormalBriefWorkflow(self.service, _Access()).import_frozen("denied", workspace_id="workspace_alpha", project_id="project_alpha", source_manifest=_conflicted_import()[1], extracted=_conflicted_import()[0])
        statements = authoritative_migration_fragment(); joined = "\n".join(statements)
        self.assertEqual({statement.split("(", 1)[0].split()[-1] for statement in statements if statement.startswith("CREATE TABLE")}, set(FORMAL_BRIEF_TABLES))
        for field in ("fact_id TEXT PRIMARY KEY", "field_path TEXT NOT NULL", "value_json TEXT NOT NULL", "certainty TEXT NOT NULL CHECK(certainty IN ('confirmed','pending'))", "resolution_evidence_ref TEXT", "brief_snapshot_id TEXT NOT NULL UNIQUE REFERENCES project_brief_snapshots", "unresolved_conflict_count INTEGER NOT NULL CHECK(unresolved_conflict_count >= 0)"):
            self.assertIn(field, joined)
        self.assertNotIn("sqlite", joined.lower())
        self.assertEqual(sum("IMMUTABLE_RECORD" in item for item in statements), 16)


def _five_formats():
    with tempfile.TemporaryDirectory(prefix="mplus-alpha-") as root:
        alpha = AlphaBriefStore(root); inputs = alpha.inputs_root; inputs.mkdir()
        pdf = inputs / "anonymous.pdf"; _text_pdf(pdf, "Project: Alpha")
        image = inputs / "anonymous.png"; Image.new("RGB", (20, 10), "blue").save(image)
        docx = inputs / "anonymous.docx"; _office_zip(docx, "word/document.xml", "<w:document xmlns:w=\"w\"><w:body><w:p><w:r><w:t>品牌: 匿名品牌</w:t></w:r></w:p></w:body></w:document>", {"word/media/image1.png": image.read_bytes()})
        xlsx = inputs / "anonymous.xlsx"; workbook = Workbook(); workbook.active.title = "资料"; workbook.active["A1"] = "脚本类型: 发布会"; workbook.active["A2"] = "目标时长: 60秒"; workbook.active["A3"] = "风格调性: 简洁"; workbook.active["A4"] = "必须出现: 产品演示"; workbook.save(xlsx)
        pptx = inputs / "anonymous.pptx"; _office_zip(pptx, "ppt/slides/slide1.xml", "<p:sld xmlns:p=\"p\"><p:spTree><p:txBody><a:p xmlns:a=\"a\"><a:r><a:t>关键事件: 产品演示</a:t></a:r></a:p></p:txBody></p:spTree></p:sld>")
        paths = [pdf, docx, xlsx, pptx, image]; frozen = alpha.freeze_documents(paths); imported = alpha.import_documents("project_alpha", paths)
        docs = []
        by_sha = {item["sha256"]: item for item in frozen["documents"]}
        for source in imported["source_documents"]:
            item = by_sha[source["sha256"]]
            docs.append({"source_document_id": source["document_id"], "relative_path": item["relative_path"], "content_sha256": source["sha256"], "critical": source["critical"]})
        return imported, SourceManifest.from_mapping({"manifest": {"ref": frozen["inputs_ref"], "sha256": frozen["inputs_sha256"]}, "documents": docs})


def _conflicted_import():
    sha = "c" * 64
    imported = {"project_id": "project_alpha", "source_documents": [{"document_id": "doc_a", "sha256": sha, "critical": True}], "extractions": [{"document_id": "doc_a", "status": "succeeded", "critical": True, "error": None, "locations": [{"evidence_ref": "artifact://evidence/doc_a/page/1", "location": {"page": 1}, "text": "项目名称: 匿名甲"}, {"evidence_ref": "artifact://evidence/doc_a/page/2", "location": {"page": 2}, "text": "项目名称: 匿名乙"}]}], "images": [], "facts": [{"fact_id": "fact_a", "field": "项目名称", "value": "匿名甲", "confidence": "direct", "evidence_refs": ["artifact://evidence/doc_a/page/1"]}, {"fact_id": "fact_b", "field": "项目名称", "value": "匿名乙", "confidence": "direct", "evidence_refs": ["artifact://evidence/doc_a/page/2"]}], "conflicts": [{"conflict_id": "conflict_project", "field": "项目名称", "fact_ids": ["fact_a", "fact_b"], "status": "unresolved"}]}
    manifest = SourceManifest.from_mapping({"manifest": {"ref": "artifact://manifest/" + sha, "sha256": sha}, "documents": [{"source_document_id": "doc_a", "relative_path": "inputs/a.txt", "content_sha256": sha, "critical": True}]})
    return imported, manifest


def _office_zip(path, member, content, extra=None):
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
        for name, value in (extra or {}).items(): archive.writestr(name, value)


def _text_pdf(path, text):
    writer = PdfWriter(); page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject(); content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")); page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as output: writer.write(output)
