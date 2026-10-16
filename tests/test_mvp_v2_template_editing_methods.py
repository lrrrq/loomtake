from copy import deepcopy
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as WorkbookImage
from PIL import Image

from mvp_v2 import template_editing_methods
from mvp_v2.template_editing_methods import (
    EVIDENCE_SCHEMA_VERSION,
    SCHEMA_VERSION,
    build_editing_methods,
    method_revision_identity,
    read_method_source,
)


# Historical v2 instructions are fixture data: a new workflow must not silently
# replace them when an existing immutable package is inherited.
_LEGACY_V2_WORKFLOW = {
    "name": "M+ 剪辑脚本规划",
    "purpose": "依据项目事实、素材条件与有来源的剪辑方法组织内容，说明观众理解怎样推进。",
    "steps": [
        {"action": "明确目标、观众和必须表达的内容。", "done_when": "已确认事实、制作要求、建议及未知项分别可追溯。"},
        {"action": "选择方法并核对素材前提、例外及案例边界。", "done_when": "选用、改编或弃用都有本项目依据。"},
        {"action": "组织内容单元，说明新增信息、动作变化或情绪关系。", "done_when": "各单元有真实事实来源或明确取材条件，重复信息已有取舍。"},
        {"action": "依据内容发展和观看目的分配段落预算，再展开镜头。", "done_when": "每镜有观察重点、来源、段落归属及能支撑时长的过程。"},
        {"action": "对照当前稿检查事实、变化、可拍性和声画关系。", "done_when": "问题引用当前原文，局部交写作、全局交原规划，保留正确及已采用内容。"},
    ],
    "roles": {
        "discussion": "用方法与原例证解释不同方案及素材需求；讨论只形成建议。",
        "planning": "先形成内容单元和段落作用，再预算及拆镜；由原规划入口负责取舍。",
        "writing": "落实已冻结镜头意图、画面与声音，保留来源和修改范围。",
        "review": "按本项目条件检查当前稿，引用具体位置和依据返回问题；方法建议不等于新增客户要求。",
    },
    "duration_policy": "短片选择代表内容；长片增加有依据的过程、观察角度与理解变化。原案例未记录秒数时标明未知。",
}


