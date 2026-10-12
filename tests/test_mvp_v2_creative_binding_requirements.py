"""Regression protection for confirmed Brief requirements during conversion."""

import unittest

from mvp_v2.planning_core import compile_plan_artifact
from tests.test_mvp_v2_planning_core import _slot


class CreativeBindingRequirementTests(unittest.TestCase):
    def test_each_canonical_flow_field_retains_confirmed_order(self):
        binding = {"draft_id": "draft_anonymous", "draft_revision": 0,
                   "payload_ref": "artifact://anonymous/draft", "payload_sha256": "a" * 64,
                   "blocks": [{"block_id": f"block_{i}", "fields": {"画面描述": value,
                               "声音": "现场声音"}, "source_refs": []}
                              for i, value in enumerate(["客户交流", "展示产品", "品牌落版"], 1)]}
        for field in ("活动流程", "事件流程", "流程事件"):
            with self.subTest(field=field):
                brief = {"项目名称": "匿名活动", "脚本类型": "发布会", "视频时长": "60s",
                         "必须出现的内容": "客户交流；展示产品", field: "展示产品；客户交流；品牌落版"}
                plan = compile_plan_artifact(brief, [_slot(1, "开场"), _slot(2, "活动"), _slot(3, "落版")],
                    source_view="ai-spec", constraints={"creative_draft_binding": binding, "planned_shot_count": 12})
                self.assertEqual(plan.audit["status"], "needs_clarification")
                self.assertTrue(any(c["type"] == "creative_brief_flow_conflict" for c in plan.conflicts))

    def test_section_titles_bind_topics_and_placeholders_do_not_confirm_unknown_brief_facts(self):
        brief = {"项目名称": "匿名活动", "脚本类型": "发布会", "视频时长": "60s",
                 "必须出现的内容": "品牌产品；客户交流"}
        binding = {"draft_id": "draft_anonymous", "draft_revision": 0,
                   "payload_ref": "artifact://anonymous/draft", "payload_sha256": "a" * 64,
                   "blocks": [{"block_id": f"block_{i}", "fields": {"段落": title,
                               "画面描述": visual, "声音": "现场声音"}, "source_refs": []}
                              for i, (title, visual) in enumerate([
                                  ("品牌产品", "产品轮廓在灯光中逐渐清晰。"),
                                  ("客户交流", "客户与工作人员在会场内交流。"),
                                  ("收尾", "主视觉定格，不添加未知口号。"),
                              ], 1)]}
        slots = [_slot(1, "开场"), _slot(2, "活动"), _slot(3, "落版")]
        plan = compile_plan_artifact(brief, slots, source_view="ai-spec", constraints={"creative_draft_binding": binding})
        self.assertEqual(plan.audit["status"], "pass")
        for index in (1, 2):
            row = next(row for row in plan.coverage if row["requirement_id"] == f"mandatory_required_{index:03d}")
            actual = [shot["shot_no"] for shot in plan.shots if f"creative_block_{index}" in shot["event_ids"]]
            self.assertEqual(row["shot_nos"], actual)
        pending = compile_plan_artifact({**brief, "必须出现的内容": "品牌产品；客户交流；待确认嘉宾致辞"},
            slots, source_view="ai-spec", constraints={"creative_draft_binding": binding})
        self.assertEqual(pending.audit["status"], "needs_clarification")
        unknown = next(row for row in pending.coverage if row["requirement_id"] == "mandatory_required_003")
        self.assertEqual(unknown["status"], "needs_fact")

    def test_selected_order_respects_explicit_positions_or_reports_actual_conflict(self):
        binding = {"draft_id": "draft_anonymous", "draft_revision": 0,
                   "payload_ref": "artifact://anonymous/draft", "payload_sha256": "a" * 64,
                   "blocks": [{"block_id": f"block_{i}", "fields": {"画面描述": value,
                               "声音": "现场声音"}, "source_refs": []}
                              for i, value in enumerate(["展示产品", "客户交流", "品牌落版"], 1)]}
        for requirement in ("第6镜展示产品", "开头客户交流", "结尾展示产品", "客户交流之后展示产品"):
            with self.subTest(requirement=requirement):
                brief = {"项目名称": "匿名活动", "脚本类型": "发布会", "视频时长": "60s",
                         "必须出现的内容": requirement}
                plan = compile_plan_artifact(brief, [_slot(1, "开场"), _slot(2, "活动"), _slot(3, "落版")],
                    source_view="ai-spec", constraints={"creative_draft_binding": binding, "planned_shot_count": 12})
                positions = {event: int(shot["shot_no"]) for shot in plan.shots for event in shot["event_ids"]}
                actual = positions["creative_block_1"]
                if requirement == "第6镜展示产品":
                    self.assertEqual(actual, 6)
                    self.assertEqual(plan.audit["status"], "pass")
                else:
                    self.assertEqual(plan.audit["status"], "needs_clarification")
                    self.assertTrue(any(c["type"] == "creative_brief_flow_conflict" for c in plan.conflicts))
                coverage = next(row for row in plan.coverage if row["requirement_id"] == "mandatory_required_001")
                expected_block = "creative_block_2" if requirement == "开头客户交流" else "creative_block_1"
                self.assertEqual(coverage["shot_nos"], [str(positions[expected_block])])

    def test_multi_paragraph_scene_descriptions_keep_selected_order_across_template_roles(self):
        brief = {"项目名称": "匿名客户活动", "脚本类型": "发布会", "视频时长": "60s",
                 "必须出现的内容": "客户交流；品牌产品"}
        visuals = [
            "灯光勾勒舞台与品牌产品展示区，主视觉逐渐浮现。",
            "主持人触发启动装置，科技线条沿舞台展开。",
            "品牌产品在几何背景中旋转，清晰展示产品轮廓。",
            "客户交流与握手自然展开，现场以中景和细节交替呈现。",
            "舞台出现答谢画面，品牌代表向观众致意。",
            "品牌主视觉定格，不添加未知口号，日期位置留待补充。",
        ]
        binding = {"draft_id": "draft_anonymous", "draft_revision": 0,
                   "payload_ref": "artifact://anonymous/draft", "payload_sha256": "a" * 64,
                   "blocks": [{"block_id": f"block_{i}", "fields": {"画面描述": visual,
                               "声音": "音乐与现场声音"}, "source_refs": []}
                              for i, visual in enumerate(visuals, 1)]}
        plan = compile_plan_artifact(brief, [_slot(1, "入场"), _slot(2, "活动"), _slot(3, "落版")],
                                     source_view="ai-spec", constraints={"creative_draft_binding": binding})
        mapped = [event_id for shot in plan.shots for event_id in shot["event_ids"]
                  if event_id.startswith("creative_")]
        self.assertEqual(mapped, ["creative_block_" + str(i) for i in range(1, 7)])
        self.assertEqual(len(mapped), len(set(mapped)))
        self.assertEqual(plan.audit["status"], "pass")
        self.assertTrue(all(event["fact_status"] == "creative_expression" for event in plan.events))
        self.assertTrue(any(shot.get("creative_order_note") for shot in plan.shots))
        self.assertEqual(sum(shot["duration_seconds"] for shot in plan.shots), 60)

    def test_accepted_draft_cannot_silently_reverse_confirmed_brief_flow(self):
        confirmed_flow = ["开场造势", "嘉宾致辞", "启动亮灯", "收束"]
        brief = {
            "项目名称": "匿名仪式",
            "脚本类型": "发布会",
            "视频时长": "60s",
            "必须出现的内容": "嘉宾致辞；启动亮灯",
            "planning_events": [
                {"title": title, "order": order, "evidence": "已确认流程顺序",
                 "fact_status": "confirmed", "allow_non_linear": False}
                for order, title in enumerate(confirmed_flow, 1)
            ],
        }
        slots = [_slot(1, "入场与空间建立"), _slot(2, "人物与产品互动"), _slot(3, "品牌落版")]
        # The same anonymous multi-event Brief is valid in the original journey.
        baseline = compile_plan_artifact(
            brief, slots, source_view="ai-spec", constraints={"planned_shot_count": 16},
        )
        self.assertEqual(baseline.audit["status"], "pass")
        accepted_flow = ["开场造势", "启动亮灯", "嘉宾致辞", "收束"]
        binding = {
            "draft_id": "draft_anonymous", "draft_revision": 0,
            "payload_ref": "artifact://anonymous/draft", "payload_sha256": "a" * 64,
            "blocks": [
                {"block_id": f"block_{index}",
                 "fields": {"画面描述": title, "声音": "现场声音"}, "source_refs": []}
                for index, title in enumerate(accepted_flow, 1)
            ],
        }
        converted = compile_plan_artifact(
            brief, slots, source_view="ai-spec",
            constraints={"planned_shot_count": 16, "creative_draft_binding": binding},
        )
        coverage = {row["requirement_id"]: row for row in converted.coverage}
        speech = coverage["mandatory_required_001"]
        ceremony = coverage["mandatory_required_002"]
        if speech["status"] == ceremony["status"] == "covered":
            silently_reversed = min(map(int, speech["shot_nos"])) > min(map(int, ceremony["shot_nos"]))
            self.assertFalse(
                silently_reversed and converted.audit["status"] == "pass",
                "已确认的致辞→亮灯流程被初稿倒置，但规划将两项标为 covered 并直接通过；"
                "应保留原流程，或明确报告流程冲突等待用户决定。",
            )


if __name__ == "__main__":
    unittest.main()
