import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mvp_v2.audit import audit_generation
from mvp_v2.creative_formal_binding import bind_plan_rows
from mvp_v2.models import SourceShot, TemplateDecision
from mvp_v2.planning_core import (
    _event_role,
    compile_plan_artifact,
    planning_semantic_input,
    validate_planning_semantics,
)
from mvp_v2.run_contract import sha256_json
from mvp_v2.scoring import assess_planning_requirements
from mvp_v2.template_rules import builtin_template_rules


ANNUAL_EXPRESSIONS = [
    ("氛围造境｜会场外景", "城市天际线与会场外景建立活动规模。"),
    ("氛围造境｜入口通道", "第一视角穿过会场入口通道，揭示主题装置与灯光结构。"),
    ("氛围造境｜品牌光影", "匿名品牌装置与灯光线条多重曝光，形成科技感空间转场。"),
    ("氛围造境｜高光预告", "遮罩滑动呈现会场装置、员工互动与新品剪影，作为视觉预告。"),
    ("嘉年华互动｜舞台表演", "舞台表演者与乐队交叉剪辑，员工在台下互动。"),
    ("嘉年华互动｜装置打卡", "员工在主题装置前打卡，侧跟自然的互动动作。"),
    ("嘉年华互动｜团队竞技", "团队接力与趣味竞技交错呈现，员工协作欢呼。"),
    ("嘉年华互动｜签到仪式感", "侧跟员工在签到墙书写，特写笔尖与主题字，呈现参与仪式感。"),
    ("嘉年华互动｜趣味挑战", "仰拍参与者完成趣味挑战，捕捉员工表情。"),
    ("嘉年华互动｜升空仪式感", "员工合影与装置升空形成向上的画面，营造参与仪式感。"),
    ("颁奖典礼｜日转夜气口", "夕阳穿过舞台桁架与品牌标识，人物与设备形成剪影。"),
    ("颁奖典礼｜会场点亮", "会场从白天延时至夜晚，灯光逐步点亮。"),
    ("颁奖典礼｜舞台启幕", "主舞台大屏显示欢迎语与匿名品牌信息。"),
    ("颁奖典礼｜核心致辞", "主舞台与致辞人近景分屏并置，保留演讲动作。"),
    ("颁奖典礼｜新品亮相", "舞台灯光聚焦，新品以剪影形态正式亮相，突出产品剪影轮廓。"),
    ("颁奖典礼｜员工灯海", "员工举起手机闪光灯，随音乐挥动，形成灯海。"),
    ("颁奖典礼｜观众反应", "观众席全景呈现员工鼓掌，保留现场规模。"),
    ("颁奖典礼｜团队领奖", "优秀团队列队上台领奖，呈现颁奖瞬间与团队合影。"),
    ("颁奖典礼｜高潮倒计时", "舞台黑场显示倒计时，通过推拉镜头进入高潮。"),
    ("派对狂欢｜庆典烟花", "主舞台上空烟花集中绽放，构成庆典全景。"),
    ("派对狂欢｜团队合影", "台上管理者与看台员工完成大规模合影。"),
    ("派对狂欢｜员工欢呼", "全体员工以多层队形齐声欢呼，广角拉出并定格。"),
    ("派对狂欢｜闭幕高潮", "彩纸、冷焰火与舞台烟雾同时出现，切换彩纸微距。"),
    ("派对狂欢｜晚宴收束", "暖色灯带勾勒晚宴会场的空间秩序。"),
    ("派对狂欢｜品牌落版", "黑蓝科技通道中匿名品牌标识居中，完成片尾收官。"),
]


