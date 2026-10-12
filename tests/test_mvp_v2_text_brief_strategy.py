import json
import unittest

from mvp_v2.excel_io import BriefError, read_text_brief
from mvp_v2.local_ui import brief_form_from_brief, serialize_brief
from mvp_v2.models import SourceShot
from mvp_v2.planning_core import compile_plan_artifact


BASE = """项目名称：匿名策略项目
项目区域文件名：匿名策略项目.xlsx
脚本类型：发布会
品牌或客户：匿名品牌
行业或活动类别：发布会
视频时长：60s
风格调性：克制、清晰
必须出现的内容：开场、产品亮相、收束
禁忌或不能出现的内容：不虚构事实
素材目录或素材文件：
备注：匿名验收
"""


class TextBriefStrategyTests(unittest.TestCase):
    def test_controlled_strategy_fields_survive_text_handoff(self):
        events = [
            {
                "title": "产品亮相",
                "evidence": "Brief 明确产品亮相",
                "fact_status": "confirmed",
                "order": 2,
                "shot_units": 1,
                "allow_non_linear": True,
                "non_linear_reason": "Brief 明确重点前置",
            },
            {
                "title": "开场",
                "evidence": "Brief 明确开场",
                "fact_status": "confirmed",
                "order": 1,
                "shot_units": 1,
                "allow_non_linear": True,
                "non_linear_reason": "Brief 明确重点前置",
            },
        ]
        binding = {
            "schema_version": "mplus-brief-strategy-binding/v1",
            "selection_sha256": "a" * 64,
        }
        brief = read_text_brief(
            BASE
            + "M+规划事件JSON："
            + json.dumps(events, ensure_ascii=False, separators=(",", ":"))
            + "\nM+创作策略绑定JSON："
            + json.dumps(binding, ensure_ascii=False, separators=(",", ":"))
        )

        self.assertEqual(brief["planning_events"], events)
        self.assertEqual(brief["creative_strategy_selection"], binding)

    def test_e03_internal_launch_form_preserves_selected_strategy(self):
        events = [
            {
                "title": "产品亮相",
                "evidence": "Brief 明确产品亮相",
                "fact_status": "confirmed",
                "order": 1,
                "shot_units": 1,
                "allow_non_linear": False,
                "non_linear_reason": "",
            }
        ]
        binding = {
            "schema_version": "mplus-brief-strategy-binding/v1",
            "selection_sha256": "b" * 64,
        }
        source = read_text_brief(BASE)
        source["planning_events"] = events
        source["creative_strategy_selection"] = binding

        form = brief_form_from_brief(source)
        restored = read_text_brief(serialize_brief(form.to_brief()))

        self.assertEqual(restored["planning_events"], events)
        self.assertEqual(restored["creative_strategy_selection"], binding)

    def test_malformed_controlled_json_is_blocked(self):
        with self.assertRaisesRegex(BriefError, "不是有效 JSON"):
            read_text_brief(BASE + "M+规划事件JSON：not-json")

        with self.assertRaisesRegex(BriefError, "必须是 array"):
            read_text_brief(BASE + "M+规划事件JSON：{}")

    def test_text_handoff_reaches_p00_in_selected_event_order(self):
        source = read_text_brief(BASE)
        source["planning_events"] = [
            {
                "title": "产品亮相",
                "evidence": "Brief 明确产品亮相",
                "fact_status": "confirmed",
                "order": 2,
                "shot_units": 1,
                "allow_non_linear": True,
                "non_linear_reason": "Brief 明确重点前置",
            },
            {
                "title": "开场",
                "evidence": "Brief 明确开场",
                "fact_status": "confirmed",
                "order": 1,
                "shot_units": 1,
                "allow_non_linear": True,
                "non_linear_reason": "Brief 明确重点前置",
            },
        ]
        restored = read_text_brief(
            serialize_brief(brief_form_from_brief(source).to_brief())
        )
        slots = [
            SourceShot(
                script_type="发布会",
                source_file="anonymous-ai-spec.xlsx",
                source_sheet="AI_规范表",
                source_row=10 + index,
                shot_no=str(index),
                structure="推进",
                duration_seconds="7.5",
                visual_desc=f"匿名槽位 {index}",
                shot_function=f"功能 {index}",
                visual_subject=f"主体 {index}",
                reuse_level="镜头功能可复用",
                slot_index=index,
            )
            for index in range(1, 9)
        ]

        plan = compile_plan_artifact(restored, slots, source_view="ai-spec")

        self.assertEqual(plan.events[0]["title"], "产品亮相")
        self.assertEqual(plan.events[1]["title"], "开场")
        self.assertEqual(plan.audit["status"], "needs_clarification")
        self.assertIn("hard_template_conflict_unresolved", plan.audit["clarification_codes"])
        self.assertEqual(plan.decision_counts["unresolved_conflicts"], 1)


if __name__ == "__main__":
    unittest.main()
