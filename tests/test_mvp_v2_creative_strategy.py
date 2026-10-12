import json
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from mvp_v2.creative_strategy import (
    CreativeStrategyError,
    build_creative_strategies,
    select_creative_strategy,
)
from mvp_v2.models import SourceShot
from mvp_v2.planning_core import compile_plan_artifact


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "creative_strategy"
    / "six_category_briefs.json"
)


def _slots(script_type: str, count: int = 3) -> list[SourceShot]:
    return [
        SourceShot(
            script_type=script_type,
            source_file=f"anonymous-{script_type}-ai-spec.xlsx",
            source_sheet="AI_规范表",
            source_row=index + 10,
            shot_no=str(index),
            structure="建立" if index == 1 else "推进" if index < count else "收束",
            duration_seconds="4",
            visual_desc=f"匿名功能槽 {index}",
            shot_function=f"匿名镜头功能 {index}",
            visual_subject=f"匿名事件主体 {index}",
            reuse_level="镜头功能可复用",
            slot_index=index,
        )
        for index in range(1, count + 1)
    ]


class CreativeStrategyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

    def setUp(self):
        self.brief = deepcopy(self.cases[-1]["brief"])
        self.slots = _slots(self.brief["脚本类型"])
        self.portfolio = build_creative_strategies(self.brief, self.slots)

    def assert_error_code(self, code: str, callable_):
        with self.assertRaises(CreativeStrategyError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, code)

    def test_six_anonymous_categories_offer_three_audited_directions(self):
        self.assertEqual(
            [item["script_type"] for item in self.cases],
            ["发布会", "展会", "年会", "赛事", "音乐节", "明星活动"],
        )
        for case in self.cases:
            with self.subTest(script_type=case["script_type"]):
                brief = deepcopy(case["brief"])
                portfolio = build_creative_strategies(
                    brief,
                    _slots(case["script_type"]),
                )

                self.assertEqual(portfolio["status"], "pass")
                self.assertEqual(portfolio["estimated_model_calls"], 0)
                self.assertEqual(
                    [item["strategy_id"] for item in portfolio["directions"]],
                    ["linear_fact_order", "focus_first", "emotional_progression"],
                )
                self.assertTrue(all(item["selectable"] for item in portfolio["directions"]))
                self.assertTrue(portfolio["coverage"]["comparison_ready"])
                input_titles = sorted(item["title"] for item in brief["planning_events"])
                for direction in portfolio["directions"]:
                    self.assertEqual(
                        sorted(item["title"] for item in direction["planning_events"]),
                        input_titles,
                    )
                    self.assertEqual(direction["recompile_audit"]["status"], "pass")
                    self.assertEqual(direction["estimated_model_calls"], 0)
                    self.assertTrue(all(item["text"] for item in direction["brief_evidence"]))
                    self.assertEqual(
                        direction["planned_shot_count"],
                        portfolio["baseline"]["planned_shot_count"],
                    )
                self.assertEqual(len(portfolio["comparison_matrix"]["rows"]), 3)
                self.assertIn(
                    "confirmed_brief_facts",
                    portfolio["comparison_matrix"]["rows"][1]["will_not_change"],
                )

    def test_default_hard_slot_quantity_is_preserved_without_explicit_tradeoff(self):
        brief = deepcopy(self.brief)
        brief["视频时长"] = "25s"
        brief["必须出现的内容"] = "事件一、事件二、事件三、事件四、事件五"
        brief["叙事重点"] = "事件四"
        brief["重点前置理由"] = "Brief 明确事件四为传播重点"
        brief["情绪递进理由"] = "Brief 明确按事件强度逐步推进"
        brief["planning_events"] = [
            {
                "title": f"事件{label}",
                "evidence": f"Brief 明确事件{label}",
                "shot_units": 1,
                "emphasis": index == 4,
                "emphasis_evidence": f"Brief 对事件{label}的重点标记" if index == 4 else "",
                "emotional_intensity": intensity,
                "emotion_evidence": f"Brief 对事件{label}标记强度 {intensity}",
            }
            for index, (label, intensity) in enumerate(
                zip(("一", "二", "三", "四", "五"), (1, 2, 4, 5, 3)),
                start=1,
            )
        ]

        portfolio = build_creative_strategies(brief, _slots("明星活动", 2))

        self.assertEqual(portfolio["baseline"]["event_count"], 5)
        baseline_count = portfolio["baseline"]["planned_shot_count"]
        self.assertGreaterEqual(baseline_count, 18)
        self.assertTrue(all(
            item["planned_shot_count"] == baseline_count
            for item in portfolio["directions"]
        ))
        self.assertTrue(all(item["duration_budget"]["planned_seconds"] == 25 for item in portfolio["directions"]))

    def test_pending_fact_marks_every_direction_non_selectable(self):
        brief = deepcopy(self.brief)
        brief["planning_events"][1]["fact_status"] = "pending_confirmation"
        brief["planning_events"][1]["evidence"] = "嘉宾身份待确认"

        portfolio = build_creative_strategies(brief, self.slots)

        self.assertEqual(portfolio["status"], "needs_clarification")
        self.assertTrue(all(not item["selectable"] for item in portfolio["directions"]))
        self.assertTrue(all(
            "fact_missing" in item["recompile_audit"]["clarification_codes"]
            for item in portfolio["directions"]
        ))
        self.assert_error_code(
            "STRATEGY_COMPARISON_NOT_READY",
            lambda: select_creative_strategy(
                brief,
                self.slots,
                portfolio=portfolio,
                plan_sha256=portfolio["plan_sha256"],
                strategy_id="linear_fact_order",
                expected_strategy_version=1,
            ),
        )

    def test_non_linear_directions_require_explicit_reason(self):
        brief = deepcopy(self.brief)
        brief.pop("重点前置理由")
        brief.pop("情绪递进理由")

        portfolio = build_creative_strategies(brief, self.slots)

        self.assertEqual(
            [item["strategy_id"] for item in portfolio["directions"]],
            ["linear_fact_order"],
        )
        self.assertEqual(portfolio["status"], "needs_clarification")
        self.assertFalse(portfolio["coverage"]["comparison_ready"])
        self.assertEqual(
            [item["reason_code"] for item in portfolio["readiness_reasons"]],
            ["comparable_directions_insufficient"],
        )
        self.assertEqual(
            {item["reason_code"] for item in portfolio["unsupported_directions"]},
            {"focus_evidence_missing", "emotion_evidence_missing"},
        )
        self.assert_error_code(
            "STRATEGY_COMPARISON_NOT_READY",
            lambda: select_creative_strategy(
                brief,
                self.slots,
                portfolio=portfolio,
                plan_sha256=portfolio["plan_sha256"],
                strategy_id="linear_fact_order",
                expected_strategy_version=1,
            ),
        )

    def test_non_linear_events_carry_reason_into_p00_audit(self):
        focus = next(
            item for item in self.portfolio["directions"]
            if item["strategy_id"] == "focus_first"
        )

        self.assertTrue(focus["baseline_diff"]["event_order_changed"])
        self.assertTrue(focus["non_linear_reason"])
        self.assertTrue(all(item["allow_non_linear"] for item in focus["planning_events"]))
        self.assertTrue(all(item["non_linear_reason"] for item in focus["planning_events"]))
        self.assertEqual(focus["recompile_audit"]["status"], "pass")

    def test_same_input_and_version_are_deterministic_and_use_zero_provider_calls(self):
        with patch("mvp_v2.text_providers.build_text_provider") as provider_factory:
            first = build_creative_strategies(self.brief, self.slots)
            second = build_creative_strategies(self.brief, self.slots)

        self.assertEqual(first, second)
        self.assertEqual(first["portfolio_sha256"], second["portfolio_sha256"])
        provider_factory.assert_not_called()

    def test_selection_returns_only_controlled_planning_inputs_and_reaudits(self):
        selection = select_creative_strategy(
            self.brief,
            self.slots,
            portfolio=self.portfolio,
            plan_sha256=self.portfolio["plan_sha256"],
            strategy_id="focus_first",
            expected_strategy_version=1,
        )

        self.assertEqual(selection["binding"]["strategy_id"], "focus_first")
        self.assertEqual(selection["binding"]["plan_sha256"], self.portfolio["plan_sha256"])
        self.assertEqual(selection["recompile_audit"]["status"], "pass")
        self.assertEqual(selection["planning_events"][0]["title"], "嘉宾亮相")
        self.assertEqual(
            selection["constraints"]["planned_shot_count"],
            self.portfolio["baseline"]["planned_shot_count"],
        )
        self.assertEqual(selection["provider_calls"], 0)
        self.assertNotIn("script_rows", selection)
        self.assertNotIn("shot_plans", selection)
        self.assertNotIn("publication", selection)

    def test_tamper_parameter_drift_unknown_strategy_and_version_are_rejected(self):
        tampered = deepcopy(self.portfolio)
        tampered["directions"][0]["name"] = "被篡改"
        self.assert_error_code(
            "STRATEGY_ARTIFACT_TAMPERED",
            lambda: select_creative_strategy(
                self.brief,
                self.slots,
                portfolio=tampered,
                plan_sha256=self.portfolio["plan_sha256"],
                strategy_id="linear_fact_order",
                expected_strategy_version=1,
            ),
        )
        drifted = deepcopy(self.brief)
        drifted["planning_events"][0]["evidence"] = "Brief 证据已更新"
        self.assert_error_code(
            "PLAN_SHA256_MISMATCH",
            lambda: select_creative_strategy(
                drifted,
                self.slots,
                portfolio=self.portfolio,
                plan_sha256=self.portfolio["plan_sha256"],
                strategy_id="linear_fact_order",
                expected_strategy_version=1,
            ),
        )
        self.assert_error_code(
            "STRATEGY_NOT_FOUND",
            lambda: select_creative_strategy(
                self.brief,
                self.slots,
                portfolio=self.portfolio,
                plan_sha256=self.portfolio["plan_sha256"],
                strategy_id="invented_strategy",
                expected_strategy_version=1,
            ),
        )
        self.assert_error_code(
            "STRATEGY_VERSION_CONFLICT",
            lambda: select_creative_strategy(
                self.brief,
                self.slots,
                portfolio=self.portfolio,
                plan_sha256=self.portfolio["plan_sha256"],
                strategy_id="linear_fact_order",
                expected_strategy_version=2,
            ),
        )

    def test_selection_rejects_when_final_p00_recompile_regresses(self):
        calls = 0

        def regressing_compile(*args, **kwargs):
            nonlocal calls
            calls += 1
            projection = compile_plan_artifact(*args, **kwargs)
            if calls == 5:
                projection.audit["status"] = "blocked"
                projection.audit["blocking_codes"].append("forced_regression")
            return projection

        with patch(
            "mvp_v2.creative_strategy.compile_plan_artifact",
            side_effect=regressing_compile,
        ):
            self.assert_error_code(
                "RECOMPILED_PLAN_REJECTED",
                lambda: select_creative_strategy(
                    self.brief,
                    self.slots,
                    portfolio=self.portfolio,
                    plan_sha256=self.portfolio["plan_sha256"],
                    strategy_id="linear_fact_order",
                    expected_strategy_version=1,
                ),
            )


if __name__ == "__main__":
    unittest.main()
