"""Anonymous regressions for reusing accepted scenes without losing contracts."""

import copy
import json
import unittest

from mvp_v2 import planning_core
from mvp_v2.creative_formal_binding import bind_plan_rows
from mvp_v2.excel_io import _adjacent_review_rows
from mvp_v2.generation_stages import _planning_brief_from_confirmed_payload
from mvp_v2.models import SourceShot
from mvp_v2.run_contract import sha256_json
from mvp_v2.template_rules import rules_from_template_shots


class Round3CreativeRequirementsBindingTests(unittest.TestCase):
    def setUp(self):
        self.topics = [
            "厨房与产品入场", "外观及型号", "备米放入内锅", "控制面板",
            "成饭开盖", "盛饭上桌", "产品片尾",
        ]
        self.raw_required = (
            "同一台松鹤ZX-TEST2040电饭锅；松鹤、ZX-TEST2040、5L、IH加热字幕；"
            "外观、备米入锅、面板、开盖成饭、盛饭上桌和产品片尾。"
        )
        self.raw_duration = "整片56秒，共7镜，每镜8秒。片尾包含在56秒内。"
        self.raw_special = (
            "轻柔钢琴配乐，加放锅、按键、开盖和盛饭现场声，无旁白。"
            "片尾写“从容用餐，享受每一天”，并保留松鹤和ZX-TEST2040。"
        )
        self.values = {
            "项目名称": "匿名电饭锅产品介绍片", "脚本类型": "发布会",
            "品牌或客户": "松鹤", "目标时长": self.raw_duration,
            "关键事件与流程": "7镜顺序：" + "；".join(self.topics) + "。每镜8秒，总56秒。",
            "必须出现的内容": self.raw_required, "特殊需求": self.raw_special,
        }
        visuals = [
            "厨房晨光中，同一台松鹤ZX-TEST2040电饭锅进入画面。",
            "展示同一台电饭锅外观，字幕为松鹤、ZX-TEST2040、5L、IH加热。"
            "字幕放在上方，不遮挡上盖和控制面板区域。",
            "将备好的米放入内锅，展示备米放入内锅动作。",
            "手指按键操作控制面板，清楚展示面板交互。",
            "成饭后开盖，展示熟饭热气和开盖动作。",
            "盛饭上桌，米饭盛入饭碗并放到餐桌。",
            "产品片尾定格，保留松鹤和ZX-TEST2040，写“从容用餐，享受每一天”。",
        ]
        sounds = [
            "钢琴配乐，无旁白。", "钢琴配乐，无旁白。", "钢琴配乐，加放锅现场声，无旁白。",
            "钢琴配乐，加按键现场声，无旁白。", "钢琴配乐，加开盖现场声，无旁白。",
            "钢琴配乐，加盛饭现场声，无旁白。", "钢琴配乐渐弱，无旁白。",
        ]
        self.binding = {
            "schema": "mplus-creative-formal-binding/v1",
            "workspace_id": "workspace-anonymous", "project_id": "project-anonymous",
            "draft_id": "draft-anonymous", "draft_revision": 9,
            "payload_ref": "artifact://creative-draft/anonymous",
            "payload_sha256": "a" * 64, "protected_block_ids": ["block-1"],
            "protection_sha256": sha256_json(["block-1"]),
            "image_references": [], "image_references_sha256": sha256_json([]),
            "blocks": [
                {
                    "block_id": f"block-{index}",
                    "fields": {"段落": f"镜{index}：{topic}。保持原有段落表达。",
                               "画面描述": visual, "声音": sound},
                    "source_refs": [{"origin": "user_instruction", "task_id": "task-anonymous",
                                     "input_artifact_ref": "artifact://creative-draft/anonymous-source",
                                     "input_sha256": "b" * 64}],
                }
                for index, (topic, visual, sound) in enumerate(zip(self.topics, visuals, sounds), 1)
            ],
        }
        self.binding["draft_sha256"] = sha256_json(self.binding["blocks"])
        self.slots = [
            SourceShot(
                script_type="发布会", source_file="anonymous-template.xlsx", source_sheet="模版",
                source_row=index + 6, shot_no=str(index), slot_index=index,
                structure="其他" if index < 7 else "品牌落版", visual_desc=topic,
                shot_function=["氛围建立", "产品展示", "人物互动", "信息发布", "产品展示", "人物互动", "品牌收束"][index - 1],
            )
            for index, topic in enumerate(self.topics, 1)
        ]

    def compile(self, *, changes=None, binding=None):
        values = {**self.values, **(changes or {})}
        payload = {
            "facts": [
                {"fact_id": f"fact-anonymous-{index}", "field_path": field,
                 "value_json": json.dumps(value, ensure_ascii=False), "certainty": "confirmed",
                 "evidence_refs": [{"artifact_ref": "artifact://evidence/anonymous",
                                    "source_document_id": "source-anonymous",
                                    "location": {"paragraph": index}}]}
                for index, (field, value) in enumerate(values.items(), 1)
            ],
            "conflicts": [],
            "source_documents": [{"document_id": "source-anonymous", "filename": "anonymous-brief.txt"}],
        }
        evidence = {"workspace_id": "workspace-anonymous", "project_id": "project-anonymous",
                    "brief_version_id": "brief-anonymous", "payload": {
                        "ref": "artifact://formal-brief/anonymous", "sha256": "c" * 64}}
        frozen_payload = copy.deepcopy(payload)
        creative = copy.deepcopy(self.binding if binding is None else binding)
        frozen_creative = copy.deepcopy(creative)
        brief, provenance = _planning_brief_from_confirmed_payload(payload, evidence)
        plan = planning_core.compile_plan_artifact(
            brief, self.slots, source_view="ai-spec",
            constraints={"creative_draft_binding": creative,
                         "template_rule_contract": {"payload": rules_from_template_shots("launch", self.slots),
                                                    "source": "anonymous_fixture"}},
        )
        self.assertEqual(payload, frozen_payload)
        self.assertEqual(creative, frozen_creative)
        return plan, brief, payload, provenance, creative

    def assert_seven(self, plan):
        self.assertEqual(plan.audit["status"], "pass", plan.audit)
        self.assertEqual(len(plan.events), 7)
        self.assertEqual(len(plan.shots), 7)
        self.assertEqual(plan.duration_budget["target_seconds"], 56)
        self.assertEqual(plan.duration_budget["planned_seconds"], 56)
        self.assertEqual([shot["duration_seconds"] for shot in plan.shots], [8] * 7)
        self.assertEqual(plan.capacity_report["requested_shot_count"], 7)
        rows = [{"镜号": shot["shot_no"], "画面描述": shot["primary_event"]} for shot in plan.shots]
        reviews = _adjacent_review_rows("匿名产品片", rows, [])
        self.assertEqual([row["边"] for row in reviews], [f"{index}->{index + 1}" for index in range(1, 7)])

    @staticmethod
    def coverage_for_source(plan, raw):
        identities = {
            row["requirement_id"] for row in plan.requirements
            if raw in [row.get("text"), row.get("original_text"), row.get("source_text")]
            or any(ref.get("text") == raw for ref in row.get("evidence_refs") or [])
        }
        return [row for row in plan.coverage if row["requirement_id"] in identities]

    def test_existing_product_attributes_sound_and_compound_flow_reuse_seven_accepted_blocks(self):
        plan, brief, payload, provenance, creative = self.compile()
        self.assert_seven(plan)
        self.assertEqual(brief["目标时长"], self.raw_duration)
        self.assertTrue(all(row["status"] == "covered" for row in plan.coverage))
        required_fact = next(row for row in payload["facts"] if row["field_path"] == "必须出现的内容")
        self.assertEqual(json.loads(required_fact["value_json"]), self.raw_required)
        self.assertTrue(any(row["field_path"] == "必须出现的内容" for row in provenance))
        compound = self.coverage_for_source(plan, "盛饭上桌和产品片尾。")
        self.assertTrue(compound, "原始合并条款及其来源必须仍可追溯")
        self.assertEqual({number for row in compound for number in row["shot_nos"]}, {"6", "7"})
        # Compound closing content must not move the independent serving scene.
        positions = {identity: shot["shot_no"] for shot in plan.shots for identity in shot["event_ids"]}
        self.assertEqual(positions["creative_block-6"], "6")
        self.assertEqual(positions["creative_block-7"], "7")
        sound_only = [row for row in plan.requirements
                      if "现场声" in str(row.get("text")) and "现场声" in str(row.get("source_text"))]
        self.assertTrue(sound_only)
        self.assertTrue(all(row["classification"] in {"production_requirement", "style_preference"}
                            for row in sound_only))
        rows = [{"备注": ""} for _ in plan.shots]
        before = copy.deepcopy(creative)
        bind_plan_rows(rows, plan.shots, creative)
        self.assertEqual(creative, before)
        for index, block in enumerate(creative["blocks"]):
            mapped = plan.shots[index]["creative_block"]
            self.assertEqual(mapped["block_id"], block["block_id"])
            self.assertEqual(mapped["fields"], block["fields"])
            self.assertEqual(mapped["source_refs"], block["source_refs"])
            self.assertEqual(mapped["assistant_protected"], index == 0)
            self.assertEqual(rows[index]["画面描述"], block["fields"]["画面描述"])
            self.assertEqual(rows[index]["声音"], block["fields"]["声音"])
        self.assertTrue(all(slot.source_file and slot.source_sheet and slot.source_row > 0
                            for slot in plan.template_slots))

    def test_action_word_order_and_inserted_function_words_keep_same_confirmed_scene(self):
        for preparation, opening in (
            ("备米入锅", "开盖成饭"),
            ("备米放入内锅", "成饭开盖"),
            ("将备米放入内锅", "成饭后开盖"),
        ):
            with self.subTest(preparation=preparation, opening=opening):
                plan, *_ = self.compile(changes={"必须出现的内容": f"{preparation}；{opening}"})
                self.assert_seven(plan)
                prep_coverage = self.coverage_for_source(plan, preparation)
                open_coverage = self.coverage_for_source(plan, opening)
                self.assertEqual({number for row in prep_coverage for number in row["shot_nos"]}, {"3"})
                self.assertEqual({number for row in open_coverage for number in row["shot_nos"]}, {"5"})

    def test_incidental_body_mention_cannot_take_explicit_section_topic(self):
        plan, *_ = self.compile(changes={"必须出现的内容": "外观；控制面板"})
        self.assert_seven(plan)
        coverage = self.coverage_for_source(plan, "控制面板")
        self.assertTrue(coverage)
        self.assertEqual({number for row in coverage for number in row["shot_nos"]}, {"4"})
        flow = [row for row in plan.requirements if row["text"] == "控制面板"]
        self.assertTrue(flow)
        self.assertNotIn("creative_brief_flow_conflict", plan.audit["clarification_codes"])

    def test_truly_independent_eighth_action_keeps_capacity_gate(self):
        plan, *_ = self.compile(changes={"必须出现的内容": self.raw_required + "；单独完成原料称重"})
        self.assertNotEqual(plan.audit["status"], "pass")
        self.assertIn("capacity_requires_choice", plan.audit["clarification_codes"])
        self.assertEqual(plan.capacity_report["requested_shot_count"], 7)
        self.assertEqual(plan.capacity_report["planned_shot_count"], 7)
        self.assertEqual(plan.capacity_report["required_shot_units"], 8)
        self.assertTrue(any(row["type"] == "template_role_capacity_conflict" for row in plan.conflicts))
        added = self.coverage_for_source(plan, "单独完成原料称重")
        self.assertTrue(added)
        added_nos = {number for row in added for number in row["shot_nos"]}
        accepted_nos = {shot["shot_no"] for shot in plan.shots
                        if any(identity.startswith("creative_") for identity in shot["event_ids"])}
        self.assertFalse(added_nos & accepted_nos, "独立动作不得凭产品旁提共享已接受段落")
        raw_requirement = next(row for row in plan.requirements if row["text"] == "单独完成原料称重")
        self.assertEqual(raw_requirement["original_text"], "单独完成原料称重")
        self.assertTrue(raw_requirement["evidence_refs"])

    def test_actual_accepted_flow_reversal_still_requires_clarification(self):
        binding = copy.deepcopy(self.binding)
        binding["blocks"][2], binding["blocks"][3] = binding["blocks"][3], binding["blocks"][2]
        plan, *_ = self.compile(binding=binding)
        self.assertNotEqual(plan.audit["status"], "pass")
        self.assertIn("creative_brief_flow_conflict", plan.audit["clarification_codes"])
        self.assertTrue(any(row["type"] == "creative_brief_flow_conflict" for row in plan.conflicts))

    def test_negated_action_is_not_coverage_of_required_positive_action(self):
        for negation in ("未", "禁止"):
            with self.subTest(negation=negation):
                binding = copy.deepcopy(self.binding)
                fields = binding["blocks"][2]["fields"]
                fields["段落"] = f"镜3：{negation}备米入锅。仅展示空内锅。"
                fields["画面描述"] = f"{negation}备米入锅，空内锅保持静止。"
                fields["声音"] = "轻音乐，无旁白。"
                plan, *_ = self.compile(changes={"必须出现的内容": "备米入锅"}, binding=binding)
                self.assertNotEqual(plan.audit["status"], "pass")
                required = self.coverage_for_source(plan, "备米入锅")
                self.assertTrue(required)
                bound_nos = {number for row in required for number in row["shot_nos"]}
                negated_nos = {shot["shot_no"] for shot in plan.shots
                               if "creative_block-3" in shot["event_ids"]}
                self.assertFalse(bound_nos & negated_nos,
                                 "未发生或禁止的动作不能证明肯定要求已被原有场景覆盖")

    def test_different_model_entity_cannot_share_by_generic_product_anchor(self):
        plan, *_ = self.compile(changes={"必须出现的内容": "松鹤ZX-TEST2041电饭锅"})
        self.assertNotEqual(plan.audit["status"], "pass")
        required = self.coverage_for_source(plan, "松鹤ZX-TEST2041电饭锅")
        self.assertTrue(required)
        bound_nos = {number for row in required for number in row["shot_nos"]}
        accepted_nos = {shot["shot_no"] for shot in plan.shots
                        if any(identity.startswith("creative_") for identity in shot["event_ids"])}
        self.assertFalse(bound_nos & accepted_nos,
                         "不同型号不能仅因品牌和产品泛词相同而宣称已被原有场景覆盖")

    def test_wrong_closing_coverage_is_still_a_position_contract_failure(self):
        plan, brief, *_ = self.compile()
        self.assert_seven(plan)
        closing = next(row for row in plan.requirements if "片尾写" in row["text"])
        coverage = copy.deepcopy(plan.coverage)
        next(row for row in coverage if row["requirement_id"] == closing["requirement_id"])["shot_nos"] = ["1"]
        audit = planning_core._audit_plan(
            brief=brief, facts=plan.facts, events=plan.events, shots=plan.shots, coverage=coverage,
            projected_slots=plan.template_slots, duration_budget=plan.duration_budget,
            source_status=plan.source_status, count_issue="", requirements=plan.requirements,
            capacity_report=plan.capacity_report, conflicts=plan.conflicts,
            decision_counts=plan.decision_counts, rule_contract=plan.template_rule_contract,
        )
        self.assertEqual(audit["status"], "blocked")
        self.assertIn("position_contract_violated", audit["blocking_codes"])
        mismatch = next(row for row in audit["findings"] if row["code"] == "position_contract_violated")
        self.assertEqual(mismatch["expected_shot_nos"], ["7"])
        self.assertEqual(mismatch["actual_shot_nos"], ["1"])


if __name__ == "__main__":
    unittest.main()
