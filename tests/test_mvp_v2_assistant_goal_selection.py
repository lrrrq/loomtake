from __future__ import annotations

from copy import deepcopy
import json
import unittest

from mvp_v2.formal_script_assistant import analyze_formal_script_instruction
from tests.test_mvp_v2_formal_script_assistant import _Editor


# Exact anonymous provider text from real-provider-v3/mandatory-wide-only on
# 2026-09-08. Its JSON is valid, but it destroys a confirmed mandatory wide shot.
_REAL_MANDATORY_WIDE_FAILURE = """{
  "schema_version": "mplus-formal-script-assistant-editor/v1",
  "operations": [
    {"shot_id": "anonymous-shot-1", "field": "shot_size", "value": "特写"},
    {"shot_id": "anonymous-shot-1", "field": "visual", "value": "固定特写聚焦白色演示样机面板上唯一的圆形按钮，按钮表面留有演示人员指尖即将按下的阴影。拍摄建议：以按钮为画面中心，浅景深虚化背景中的演示桌边缘，保留按钮的材质细节。"}
  ],
  "shot_reasons": [
    {"shot_id": "anonymous-shot-1", "reason": "将开场必拍远景改为特写，以突出产品核心操作细节，同时通过浅景深保留空间暗示，满足Brief对操作展示的强调。"}
  ]
}"""


# Exact anonymous count response from real-provider-v3/verified/count-two.
# Valid operations alone do not establish that the original press was preserved.
_REAL_COUNT_ACTION_FAILURE = """{
  "schema_version": "mplus-formal-script-assistant-editor/v1",
  "operations": [
    {"shot_id": "anonymous-shot-2", "field": "shot_size", "value": "特写"},
    {"shot_id": "anonymous-shot-2", "field": "visual", "value": "拍摄建议：固定特写捕捉演示人员引导观众时的手势，手指指向演示桌方向，画面以手部动作为主体，背景虚化保留环境氛围。"},
    {"shot_id": "anonymous-shot-3", "field": "shot_size", "value": "特写"},
    {"shot_id": "anonymous-shot-3", "field": "visual", "value": "拍摄建议：固定特写对准白色演示样机面板上的圆形按钮，演示人员的指尖悬停在按钮上方，即将按下，画面突出按钮的质感和手指的接触瞬间。"}
  ],
  "shot_reasons": [
    {"shot_id": "anonymous-shot-2", "reason": "将引导动作改为特写，强化手势指引的细节，同时保留固定机位和实拍约束。"},
    {"shot_id": "anonymous-shot-3", "reason": "将操作展示改为特写，突出按钮与手指的接触细节，增强产品演示的视觉重点。"}
  ]
}"""

# Exact unsupported physical detail from the fixed-SHA goal recheck.
_REAL_UNKNOWN_MATERIAL_FAILURE = (
    "拍摄建议：固定特写框住演示人员伸出的手指，指尖正按下白色样机面板上唯一的圆形按钮，"
    "按钮表面随下压产生轻微形变，手指与按钮的接触清晰可见。"
)


def _shots():
    return [{
        "shot_id": f"shot-{i}", "shot_no": str(i),
        "structure": "空间交代" if i == 1 else "内容展示",
        "camera_type": "实拍", "shot_size": "特写" if i == 10 else "全景",
        "camera_move": "固定",
        "visual": "远景交代空间布局。" if i == 1 else f"第{i}镜展示产品操作。",
        "sound": "指定底乐连续铺底。", "voiceover": "产品操作演示。",
        "subtitle": "产品操作", "notes": "",
    } for i in range(1, 11)]


def _closeups(*numbers):
    return [op for number in numbers for op in (
        {"shot_id": f"shot-{number}", "field": "shot_size", "value": "特写"},
        {"shot_id": f"shot-{number}", "field": "visual", "value": f"拍摄建议：固定机位展示第{number}镜产品按键细节。"},
    )]


def _response(operations, **extra):
    return json.dumps({
        "schema_version": "mplus-formal-script-assistant-editor/v1",
        "operations": operations, **extra,
    }, ensure_ascii=False)


