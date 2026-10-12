from __future__ import annotations

from dataclasses import is_dataclass
from io import BytesIO
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from mvp_v2.formal_brief_domain import ArtifactPayload, FormalBriefDomainError
from mvp_v2.formal_brief_input_adapter import (
    FormalBriefInputAdapter,
    FormalBriefInputError,
    FormalBriefOcrPolicy,
    FormalBriefUnderstandingPolicy,
    UploadedDocument,
)
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class FormalBriefInputAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-r01-input-")
        self.root = Path(self.temporary.name)
        self.store = WorkspaceStore(self.root / "data" / "workspace.sqlite3")
        owner = LocalIdentityAdapter(self.store).bootstrap_owner(workspace_name="匿名工作区", display_name="匿名 owner", confirmed=True, token="anonymous-owner-token")
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.first = self.projects.create_project(self.token, workspace_id=self.workspace_id, name="匿名项目甲", category_id="发布会", expected_workspace_version=0)
        self.second = self.projects.create_project(self.token, workspace_id=self.workspace_id, name="匿名项目乙", category_id="发布会", expected_workspace_version=1)
        self.adapter = FormalBriefInputAdapter(FormalBriefWorkspaceService(self.store, artifact_root=self.root / "data"), data_root=self.root / "data")

    def tearDown(self):
        self.temporary.cleanup()

    def test_question_append_keeps_prior_model_fact_when_next_model_omits_it(self):
        prompts = []
        def understand(prompt, _model, _limit):
            prompts.append(prompt)
            return json.dumps({'facts': [{'field': '视频目标', 'value': '带来秋季新品发布会期待感',
                'classification': 'content_goal', 'scope': 'whole_video',
                'evidence': [{'key': 'E0001', 'quote': '带来秋季新品发布会期待感'}]}]
                if len(prompts) == 1 else []}, ensure_ascii=False)
        adapter = FormalBriefInputAdapter(self.adapter.service, data_root=self.root / 'data',
            understanding_policy=FormalBriefUnderstandingPolicy(invoke=understand,
                provider='anonymous', model='anonymous'))
        first = adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first['project_id'], expected_project_version=0,
            uploads=[_upload('原始想法.txt', '希望这支视频带来秋季新品发布会期待感。')])
        old_fact = next(f for f in self._raw_brief(first)['facts'] if f['field_path'] == '视频目标')
        appended = adapter.import_question_answer(self.token, workspace_id=self.workspace_id,
            project_id=self.first['project_id'], expected_project_version=1,
            binding={'field': '目标时长', 'question_id': 'task-question:1', 'question': '成片大概多长？'},
            value='30秒，共6镜，每镜5秒。')
        current = self._raw_brief(appended)
        goal = next(f for f in current['facts'] if f['field_path'] == '视频目标')
        self.assertEqual(goal['value_json'], old_fact['value_json'])
        self.assertEqual(goal['evidence_refs'], old_fact['evidence_refs'])
        self.assertEqual(goal['semantic_metadata'], old_fact['semantic_metadata'])
        self.assertNotIn('希望这支视频带来秋季新品发布会期待感', prompts[-1])
        self.assertEqual(len(appended['brief']['documents']), 2)

    def test_append_to_legacy_extraction_preserves_original_verified_references(self):
        from tests.test_mvp_v2_formal_generation_service import FormalGenerationServiceTests
        fixture = FormalGenerationServiceTests('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = fixture.briefs
        scope = {'workspace_id': fixture.workspace_id, 'project_id': fixture.project['project_id']}
        original = service.latest_import_base(fixture.token, **scope).payload_data
        self.assertNotIn('extraction_reuse_key', original['source_documents'][0])
        adapter = FormalBriefInputAdapter(service, data_root=fixture.root / 'runtime')
        result = adapter.import_uploads(fixture.token, **scope, expected_project_version=2,
            uploads=[_upload('补充用途.txt', '视频目标：让观众了解新品。')])
        current = service.latest_import_base(fixture.token, **scope).payload_data
        for old in original['facts']:
            retained = next(row for row in current['facts'] if row['field_path'] == old['field_path']
                            and row['value_json'] == old['value_json'])
            self.assertEqual(retained['evidence_refs'], old['evidence_refs'])
        self.assertTrue(any(row['field_path'] == '视频目标' for row in current['facts']))
        restarted = FormalBriefInputAdapter(service, data_root=fixture.root / 'runtime')
        self.assertEqual(restarted.read_structured(fixture.token, **scope,
            brief_version_id=result['brief_key'])['brief'], result['brief'])

    def test_five_document_formats_three_image_formats_are_frozen_and_restart_readable(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], uploads=_five_uploads(), expected_project_version=0,
        )
        self._assert_http_json(result)
        self.assertEqual(result["confirmation_status"], "pending")
        self.assertEqual({item["status"] for item in result["brief"]["documents"]}, {"succeeded"})
        self.assertEqual(len(result["brief"]["documents"]), 7)
        self.assertEqual(result["creative_standard"]["status"], "draft")
        self.assertNotIn("avoid_expressions", result["creative_standard"].get("missing_fields", []))
        restarted = FormalBriefInputAdapter(FormalBriefWorkspaceService(WorkspaceStore(self.store.database_path), artifact_root=self.root / "data"), data_root=self.root / "data")
        again = restarted.read_structured(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=result["brief_key"])
        self._assert_http_json(again)
        self.assertEqual(again["brief"], result["brief"])
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            restarted.read_structured(self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"], brief_version_id=result["brief_key"])

    def test_docx_break_keeps_main_and_specific_type_separate_and_duration_mismatch_is_warning(self):
        xml = """<w:document xmlns:w="w"><w:body>
        <w:p><w:r><w:t>项目名称：60s匿名挑战赛</w:t></w:r></w:p>
        <w:p><w:r><w:t>脚本类型：明星活动</w:t><w:br/><w:t>具体类型：匿名互动回顾片</w:t></w:r></w:p>
        <w:p><w:r><w:t>目标时长：30s</w:t></w:r></w:p>
        <w:p><w:r><w:t>风格调性：浪漫真实</w:t></w:r></w:p>
        <w:p><w:r><w:t>视频目标：回顾活动</w:t></w:r></w:p>
        <w:p><w:r><w:t>必须出现的内容：互动现场</w:t></w:r></w:p>
        </w:body></w:document>"""
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=[_upload("匿名需求.docx", _office_zip("word/document.xml", xml))],
            expected_project_version=0,
        )
        values = {item["label"]: formal_json_value(item["value"]) for item in result["brief"]["facts"]}
        self.assertEqual(values["脚本类型"], "明星活动")
        self.assertEqual(values["具体类型"], "匿名互动回顾片")
        self.assertEqual(result["brief"]["warnings"][0]["code"], "DURATION_NAME_MISMATCH")
        self.assertIn("目标时长 30s 为准", result["brief"]["warnings"][0]["message"])
        self.assertEqual(result["routing"]["status"], "ready")
        self.assertEqual(result["routing"]["script_type"], "明星活动")
        self.assertEqual(result["routing"]["source_workbook_name"], "M+明星活动类通用脚本库.xlsx")
        self.assertNotIn("avoid_expressions", result["creative_standard"].get("missing_fields", []))
        confirmed = self.adapter.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=result["brief_key"], confirmed_at="2026-08-19T00:00:00Z",
            expected_project_version=1,
        )
        self.assertEqual(confirmed["confirmation_status"], "confirmed")

    def test_descriptive_specific_type_is_preflighted_before_confirmation(self):
        content = """项目名称：匿名全球大会
脚本类型：活动短视频
具体类型：发布会活动回顾视频
目标时长：60s
风格调性：企业、商务、大气
必须出现：全球合作伙伴与启动仪式
"""
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=[_upload("匿名需求.txt", content.encode("utf-8"))],
            expected_project_version=0,
        )

        self.assertEqual(result["routing"]["status"], "ready")
        self.assertEqual(result["routing"]["script_type"], "发布会")
        self.assertEqual(result["routing"]["authority"], "industry_or_activity_category")

    def test_delivery_requirement_is_preserved_as_a_brief_fact(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=[_upload(
                "匿名交付要求.txt",
                "项目名称：匿名明星活动\n脚本类型：明星活动\n目标时长：60s\n"
                "交付要求：交付8镜脚本Excel与PDF，保留旁白字幕",
            )],
            expected_project_version=0,
        )
        facts = {
            item["label"]: formal_json_value(item["value"])
            for item in result["brief"]["facts"]
        }
        self.assertEqual(
            facts["交付要求"], "交付8镜脚本Excel与PDF，保留旁白字幕",
        )

    def test_docx_section_headings_compile_flow_required_and_special_facts(self):
        xml = """<w:document xmlns:w="w"><w:body>
        <w:p><w:r><w:t>项目名称：匿名七夕活动</w:t></w:r></w:p>
        <w:p><w:r><w:t>脚本类型：活动短视频</w:t></w:r></w:p>
        <w:p><w:r><w:t>具体类型：七夕快闪互动回顾片</w:t></w:r></w:p>
        <w:p><w:r><w:t>目标时长：30s</w:t></w:r></w:p>
        <w:p><w:r><w:t>风格调性：浪漫真实</w:t></w:r></w:p>
        <w:p><w:r><w:t>现场流程：</w:t></w:r></w:p>
        <w:p><w:r><w:t>情侣回答问题</w:t></w:r></w:p>
        <w:p><w:r><w:t>答案一致后撤掉隔板</w:t></w:r></w:p>
        <w:p><w:r><w:t>关键内容：</w:t></w:r></w:p>
        <w:p><w:r><w:t>捕捉情侣甜蜜表情</w:t></w:r></w:p>
        <w:p><w:r><w:t>特殊需求：</w:t></w:r></w:p>
        <w:p><w:r><w:t>开头AI蝴蝶从KV花园飞出</w:t></w:r></w:p>
        </w:body></w:document>"""

        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=[_upload("匿名需求.docx", _office_zip("word/document.xml", xml))],
            expected_project_version=0,
        )

        values = {
            item["label"]: formal_json_value(item["value"])
            for item in result["brief"]["facts"]
        }
        self.assertEqual(values["关键事件与流程"], "情侣回答问题；答案一致后撤掉隔板")
        self.assertEqual(values["必须出现的内容"], "捕捉情侣甜蜜表情")
        self.assertEqual(values["特殊需求"], "开头AI蝴蝶从KV花园飞出")
        self.assertEqual(result["routing"]["category_id"], "star_activity")
        self.assertNotIn(
            "expression_focus", result["creative_standard"].get("missing_fields", []),
        )

    def test_equivalent_list_punctuation_merges_sources_without_false_conflict(self):
        result = self.adapter.import_uploads(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.first["project_id"],
            uploads=[
                _upload("甲.txt", "必须出现的内容: 品牌 Logo、主会场、核心产品"),
                _upload("乙.txt", "必须出现的内容: 品牌 Logo；主会场；核心产品"),
            ],
            expected_project_version=0,
        )

        required = next(
            item for item in result["brief"]["facts"]
            if item["label"] == "必须出现的内容"
        )
        self.assertEqual(formal_json_value(required["value"]), "品牌 Logo、主会场、核心产品")
        self.assertEqual(len(required["sources"]), 2)
        self.assertEqual(result["brief"]["conflicts"], [])

    def test_complementary_sources_merge_with_sentence_punctuation_and_keep_all_evidence(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_version=0, uploads=[
                _upload("场景.txt", "特殊需求：自然光。木吉他配乐。少字幕。\n风格调性：自然、明亮"),
                _upload("产品.txt", "特殊需求：米白色杯身；木吉他配乐；少字幕\n风格调性：温暖"),
                _upload("片尾.txt", "特殊需求：片尾文案“带上随行杯，轻松出发”"),
            ],
        )
        self.assertEqual(result["brief"]["conflicts"], [])
        facts = {item["label"]: item for item in result["brief"]["facts"]}
        value = formal_json_value(facts["特殊需求"]["value"])
        self.assertEqual(value.count("木吉他配乐"), 1)
        self.assertEqual(value.count("少字幕"), 1)
        self.assertIn("米白色杯身", value)
        self.assertIn("片尾文案“带上随行杯，轻松出发”", value)
        self.assertEqual(len(facts["特殊需求"]["sources"]), 3)
        self.assertEqual(formal_json_value(facts["风格调性"]["value"]), "自然；明亮；温暖")

    def test_real_shot_and_voiceover_conflicts_keep_common_requirements_in_each_choice(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_version=0, uploads=[
                _upload("布置.txt", "特殊需求：自然光；产品居中"),
                _upload("镜数甲.txt", "特殊需求：全片8镜"),
                _upload("镜数乙.txt", "特殊需求：全片12镜"),
                _upload("声音甲.txt", "特殊需求：无旁白"),
                _upload("声音乙.txt", "特殊需求：需要旁白"),
            ],
        )
        self.assertEqual(len(result["brief"]["conflicts"]), 1)
        choices = result["brief"]["conflicts"][0]["choices"]
        self.assertEqual(len(choices), 4)
        for choice in choices:
            text = formal_json_value(choice["value"])
            self.assertIn("自然光", text)
            self.assertIn("产品居中", text)
            self.assertNotEqual("全片8镜" in text, "全片12镜" in text)
            self.assertNotEqual("无旁白" in text, "需要旁白" in text)

    def test_reversed_explicit_flow_stays_a_conflict(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_version=0, uploads=[
                _upload("流程甲.txt", "关键事件与流程：签到；产品发布；合影"),
                _upload("流程乙.txt", "关键事件与流程：签到；合影；产品发布"),
            ],
        )
        self.assertEqual(len(result["brief"]["conflicts"]), 1)
        self.assertEqual(len(result["brief"]["conflicts"][0]["choices"]), 2)

    def test_complementary_flow_keeps_each_source_order_and_a_cycle_is_not_silently_merged(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_version=0, uploads=[
                _upload("流程甲.txt", "关键事件与流程：签到；正式发布"),
                _upload("流程乙.txt", "关键事件与流程：产品体验；正式发布"),
            ],
        )
        self.assertEqual(result["brief"]["conflicts"], [])
        flow = next(item for item in result["brief"]["facts"] if item["label"] == "关键事件与流程")
        self.assertEqual(formal_json_value(flow["value"]), "签到；产品体验；正式发布")
        cyclic = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
            expected_project_version=0, uploads=[
                _upload("环节甲.txt", "关键事件与流程：签到；体验"),
                _upload("环节乙.txt", "关键事件与流程：体验；发布"),
                _upload("环节丙.txt", "关键事件与流程：发布；签到"),
            ],
        )
        self.assertEqual(len(cyclic["brief"]["conflicts"]), 1)
        self.assertEqual(len(cyclic["brief"]["conflicts"][0]["choices"]), 3)

    def test_quoted_copy_and_a_shot_reference_do_not_create_a_production_conflict(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_version=0, uploads=[
                _upload("字幕.txt", '特殊需求：字幕显示“无旁白”；参考第8镜画面'),
                _upload("配音.txt", "特殊需求：保留旁白；全片12镜"),
            ],
        )
        self.assertEqual(result["brief"]["conflicts"], [])

    def test_amended_pdf_page_preserves_duration_and_multiple_labelled_fields(self):
        pdf = PdfWriter()
        page = pdf.add_blank_page(width=200, height=100)
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 10 Tf 10 70 Td (fixture amendment) Tj ET")
        page[NameObject("/Contents")] = pdf._add_object(content)
        output = BytesIO(); pdf.write(output)
        from pypdf._page import PageObject
        with patch.object(PageObject, "extract_text", return_value="目标时长：90秒\n特殊需求：12个镜头"):
            result = self.adapter.import_uploads(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                expected_project_version=0, uploads=[
                    _upload("原要求.txt", "目标时长：60s"),
                    _upload("时长调整.pdf", output.getvalue()),
                ],
            )
        durations = [formal_json_value(item["value"]) for item in result["brief"]["facts"] if item["label"] == "目标时长"]
        self.assertEqual(set(durations), {"60s", "90秒"})
        self.assertEqual({formal_json_value(item["value"]) for item in result["brief"]["conflicts"][0]["choices"]}, {"60s", "90秒"})
        amendment = next(item for item in result["brief"]["facts"] if item["label"] == "特殊需求")
        self.assertEqual(formal_json_value(amendment["value"]), "12个镜头")
        self.assertEqual(amendment["sources"], ["时长调整.pdf · 第 1 页"])

    def test_narrative_duration_in_another_source_is_not_hidden_by_direct_duration(self):
        def understand(prompt, _model, _tokens):
            evidence = json.loads(prompt.split("EVIDENCE=", 1)[1])
            row = next(item for item in evidence if "调整为90秒" in item["text"])
            return json.dumps({"facts": [{"field": "目标时长", "value": "90秒",
                "evidence": [{"key": row["key"], "quote": "调整为90秒"}]}]}, ensure_ascii=False)
        adapter = FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data",
            understanding_policy=FormalBriefUnderstandingPolicy(invoke=understand, provider="anonymous", model="fixture"))
        result = adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0, uploads=[
                _upload("原要求.txt", "目标时长：60秒"),
                _upload("通知.txt", "本次成片调整为90秒。"),
            ])
        durations = {formal_json_value(item["value"]) for item in result["brief"]["facts"] if item["label"] == "目标时长"}
        self.assertEqual(durations, {"60秒", "90秒"})
        self.assertEqual(len(result["brief"]["conflicts"]), 1)

    def test_inline_multi_field_answer_does_not_swallow_special_requirements(self):
        result = self.adapter.import_inline_answer(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], field="目标时长",
            value="60秒。特殊需求：8个镜头。风格调性：自然明亮", expected_project_version=0)
        facts = {item["label"]: formal_json_value(item["value"]) for item in result["brief"]["facts"]}
        self.assertEqual(facts["目标时长"], "60秒")
        self.assertEqual(facts["特殊需求"], "8个镜头")
        self.assertEqual(facts["风格调性"], "自然明亮")
        multiline = self.adapter.import_inline_answer(self.token, workspace_id=self.workspace_id,
            project_id=self.second["project_id"], field="目标时长",
            value="90秒\n特殊需求：12个镜头\n风格调性：明亮", expected_project_version=0)
        multiline_facts = {item["label"]: formal_json_value(item["value"])
            for item in multiline["brief"]["facts"]}
        self.assertEqual(multiline_facts["目标时长"], "90秒")
        self.assertEqual(multiline_facts["特殊需求"], "12个镜头")

    def test_ppt_media_directory_is_ignored_but_real_unsupported_media_is_rejected(self):
        slide = '<p:sld xmlns:p="p"><p:cSld>项目名称：匿名新品发布会</p:cSld></p:sld>'
        result = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0, uploads=[
                _upload("资料.pptx", _office_zip("ppt/slides/slide1.xml", slide,
                    {"ppt/media/": b"", "ppt/media/image1.png": _image_bytes("PNG")})),
            ])
        self.assertEqual(result["brief"]["documents"][0]["status"], "succeeded")
        self.assertEqual(len(self.adapter.service.read_structured(self.token,
            workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=result["brief_key"])["images"]), 1)
        with self.assertRaisesRegex(FormalBriefInputError, "UNSUPPORTED_EMBEDDED_IMAGE"):
            self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], expected_project_version=1, uploads=[
                    _upload("不支持.pptx", _office_zip("ppt/slides/slide1.xml", slide,
                        {"ppt/media/": b"", "ppt/media/image.svg": b"<svg/>"})),
                ])

    def test_same_content_cross_project_has_isolated_formal_fact_ids(self):
        frozen_uploads = tuple(
            (item.filename, item.stream.getvalue(), item.critical)
            for item in _five_uploads()
        )

        def same_uploads():
            return [
                UploadedDocument(filename, BytesIO(content), critical)
                for filename, content, critical in frozen_uploads
            ]

        first = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=same_uploads(), expected_project_version=0,
        )
        second = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
            uploads=same_uploads(), expected_project_version=0,
        )
        first_raw = self.adapter.service.read_structured(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=first["brief_key"],
        )
        second_raw = self.adapter.service.read_structured(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
            brief_version_id=second["brief_key"],
        )
        self.assertNotEqual(first["brief_key"], second["brief_key"])
        self.assertTrue({item["fact_id"] for item in first_raw["facts"]}.isdisjoint(
            {item["fact_id"] for item in second_raw["facts"]}
        ))
        self.assertEqual(
            {item["content_sha256"] for item in first_raw["images"]},
            {item["content_sha256"] for item in second_raw["images"]},
        )
        with self.store.connection() as connection:
            first_files = connection.execute(
                "SELECT file_id, source_document_id FROM project_files WHERE project_id=?",
                (self.first["project_id"],),
            ).fetchall()
            second_files = connection.execute(
                "SELECT file_id, source_document_id FROM project_files WHERE project_id=?",
                (self.second["project_id"],),
            ).fetchall()
            first_images = connection.execute(
                """SELECT document_image_id, image_sha256 FROM document_images i
                   JOIN project_files f ON f.file_id=i.file_id WHERE f.project_id=?""",
                (self.first["project_id"],),
            ).fetchall()
            second_images = connection.execute(
                """SELECT document_image_id, image_sha256 FROM document_images i
                   JOIN project_files f ON f.file_id=i.file_id WHERE f.project_id=?""",
                (self.second["project_id"],),
            ).fetchall()
            versions_before_retry = connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0]
        self.assertEqual({row["source_document_id"] for row in first_files}, {row["source_document_id"] for row in second_files})
        self.assertTrue({row["file_id"] for row in first_files}.isdisjoint({row["file_id"] for row in second_files}))
        self.assertEqual({row["image_sha256"] for row in first_images}, {row["image_sha256"] for row in second_images})
        self.assertTrue({row["document_image_id"] for row in first_images}.isdisjoint({row["document_image_id"] for row in second_images}))
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.adapter.import_uploads(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                uploads=_five_uploads(), expected_project_version=0,
            )
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0], versions_before_retry)
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.adapter.service.read_structured(
                self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
                brief_version_id=first["brief_key"],
            )

    def test_same_conflicted_content_cross_project_rekeys_conflicts_and_preserves_resolution(self):
        first = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=_conflicted_uploads(), expected_project_version=0,
        )
        second = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
            uploads=_conflicted_uploads(), expected_project_version=0,
        )
        first_raw = self.adapter.service.read_structured(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=first["brief_key"],
        )
        second_raw = self.adapter.service.read_structured(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
            brief_version_id=second["brief_key"],
        )
        first_conflict = first_raw["conflicts"][0]
        second_conflict = second_raw["conflicts"][0]
        self.assertNotEqual(first_conflict["conflict_id"], second_conflict["conflict_id"])
        self.assertTrue(set(first_conflict["fact_ids"]).isdisjoint(second_conflict["fact_ids"]))
        resolved = self.adapter.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=first["brief_key"], conflict_id=first_conflict["conflict_id"],
            selected_fact_id=first_conflict["fact_ids"][0], expected_project_version=1,
        )
        confirmed = self.adapter.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=resolved["brief_key"], confirmed_at="2026-08-14T00:00:00Z",
            expected_project_version=2,
        )
        self.assertEqual(confirmed["confirmation_status"], "confirmed")
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.adapter.service.read_structured(
                self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
                brief_version_id=resolved["brief_key"],
            )

    def test_conflict_resolution_then_human_confirmation_and_input_tamper_rejection(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0,
            uploads=[
                _upload("first.txt", "项目名称: 匿名甲\n风格调性: 克制\n视频目标: 说明重点\n必须出现的内容: 产品\n关键事件与流程: 发布\n禁止或不能出现的内容: 夸张"),
                _upload("second.txt", "项目名称: 匿名乙\n风格调性: 克制\n视频目标: 说明重点\n必须出现的内容: 产品\n关键事件与流程: 发布\n禁止或不能出现的内容: 夸张"),
            ],
        )
        self.assertEqual(result["creative_standard"]["status"], "draft")
        self.assertEqual(len(result["brief"]["conflicts"]), 1)
        conflict = result["brief"]["conflicts"][0]
        resolved = self.adapter.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=result["brief_key"],
            conflict_id=conflict["conflict_key"], selected_fact_id=conflict["choices"][0]["fact_key"], expected_project_version=1,
        )
        self._assert_http_json(resolved)
        confirmed = self.adapter.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=resolved["brief_key"],
            confirmed_at="2026-08-13T00:00:01Z", expected_project_version=2,
        )
        self._assert_http_json(confirmed)
        self.assertEqual(confirmed["confirmation_status"], "confirmed")
        self.assertEqual(confirmed["creative_standard"]["status"], "confirmed")
        self.assertIsNotNone(confirmed["creative_standard"]["confirmed_at"])
        self.assertNotIn("artifact://", json.dumps(confirmed, ensure_ascii=False))
        tampered = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"], expected_project_version=0,
            uploads=[_upload("third.txt", "项目名称: 匿名丙")],
        )
        raw_tampered = self.adapter.service.read_structured(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
            brief_version_id=tampered["brief_key"],
        )
        digest = raw_tampered["source_manifest"]["documents"][0]["content_sha256"]
        self.adapter.store.file_path(digest, ".txt").write_bytes(b"tampered")
        with self.assertRaisesRegex(FormalBriefInputError, "INPUT_TAMPERED"):
            self.adapter.confirm(self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"], brief_version_id=tampered["brief_key"], confirmed_at="2026-08-13T00:00:02Z", expected_project_version=1)

    def test_duplicate_capacity_stream_path_and_scanned_pdf_fail_closed(self):
        same = "项目名称: 匿名".encode("utf-8")
        with self.assertRaisesRegex(FormalBriefInputError, "DUPLICATE_UPLOAD"):
            self.adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[_upload("a.txt", same), _upload("b.txt", same)])
        limited = FormalBriefInputAdapter(FormalBriefWorkspaceService(self.store, artifact_root=self.root / "data"), data_root=self.root / "data", max_file_bytes=16)
        with self.assertRaisesRegex(FormalBriefInputError, "FILE_TOO_LARGE"):
            limited.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[_upload("large.txt", b"x" * 17)])
        with self.assertRaisesRegex(FormalBriefInputError, "二进制流"):
            self.adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[UploadedDocument("not-a-path.txt", "/tmp/forbidden")])  # type: ignore[arg-type]
        scanned = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[_upload("scanned.pdf", _scanned_pdf())])
        self.assertEqual(scanned["brief"]["documents"][0]["status"], "failed")
        with self.assertRaisesRegex(FormalBriefDomainError, "不可确认"):
            self.adapter.confirm(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=scanned["brief_key"], confirmed_at="2026-08-13T00:00:00Z", expected_project_version=1)

    def test_supplemental_batch_inherits_confirmed_brief_and_refresh_restores_pending_child(self):
        first = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0,
            uploads=[_upload("首批资料.txt", "\n".join((
                "项目名称: 匿名活动", "脚本类型: 发布会", "视频目标: 说明活动重点",
                "目标时长: 60秒", "必须出现的内容: 产品演示",
                "关键事件与流程: 嘉宾发布", "风格调性: 克制真实",
                "禁止或不能出现的内容: 不得虚构事实",
            )))],
        )
        confirmed = self.adapter.confirm(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=first["brief_key"],
            confirmed_at="2026-08-19T00:00:00Z", expected_project_version=1,
        )
        self.assertEqual(confirmed["confirmation_status"], "confirmed")

        supplemented = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=2,
            uploads=[_upload("补充说明.txt", "使用场景: 线下挑战赛\n必须出现的内容: 产品演示")],
        )
        self.assertEqual(supplemented["import_status"], "appended")
        self.assertEqual(
            {item["filename"] for item in supplemented["brief"]["documents"]},
            {"首批资料.txt", "补充说明.txt"},
        )
        raw = self.adapter.service.read_structured(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"],
            brief_version_id=supplemented["brief_key"],
        )
        self.assertEqual(raw["parent_brief_version_id"], first["brief_key"])
        restarted = FormalBriefInputAdapter(
            FormalBriefWorkspaceService(
                WorkspaceStore(self.store.database_path), artifact_root=self.root / "data",
            ),
            data_root=self.root / "data",
        )
        current = restarted.read_current_formal(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"],
        )
        self.assertEqual(current["brief_key"], supplemented["brief_key"])
        self.assertEqual(current["confirmation_status"], "pending")
        self.assertEqual(
            {item["filename"] for item in current["brief"]["documents"]},
            {"首批资料.txt", "补充说明.txt"},
        )

    def test_text_pdf_images_are_frozen_with_page_locations(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0,
            uploads=[_upload("图文方案.pdf", _text_and_image_pdf())],
        )
        raw = self.adapter.service.read_structured(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=result["brief_key"],
        )
        self.assertEqual(len(raw["images"]), 1)
        self.assertEqual(
            [link["evidence"]["location"]["page"] for link in raw["images"][0]["source_links"]],
            [1, 2],
        )
        self.assertTrue(all(
            link["evidence"]["location"]["image_index"] == 1
            for link in raw["images"][0]["source_links"]
        ))
        digest = raw["images"][0]["content_sha256"]
        self.assertEqual(self.adapter.store.image_path(digest).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_narrative_text_is_understood_with_exact_source_evidence(self):
        calls = []

        def understand(prompt, model, max_tokens):
            calls.append((prompt, model, max_tokens))
            return json.dumps({"facts": [
                {"field": "项目名称", "value": "60s 挑战赛", "evidence": [{"key": "E0001", "quote": "60s 挑战赛"}]},
                {"field": "目标时长", "value": "60 秒", "evidence": [{"key": "E0001", "quote": "60 秒"}]},
                {"field": "视频目标", "value": "展现活动挑战流程与现场氛围", "evidence": [{"key": "E0002", "quote": "展现活动挑战流程与现场氛围"}]},
                {"field": "使用场景", "value": "未提供", "evidence": []},
            ]}, ensure_ascii=False)

        adapter = FormalBriefInputAdapter(
            self.adapter.service,
            data_root=self.root / "data",
            understanding_policy=FormalBriefUnderstandingPolicy(
                invoke=understand, provider="deepseek-http", model="deepseek-v4-flash",
            ),
        )
        result = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=[_upload("活动方案.txt", "60s 挑战赛将制作 60 秒活动视频。\n视频需要展现活动挑战流程与现场氛围。")],
            expected_project_version=0,
        )
        facts = {item["label"]: item for item in result["brief"]["facts"]}
        self.assertEqual(formal_json_value(facts["项目名称"]["value"]), "60s 挑战赛")
        self.assertEqual(formal_json_value(facts["目标时长"]["value"]), "60 秒")
        self.assertEqual(facts["视频目标"]["sources"], ["活动方案.txt · 第 2 段"])
        self.assertNotIn("使用场景", facts)
        self.assertEqual(calls[0][1], "deepseek-v4-flash")
        self.assertNotIn(str(self.root), calls[0][0])
        self.assertNotIn("source_doc_", calls[0][0])

    def test_understanding_rejects_fabricated_evidence_before_database_write(self):
        def fabricated(_prompt, _model, _max_tokens):
            return json.dumps({"facts": [{
                "field": "项目名称", "value": "伪造项目",
                "evidence": [{"key": "E0001", "quote": "原文不存在的句子"}],
            }]}, ensure_ascii=False)

        adapter = FormalBriefInputAdapter(
            self.adapter.service, data_root=self.root / "data",
            understanding_policy=FormalBriefUnderstandingPolicy(
                invoke=fabricated, provider="deepseek-http", model="deepseek-v4-flash",
            ),
        )
        with self.store.connection() as connection:
            before = connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0]
        with self.assertRaisesRegex(FormalBriefInputError, "BRIEF_UNDERSTANDING_EVIDENCE_INVALID"):
            adapter.import_uploads(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                uploads=[_upload("narrative.txt", "真实资料只有这一句")], expected_project_version=0,
            )
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM brief_versions").fetchone()[0], before)
        self.assertEqual(self._final_artifacts(), [])

    def test_explicit_specific_type_cannot_be_relabelled_as_video_goal(self):
        calls = []

        def understand(prompt, _model, _max_tokens):
            calls.append(prompt)
            return json.dumps({"facts": [
                {"field": "视频目标", "value": "明星活动类", "evidence": [{"key": "E0002", "quote": "明星活动类"}]},
                {"field": "行业或活动类别", "value": "明星活动类", "evidence": [{"key": "E0002", "quote": "明星活动类"}]},
            ]}, ensure_ascii=False)

        adapter = FormalBriefInputAdapter(
            self.adapter.service, data_root=self.root / "data",
            understanding_policy=FormalBriefUnderstandingPolicy(
                invoke=understand, provider="deepseek-http", model="deepseek-v4-flash",
            ),
        )
        result = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0,
            uploads=[_upload("brief.txt", "脚本类型: 活动短视频\n具体类型: 明星活动类\n目标时长: 60s")],
        )
        facts = {
            item["label"]: formal_json_value(item["value"])
            for item in result["brief"]["facts"]
        }
        self.assertEqual(facts["脚本类型"], "活动短视频")
        self.assertEqual(facts["具体类型"], "明星活动类")
        self.assertNotIn("视频目标", facts)
        self.assertIn("具体类型", calls[0])
        self.assertIn("行业或活动类别", calls[0])

    def test_positive_special_requirement_cannot_be_reversed_into_prohibition(self):
        def understand(_prompt, _model, _max_tokens):
            return json.dumps({"facts": [{
                "field": "禁止或不能出现的内容",
                "value": "需要以明星的歌曲作为视频背景音乐",
                "evidence": [{"key": "E0002", "quote": "需要以明星的歌曲作为视频背景音乐"}],
            }]}, ensure_ascii=False)

        adapter = FormalBriefInputAdapter(
            self.adapter.service, data_root=self.root / "data",
            understanding_policy=FormalBriefUnderstandingPolicy(
                invoke=understand, provider="deepseek-http", model="deepseek-v4-flash",
            ),
        )
        result = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0,
            uploads=[_upload(
                "brief.txt",
                "特殊需求:\n需要以明星的歌曲作为视频背景音乐",
            )],
        )
        facts = {
            item["label"]: formal_json_value(item["value"])
            for item in result["brief"]["facts"]
        }
        self.assertEqual(facts["特殊需求"], "需要以明星的歌曲作为视频背景音乐")
        self.assertNotIn("必须出现的内容", facts)
        self.assertNotIn("禁止或不能出现的内容", facts)


    def test_exact_retry_is_idempotent_and_zero_fact_import_can_be_reprocessed(self):
        narrative = "这是 60s 挑战赛的活动方案。"
        first = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=[_upload("brief.txt", narrative)], expected_project_version=0,
        )
        self.assertEqual(first["brief"]["facts"], [])
        calls = []

        def understand(_prompt, _model, _max_tokens):
            calls.append("called")
            return json.dumps({"facts": [{
                "field": "项目名称", "value": "60s 挑战赛",
                "evidence": [{"key": "E0001", "quote": "60s 挑战赛"}],
            }]}, ensure_ascii=False)

        adapter = FormalBriefInputAdapter(
            self.adapter.service, data_root=self.root / "data",
            understanding_policy=FormalBriefUnderstandingPolicy(
                invoke=understand, provider="deepseek-http", model="deepseek-v4-flash",
            ),
        )
        reprocessed = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=[_upload("brief.txt", narrative)], expected_project_version=1,
        )
        self.assertEqual(reprocessed["import_status"], "created")
        self.assertNotEqual(reprocessed["brief_key"], first["brief_key"])
        retry = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            uploads=[_upload("brief.txt", narrative)], expected_project_version=2,
        )
        self.assertEqual(retry["import_status"], "already_imported")
        self.assertEqual(retry["brief_key"], reprocessed["brief_key"])
        self.assertEqual(calls, ["called"])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_files WHERE project_id=?", (self.first["project_id"],)).fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM brief_versions WHERE project_id=?", (self.first["project_id"],)).fetchone()[0], 2)

    def test_rejected_auth_lock_and_malformed_uploads_leave_no_final_artifact(self):
        baseline = self._final_artifacts()
        with self.store.connection() as connection:
            database_before = list(connection.iterdump())
        kwargs = {"workspace_id": self.workspace_id, "project_id": self.first["project_id"], "uploads": [UploadedDocument("valid.txt", _NeverReadStream())], "expected_project_version": 0}
        for token, changed in (("invalid-token", {}), (self.token, {"workspace_id": "other-workspace"}), (self.token, {"expected_project_version": 9})):
            with self.assertRaises(WorkspaceServiceError):
                self.adapter.import_uploads(token, **(kwargs | changed))
            self.assertEqual(self._final_artifacts(), baseline)
            with self.store.connection() as connection:
                self.assertEqual(list(connection.iterdump()), database_before)
        with self.assertRaisesRegex(FormalBriefInputError, "UPLOAD_REJECTED"):
            self.adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[_upload("damaged.pdf", b"not a PDF")])
        self.assertEqual(self._final_artifacts(), baseline)
        with self.store.connection() as connection:
            self.assertEqual(list(connection.iterdump()), database_before)

    def test_content_magic_zip_and_image_safety_reject_before_promote(self):
        baseline = self._final_artifacts()
        with self.store.connection() as connection:
            database_before = list(connection.iterdump())
        def rejected(filename, content):
            with self.assertRaisesRegex(FormalBriefInputError, "UPLOAD_REJECTED"):
                self.adapter.import_uploads(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0, uploads=[_upload(filename, content)])
            self.assertEqual(self._final_artifacts(), baseline)
            with self.store.connection() as connection:
                self.assertEqual(list(connection.iterdump()), database_before)

        rejected("named.pdf", _image_bytes("PNG"))
        rejected("unsafe.docx", _office_zip("../word/document.xml", "x"))
        with patch("mvp_v2.formal_brief_input_adapter._MAX_ZIP_MEMBERS", 1):
            rejected("many.docx", _office_zip("word/document.xml", "<w:document xmlns:w=\"w\"/>", {"word/extra.xml": "x"}))
        with patch("mvp_v2.formal_brief_input_adapter._MAX_ZIP_MEMBER_BYTES", 8):
            rejected("member.docx", _office_zip("word/document.xml", "x" * 32))
        with patch("mvp_v2.formal_brief_input_adapter._MAX_ZIP_MEMBER_BYTES", 128), patch("mvp_v2.formal_brief_input_adapter._MAX_ZIP_TOTAL_BYTES", 16):
            rejected("total.docx", _office_zip("word/document.xml", "x" * 12, {"word/extra.xml": "x" * 12}))
        with patch("mvp_v2.formal_brief_input_adapter._MAX_ZIP_COMPRESSION_RATIO", 2):
            rejected("ratio.docx", _office_zip("word/document.xml", "x" * 512))
        rejected("encrypted.docx", _encrypted_office_zip())
        with patch("mvp_v2.formal_brief_input_adapter._MAX_IMAGE_PIXELS", 10):
            rejected("large.png", _image_bytes("PNG"))

    def _final_artifacts(self):
        root = self.root / "data" / "artifacts" / "formal-inputs"
        return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and ".staging" not in path.parts)

    def _field_update_fixture(self):
        result = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_version=0, uploads=[
                _upload("原始需求.txt", "\n".join((
                    "项目名称：匿名甲", "脚本类型：发布会", "具体类型：产品发布活动",
                    "目标时长：60秒", "视频目标：说明产品重点", "风格调性：克制，真实",
                    "必须出现的内容：产品演示", "关键事件与流程：嘉宾发布",
                    "禁止或不能出现的内容：不得虚构事实",
                ))),
                _upload("补充需求.txt", "项目名称：匿名乙\n目标时长：90秒"),
            ],
        )
        for project_version, field in enumerate(("项目名称", "目标时长"), start=1):
            conflict = next(item for item in result["brief"]["conflicts"]
                if item["label"] == field and item["resolution_status"] != "resolved")
            result = self.adapter.resolve_conflict(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                brief_version_id=result["brief_key"], conflict_id=conflict["conflict_key"],
                selected_fact_id=conflict["choices"][0]["fact_key"],
                expected_project_version=project_version,
            )
        return result

    def _raw_brief(self, result):
        return self.adapter.service.read_structured(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=result["brief_key"])

    def _brief_field_signature(self, raw, excluded=()):
        facts = {fact["fact_id"]: fact for fact in raw["facts"]}
        return {
            "facts": sorted(json.dumps({key: value for key, value in fact.items()
                if key != "fact_id"}, sort_keys=True) for fact in raw["facts"]
                if fact["field_path"] not in excluded),
            "conflicts": sorted(json.dumps({
                **{key: value for key, value in item.items()
                    if key not in {"conflict_id", "fact_ids", "resolved_fact_id"}},
                "values": sorted(facts[fact_id]["value_json"] for fact_id in item["fact_ids"]),
                "selected_value": facts[item["resolved_fact_id"]]["value_json"]
                    if item.get("resolved_fact_id") else None,
            }, sort_keys=True) for item in raw["conflicts"] if item["field_path"] not in excluded),
        }

    def _database_dump(self):
        with self.store.connection() as connection:
            return list(connection.iterdump())

    def test_field_update_replaces_one_value_and_preserves_other_resolved_choices(self):
        prior = self._field_update_fixture()
        previous = self._raw_brief(prior)
        calls = []

        def forbidden_understanding(*args):
            calls.append(args)
            raise AssertionError("显式字段修改不得重新调用需求理解")

        editor = FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data",
            understanding_policy=FormalBriefUnderstandingPolicy(
                invoke=forbidden_understanding, provider="anonymous", model="anonymous"))
        edited = editor.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value="小红书，自然", expected_project_version=3)
        current = self._raw_brief(edited)
        self.assertEqual(edited["field_update_status"], "updated")
        self.assertEqual(edited["project_version"], 4)
        self.assertEqual(current["parent_brief_version_id"], prior["brief_key"])
        self.assertEqual(current["confirmation_status"], "pending")
        self.assertEqual(self._brief_field_signature(previous, ("风格调性",)),
            self._brief_field_signature(current, ("风格调性",)))
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "风格调性"], ["小红书，自然"])

        again = editor.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=edited["brief_key"],
            field_label="视频目标", value="说明用户收益", expected_project_version=4)
        self.assertEqual(self._brief_field_signature(current, ("视频目标",)),
            self._brief_field_signature(self._raw_brief(again), ("视频目标",)))
        confirmed = editor.confirm(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=again["brief_key"],
            confirmed_at="2026-10-08T00:00:00Z", expected_project_version=5)
        pair = self.adapter.service.resolve_current_paired(self.token,
            workspace_id=self.workspace_id, project_id=self.first["project_id"])
        payload = self.adapter.service.resolve_confirmed(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], confirmed=pair.brief)
        self.assertEqual(confirmed["confirmation_status"], "confirmed")
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in payload["facts"]
            if fact["field_path"] == "风格调性"], ["小红书，自然"])
        self.assertEqual(calls, [])

    def test_field_update_removes_only_target_conflict_and_handles_employee_aliases(self):
        prior = self._field_update_fixture()
        previous = self._raw_brief(prior)
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="视频时长", value="45秒", expected_project_version=3)
        current = self._raw_brief(edited)
        self.assertEqual(edited["updated_field"], "目标时长")
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "目标时长"], ["45秒"])
        self.assertEqual([conflict["field_path"] for conflict in current["conflicts"]], ["项目名称"])
        self.assertEqual(self._brief_field_signature(previous, ("目标时长",)),
            self._brief_field_signature(current, ("目标时长",)))
        typed = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=edited["brief_key"],
            field_label="具体类型", value="匿名科技发布活动", expected_project_version=4)
        self.assertEqual(typed["updated_field"], "行业或活动类别")
        adopted = {fact["label"]: formal_json_value(fact["value"])
            for fact in typed["brief"]["facts"] if fact.get("adopted")}
        self.assertEqual(adopted["具体类型"], "匿名科技发布活动")
        self.assertEqual(adopted["脚本类型"], "发布会")
        self.assertEqual(typed["routing"]["script_type"], "发布会")

    def test_field_update_history_survives_refresh_without_reentering_canonical_facts(self):
        prior = self._field_update_fixture()
        original = self._raw_brief(prior)
        result = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value="小红书，自然", expected_project_version=3)
        result = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=result["brief_key"],
            field_label="风格调性", value="自然，明亮", expected_project_version=4)
        refreshed = self.adapter.read_current_formal(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"])
        self.assertEqual(result["brief"], refreshed["brief"])
        styles = [fact for fact in refreshed["brief"]["facts"] if fact["label"] == "风格调性"]
        self.assertEqual([formal_json_value(fact["value"]) for fact in styles if fact["adopted"]], ["自然，明亮"])
        self.assertEqual({formal_json_value(fact["value"]) for fact in styles if fact.get("historical")},
            {"克制，真实", "小红书，自然"})
        self.assertTrue(all(not fact["adopted"] and fact["sources"] for fact in styles if fact.get("historical")))
        self.assertTrue(any("原始需求.txt" in source for fact in styles if fact.get("historical") for source in fact["sources"]))
        identities = [(fact["label"], fact["value"], tuple(fact["sources"])) for fact in refreshed["brief"]["facts"]]
        self.assertEqual(len(identities), len(set(identities)))
        self.assertEqual(self._raw_brief(prior), original)
        raw = self._raw_brief(result)
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in raw["facts"]
            if fact["field_path"] == "风格调性"], ["自然，明亮"])
        self.assertFalse(any("historical" in fact for fact in raw["facts"]))
        encoded = json.dumps(refreshed, ensure_ascii=False)
        for forbidden in (str(self.root), "artifact://", "source_doc_", "content_sha256"):
            self.assertNotIn(forbidden, encoded)

    def test_field_update_does_not_parse_multiline_value_into_other_fields(self):
        prior = self._field_update_fixture()
        value = "自然清晰\n脚本类型：明星活动\n目标时长：0秒"
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value=value, expected_project_version=3)
        current = self._raw_brief(edited)
        self.assertEqual(self._brief_field_signature(self._raw_brief(prior), ("风格调性",)),
            self._brief_field_signature(current, ("风格调性",)))
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "风格调性"], [value])
        self.assertEqual(edited["routing"]["script_type"], "发布会")

    def test_field_update_invalid_noop_and_rollback_preserve_versions_sources_and_history(self):
        prior = self._field_update_fixture()
        raw = self._raw_brief(prior)
        before = self._database_dump()
        files = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for field, value in (("未知字段", "新值"), (None, "新值"), ([], "新值"),
                             ("风格调性", ""), ("风格调性", " \n"), ("风格调性", None),
                             ("风格调性", []), ("风格调性", "字" * 4001)):
            with self.subTest(field=field, value=repr(value)[:40]), self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
                self.adapter.update_field(self.token, workspace_id=self.workspace_id,
                    project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
                    field_label=field, value=value, expected_project_version=3)
            self.assertEqual(self._database_dump(), before)
        for brief_key, version in ((raw["parent_brief_version_id"], 3), ("missing", 3), (prior["brief_key"], 2)):
            with self.subTest(brief_key=brief_key, version=version), self.assertRaisesRegex(WorkspaceServiceError, "VERSION_CONFLICT"):
                self.adapter.update_field(self.token, workspace_id=self.workspace_id,
                    project_id=self.first["project_id"], brief_version_id=brief_key,
                    field_label="风格调性", value="新值", expected_project_version=version)
            self.assertEqual(self._database_dump(), before)
        value = next(formal_json_value(fact["value_json"]) for fact in raw["facts"] if fact["field_path"] == "风格调性")
        same = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value=value, expected_project_version=3)
        self.assertEqual(same["field_update_status"], "unchanged")
        self.assertEqual(same["brief_key"], prior["brief_key"])
        self.assertEqual(same["project_version"], 3)
        self.assertEqual(self._database_dump(), before)

        def fail_transaction(*_args):
            raise RuntimeError("anonymous-field-rollback")

        with self.assertRaisesRegex(RuntimeError, "anonymous-field-rollback"):
            self.adapter.update_field(self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
                field_label="风格调性", value="事务失败的值", expected_project_version=3,
                transaction_hook=fail_transaction)
        self.assertEqual(self._database_dump(), before)
        self.assertTrue(all(path.is_file() and path.read_bytes() == content for path, content in files.items()))
        self.assertEqual(list((self.adapter.store.root / ".staging").rglob("batch-*")), [])
        self.assertEqual(self._raw_brief(prior), raw)

    def test_unrelated_upload_preserves_unchanged_resolved_field_decisions(self):
        prior = self._field_update_fixture()
        previous = self._raw_brief(prior)
        appended = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=3,
            uploads=[_upload("新补充.txt", "使用场景：活动社交平台")])
        raw = self._raw_brief(appended)
        self.assertEqual(self._brief_field_signature(previous),
            self._brief_field_signature(raw, ("使用场景",)))
        self.assertEqual(raw["parent_brief_version_id"], prior["brief_key"])
        self.assertEqual(appended["unresolved_conflict_count"], 0)
        confirmed = self.adapter.confirm(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=appended["brief_key"],
            confirmed_at="2026-10-08T00:00:00Z", expected_project_version=4)
        self.assertEqual(confirmed["confirmation_status"], "confirmed")

    def test_unrelated_upload_after_field_update_keeps_explicitly_replaced_values(self):
        prior = self._field_update_fixture()
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value="自然，明亮", expected_project_version=3)
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=edited["brief_key"],
            field_label="视频目标", value="说明用户收益", expected_project_version=4)
        previous = self._raw_brief(edited)
        appended = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=5,
            uploads=[_upload("平台补充.txt", "使用场景：活动社交平台")])
        current = self._raw_brief(appended)
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "风格调性"], ["自然，明亮"])
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "视频目标"], ["说明用户收益"])
        self.assertEqual(self._brief_field_signature(previous),
            self._brief_field_signature(current, ("使用场景",)))
        self.assertEqual(appended["unresolved_conflict_count"], 0)

    def test_employee_history_failure_is_visible_without_hiding_current_field_or_auth_errors(self):
        prior = self._field_update_fixture()
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value="明亮", expected_project_version=3)
        original_read = self.adapter.service.read_structured

        for code in ("NOT_FOUND", "BRIEF_ARTIFACT_UNAVAILABLE", "ARTIFACT_HASH_MISMATCH"):
            def missing_parent(*args, **kwargs):
                if kwargs.get("brief_version_id") == prior["brief_key"]:
                    raise WorkspaceServiceError(code, "anonymous-history-unavailable")
                return original_read(*args, **kwargs)

            with self.subTest(code=code), patch.object(self.adapter.service, "read_structured", side_effect=missing_parent):
                current = self.adapter.read_structured(self.token, workspace_id=self.workspace_id,
                    project_id=self.first["project_id"], brief_version_id=edited["brief_key"])
            self.assertTrue(current["brief"]["history_unavailable"])
            self.assertEqual([formal_json_value(fact["value"]) for fact in current["brief"]["facts"]
                if fact["label"] == "风格调性" and fact["adopted"]], ["明亮"])

        def cyclic_parent(*args, **kwargs):
            raw = original_read(*args, **kwargs)
            if kwargs.get("brief_version_id") == prior["brief_key"]:
                raw["parent_brief_version_id"] = edited["brief_key"]
            return raw

        with patch.object(self.adapter.service, "read_structured", side_effect=cyclic_parent) as reader:
            cyclic = self.adapter.read_structured(self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], brief_version_id=edited["brief_key"])
        self.assertTrue(cyclic["brief"]["history_unavailable"])
        self.assertEqual(reader.call_count, 2)

        def denied_parent(*args, **kwargs):
            if kwargs.get("brief_version_id") == prior["brief_key"]:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "anonymous-denied")
            return original_read(*args, **kwargs)

        with patch.object(self.adapter.service, "read_structured", side_effect=denied_parent), self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.adapter.read_structured(self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], brief_version_id=edited["brief_key"])

    def test_new_source_for_replaced_field_compares_with_current_value_without_reviving_old_value(self):
        prior = self._field_update_fixture()
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="视频目标", value="说明用户收益", expected_project_version=3)
        appended = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=4,
            uploads=[_upload("最新目标.txt", "视频目标：强调报名转化")])
        current = self._raw_brief(appended)
        self.assertEqual({formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "视频目标"}, {"说明用户收益", "强调报名转化"})
        conflict = next(item for item in appended["brief"]["conflicts"] if item["label"] == "视频目标")
        self.assertEqual(conflict["resolution_status"], "unresolved")
        self.assertEqual(len(conflict["choices"]), 2)
        chosen = next(item for item in conflict["choices"] if formal_json_value(item["value"]) == "强调报名转化")
        resolved = self.adapter.resolve_conflict(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=appended["brief_key"],
            conflict_id=conflict["conflict_key"], selected_fact_id=chosen["fact_key"],
            expected_project_version=5)
        later = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=6,
            uploads=[_upload("后续补充.txt", "使用场景：线上活动传播")])
        self.assertEqual(self._brief_field_signature(self._raw_brief(resolved)),
            self._brief_field_signature(self._raw_brief(later), ("使用场景",)))
        self.assertEqual(later["unresolved_conflict_count"], 0)

    def test_replaced_multiline_field_sources_never_inject_other_fields_on_later_import(self):
        prior = self._field_update_fixture()
        first_value = "自然清晰\n\n脚本类型：明星活动\n目标时长：0秒"
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value=first_value, expected_project_version=3)
        appended = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=4,
            uploads=[_upload("首条补充.txt", "使用场景：活动平台传播")])
        current = self._raw_brief(appended)
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "风格调性"], [first_value])
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=appended["brief_key"],
            field_label="风格调性", value="明亮", expected_project_version=5)
        later = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=6,
            uploads=[_upload("后续信息.txt", "品牌或客户：匿名品牌")])
        self.assertEqual(self._brief_field_signature(self._raw_brief(edited)),
            self._brief_field_signature(self._raw_brief(later), ("品牌或客户",)))
        self.assertEqual(later["routing"]["script_type"], "发布会")
        self.assertEqual(later["unresolved_conflict_count"], 0)

    def test_understanding_filters_retired_field_sources_and_literal_edits_but_keeps_new_evidence(self):
        prior = self._field_update_fixture()
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="视频目标", value="说明用户收益", expected_project_version=3)
        calls = []

        def understand(prompt, model, max_tokens):
            calls.append(prompt)
            rows = json.loads(prompt.split("EVIDENCE=", 1)[1])
            evidence = {row["text"]: row for row in rows}
            new = evidence["视频目标：强调报名转化"]
            self.assertNotIn("视频目标：说明产品重点", evidence)
            self.assertNotIn("视频目标：说明用户收益", evidence)
            self.assertNotIn("风格调性：克制，真实", evidence)
            return json.dumps({"facts": [
                {"field": "视频目标", "value": "强调报名转化", "evidence": [{"key": new["key"], "quote": "强调报名转化"}]},
            ]}, ensure_ascii=False)

        editor = FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data",
            understanding_policy=FormalBriefUnderstandingPolicy(invoke=understand,
                provider="anonymous", model="anonymous"))
        appended = editor.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=4,
            uploads=[_upload("最新目标.txt", "视频目标：强调报名转化")])
        current = self._raw_brief(appended)
        self.assertEqual({formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "视频目标"}, {"说明用户收益", "强调报名转化"})
        self.assertEqual([formal_json_value(fact["value_json"]) for fact in current["facts"]
            if fact["field_path"] == "风格调性"], ["克制，真实"])
        self.assertEqual(current["extractions"][:-1], self._raw_brief(edited)["extractions"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("source_doc_", calls[0])

    def test_damaged_historical_payload_does_not_hide_verified_current_field(self):
        prior = self._field_update_fixture()
        edited = self.adapter.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value="明亮", expected_project_version=3)
        with self.store.connection() as connection:
            digest = connection.execute("SELECT payload_sha256 FROM brief_versions WHERE brief_version_id=?",
                (prior["brief_key"],)).fetchone()[0]
        (self.adapter.service.artifacts.root / digest[:2] / f"{digest}.json").write_bytes(b"anonymous-corrupt-history")
        current = self.adapter.read_current_formal(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"])
        self.assertEqual(current["brief_key"], edited["brief_key"])
        self.assertTrue(current["brief"]["history_unavailable"])
        self.assertEqual([formal_json_value(fact["value"]) for fact in current["brief"]["facts"]
            if fact["label"] == "风格调性" and fact["adopted"]], ["明亮"])

    def test_field_update_preserves_pending_choices_and_failed_ocr_without_retrying_them(self):
        prior = self.adapter.import_uploads(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_version=0,
            uploads=_conflicted_uploads() + [_upload("扫描资料.pdf", _scanned_pdf())])
        previous = self._raw_brief(prior)
        calls = []

        def forbidden_ocr(*args):
            calls.append(args)
            raise AssertionError("字段修改不得暗中重试失败 OCR")

        editor = FormalBriefInputAdapter(self.adapter.service, data_root=self.root / "data",
            ocr_policy=FormalBriefOcrPolicy(invoke=forbidden_ocr, provider="anonymous", model="changed"))
        edited = editor.update_field(self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], brief_version_id=prior["brief_key"],
            field_label="风格调性", value="真实，明亮", expected_project_version=1)
        current = self._raw_brief(edited)
        self.assertEqual(previous["extractions"], current["extractions"][:-1])
        self.assertEqual(self._brief_field_signature(previous, ("风格调性",)),
            self._brief_field_signature(current, ("风格调性",)))
        self.assertEqual(edited["unresolved_conflict_count"], 1)
        self.assertTrue(edited["has_failed_extractions"])
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(FormalBriefDomainError, "不可确认"):
            editor.confirm(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                brief_version_id=edited["brief_key"], confirmed_at="2026-10-08T00:00:00Z",
                expected_project_version=2)

    def _assert_http_json(self, value):
        self.assertIsInstance(value, dict)
        json.dumps(value, ensure_ascii=False, allow_nan=False)

        def inspect(item):
            self.assertNotIsInstance(item, (ArtifactPayload, bytes, bytearray, Path))
            self.assertFalse(is_dataclass(item))
            if isinstance(item, dict):
                self.assertTrue(all(isinstance(key, str) for key in item))
                for child in item.values(): inspect(child)
            elif isinstance(item, list):
                for child in item: inspect(child)
        inspect(value)


def _five_uploads():
    png = _image_bytes("PNG")
    return [
        _upload("anonymous.pdf", _text_pdf("Project: Alpha")),
        _upload("anonymous.docx", _office_zip("word/document.xml", "<w:document xmlns:w=\"w\"><w:body><w:p><w:r><w:t>品牌: 匿名品牌</w:t></w:r></w:p></w:body></w:document>", {"word/media/image1.png": png})),
        _upload("anonymous.xlsx", _xlsx_bytes()),
        _upload("anonymous.pptx", _office_zip("ppt/slides/slide1.xml", "<p:sld xmlns:p=\"p\"><p:spTree><p:txBody><a:p xmlns:a=\"a\"><a:r><a:t>关键事件: 产品演示</a:t></a:r></a:p></p:txBody></p:spTree></p:sld>")),
        _upload("anonymous.png", png), _upload("anonymous.jpg", _image_bytes("JPEG")), _upload("anonymous.webp", _image_bytes("WEBP")),
    ]


def _conflicted_uploads():
    return [
        _upload("first.txt", "项目名称: 匿名甲\n风格调性: 克制\n视频目标: 说明重点\n必须出现的内容: 产品\n关键事件与流程: 发布\n禁止或不能出现的内容: 夸张"),
        _upload("second.txt", "项目名称: 匿名乙\n风格调性: 克制\n视频目标: 说明重点\n必须出现的内容: 产品\n关键事件与流程: 发布\n禁止或不能出现的内容: 夸张"),
    ]


def _upload(filename, value): return UploadedDocument(filename, BytesIO(value.encode("utf-8") if isinstance(value, str) else value))


def formal_json_value(value):
    return json.loads(value)


def _image_bytes(kind):
    output = BytesIO(); Image.new("RGB", (20, 10), "blue").save(output, format=kind); return output.getvalue()


def _xlsx_bytes():
    workbook = Workbook(); sheet = workbook.active; sheet.title = "资料"
    for row, value in enumerate(("脚本类型: 发布会", "目标时长: 60秒", "风格调性: 简洁", "必须出现: 产品演示"), start=1): sheet.cell(row, 1, value)
    output = BytesIO(); workbook.save(output); return output.getvalue()


def _office_zip(member, value, extra=None):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(member, value)
        for name, content in (extra or {}).items(): archive.writestr(name, content)
    return output.getvalue()


def _encrypted_office_zip():
    raw = bytearray(_office_zip("word/document.xml", "x"))
    local, central = raw.index(b"PK\x03\x04"), raw.index(b"PK\x01\x02")
    raw[local + 6] |= 1
    raw[central + 8] |= 1
    return bytes(raw)


class _NeverReadStream:
    def read(self, _size):
        raise AssertionError("鉴权或锁失败时不得读取上传流")


def _text_pdf(text):
    writer = PdfWriter(); page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = DecodedStreamObject(); content.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("ascii")); page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO(); writer.write(output); return output.getvalue()


def _scanned_pdf():
    writer = PdfWriter(); writer.add_blank_page(width=612, height=792); output = BytesIO(); writer.write(output); return output.getvalue()


def _text_and_image_pdf():
    output = BytesIO()
    page = canvas.Canvas(output, pagesize=(612, 792))
    image = ImageReader(BytesIO(_image_bytes("PNG")))
    for number in (1, 2):
        page.drawString(72, 720, f"Project: Anonymous page {number}")
        page.drawImage(image, 72, 600, width=120, height=60)
        page.showPage()
    page.save()
    return output.getvalue()
