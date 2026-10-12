from copy import deepcopy
import unittest

from mvp_v2.generation_stages import _traceability_with_output


class OutputCoverageTests(unittest.TestCase):
    def test_planned_shot_is_not_proof_of_current_content(self):
        traces = [{"requirement_id": "brief-1", "brief_text": "产品体验",
                   "plan_shot_nos": ["2"], "final_status": "covered"}]
        original = deepcopy(traces)
        rows = [{"镜号": "1", "画面描述": "来宾走入会场"},
                {"镜号": "2", "画面描述": "会场空镜", "声音": "音乐"},
                {"镜号": "3", "画面描述": "品牌收束"}]
        missing = _traceability_with_output(traces, rows, [])[0]
        self.assertEqual(missing["plan_status"], "covered")
        self.assertEqual(missing["output_status"], "pending_review")
        self.assertEqual(missing["final_status"], "pending_review")
        self.assertEqual(traces, original)
        rows[1]["画面描述"] = "观众在产品体验区操作装置"
        verified = _traceability_with_output([missing], rows, [])[0]
        self.assertEqual(verified["output_status"], "pending_review")
        self.assertEqual(verified["final_status"], "pending_review")
        self.assertEqual(verified["output_evidence"][0]["verified_fields"], [])
        self.assertTrue(verified["output_evidence"][0]["content_evidence"]["mentioned"])
        rows[1]["画面描述"] = "会场空镜"
        changed = _traceability_with_output([verified], rows, [])[0]
        self.assertEqual(changed["output_status"], "pending_review")
        self.assertEqual(changed["output_evidence"][0]["visual_description"], "会场空镜")

    def test_optional_unassigned_negated_and_unproved_position_remain_reviewable(self):
        for text, rule in (("不安排产品体验", {}), ("产品体验待确认", {}),
                           ("产品体验", {"type": "closing"}),
                           ("产品体验", {"type": "after", "anchor_text": "发言"})):
            with self.subTest(text=text, rule=rule):
                item = _traceability_with_output([{
                    "brief_text": "产品体验", "plan_shot_nos": ["1"],
                    "final_status": "covered", "position_rule": rule,
                }], [{"镜号": "1", "画面描述": text}, {"镜号": "2", "画面描述": "收束"}], [])[0]
                self.assertEqual(item["output_status"], "pending_review")
        item = _traceability_with_output([{
            "brief_text": "可选茶歇", "classification": "optional",
            "plan_shot_nos": [], "final_status": "optional",
        }], [{"镜号": "1", "画面描述": "会场"}], [])[0]
        self.assertEqual(item["final_status"], "optional")
        self.assertEqual(item["output_evidence"], [])

    def test_edit_rebinds_only_changed_content_and_preserves_parent(self):
        rows = [{"镜号": "1", "画面描述": "工人挂画。"},
                {"镜号": "2", "画面描述": "观众观看LED。"}]
        traces = [{"requirement_id": "event_1", "brief_text": "挂画", "plan_shot_nos": ["1"]},
                  {"requirement_id": "event_2", "brief_text": "LED", "plan_shot_nos": ["2"]}]
        identities = {"1": "stable-a", "2": "stable-b"}
        parent = _traceability_with_output(traces, rows, [], shot_ids_by_no=identities)
        original = deepcopy(parent)
        rows[0]["画面描述"] = "工人离场。"
        child = _traceability_with_output(parent, rows, [], shot_ids_by_no=identities)
        before, after = parent[0]["output_evidence"][0], child[0]["output_evidence"][0]
        self.assertNotEqual(before["content_evidence"]["script_sha256"], after["content_evidence"]["script_sha256"])
        self.assertEqual(after["content_evidence"]["fragments"], [])
        self.assertEqual(after["content_evidence"]["shot_id"], "stable-a")
        self.assertEqual(after["content_evidence"]["requirement_id"], "event_1")
        self.assertEqual(parent[1]["output_evidence"], child[1]["output_evidence"])
        self.assertEqual(parent, original)
        self.assertEqual(child[0]["final_status"], "pending_review")
