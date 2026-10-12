import unittest

from mvp_v2.planning_core import compile_plan_artifact
from tests import test_mvp_v2_planning_core as fixtures


class PlanningFactInstructionTests(unittest.TestCase):
    def test_uncertainty_handling_instruction_is_preserved_without_inventing_an_event(self):
        fixture = fixtures.PlanningCoreTests("runTest")
        fixture.setUp()
        for text in ("未确认事实保留待确认", "未知信息标注为待确认", "对未确认的事实标记为待定"):
            with self.subTest(text=text):
                plan = compile_plan_artifact({**fixture.brief, "特殊需求": text}, fixture.slots, source_view="ai-spec")
                clause = next(item for item in plan.requirements if item["original_text"] == text)
                self.assertEqual(clause["classification"], "production_requirement")
                coverage = next(item for item in plan.coverage if item["requirement_id"] == clause["requirement_id"])
                self.assertNotEqual(coverage["status"], "needs_fact")
                self.assertNotIn(text, [event["title"] for event in plan.events])
                self.assertTrue(clause["evidence_refs"])

    def test_actual_unknown_guest_fact_still_requires_clarification(self):
        fixture = fixtures.PlanningCoreTests("runTest")
        fixture.setUp()
        plan = compile_plan_artifact({**fixture.brief, "特殊需求": "嘉宾姓名待确认"}, fixture.slots, source_view="ai-spec")
        clause = next(item for item in plan.requirements if item["original_text"] == "嘉宾姓名待确认")
        self.assertEqual(clause["classification"], "mandatory_content")
        coverage = next(item for item in plan.coverage if item["requirement_id"] == clause["requirement_id"])
        self.assertEqual(coverage["status"], "needs_fact")
