"""Regressions for accepted creative input and evidence-based planning gates."""
from dataclasses import replace
import unittest

from mvp_v2.creative_formal_binding import bind_plan_rows
from mvp_v2.planning_core import compile_plan_artifact
from mvp_v2.template_rules import rules_from_template_shots
from tests.test_mvp_v2_planning_core import _slot


class PlanningHandshakeTests(unittest.TestCase):
    def compile(self, *, template_count=25, requested_count=None, duration=60, required_content="", source_view="ai-spec"):
        slots = [replace(_slot(i, "匿名模板参考"),
                         structure="开场" if i <= 3 else "收束" if i == template_count else "活动")
                 for i in range(1, template_count + 1)]
        binding = {
            "draft_id": "anonymous-draft", "draft_revision": 3,
            "draft_sha256": "b" * 64, "payload_ref": "artifact://anonymous/creative.json",
            "payload_sha256": "c" * 64, "protected_block_ids": ["block_3"],
            "blocks": [{"block_id": f"block_{i}", "fields": {
                "段落": f"第{i}段", "画面描述": f"活动现场第{i}段已接受画面", "声音": f"第{i}段原有声音"},
                "source_refs": [{"type": "user_instruction", "input_sha256": "a" * 64}]}
                for i in range(1, 6)],
        }
        constraints = {"creative_draft_binding": binding,
                       "template_rule_contract": {"payload": rules_from_template_shots("star_activity", slots)}}
        if requested_count is not None:
            constraints["planned_shot_count"] = requested_count
        brief = {"项目名称": "匿名活动", "脚本类型": "明星活动", "视频时长": f"{duration}s",
                 "必须出现的内容": required_content}
        plan = compile_plan_artifact(brief, slots, constraints=constraints, source_view=source_view)
        return plan, binding

    def test_five_accepted_blocks_keep_25_template_shots_and_traceable_order(self):
        plan, binding = self.compile()
        self.assertEqual(len(plan.shots), 25)
        self.assertIsNone(plan.capacity_report["requested_shot_count"])
        self.assertEqual(plan.capacity_report["hard_slot_quantity_mode"], "template_default")
        self.assertEqual(plan.duration_budget["planned_seconds"], 60)
        rows = [{"备注": ""} for _ in plan.shots]
        bind_plan_rows(rows, plan.shots, binding)
        mapped = [shot["creative_block"] for shot in plan.shots if shot.get("creative_block")]
        self.assertEqual([item["block_id"] for item in mapped], [item["block_id"] for item in binding["blocks"]])
        self.assertEqual([item["fields"] for item in mapped], [item["fields"] for item in binding["blocks"]])
        self.assertTrue(mapped[2]["assistant_protected"])
        self.assertTrue(all(slot.source_file and slot.source_sheet and slot.source_row > 0 for slot in plan.template_slots))

    def test_other_template_default_is_dynamic(self):
        plan, _ = self.compile(template_count=17)
        self.assertEqual(len(plan.shots), 17)
        self.assertIsNone(plan.capacity_report["requested_shot_count"])
        self.assertEqual(plan.duration_budget["planned_seconds"], 60)

    def test_explicit_user_count_is_preserved_and_identified(self):
        plan, _ = self.compile(requested_count=12)
        self.assertEqual(len(plan.shots), 12)
        self.assertEqual(plan.capacity_report["requested_shot_count"], 12)
        self.assertEqual(plan.capacity_report["hard_slot_quantity_mode"], "explicit_shot_count")

    def test_unstated_extra_required_content_does_not_block_accepted_draft(self):
        plan, _ = self.compile()
        self.assertNotIn("fact_missing", plan.audit["clarification_codes"])
        self.assertEqual(plan.audit["blocking_codes"], [])
        self.assertIn("required_content_unspecified", plan.audit["warning_codes"])

    def test_actual_unknown_required_guest_remains_actionable_clarification(self):
        plan, _ = self.compile(required_content="嘉宾姓名待确认")
        self.assertEqual(plan.audit["status"], "needs_clarification")
        findings = [item for item in plan.audit["findings"] if item["severity"] == "clarification"]
        self.assertTrue(findings)
        self.assertTrue(any(item.get("event_ids") or item.get("requirement_ref") for item in findings))
        self.assertIn("嘉宾姓名待确认", str(plan.requirements))

    def test_short_duration_conflict_does_not_pretend_user_chose_count(self):
        plan, _ = self.compile(duration=3)
        self.assertIsNone(plan.capacity_report["requested_shot_count"])
        self.assertEqual(plan.capacity_report["status"], "needs_clarification")
        self.assertTrue(plan.capacity_report["choices"])
        self.assertEqual(plan.audit["status"], "needs_clarification")


if __name__ == "__main__":
    unittest.main()
