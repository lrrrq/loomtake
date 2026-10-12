"""RV04 negative oracles: mechanical evidence never becomes human approval."""
from copy import deepcopy
import unittest

from mvp_v2.script_draft import (
    _ground_primary_events, _ground_required_assignments,
    _required_content_clause, requirement_output_evidence,
)


class ContentEvidenceTests(unittest.TestCase):
    def test_missing_interaction_does_not_pollute_neighbor_or_repeat_on_retry(self):
        payload = {"shots": [
            {"shot_no": "8", "visual_description": "店员递出茶饮，来宾接过纸杯。"},
            {"shot_no": "21", "visual_description": "明星互动要求。"},
        ]}
        original = deepcopy(payload)
        source = {"confirmed_required_brief_content": ["明星互动"], "shots": [
            {"shot_no": "8", "required_brief_content": ["明星互动"]},
            {"shot_no": "21", "planning_contract": {"primary_event": "明星互动"}},
        ]}
        for _ in range(2):
            payload = _ground_primary_events(_ground_required_assignments(payload, source), source)
        self.assertEqual(original, payload)
        self.assertEqual("明星互动", _required_content_clause("明星互动"))

    def test_restatement_is_locatable_but_never_verified(self):
        row = {"画面描述": "完整呈现明星互动的可执行动作。"}
        value = requirement_output_evidence("明星互动", row, shot_id="stable-21", event_ids=("event-21",))
        self.assertTrue(value["mentioned"])
        self.assertTrue(value["fragments"][0]["generic_restatement"])
        self.assertEqual("pending_review", value["verification_status"])
        fragment = value["fragments"][0]
        self.assertEqual(fragment["text"], row[fragment["field"]][fragment["start"]:fragment["end"]])
        self.assertEqual("stable-21", value["shot_id"])

    def test_explicit_source_alias_can_locate_synonym_without_semantic_pass(self):
        row = {"画面描述": "工人将装框海报挂在入口墙面。"}
        value = requirement_output_evidence("挂画", row, aliases=("装框海报",))
        self.assertTrue(value["mentioned"])
        self.assertFalse(value["fragments"][0]["generic_restatement"])
        self.assertEqual("pending_review", value["verification_status"])
        self.assertEqual("", value["shot_id"])
        self.assertEqual([], requirement_output_evidence("挂画", row)["fragments"])

    def test_assigned_event_parts_locate_original_prose_without_alias_injection(self):
        from mvp_v2.generation_stages import _traceability_with_output
        cases = [
            ("event_003", "甲中庭：挂画", "横移甲中庭，挂画沿廊道排开，来宾驻足观看。"),
            ("event_006", "乙中庭：挂画", "前推乙中庭，挂画与花艺布置相映，来宾合影。"),
            ("event_008", "丙中庭：LED屏", "环绕丙中庭，LED屏播放活动内容，来宾观看。"),
            ("event_009", "步行街：人形立牌", "推进步行街，人形立牌沿街排布，来宾走近合影。"),
        ]
        for event, requirement, prose in cases:
            with self.subTest(event=event):
                result = _traceability_with_output(
                    [{"requirement_id": event, "brief_text": requirement,
                      "plan_shot_nos": ["1"], "plan_status": "covered"}],
                    [{"镜号": "1", "画面描述": prose}],
                    [{"shot_no": "1", "event_ids": [event]}],
                    shot_ids_by_no={"1": "stable-event-shot"})[0]
                content = result["output_evidence"][0]["content_evidence"]
                self.assertTrue(content["mentioned"])
                self.assertEqual(content["missing_requirement_parts"], [])
                self.assertEqual(content["shot_id"], "stable-event-shot")
                self.assertEqual(content["event_ids"], [event])
                self.assertEqual(result["final_status"], "pending_review")
                fragment = content["fragments"][0]
                self.assertEqual(fragment["text"], prose[fragment["start"]:fragment["end"]])

    def test_separated_compound_reports_missing_part_and_restatement_without_approval(self):
        partial = requirement_output_evidence("甲中庭：挂画、LED屏", {"画面描述": "甲中庭挂画沿墙排布。"})
        self.assertTrue(partial["mentioned"])
        self.assertEqual(partial["missing_requirement_parts"], ["LED屏"])
        wrong_place = requirement_output_evidence("甲中庭：挂画", {"画面描述": "乙中庭的挂画沿墙排布。"})
        self.assertEqual(wrong_place["missing_requirement_parts"], ["甲中庭"])
        generic = requirement_output_evidence("甲中庭：挂画", {"画面描述": "需要体现甲中庭的挂画。"})
        self.assertTrue(generic["fragments"][0]["generic_restatement"])
        for value in (partial, wrong_place, generic):
            self.assertEqual(value["verification_status"], "pending_review")

    def test_source_word_is_not_split_at_participation_character(self):
        value = requirement_output_evidence("嘉宾参与互动", {"画面描述": "嘉宾参与互动。"})
        self.assertEqual(value["requirement_parts"], ["嘉宾参与互动"])
        self.assertEqual(value["missing_requirement_parts"], [])

    def test_negative_and_wrong_position_never_pass(self):
        value = requirement_output_evidence("明星", {"画面描述": "明星未到场。"}, position_verified=False)
        self.assertTrue(value["fragments"][0]["negated_or_uncertain"])
        self.assertFalse(value["position_verified"])
        self.assertEqual("pending_review", value["verification_status"])

    def test_partial_compound_is_not_claimed_complete(self):
        value = requirement_output_evidence("挂画与LED", {"画面描述": "工人挂画。"}, aliases=("挂画", "LED"))
        self.assertEqual(["挂画"], value["fragments"][0]["matched_terms"])
        self.assertEqual("pending_review", value["verification_status"])

    def test_changed_text_changes_digest_without_mutating_parent_or_reviews(self):
        parent = {"画面描述": "工人挂画。", "人工审核": "pending_review"}
        original = deepcopy(parent)
        before = requirement_output_evidence("挂画", parent)
        child = dict(parent, 画面描述="工人离场。")
        after = requirement_output_evidence("挂画", child)
        self.assertNotEqual(before["script_sha256"], after["script_sha256"])
        self.assertFalse(after["mentioned"])
        self.assertEqual(original, parent)

    def test_technical_compound_and_duration_are_not_rewritten(self):
        payload = {"shots": [{"shot_no": "12", "visual_description": "镜头向上推进，同时继续上摇。", "duration": 2}]}
        source = {"shots": [{"shot_no": "12", "planning_contract": {"primary_event": "建筑展示"}}]}
        self.assertEqual(payload, _ground_primary_events(payload, source))


if __name__ == "__main__":
    unittest.main()