class AssistantGoalSelectionTests(unittest.TestCase):
    def _analyze(self, instruction, operations, *, selected=None, locked=(), extra=None, shots=None, context=None):
        shots = deepcopy(shots or _shots())
        original = deepcopy(shots)
        editor = _Editor(_response(operations, **(extra or {})))
        result = analyze_formal_script_instruction(
            instruction=instruction, shots=shots,
            assistant_locked_shot_ids=list(locked), selected_shot_ids=selected,
            editor_policy=editor.policy(), context=context if context is not None else {
                "brief": {"必须出现的内容": "保留开场空间交代"},
                "neighbor_shots": deepcopy(shots),
                "technical_candidates": {"shot_size": ["全景", "特写"]},
                "allow_original_adaptation": True,
            },
        )
        self.assertEqual(shots, original, "分析不得改写调用方镜头快照")
        return result, editor

    def test_recorded_count_action_failure_keeps_source_action_and_completion_in_editor_contract(self):
        shots = _shots()
        for shot in shots:
            shot["shot_id"] = f"anonymous-shot-{shot['shot_no']}"
        shots[1]["visual"] = "演示人员在入口侧引导到场观众走向演示桌，固定中景保留两者的交流关系。"
        shots[2]["visual"] = "演示人员伸出手指按下白色演示样机面板上唯一的圆形按钮，固定中景同时看见手和样机。"
        before = deepcopy(shots)
        editor = _Editor(_REAL_COUNT_ACTION_FAILURE)
        analyze_formal_script_instruction(
            instruction="把其中2镜改成特写", shots=shots, assistant_locked_shot_ids=[],
            editor_policy=editor.policy(), context={"technical_candidates": {"shot_size": ["全景", "特写"]}},
        )
        self.assertEqual(shots, before)
        self.assertEqual(len(editor.calls), 1, "完成态约束由同次受限改写消费，不增加质量模型调用")
        prompt = editor.calls[0]["prompt"]
        data = json.loads(prompt.split("\nDATA:", 1)[1])
        target = next(item for item in data["targets"]
                      if item["shot_id"] == "anonymous-shot-3" and item["field"] == "visual")
        self.assertEqual(target["current_value"], before[2]["visual"])
        self.assertIn("起点、过程与完成态", target["rewrite_requirements"])
        self.assertIn("不能把“按下按钮”退成“悬停”", prompt)
        self.assertIn("不把尝试改成成功", prompt)

    def test_goal_reads_full_context_but_only_returns_necessary_allowed_subset(self):
        reasons = [{"shot_id": "shot-3", "reason": "集中展示操作细节。"},
                   {"shot_id": "shot-5", "reason": "补充产品局部，保留空间交代。"}]
        result, editor = self._analyze("增加特写", _closeups(3, 5), locked=["shot-1"], extra={"shot_reasons": reasons})
        self.assertEqual(result["type"], "rewrite_operations", result)
        self.assertEqual(result["selection_mode"], "goal")
        self.assertEqual(result["actual_changed_shot_ids"], ["shot-3", "shot-5"])
        self.assertEqual(result["allowed_shot_ids"], [f"shot-{i}" for i in range(2, 10)])
        self.assertEqual(result["shot_reasons"], reasons)
        self.assertEqual(len(editor.calls), 1)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual(len(data["context"]["neighbor_shots"]), 10)
        self.assertNotIn("shot-1", {target["shot_id"] for target in data["targets"]})
        self.assertEqual(data["selection_mode"], "goal")

    def test_goal_and_count_share_narrative_selection_without_changing_explicit_all_scope(self):
        for instruction, mode in (("增加特写", "goal"), ("把其中2镜改成特写", "count"),
                                  ("所选镜头全部改为特写", "all")):
            with self.subTest(mode=mode):
                _, editor = self._analyze(instruction, _closeups(3, 5), selected=["shot-3", "shot-5"])
                self.assertEqual(len(editor.calls), 1)
                prompt = editor.calls[0]["prompt"]
                data = json.loads(prompt.split("\nDATA:", 1)[1])
                self.assertEqual(data["selection_mode"], mode)
                if mode in {"goal", "count"}:
                    self.assertIn("只有细节或表达得到具体改善、原叙事职责仍完整", prompt)
                    self.assertIn("衣角或手部陪衬不能替代双方同框关系", prompt)
                else:
                    self.assertNotIn("只有细节或表达得到具体改善、原叙事职责仍完整", prompt)
                if mode == "count":
                    self.assertEqual(data["requested_count"], 2)
                    self.assertIn("不能按许可列表顺序直接取前几镜", prompt)
                    self.assertNotIn("不规定修改数量或比例", prompt)

    def test_recorded_unknown_material_detail_is_constrained_by_evidence_in_same_editor_call(self):
        shots = _shots()
        shots[2]["visual"] = "演示人员伸出手指按下白色演示样机面板上唯一的圆形按钮，固定中景同时看见手和样机。"
        operations = _closeups(3)
        operations[1]["value"] = _REAL_UNKNOWN_MATERIAL_FAILURE
        _, editor = self._analyze("增加特写", operations, selected=["shot-3"], shots=shots)
        self.assertEqual(len(editor.calls), 1)
        prompt = editor.calls[0]["prompt"]
        data = json.loads(prompt.split("\nDATA:", 1)[1])
        target = next(item for item in data["targets"] if item["field"] == "visual")
        self.assertEqual(target["current_value"], shots[2]["visual"])
        self.assertNotIn("形变", target["current_value"], "源稿没有未知材质反馈，不能由按压动作推断")
        self.assertIn("材质、机械结构、功能和物理反馈未知时", prompt)
        self.assertIn("未经确认的表面形变、发光、震动或其他状态变化", prompt)
        self.assertIn("已有明确输入依据的属性可以保留", prompt)

    def test_explicit_all_replaces_every_eligible_selected_shot_and_keeps_satisfied_shot(self):
        result, editor = self._analyze("所选镜头全部改为特写", _closeups(3, 5), selected=["shot-3", "shot-5", "shot-10"])
        self.assertEqual(result["type"], "rewrite_operations", result)
        self.assertEqual(result["selection_mode"], "all")
        self.assertEqual(result["allowed_shot_ids"], ["shot-3", "shot-5"])
        self.assertEqual(result["actual_changed_shot_ids"], ["shot-3", "shot-5"])
        self.assertEqual(result["shot_reasons"], [], "旧模型输出继续有效，不伪造选择理由")
        self.assertEqual(len(editor.calls), 1)

    def test_all_output_cannot_silently_skip_an_eligible_selected_shot(self):
        result, _ = self._analyze("所选镜头全部改为特写", _closeups(3), selected=["shot-3", "shot-5"])
        self.assertEqual(result["analysis_code"], "editor_selection_incomplete", result)
        self.assertNotIn("operations", result)

    def test_goal_does_not_ban_all_eligible_changes_by_count_alone(self):
        result, _ = self._analyze("增加特写", _closeups(3, 5), selected=["shot-3", "shot-5"])
        self.assertEqual(result["type"], "rewrite_operations", result)
        self.assertEqual(result["selection_mode"], "goal")

    def test_count_goals_choose_two_existing_shots_without_second_scope_roundtrip(self):
        for instruction in (
            "增加2个特写", "增加两个特写", "把其中2镜改成特写", "指定2镜改为特写",
            "在第2至8镜中选2镜改为特写", "把两镜改为特写",
        ):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _closeups(3, 5))
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(result["selection_mode"], "count")
                self.assertEqual(result["requested_count"], 2)
                self.assertEqual(result["actual_changed_shot_ids"], ["shot-3", "shot-5"])
                self.assertEqual((result["shot_count_before"], result["shot_count_after"]), (10, 10))
                self.assertEqual(len(editor.calls), 1)

    def test_count_is_number_of_changed_shots_not_number_of_operations(self):
        for numbers in ((3,), (3, 5, 6), ()):
            with self.subTest(numbers=numbers):
                result, _ = self._analyze("增加2个特写", _closeups(*numbers))
                self.assertEqual(result["analysis_code"], "editor_count_mismatch", result)
                self.assertNotIn("operations", result)

    def test_count_larger_than_available_scope_explains_shortfall_without_invented_targets(self):
        result, editor = self._analyze("增加2个特写", _closeups(3), selected=["shot-3", "shot-10"])
        self.assertEqual(result["analysis_code"], "insufficient_targets", result)
        self.assertNotIn("operations", result)
        self.assertEqual(editor.calls, [])
        self.assertIn("2", result["summary"])
        self.assertIn("1", result["summary"])

    def test_new_shots_still_use_planner_instead_of_local_count_goal(self):
        for instruction in ("新增2个镜头", "请新增两个镜头"):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _closeups(3, 5))
                self.assertEqual(result["type"], "replan_required", result)
                self.assertEqual(result["shot_count_after"], 12)
                self.assertEqual(editor.calls, [])

    def test_explicit_range_conflict_requires_clarification_before_model(self):
        result, editor = self._analyze("第1镜增加特写", _closeups(3), selected=["shot-3"])
        self.assertEqual(result["analysis_code"], "scope_conflict", result)
        self.assertNotIn("operations", result)
        self.assertEqual(editor.calls, [])

    def test_text_can_narrow_visible_permission_without_becoming_conflict(self):
        result, editor = self._analyze("第3镜增加特写", _closeups(3), selected=["shot-3", "shot-5"])
        self.assertEqual(result["type"], "rewrite_operations", result)
        self.assertEqual(result["allowed_shot_ids"], ["shot-3"])
        self.assertEqual(len(editor.calls), 1)

    def test_shot_number_is_not_silently_reinterpreted_as_a_quantity_goal(self):
        for instruction in ("第2镜改为特写", "把2镜改为特写"):
            with self.subTest(instruction=instruction):
                result, _ = self._analyze(instruction, _closeups(2))
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(result["allowed_shot_ids"], ["shot-2"])
                self.assertNotIn("requested_count", result)

    def test_these_two_selected_shots_do_not_become_a_reference_to_shot_two(self):
        for instruction in ("把这两镜改为地面特写", "将这两镜改为特写"):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _closeups(3, 4), selected=["shot-3", "shot-4"])
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(result["actual_changed_shot_ids"], ["shot-3", "shot-4"])
                self.assertEqual(result["requested_count"], 2)
                self.assertEqual(len(editor.calls), 1)
        result, editor = self._analyze("把第2镜改为地面特写", _closeups(2), selected=["shot-3", "shot-4"])
        self.assertEqual(result["analysis_code"], "scope_conflict", result)
        self.assertEqual(editor.calls, [])

    def test_unknown_natural_goal_reaches_existing_controlled_editor(self):
        for instruction in ("整体更好", "这段读起来拧巴", "画面有点呆板"):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, [{
                    "shot_id": "shot-3", "field": "visual", "value": "拍摄建议：固定机位呈现按键与手指的操作关系。",
                }], selected=["shot-3"])
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(len(editor.calls), 1)
                data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
                self.assertTrue({item["field"] for item in data["targets"]}.issubset({"visual", "sound", "voiceover", "subtitle"}))
                self.assertEqual({item["shot_id"] for item in data["targets"]}, {"shot-3"})

    def test_reasons_only_describe_actual_changed_shots_and_stay_short_plain_text(self):
        invalid_reasons = (
            [{"shot_id": "shot-5", "reason": "没有本次建议的镜头"}],
            [{"shot_id": "shot-3", "reason": ""}],
            [{"shot_id": "shot-3", "reason": "长" * 241}],
            [{"shot_id": "shot-3", "reason": "第一行\n第二行"}],
            [{"shot_id": "shot-3", "reason": "建议", "verified": True}],
            [{"shot_id": "shot-3", "reason": "建议"}, {"shot_id": "shot-3", "reason": "重复"}],
        )
        for reasons in invalid_reasons:
            with self.subTest(reasons=reasons):
                result, _ = self._analyze("增加特写", _closeups(3), extra={"shot_reasons": reasons})
                self.assertEqual(result["analysis_code"], "editor_reason_invalid", result)
                self.assertNotIn("operations", result)

    def test_outside_or_locked_targets_remain_rejected_for_goal_and_count(self):
        for instruction, numbers in (("增加特写", (1, 3)), ("增加2个特写", (1, 3))):
            with self.subTest(instruction=instruction):
                result, _ = self._analyze(instruction, _closeups(*numbers), selected=["shot-3", "shot-5"], locked=["shot-1"])
                self.assertEqual(result["analysis_code"], "editor_scope_violation", result)
                self.assertNotIn("operations", result)

    def test_technical_change_requires_complete_visual_suggestion_for_same_shot(self):
        for operations in ([_closeups(3)[0]], [_closeups(3)[1]], [*_closeups(3), _closeups(5)[0]]):
            with self.subTest(operations=operations):
                result, _ = self._analyze("增加特写", operations, selected=["shot-3", "shot-5"])
                self.assertEqual(result["analysis_code"], "editor_value_invalid", result)
                self.assertNotIn("operations", result)

    def test_recorded_real_model_cannot_replace_mandatory_opening_with_button_closeup(self):
        from tests.support.formal_goal_real_provider_probe import anonymous_fixture
        for instruction in ("增加特写", "第1镜改为特写", "所选镜头全部改为特写"):
            with self.subTest(instruction=instruction):
                shots, context = anonymous_fixture()
                before = deepcopy(shots)
                editor = _Editor(_REAL_MANDATORY_WIDE_FAILURE)
                result = analyze_formal_script_instruction(
                    instruction=instruction, shots=shots, selected_shot_ids=[shots[0]["shot_id"]],
                    assistant_locked_shot_ids=[shots[7]["shot_id"]], context=context,
                    editor_policy=editor.policy(),
                )
                self.assertNotIn("operations", result, "虚化背景不能替代 Brief 明确要求的空间远景")
                self.assertIn("必拍", result["summary"])
                self.assertEqual(editor.calls, [], "已知无合法目标时不调用模型")
                self.assertEqual(shots, before)

    def test_recorded_model_ignoring_filtered_mandatory_target_is_rejected_atomically(self):
        from tests.support.formal_goal_real_provider_probe import anonymous_fixture
        shots, context = anonymous_fixture()
        editor = _Editor(_REAL_MANDATORY_WIDE_FAILURE)
        result = analyze_formal_script_instruction(
            instruction="增加特写", shots=shots, selected_shot_ids=None,
            assistant_locked_shot_ids=[shots[7]["shot_id"]], context=context,
            editor_policy=editor.policy(),
        )
        self.assertEqual(result["analysis_code"], "editor_scope_violation", result)
        self.assertNotIn("operations", result)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertNotIn(shots[0]["shot_id"], {target["shot_id"] for target in data["targets"]})

    def test_confirmed_mandatory_plan_positions_protect_only_the_anchored_wide_shot(self):
        for kind, number in (("fixed_shot", 3), ("opening", 1), ("closing", 10)):
            with self.subTest(kind=kind):
                shots = _shots()
                shots[9]["shot_size"] = "全景"
                context = {
                    "current_script_summary": {"requirements": [{
                        "text": "必须保留全景交代空间位置。", "mandatory": True, "fact_status": "confirmed",
                        "origin": "brief", "position_rule": {"type": kind, "shot_no": str(number)},
                    }]},
                    "technical_candidates": {"shot_size": ["全景", "特写"]},
                }
                result, editor = self._analyze("增加特写", _closeups(number),
                    selected=[f"shot-{number}"], shots=shots, context=context)
                self.assertNotIn("operations", result)
                self.assertIn("必拍", result["summary"])
                self.assertEqual(editor.calls, [])
                result, editor = self._analyze("增加特写", _closeups(5),
                    selected=["shot-5"], shots=shots, context=context)
                self.assertEqual(result["actual_changed_shot_ids"], ["shot-5"])

    def test_required_wide_shot_cannot_be_silently_skipped_in_all_or_count_goals(self):
        context = {"brief": {"必须出现的内容": "第1镜必须保持全景或远景。"},
                   "technical_candidates": {"shot_size": ["全景", "特写"]}}
        for instruction in ("所选镜头全部改为特写", "把其中2镜改成特写", "增加2个特写"):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _closeups(1, 3),
                    selected=["shot-1", "shot-3"], context=context)
                self.assertNotIn("operations", result)
                self.assertIn("必拍", result["summary"])
                self.assertEqual(editor.calls, [])

    def test_legacy_confirmed_standard_explicit_wide_requirement_is_enforced(self):
        context = {"creative_standard": {"expression_focus": [{
            "value": "第3镜必须保留远景。", "fact_id": "anonymous-confirmed-fact",
            "evidence_refs": [{"source_document_id": "anonymous-brief"}],
        }]}, "technical_candidates": {"shot_size": ["全景", "特写"]}}
        result, editor = self._analyze("增加特写", _closeups(3), selected=["shot-3"], context=context)
        self.assertNotIn("operations", result)
        self.assertEqual(editor.calls, [])

    def test_template_notes_preferences_and_inactive_requirements_do_not_create_hard_locks(self):
        requirement = {"text": "第3镜必须保留远景。", "mandatory": True, "fact_status": "confirmed",
                       "origin": "brief", "position_rule": {"type": "fixed_shot", "shot_no": "3"}}
        contexts = [
            {"brief": {"必须出现的内容": "第3镜不必保留全景，可改为特写。"}},
            {"brief": {"必须出现的内容": "第3镜最好用全景表现空间。"}},
            {"source_summary": {"references": [{"notes": "第3镜必须保留远景。"}]}},
            {"brief": {"参考方案": "第3镜必须保留远景。"}},
            {"current_script_summary": {"requirements": [{**requirement, "mandatory": False}]}},
            {"current_script_summary": {"requirements": [{**requirement, "fact_status": "pending"}]}},
            {"current_script_summary": {"requirements": [{**requirement, "decision_source": "template_wins"}]},
             "brief": {"必须出现的内容": "第3镜必须保留远景。"}},
        ]
        for context in contexts:
            with self.subTest(context=context):
                context = {**context, "technical_candidates": {"shot_size": ["全景", "特写"]}}
                shots = _shots()
                shots[2]["notes"] = "模板建议：第3镜必须保留远景。"
                result, editor = self._analyze("增加特写", _closeups(3), selected=["shot-3"], shots=shots, context=context)
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(len(editor.calls), 1)


if __name__ == "__main__":
    unittest.main()
