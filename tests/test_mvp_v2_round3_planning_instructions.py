import copy
import json
import unittest

from mvp_v2 import planning_core
from mvp_v2.excel_io import _adjacent_review_rows
from mvp_v2.generation_stages import _planning_brief_from_confirmed_payload
from mvp_v2.models import SourceShot
from mvp_v2.template_rules import rules_from_template_shots


class Round3PlanningInstructionsTests(unittest.TestCase):
    def setUp(self):
        self.event_titles = [
            "厨房空镜", "产品展示", "备米入锅", "控制面板", "成饭开盖", "盛饭上桌", "品牌落版",
        ]
        self.raw_duration = "整片56秒，共7镜，每镜8秒。片尾包含在56秒内。"
        self.raw_flow = "7镜顺序：" + "；".join(self.event_titles) + "。每镜8秒，总56秒。"
        self.literal_copy = "用心吃饭，认真生活。每一天"
        self.values = {
            "项目名称": "匿名产品介绍片",
            "脚本类型": "发布会",
            "目标时长": self.raw_duration,
            "关键事件与流程": self.raw_flow,
            "必须出现的内容": "；".join(self.event_titles),
            "特殊需求": f"无旁白。片尾写“{self.literal_copy}”",
        }
        self.slots = [
            SourceShot(
                script_type="发布会", source_file="anonymous-template.xlsx", source_sheet="模版",
                source_row=index + 6, shot_no=str(index), slot_index=index,
                structure="其他" if index < 7 else "品牌落版", visual_desc=title,
                shot_function=["氛围建立", "产品展示", "人物互动", "信息发布", "产品展示", "人物互动", "品牌收束"][index - 1],
            )
            for index, title in enumerate(self.event_titles, start=1)
        ]
        self.constraints = {
            "template_rule_contract": {
                "payload": rules_from_template_shots("launch", self.slots),
                "source": "anonymous_fixture",
            },
        }
        self.evidence = {
            "workspace_id": "workspace-anonymous", "project_id": "project-anonymous",
            "brief_version_id": "brief-anonymous-v1",
            "payload": {"ref": "artifact://formal-brief/anonymous", "sha256": "a" * 64},
        }

    def planning_input(self, **changes):
        values = {**self.values, **changes}
        payload = {
            "facts": [
                {
                    "fact_id": f"fact-anonymous-{index}", "field_path": field,
                    "value_json": json.dumps(value, ensure_ascii=False), "certainty": "confirmed",
                    "evidence_refs": [{
                        "artifact_ref": f"artifact://formal-evidence/anonymous/paragraph/{index}",
                        "source_document_id": "source-anonymous", "location": {"paragraph": index},
                    }],
                }
                for index, (field, value) in enumerate(values.items(), start=1)
            ],
            "conflicts": [],
            "source_documents": [{"document_id": "source-anonymous", "filename": "anonymous-brief.txt"}],
        }
        before = copy.deepcopy(payload)
        brief, provenance = _planning_brief_from_confirmed_payload(payload, self.evidence)
        self.assertEqual(payload, before)
        return brief, provenance, payload

    def plan(self, brief):
        return planning_core.compile_plan_artifact(
            brief, self.slots, source_view="ai-spec", constraints=self.constraints,
        )

    def test_confirmed_rich_duration_and_flow_keep_seven_shots_and_six_review_pairs(self):
        brief, provenance, payload = self.planning_input()
        self.assertEqual(brief["视频时长"], "56s")
        self.assertEqual(brief["目标时长"], self.raw_duration)
        self.assertEqual([event["title"] for event in brief["planning_events"]], self.event_titles)
        self.assertEqual(
            planning_core._split_event_text('等待8秒后开盖；产品字幕“总56秒”'),
            ["等待8秒后开盖", '产品字幕“总56秒”'],
        )
        flow_fact = next(fact for fact in payload["facts"] if fact["field_path"] == "关键事件与流程")
        duration_fact = next(fact for fact in payload["facts"] if fact["field_path"] == "目标时长")
        projected_duration = next(row for row in provenance if row["field_path"] == "目标时长")
        self.assertEqual(projected_duration["duration_projection"]["source_value"], self.raw_duration)
        self.assertEqual(projected_duration["evidence_refs"], duration_fact["evidence_refs"])
        self.assertTrue(all(event["source_fact_id"] == flow_fact["fact_id"] for event in brief["planning_events"]))
        self.assertTrue(all(event["source"]["file"] == "anonymous-brief.txt" for event in brief["planning_events"]))
        plan = self.plan(brief)
        self.assertEqual(plan.audit["status"], "pass")
        self.assertEqual(len(plan.events), 7)
        self.assertEqual(len(plan.shots), 7)
        self.assertEqual(plan.duration_budget["target_seconds"], 56)
        self.assertEqual(plan.duration_budget["planned_seconds"], 56)
        self.assertEqual([shot["duration_seconds"] for shot in plan.shots], [8] * 7)
        script_rows = [{"镜号": shot["shot_no"], "画面描述": shot["primary_event"]} for shot in plan.shots]
        adjacent = _adjacent_review_rows("匿名产品介绍片", script_rows, [])
        self.assertEqual([row["边"] for row in adjacent], ["1->2", "2->3", "3->4", "4->5", "5->6", "6->7"])

    def test_sentence_split_preserves_literal_copy_and_distinct_production_and_closing_contracts(self):
        brief, _, payload = self.planning_input()
        plan = self.plan(brief)
        no_voice = next(row for row in plan.requirements if row["text"] == "无旁白")
        closing = next(row for row in plan.requirements if row["text"] == f"片尾写“{self.literal_copy}”")
        coverage = {row["requirement_id"]: row for row in plan.coverage}
        self.assertEqual(no_voice["classification"], "production_requirement")
        self.assertEqual(no_voice["position_rule"]["type"], "none")
        self.assertEqual(coverage[no_voice["requirement_id"]]["shot_nos"], [])
        self.assertEqual(coverage[no_voice["requirement_id"]]["status"], "covered")
        self.assertEqual(closing["classification"], "position_constraint")
        self.assertEqual(closing["position_rule"]["type"], "closing")
        self.assertEqual(coverage[closing["requirement_id"]]["shot_nos"], ["7"])
        special_fact = next(row for row in payload["facts"] if row["field_path"] == "特殊需求")
        self.assertEqual(closing["evidence_refs"][0]["fact_id"], special_fact["fact_id"])
        self.assertIn(self.literal_copy, closing["source_text"])
        self.assertNotIn("position_contract_violated", plan.audit["blocking_codes"])

    def test_conflicting_total_counts_survive_formal_field_mapping_and_require_clarification(self):
        brief, _, _ = self.planning_input(**{"关键事件与流程": self.raw_flow.replace("7镜顺序", "9镜顺序")})
        self.assertEqual(brief["事件流程"], self.raw_flow.replace("7镜顺序", "9镜顺序"))
        self.assertEqual(
            planning_core._brief_requested_shot_count(brief),
            (None, "conflicting_brief_shot_counts"),
        )
        plan = self.plan(brief)
        self.assertEqual(plan.audit["status"], "needs_clarification")
        self.assertIn("conflicting_brief_shot_counts", [row["code"] for row in plan.audit["findings"]])

    def test_wrong_closing_binding_still_blocks_after_sentence_split(self):
        brief, _, _ = self.planning_input()
        plan = self.plan(brief)
        coverage = copy.deepcopy(plan.coverage)
        closing = next(row for row in plan.requirements if row["classification"] == "position_constraint")
        next(row for row in coverage if row["requirement_id"] == closing["requirement_id"])["shot_nos"] = ["1"]
        audit = planning_core._audit_plan(
            brief=brief, facts=plan.facts, events=plan.events, shots=plan.shots, coverage=coverage,
            projected_slots=plan.template_slots, duration_budget=plan.duration_budget,
            source_status=plan.source_status, count_issue="", requirements=plan.requirements,
            capacity_report=plan.capacity_report, conflicts=plan.conflicts,
            decision_counts=plan.decision_counts, rule_contract=plan.template_rule_contract,
        )
        self.assertEqual(audit["status"], "blocked")
        mismatch = next(row for row in audit["findings"] if row["code"] == "position_contract_violated")
        self.assertEqual(mismatch["expected_shot_nos"], ["7"])
        self.assertEqual(mismatch["actual_shot_nos"], ["1"])


if __name__ == "__main__":
    unittest.main()