# Immediately preceding frozen instructions, retained as immutable fixture data.
_PRE_HIERARCHY_V2_WORKFLOW = {
    "name": "M+ 剪辑脚本规划",
    "purpose": "在同一份规划内，把已确认事实与有来源的方法展开为具体内容发展，完成取舍后分配预算及拆镜。",
    "steps": [
        {"action": "核对目标、观众、已确认事件与requirement、事实顺序和已采用内容，分别列出项目事实、制作要求、现有素材及未知项。",
         "done_when": "后续引用能回到原事实ID；必现、禁写、时长与已采用保护字段明确，案例与建议仍保留各自来源身份。"},
        {"action": "围绕已确认事件或requirement提出内容发展候选：按需要观察动作如何发生、参与关系、空间与细节、过程前后变化及真实反应，把借用或改编的方法落实到本项目具体画面。",
         "done_when": "每项候选写明原event/requirement ID、方法及适用理由、具体观察内容、新增理解或可观察变化、与前后内容的关系、取材条件，以及该过程或信息需要多少时间和理由；数量随内容发展确定。"},
        {"action": "逐项判断取材依据：缺事实时写明尚未知的事项及受影响内容；计划拍摄时依已确认流程说明要记录的动作、观察条件和现场未知结果；现有素材缺失时说明缺哪段实际素材及可用替代。",
         "done_when": "候选的事实依据与素材可用性分别说明；images为空只说明当前未附图，已确认流程仍可形成计划拍摄内容，具体结果保持待现场记录。"},
        {"action": "比较候选与已有内容，逐项决定选用、改编、合并或舍弃；按观众新增理解及发生前后关系组成内容单元，检查必现覆盖、重复表达和方法例外。",
         "done_when": "每个选中单元有独立的信息、动作或关系推进，舍弃与合并有理由；不同机位只有呈现新增信息或可观察变化时才作为独立内容，换角度本身不计作发展。"},
        {"action": "依据选中内容的动作阶段、信息阅读或关系变化分配段落预算，并与目标总时长核对；存在差距时回看候选及取舍，检查可展开的真实过程、遗漏要求和重复占用。",
         "done_when": "每段秒数对应具体内容与持续过程。仍有差距时准确保留未落实秒数和原因，并区分缺事实、计划拍摄条件与现有素材缺失；不能仅从当前写得少推断客户事实不足，也不靠拉长静态描述填满预算。"},
        {"action": "将已选内容单元展开为可执行镜头，说明每镜观察重点、动作起止、机位、段落归属及具体时长依据；实际切换机位时按真实切镜分别计算。",
         "done_when": "每镜承接已选内容的发展，镜间前后关系清楚，镜头小计与段落预算一致；单镜持续时间由可观察过程支撑，镜数与机位变化不反过来制造事件或填预算。"},
        {"action": "对照当前候选取舍、内容单元与镜头稿检查事实、重复、可拍性、声画关系及预算；引用当前原文，把局部表达问题交写作，把内容展开与预算问题交原规划负责人。",
         "done_when": "修订保留无关正确内容与已采用保护字段，逐项说明问题对应的实际内容变化；只删一句、改秒数或新增缺口声明时，仍未改善的问题保持可见，不以修订动作代替质量结论。"},
    ],
    "roles": {
        "discussion": "用原例证解释候选内容如何增加理解或变化，说明适用条件与取舍；把拍摄建议和待确认事实分开，讨论只形成建议。",
        "planning": "由原规划入口在同一规划内完成事实核对、内容发展候选、取舍、预算及拆镜；选了方法仍须交代本项目具体增加什么。全局修订回到候选与内容关系，不另建一套规划或用逐镜加秒代替内容展开。",
        "writing": "落实已冻结的内容单元与镜头意图，具体写明画面、动作变化和声音，保留来源及修改范围；发现内容或预算缺口时反馈原规划，不自行编造活动、结果或改动保护字段。",
        "review": "按本项目条件核对候选取舍是否落实为新增信息、可观察变化与可拍过程，并核对镜段预算；用当前原文和事实ID指出问题，分别路由局部表达与全局规划，不把无附图当作无可拍内容。",
    },
    "duration_policy": "短片选择能表达关键关系的代表内容；长片展开有事实依据的过程阶段、不同观察信息与理解变化。时长由动作或信息发展的具体依据决定，原案例没有秒数时明确标为未知，不设固定内容单元数、镜数或单镜秒数。",
}

class TemplateEditingMethodsTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "anonymous-template.xlsx"
        self.image_bytes = []
        for color in ("red", "green", "blue"):
            stream = BytesIO()
            Image.new("RGB", (8, 6), color).save(stream, format="PNG")
            self.image_bytes.append(stream.getvalue())

        workbook = Workbook()
        template = workbook.active
        template.title = "模版"
        template["A1"] = "匿名活动参考"
        template.merge_cells("A1:I1")
        template["B2"] = "氛围造境；空间巡游；互动体验；日夜递进；核心信息发布；品牌落版"
        template["H6"] = "通用描述"
        template["H7"] = "先观察匿名装置全貌，让观众认出可参与的对象。"
        template["H8"] = "切近参与者触碰装置的手部，交代怎样参与。"
        template.add_image(WorkbookImage(BytesIO(self.image_bytes[0])), "C7")
        template.add_image(WorkbookImage(BytesIO(self.image_bytes[1])), "C8")

        creative = workbook.create_sheet("创意库")
        creative["I6"] = "通用描述"
        creative["I7"] = "回到参与者与同伴的反应，呈现触碰后的交流。"
        creative["I8"] = "另一个空间展示备选，不属于已选参与过程。"
        creative.add_image(WorkbookImage(BytesIO(self.image_bytes[2])), "D7")
        specification = workbook.create_sheet("AI_规范表")
        specification["A1"] = "derived-v1"
        workbook.save(self.path)
        workbook.close()

        self.snapshot = read_method_source(self.path)
        self.source = {
            "source_file": self.path.name,
            "workbook_sha256": self.snapshot["workbook_sha256"],
            "original_workbook_sha256": self.snapshot["workbook_sha256"],
            "sheets": [{"source_sheet": "模版", "overview_cells": [
                {"cell": "B2", "text": template["B2"].value},
            ]}],
            "template_rows": [
                {"source_sheet": "模版", "source_row": 7, "shot_function": "核心信息发布"},
                {"source_sheet": "模版", "source_row": 8, "shot_function": "印证内容吸引力"},
            ],
        }
        self.evidence = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "category_id": "anonymous_category",
            "source_workbook": {
                "file_name": self.path.name,
                "sha256": self.snapshot["workbook_sha256"],
                "source_content_sha256": self.snapshot["source_content_sha256"],
            },
            "methods": [{
                "method_id": "anonymous_category.observe_participate_respond",
                "purpose": "让观众从看懂对象转向看懂参与方式与现场反应。",
                "audience_change": "从知道有什么，变为理解怎样参与及参与后的交流。",
                "applies_when": ["当前项目有可见的装置与参与过程。"],
                "material_conditions": ["有同一项目的装置、触碰动作和人物反应素材。"],
                "guidance": ["先明确对象，再交代参与动作，最后呈现真实反应。"],
                "exceptions": ["缺少反应素材时不推定参与者满意。"],
                "duration_adaptation": {
                    "short": "推导：保留对象、动作和反应的关键关系。",
                    "long": "推导：增加有依据的观察与参与过程。",
                    "boundary": "原例没有逐镜时长，不规定固定镜数或秒数。",
                },
                "review_questions": ["反应是否对应前面的参与行为？"],
                "sequence": [
                    {"role": "观察对象", "source_sheet": "模版", "source_rows": [7],
                     "information_change": "看清可以参与的对象。"},
                    {"role": "参与动作", "source_sheet": "模版", "source_rows": [8],
                     "information_change": "理解怎样接触对象。"},
                    {"role": "实际反应", "source_sheet": "创意库", "source_rows": [7],
                     "information_change": "看到参与后的人物交流。"},
                ],
                "evidence_refs": [
                    {"source_sheet": "模版", "cell": "H7", "quote": "先观察匿名装置全貌"},
                    {"source_sheet": "模版", "cell": "H8", "quote": "切近参与者触碰装置的手部"},
                    {"source_sheet": "创意库", "cell": "I7", "quote": "回到参与者与同伴的反应"},
                ],
                "visual_evidence": [
                    {"source_sheet": sheet, "anchor_row": row, "anchor_col": col,
                     "image_sha256": sha256(blob).hexdigest(),
                     "observation": "匿名色块仅用于验证图片身份与锚点。",
                     "limitation": "此测试图不能证明实际剪辑质量。"}
                    for sheet, row, col, blob in (
                        ("模版", 7, 3, self.image_bytes[0]),
                        ("模版", 8, 3, self.image_bytes[1]),
                        ("创意库", 7, 4, self.image_bytes[2]),
                    )
                ],
                "evidence_kind": "source_interpretation",
                "case_fact_boundary": ["案例人物和装置不作为新项目事实。"],
                "external_basis": [],
            }],
            "source_issues": [],
            "coverage": {"read_sheets": ["模版", "创意库"]},
        }

    def _build(self, evidence=None, snapshot=None):
        return build_editing_methods(
            self.source, "anonymous_category",
            evidence=self.evidence if evidence is None else evidence,
            source_snapshot=self.snapshot if snapshot is None else snapshot,
        )

    def _changed_workbook(self, mutate):
        changed = Path(self.directory.name) / "changed.xlsx"
        workbook = load_workbook(self.path)
        mutate(workbook)
        workbook.save(changed)
        workbook.close()
        return read_method_source(changed)

    def test_category_and_legacy_keywords_do_not_create_methods_without_evidence(self):
        for category in ("launch", "annual", "exhibition", "pop_up_activity", "new_category"):
            with self.subTest(category=category):
                result = build_editing_methods(self.source, category, source_snapshot=self.snapshot)
                self.assertEqual(result["status"], "not_recorded")
                self.assertEqual(result["methods"], [])
                self.assertEqual(result["coverage"]["method_count"], 0)
                self.assertEqual(result["source_binding"]["workbook_sha256"],
                                 self.snapshot["workbook_sha256"])
        self.assertEqual(build_editing_methods({}, "sports")["status"], "not_recorded")

    def test_multiple_source_rows_and_images_are_bound_to_their_exact_locations(self):
        result = self._build()
        self.assertEqual(result["schema_version"], SCHEMA_VERSION)
        self.assertEqual(result["status"], "evidence_bound")
        self.assertEqual(result["coverage"]["method_count"], 1)
        method = result["methods"][0]
        refs = method["evidence_refs"]
        self.assertEqual([(ref["source_sheet"], ref["cell"], ref["source_row"]) for ref in refs],
                         [("模版", "H7", 7), ("模版", "H8", 8), ("创意库", "I7", 7)])
        self.assertEqual([ref["quote"] for ref in refs],
                         [ref["quote"] for ref in self.evidence["methods"][0]["evidence_refs"]])
        self.assertTrue(all(ref["source_file"] == self.path.name for ref in refs))
        self.assertTrue(all(ref["workbook_sha256"] == self.snapshot["workbook_sha256"] for ref in refs))
        self.assertEqual(method["visual_evidence"], self.evidence["methods"][0]["visual_evidence"])
        self.assertEqual(method["sequence"], self.evidence["methods"][0]["sequence"])
        self.assertFalse(result["fact_policy"]["project_facts"])
        self.assertEqual(method["fact_policy"], "editorial_guidance_only")
        images = [(sheet["source_sheet"], item["anchor_row"], item["anchor_col"], item["image_sha256"])
                  for sheet in self.snapshot["sheets"] for item in sheet["images"]]
        self.assertCountEqual(images, [
            ("模版", 7, 3, sha256(self.image_bytes[0]).hexdigest()),
            ("模版", 8, 3, sha256(self.image_bytes[1]).hexdigest()),
            ("创意库", 7, 4, sha256(self.image_bytes[2]).hexdigest()),
        ])

    def test_evidence_requires_a_verified_workbook_snapshot(self):
        with self.assertRaisesRegex(ValueError, "verified workbook source"):
            build_editing_methods(self.source, "anonymous_category", evidence=self.evidence)

    def test_wrong_quote_or_quote_at_the_wrong_cell_is_rejected(self):
        for change in ("text", "cell"):
            with self.subTest(change=change):
                evidence = deepcopy(self.evidence)
                ref = evidence["methods"][0]["evidence_refs"][0]
                if change == "text":
                    ref["quote"] = "原表没有记载的彩排与仪式"
                else:
                    ref["cell"] = "H8"
                with self.assertRaisesRegex(ValueError, "quote does not match"):
                    self._build(evidence=evidence)

    def test_empty_source_row_and_existing_but_uncited_row_are_rejected(self):
        for row, error in ((99, "empty source row"), (8, "lacks cited row evidence")):
            with self.subTest(row=row):
                evidence = deepcopy(self.evidence)
                evidence["methods"][0]["sequence"][-1]["source_rows"] = [row]
                with self.assertRaisesRegex(ValueError, error):
                    self._build(evidence=evidence)

    def test_another_embedded_image_cannot_be_claimed_at_the_wrong_anchor(self):
        for change in ("digest", "anchor"):
            with self.subTest(change=change):
                evidence = deepcopy(self.evidence)
                visual = evidence["methods"][0]["visual_evidence"][0]
                if change == "digest":
                    visual["image_sha256"] = sha256(self.image_bytes[1]).hexdigest()
                else:
                    visual["anchor_row"] = 8
                with self.assertRaisesRegex(ValueError, "image identity does not match"):
                    self._build(evidence=evidence)

    def test_category_and_schema_mismatch_are_rejected(self):
        for key, value in (("category_id", "another_category"), ("schema_version", "obsolete/v1")):
            with self.subTest(key=key):
                evidence = deepcopy(self.evidence)
                evidence[key] = value
                with self.assertRaisesRegex(ValueError, "category/schema mismatch"):
                    self._build(evidence=evidence)

    def test_ai_specification_only_change_can_reuse_verified_human_source_content(self):
        snapshot = self._changed_workbook(lambda book: book["AI_规范表"].__setitem__("A1", "derived-v2"))
        self.assertNotEqual(snapshot["workbook_sha256"], self.snapshot["workbook_sha256"])
        self.assertEqual(snapshot["source_content_sha256"], self.snapshot["source_content_sha256"])
        self.assertEqual(self._build(snapshot=snapshot)["status"], "evidence_bound")
        evidence_without_content_proof = deepcopy(self.evidence)
        evidence_without_content_proof["source_workbook"].pop("source_content_sha256")
        with self.assertRaisesRegex(ValueError, "workbook content changed"):
            self._build(evidence=evidence_without_content_proof, snapshot=snapshot)

    def test_changed_human_text_or_image_rejects_old_evidence_even_when_quotes_still_match(self):
        def change_uncited_text(book):
            book["创意库"]["I8"] = "新增未在旧证据中引用的人用内容。"

        def replace_image(book):
            book["模版"]._images[0] = WorkbookImage(BytesIO(self.image_bytes[2]))
            book["模版"]._images[0].anchor = "C7"

        for change in (change_uncited_text, replace_image):
            with self.subTest(change=change.__name__):
                snapshot = self._changed_workbook(change)
                self.assertNotEqual(snapshot["source_content_sha256"],
                                    self.snapshot["source_content_sha256"])
                for ref in self.evidence["methods"][0]["evidence_refs"]:
                    sheet = next(sheet for sheet in snapshot["sheets"]
                                 if sheet["source_sheet"] == ref["source_sheet"])
                    self.assertIn(ref["quote"], sheet["cells"][ref["cell"]])
                with self.assertRaisesRegex(ValueError, "workbook content changed"):
                    self._build(snapshot=snapshot)

    def test_projection_is_deterministic_preserves_inputs_and_does_not_write_the_workbook(self):
        before_bytes = self.path.read_bytes()
        before_mtime = self.path.stat().st_mtime_ns
        before_inputs = deepcopy((self.source, self.evidence, self.snapshot))
        first = self._build()
        self.assertEqual(first, self._build())
        first["methods"][0]["evidence_refs"][0]["quote"] = "caller changed"
        first["methods"][0]["visual_evidence"][0]["observation"] = "caller changed"
        first["workflow"]["steps"][0]["action"] = "caller changed"
        first["fact_policy"]["project_facts"] = True
        second = self._build()
        self.assertNotEqual(second["methods"][0]["evidence_refs"][0]["quote"], "caller changed")
        self.assertNotEqual(second["workflow"]["steps"][0]["action"], "caller changed")
        self.assertFalse(second["fact_policy"]["project_facts"])
        self.assertEqual((self.source, self.evidence, self.snapshot), before_inputs)
        self.assertEqual(read_method_source(self.path), self.snapshot)
        self.assertEqual(self.path.read_bytes(), before_bytes)
        self.assertEqual(self.path.stat().st_mtime_ns, before_mtime)

    def test_evidence_content_changes_revision_identity(self):
        first = self._build()["revision_identity"]
        changed = deepcopy(self.evidence)
        changed["methods"][0]["duration_adaptation"]["long"] += "增加新的观察理由。"
        second = self._build(evidence=changed)["revision_identity"]
        self.assertNotEqual(first["evidence_sha256"], second["evidence_sha256"])
        self.assertEqual(first["workflow_sha256"], second["workflow_sha256"])
        self.assertEqual(first, method_revision_identity(deepcopy(self.evidence)))

    def test_workflow_content_changes_revision_identity(self):
        first = self._build()["revision_identity"]
        workflow = deepcopy(template_editing_methods._WORKFLOW)
        workflow["steps"][0]["done_when"] += "匿名修改后的完成条件。"
        with patch.object(template_editing_methods, "_WORKFLOW", workflow):
            second = self._build()["revision_identity"]
        self.assertNotEqual(first["workflow_sha256"], second["workflow_sha256"])
        self.assertEqual(first["evidence_sha256"], second["evidence_sha256"])
        self.assertEqual(first, self._build()["revision_identity"])

    def test_new_content_workflow_has_distinct_identity_and_preserves_frozen_v2_bundle(self):
        for label, workflow in (("original_v2", _LEGACY_V2_WORKFLOW),
                                ("before_hierarchy", _PRE_HIERARCHY_V2_WORKFLOW)):
            with self.subTest(frozen_workflow=label):
                with patch.object(template_editing_methods, "_WORKFLOW", workflow):
                    frozen = self._build()
                frozen_json = json.dumps(frozen, ensure_ascii=False, sort_keys=True)
                frozen = json.loads(frozen_json)

                current = self._build()
                self.assertEqual(current["schema_version"], frozen["schema_version"])
                self.assertEqual(current["schema_version"], "mplus-template-editing-methods/v2")
                self.assertNotEqual(current["workflow"], frozen["workflow"])
                self.assertNotEqual(current["revision_identity"]["workflow_sha256"],
                                    frozen["revision_identity"]["workflow_sha256"])
                for key in ("evidence_sha256", "fact_policy_sha256"):
                    self.assertEqual(current["revision_identity"][key], frozen["revision_identity"][key])
                for key in ("methods", "source_binding", "fact_policy", "coverage"):
                    self.assertEqual(current[key], frozen[key])
                self.assertEqual(method_revision_identity(self.evidence), current["revision_identity"])
                self.assertEqual(method_revision_identity(compiled=frozen), frozen["revision_identity"])

                revised_evidence = deepcopy(self.evidence)
                revised_evidence["methods"][0]["review_questions"].append("此方法是否服务当前层级的表达目的？")
                explicit = self._build(evidence=revised_evidence)
                self.assertEqual(explicit["workflow"], current["workflow"])
                self.assertEqual(explicit["revision_identity"], method_revision_identity(revised_evidence))
                self.assertNotEqual(explicit["revision_identity"]["evidence_sha256"],
                                    frozen["revision_identity"]["evidence_sha256"])
                self.assertEqual(json.dumps(frozen, ensure_ascii=False, sort_keys=True), frozen_json)

    def test_outline_workflow_projects_level_completion_and_keeps_shot_rules_separate(self):
        # Check instructions in the package projection, including packages with
        # no reviewed methods. This does not assert a model or runtime follows
        # them; those behaviors belong to planner integration coverage.
        recorded = self._build()
        unrecorded = build_editing_methods(self.source, "anonymous_category")
        self.assertEqual(unrecorded["status"], "not_recorded")
        self.assertEqual(unrecorded["workflow"], recorded["workflow"])
        workflow = recorded["workflow"]
        actions = "\n".join(step["action"] for step in workflow["steps"])
        completion = "\n".join(step["done_when"] for step in workflow["steps"])
        self.assertIn("整片、段落、必要子内容", workflow["purpose"])
        self.assertIn("整片符合Brief、视频类型正确、结构符合所选模板", completion)
        self.assertIn("每个分支达到本层表达目的即停止", completion)
        self.assertIn("只有必要分支继续细分，不同分支可以停在不同层级", completion)
        self.assertIn("大纲不强制动作细节、机位、笑点或每项秒数", completion)
        self.assertIn("仅在进入正式拆镜任务时", actions)
        self.assertIn("镜头小计与段落及整片预算一致", completion)
        self.assertIn("不把缺少动作细节当作大纲质量问题", workflow["roles"]["review"])
        self.assertIn("已确认总时长与明确固定时长始终保留", workflow["duration_policy"])
        self.assertFalse(recorded["fact_policy"]["project_facts"])

    def test_fact_policy_content_changes_identity_without_changing_workflow_or_evidence(self):
        first = self._build()["revision_identity"]
        policy = deepcopy(template_editing_methods._FACT_POLICY)
        policy["application"] += "匿名新增规则：先说明当前素材满足哪些前提。"
        with patch.object(template_editing_methods, "_FACT_POLICY", policy):
            changed = self._build()
            second = changed["revision_identity"]
        self.assertNotEqual(first["fact_policy_sha256"], second["fact_policy_sha256"])
        self.assertEqual(first["workflow_sha256"], second["workflow_sha256"])
        self.assertEqual(first["evidence_sha256"], second["evidence_sha256"])
        self.assertEqual(changed["fact_policy"], policy)
        self.assertEqual(first, self._build()["revision_identity"])

    def test_compiled_identity_uses_frozen_instructions_even_after_code_policy_changes(self):
        compiled = self._build()
        expected = deepcopy(compiled["revision_identity"])
        # Older compiled packages may lack the policy digest or carry a stale
        # workflow digest. Recompute those identities from the actual payload.
        compiled["revision_identity"].pop("fact_policy_sha256")
        compiled["revision_identity"]["workflow_sha256"] = "0" * 64
        before = deepcopy(compiled)
        workflow = deepcopy(template_editing_methods._WORKFLOW)
        workflow["steps"][0]["action"] += "匿名新版动作。"
        policy = deepcopy(template_editing_methods._FACT_POLICY)
        policy["precedence"] += "匿名新版优先条件。"
        with patch.object(template_editing_methods, "_WORKFLOW", workflow), \
                patch.object(template_editing_methods, "_FACT_POLICY", policy):
            inherited = method_revision_identity(compiled=compiled)
            explicit = method_revision_identity(self.evidence)
        self.assertEqual(inherited, expected)
        self.assertNotEqual(explicit["workflow_sha256"], inherited["workflow_sha256"])
        self.assertNotEqual(explicit["fact_policy_sha256"], inherited["fact_policy_sha256"])
        self.assertEqual(explicit["evidence_sha256"], inherited["evidence_sha256"])
        self.assertEqual(compiled, before)


if __name__ == "__main__":
    unittest.main()