def anonymous_annual_context():
    clauses = "某个新品亮相；新品名称尚未确定；仅以产品剪影表现；不写型号与性能"
    brief = {"项目名称": "匿名年会", "脚本类型": "年会", "品牌或客户": "匿名品牌", "视频时长": "60",
        "必须出现的内容": clauses, "风格调性": "高级、克制、科技感",
        "Brief来源文件": "anonymous-brief.txt", "Brief来源 sheet": "需求", "Brief来源行": 1,
        "_formal_fact_evidence": {"必须出现的内容": {"fact_id": "anonymous-product-boundary"}}}
    blocks = [{"block_id": f"block_{index:02d}", "fields": {"段落": section,
        "画面描述": visual, "声音": "现场环境声与音乐", "时长": "3" if index <= 10 else "2"},
        "source_refs": [{"type": "user_instruction", "input_sha256": "a" * 64}]}
        for index, (section, visual) in enumerate(ANNUAL_EXPRESSIONS, 1)]
    binding = {"draft_id": "anonymous-draft", "draft_revision": 2, "draft_sha256": sha256_json(blocks),
        "payload_ref": "artifact://creative-draft/" + "b" * 64, "payload_sha256": "b" * 64,
        "protected_block_ids": [], "image_references": [], "blocks": blocks}
    slots = [SourceShot("年会", "anonymous-annual-library.xlsx", "AI_规范表", index + 6,
        shot_no=str(index), structure=section.split("｜", 1)[0], duration_seconds="2",
        visual_desc=visual, shot_function=section, visual_subject=visual,
        reuse_level="镜头功能可复用", slot_index=index)
        for index, (section, visual) in enumerate(ANNUAL_EXPRESSIONS, 1)]
    constraints = {"creative_draft_binding": binding,
        "template_rule_contract": {"payload": builtin_template_rules("annual"), "source": "fixture"}}
    return brief, slots, constraints


def annual_semantic_response(prompt, *_):
    data = json.loads(prompt.split("\nINPUT:", 1)[1])
    reveal_id = data["events"][14]["event_id"]
    bindings = []
    for item in data["requirements"]:
        negative = item["text"].startswith(("不写", "不得", "禁止"))
        bindings.append({"requirement_id": item["requirement_id"],
            "event_id": "" if negative else reveal_id,
            "relation": "unresolved" if negative else "same_action" if item["text"] == "某个新品亮相" else "attribute",
            "reason": "否定限制不是正向事件。" if negative else "同一新品亮相画面承载已给出的剪影及身份边界。"})
    arcs = [0] * 4 + [1] * 6 + [2] * 9 + [3] * 6
    return json.dumps({"bindings": bindings, "structures": [
        {"event_id": event["event_id"], "rule_id": data["structural_rules"][arc]["rule_id"],
            "reason": "该段按已接受初稿顺序承担对应结构的叙事功能。"}
        for event, arc in zip(data["events"], arcs)]}, ensure_ascii=False)


class ApplicationPlanningRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.brief, self.slots, self.constraints = anonymous_annual_context()

    def assessment(self):
        initial = compile_plan_artifact(self.brief, self.slots, source_view="ai-spec", constraints=self.constraints)
        result = assess_planning_requirements(self.brief, initial,
            SimpleNamespace(writer=annual_semantic_response, writer_model="anonymous"))
        return initial, result

    def output_audit(self, *, brief=None, mutations=None, script_type="年会"):
        brief = {**(brief or self.brief), "脚本类型": script_type}
        slots = [replace(slot, script_type=script_type) for slot in self.slots]
        decisions = [TemplateDecision(slot, None, "KEEP", 100, "匿名原稿与模板对应") for slot in slots]
        rows = [{"镜号": str(index), "结构": slot.structure,
            "时长(秒)": 3 if index <= 10 else 2, "画面描述": visual,
            "声音": "现场环境声与音乐", "字幕": "", "备注": "", "效果": ""}
            for index, (slot, (_, visual)) in enumerate(zip(slots, ANNUAL_EXPRESSIONS), 1)]
        for index, fields in (mutations or {}).items():
            rows[index].update(fields)
        details = [{"slot_index": index, "模板镜号": str(index), "替换方式": "KEEP",
            "来源文件": slot.source_file, "来源sheet": slot.source_sheet, "来源行": slot.source_row,
            "模板来源文件": slot.source_file, "模板来源sheet": slot.source_sheet, "模板来源行": slot.source_row,
            "规划主事件": ANNUAL_EXPRESSIONS[index - 1][1],
            "规划Brief证据": "artifact://formal-evidence/anonymous-product-boundary"}
            for index, slot in enumerate(slots, 1)]
        return audit_generation(brief, slots, decisions, rows, details, require_complete_visuals=False)

    def test_ritual_feeling_keeps_valid_semantic_bindings_for_all_25_blocks(self):
        initial, result = self.assessment()
        self.assertEqual(len(initial.events), 25)
        for index in (7, 9):
            self.assertNotEqual(_event_role(initial.events[index]), "ceremony")
        self.assertEqual(result["status"], "ready", result.get("message"))
        self.assertEqual(len(result["structures"]), 25)
        self.assertEqual(result["provider_calls"], 1)

    def test_generator_resolves_content_conflicts_outside_allocated_shots(self):
        from mvp_v2.loop_runner import _generate

        before = compile_plan_artifact(self.brief, self.slots, source_view="ai-spec",
            constraints=self.constraints)
        content_ids = {item["requirement_id"] for item in before.requirements
            if item["classification"] == "mandatory_content"}
        conflicted = {item["requirement_id"] for item in before.coverage
            if item["requirement_id"] in content_ids and item["status"] == "conflict"}
        self.assertEqual(len(conflicted), 2)
        allocated_ids = {event["event_id"] for shot in before.shots for event in shot.get("events", [])}
        extra_requirement_ids = {item["requirement_id"] for item in before.requirements} - {
            event["event_id"] for event in before.events}
        self.assertTrue(allocated_ids.isdisjoint(extra_requirement_ids),
            "The regression requires content conflicts absent from every allocated shot.")
        self.assertGreater(len(before.shots), 25)
        calls = []

        def writer(prompt, *args):
            calls.append(prompt)
            return annual_semantic_response(prompt, *args)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence = root / "semantic_matching.json"
            with patch("mvp_v2.loop_runner.resolve_script_type_workbook",
                       return_value=root / "anonymous-annual-library.xlsx"), patch(
                       "mvp_v2.loop_runner.load_ai_spec_shots", return_value=(self.slots, [])):
                rows, details, decisions, slots, _, plan = _generate(
                    [self.brief], root, self.constraints, source_view="ai-spec",
                    semantic_policy=SimpleNamespace(writer=writer, writer_model="anonymous"),
                    semantic_evidence_path=evidence)
            frozen = json.loads(evidence.with_name("planning_semantics.json").read_text(encoding="utf-8"))

        self.assertEqual(len(calls), 1)
        self.assertEqual(frozen, plan["planning_semantics"])
        self.assertEqual(frozen["status"], "ready", frozen)
        self.assertEqual(frozen["provider_calls"], 1)
        self.assertEqual([len(value) for value in (rows, details, decisions, slots)], [25] * 4)
        self.assertEqual(sum(shot["duration_seconds"] for shot in plan["shot_plans"]), 60)
        self.assertEqual(plan["planning_audit"]["blocking_codes"], [])
        self.assertEqual(plan["planning_audit"]["clarification_codes"], [])
        self.assertEqual([shot["creative_block"]["block_id"] for shot in plan["shot_plans"]],
            [block["block_id"] for block in self.constraints["creative_draft_binding"]["blocks"]])
        self.assertEqual([row["画面描述"] for row in rows], [visual for _, visual in ANNUAL_EXPRESSIONS])

    def test_real_ceremony_in_an_interaction_arc_still_fails_hard_role_validation(self):
        initial, _ = self.assessment()
        inputs = planning_semantic_input(self.brief, initial.events, initial.requirements,
            initial.template_rule_contract)
        assessment = json.loads(annual_semantic_response("\nINPUT:" + json.dumps(inputs, ensure_ascii=False)))
        invalid = copy.deepcopy(assessment)
        invalid["structures"][17]["rule_id"] = "annual.structure.02"
        self.assertEqual(_event_role(initial.events[17]), "ceremony")
        with self.assertRaisesRegex(ValueError, "concrete event violates structural rule"):
            validate_planning_semantics(inputs, initial.events, invalid)

    def test_unknown_name_and_prohibition_do_not_add_shots_or_lose_source_requirements(self):
        _, assessment = self.assessment()
        self.assertEqual(assessment["status"], "ready", assessment.get("message"))
        plan = compile_plan_artifact(self.brief, self.slots, source_view="ai-spec",
            constraints={**self.constraints, "planning_semantics": assessment})
        self.assertEqual(len(plan.shots), 25)
        self.assertEqual(sum(shot["duration_seconds"] for shot in plan.shots), 60)
        self.assertEqual(plan.audit["blocking_codes"], [])
        self.assertEqual(plan.audit["clarification_codes"], [])
        by_text = {item["original_text"]: item for item in plan.requirements}
        for text in self.brief["必须出现的内容"].split("；"):
            self.assertIn(text, by_text)
            self.assertTrue(by_text[text]["evidence_refs"])
            self.assertEqual(by_text[text]["evidence_refs"][0]["fact_id"], "anonymous-product-boundary")
        self.assertNotEqual(by_text["不写型号与性能"]["classification"], "mandatory_content")
        self.assertNotIn("不写型号与性能", [shot["primary_event"] for shot in plan.shots])
        coverage = {item["requirement_id"]: item for item in plan.coverage}
        reveal = coverage[by_text["某个新品亮相"]["requirement_id"]]["shot_nos"]
        silhouette = coverage[by_text["仅以产品剪影表现"]["requirement_id"]]["shot_nos"]
        self.assertEqual(reveal, silhouette)
        self.assertEqual(len(reveal), 1)
        rows = [{"画面描述": "", "声音": "", "备注": ""} for _ in plan.shots]
        bind_plan_rows(rows, plan.shots, self.constraints["creative_draft_binding"])
        self.assertEqual([shot["creative_block"]["block_id"] for shot in plan.shots],
            [block["block_id"] for block in self.constraints["creative_draft_binding"]["blocks"]])
        self.assertEqual([row["画面描述"] for row in rows], [visual for _, visual in ANNUAL_EXPRESSIONS])

    def test_shared_classification_keeps_original_clauses_and_unknown_event_gate(self):
        from mvp_v2.formal_brief_input_adapter import _fact_semantics
        from mvp_v2.planning_core import brief_content_clauses

        clauses = brief_content_clauses(self.brief)
        classification = {item["text"]: item["classification"] for item in clauses}
        self.assertEqual(classification, {"某个新品亮相": "mandatory_content",
            "新品名称尚未确定": "production_requirement", "仅以产品剪影表现": "mandatory_content",
            "不写型号与性能": "forbidden"})
        self.assertTrue(all(item["source_text"] for item in clauses))
        metadata = _fact_semantics("必须出现的内容", self.brief["必须出现的内容"], [], origin="literal_extraction")
        self.assertEqual({item["text"]: item["classification"] for item in metadata["clauses"]}, classification)
        plan = compile_plan_artifact({**self.brief, "必须出现的内容": "嘉宾姓名待确认"},
            self.slots, source_view="ai-spec", constraints=self.constraints)
        unknown = next(item for item in plan.requirements if item["original_text"] == "嘉宾姓名待确认")
        self.assertEqual(unknown["classification"], "mandatory_content")
        coverage = next(item for item in plan.coverage if item["requirement_id"] == unknown["requirement_id"])
        self.assertEqual(coverage["status"], "needs_fact")
        self.assertIn("requirement_needs_fact", plan.audit["clarification_codes"])

    def test_output_audit_accepts_silhouette_without_requiring_boundary_sentences(self):
        for script_type in ("年会", "发布会"):
            with self.subTest(script_type=script_type):
                audited = self.output_audit(script_type=script_type)
                failures = [check for check in audited["checks"]
                    if check["检查项"] in {"required_content_missing", "brief_forbidden_hit"}]
                self.assertEqual(failures, [], failures)

    def test_negative_enumerations_keep_all_items_forbidden_in_planning_and_audit(self):
        brief = {**self.brief, "必须出现的内容": "不写型号、性能；不要出现价格、参数"}
        plan = compile_plan_artifact(brief, self.slots, source_view="ai-spec",
            constraints=self.constraints)
        by_text = {item["original_text"]: item for item in plan.requirements}
        for clause in ("不写型号、性能", "不要出现价格、参数"):
            self.assertIn(clause, by_text)
            self.assertEqual(by_text[clause]["classification"], "forbidden")
            self.assertTrue(by_text[clause]["evidence_refs"])
        self.assertFalse(any(item["classification"] == "mandatory_content" for item in plan.requirements),
            "Enumeration items must not become positive obligations after losing their shared negation.")
        forbidden_ids = {item["requirement_id"] for item in plan.requirements
            if item["classification"] == "forbidden"}
        allocated_ids = {event["event_id"] for shot in plan.shots for event in shot.get("events", [])}
        self.assertTrue(allocated_ids.isdisjoint(forbidden_ids))

        for script_type in ("年会", "发布会"):
            with self.subTest(script_type=script_type, content="safe"):
                safe = self.output_audit(brief=brief, script_type=script_type)
                failures = [check for check in safe["checks"]
                    if check["检查项"] in {"required_content_missing", "brief_forbidden_hit"}]
                self.assertEqual(failures, [])
            for expression in ("新品型号 X100", "新品性能提升50%", "新品价格999元", "新品参数公布"):
                with self.subTest(script_type=script_type, content=expression):
                    audited = self.output_audit(brief=brief, script_type=script_type,
                        mutations={14: {"字幕": expression}})
                    hits = [check for check in audited["checks"] if check["检查项"] == "brief_forbidden_hit"]
                    self.assertTrue(hits, audited["checks"])
                    self.assertTrue(all(check["级别"] == "hard" for check in hits))

    def test_prohibited_model_and_performance_in_caption_or_voiceover_still_fail(self):
        for field in ("画面描述", "声音", "字幕"):
            with self.subTest(field=field):
                audited = self.output_audit(mutations={14: {field: "展示新品型号 X100，性能提升50%。"}})
                hits = [check for check in audited["checks"] if check["检查项"] == "brief_forbidden_hit"]
                self.assertTrue(hits, audited["checks"])
                self.assertTrue(all(check["级别"] == "hard" for check in hits))
        safe = self.output_audit(mutations={14: {"备注": "不写型号与性能，仅以产品剪影表现。"}})
        self.assertNotIn("brief_forbidden_hit", safe["blocking_items"])

    def test_explicit_model_identity_is_not_relaxed_to_any_product(self):
        audited = self.output_audit(brief={**self.brief, "必须出现的内容": "新品型号 X100"},
            mutations={14: {"字幕": "新品型号 X200"}})
        self.assertIn("required_content_missing", audited["blocking_items"])

    def test_exclusive_presentation_requires_review_even_when_positive_content_is_covered(self):
        audited = self.output_audit(mutations={14: {"画面描述":
            "舞台新品亮相，先以产品剪影表现，随后清晰展示产品全貌。"}})
        warnings = [check for check in audited["checks"]
            if check["检查项"] == "exclusive_content_requires_review"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["级别"], "warning")
        self.assertIn("仅以产品剪影表现", str(warnings[0]))

    def test_positive_coverage_still_rejects_negation_and_another_subject(self):
        from mvp_v2.audit import _required_term_present

        for requirement, expression in (
            ("某个新品亮相", "员工亮相，产品仍未出现。"),
            ("某个新品亮相", "新品禁止亮相，只拍现场环境。"),
            ("仅以产品剪影表现", "人物剪影入画，产品全貌清晰展示。"),
            ("仅以产品剪影表现", "不展示产品剪影，镜头只拍来宾。"),
        ):
            with self.subTest(requirement=requirement, expression=expression):
                self.assertFalse(_required_term_present(requirement, expression))

    def test_real_position_conflict_is_not_reported_as_an_unconfirmed_fact(self):
        plan = compile_plan_artifact({**self.brief, "必须出现的内容": "第1镜剪彩仪式"},
            self.slots, source_view="ai-spec", constraints=self.constraints)
        requirement = next(item for item in plan.requirements if item["original_text"] == "第1镜剪彩仪式")
        self.assertEqual(requirement["fact_status"], "confirmed")
        conflicts = [item for item in plan.conflicts if item["requirement_id"] == requirement["requirement_id"]]
        self.assertTrue(conflicts)
        self.assertTrue(any(item["status"] == "unresolved" for item in conflicts))
        self.assertIn("hard_template_conflict_unresolved", plan.audit["clarification_codes"])
        findings = [item for item in plan.audit["findings"]
            if item.get("requirement_ref") == requirement["requirement_id"]]
        self.assertNotIn("requirement_needs_fact", {item["code"] for item in findings})


if __name__ == "__main__":
    unittest.main()
