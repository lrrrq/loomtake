from __future__ import annotations

import json
import unittest
from copy import deepcopy

from mvp_v2.formal_script_assistant import analyze_formal_script_instruction
from tests.test_mvp_v2_formal_script_assistant import _Editor, _shots


def _response(*operations):
    return json.dumps({
        "schema_version": "mplus-formal-script-assistant-editor/v1",
        "operations": list(operations),
    }, ensure_ascii=False)


def _op(shot_id, field, value):
    return {"shot_id": shot_id, "field": field, "value": value}


class ScriptAssistantUsabilityTests(unittest.TestCase):
    def _analyze(self, instruction, response, **kwargs):
        editor = _Editor(response)
        result = analyze_formal_script_instruction(
            instruction=instruction, shots=kwargs.pop("shots", _shots()),
            assistant_locked_shot_ids=kwargs.pop("assistant_locked_shot_ids", []),
            editor_policy=editor.policy(), **kwargs,
        )
        return result, editor

    def test_natural_amount_and_progression_requests_reach_existing_editor_targets(self):
        for instruction, shot_id, operations, expected_fields in (
            ("多一点特写", "shot-c", [_op("shot-c", "shot_size", "特写"), _op("shot-c", "visual", "拍摄建议：展示产品按钮细节。")], {"shot_size", "visual"}),
            ("特写镜头多一些", "shot-c", [_op("shot-c", "shot_size", "特写"), _op("shot-c", "visual", "拍摄建议：展示产品按钮细节。")], {"shot_size", "visual"}),
            ("航拍少一点", "shot-b", [_op("shot-b", "camera_type", "固定机位"), _op("shot-b", "visual", "拍摄建议：从入口平视记录来宾进场。")], {"camera_type", "shot_size", "camera_move", "visual"}),
            ("声音做些递进", "shot-c", [_op("shot-c", "sound", "延续同一背景乐，操作声由弱渐强再承接下一镜。")], {"sound"}),
            ("声音再轻盈一点", "shot-c", [_op("shot-c", "sound", "同一背景乐轻铺，保留清楚的现场操作声。")], {"sound"}),
            ("旁白帮我处理一下", "shot-c", [_op("shot-c", "voiceover", "从按钮到界面，操作过程清楚可见。")], {"voiceover"}),
        ):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _response(*operations), selected_shot_ids=[shot_id])
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(len(editor.calls), 1)
                data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
                self.assertEqual({target["field"] for target in data["targets"]}, expected_fields)
                self.assertEqual({target["shot_id"] for target in data["targets"]}, {shot_id})

    def test_amount_and_progression_questions_or_preservation_do_not_invoke_editor(self):
        for instruction in (
            "特写", "特写镜头", "航拍", "声音", "旁白",
            "特写镜头多一些是什么意思", "航拍少一点有什么作用？", "声音做些递进是什么效果？",
            "为什么要多一点特写", "请解释声音递进", "不用多一点特写", "不要让航拍少一点",
            "声音不用做些递进", "特写镜头保持原样", "保留现有航拍", "声音不必处理",
        ):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _response(_op("shot-c", "sound", "不应采用")))
                self.assertEqual(result["analysis_code"], "unsupported_instruction", result)
                self.assertEqual(editor.calls, [])

    def test_natural_sound_requests_share_one_editor_call_and_only_sound_targets(self):
        for instruction in (
            "减少声音的重复", "声音换个设计", "声效层次",
            "配合高潮重新组织声音", "听起来层次丰富些",
            "不要每镜都重复同一条音乐", "音乐不能盖过旁白，衔接自然些",
        ):
            with self.subTest(instruction=instruction):
                shots = _shots()
                before = deepcopy(shots)
                result, editor = self._analyze(instruction, _response(
                    _op("shot-b", "sound", "指定BGM承接上镜，现场声渐入"),
                    _op("shot-c", "sound", "指定BGM持续轻铺，突出操作声"),
                ), shots=shots, assistant_locked_shot_ids=["shot-a"])
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(len(editor.calls), 1)
                data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
                self.assertEqual({item["field"] for item in data["targets"]}, {"sound"})
                self.assertEqual({item["shot_id"] for item in data["targets"]}, {"shot-b", "shot-c", "shot-d"})
                self.assertEqual({op["field"] for op in result["operations"]}, {"sound"})
                self.assertEqual(shots, before)

    def test_explicit_text_field_and_natural_edit_goal_do_not_need_a_quick_phrase(self):
        for instruction, field in (
            ("旁白换一种说法", "voiceover"),
            ("画面配合高潮重新组织", "visual"),
            ("字幕信息太挤，帮我梳理一下", "subtitle"),
            ("让这一段更有张力", "visual"),
        ):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _response(
                    _op("shot-c", field, "拍摄建议：展示操作前后的产品细节。" if field == "visual" else "先看操作，再看细节。"),
                ), selected_shot_ids=["shot-c"])
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(len(editor.calls), 1)
                data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
                self.assertEqual({item["shot_id"] for item in data["targets"]}, {"shot-c"})
                if "这一段" not in instruction:
                    self.assertEqual({item["field"] for item in data["targets"]}, {field})

    def test_sound_scope_preserves_explicit_range_selection_and_locks(self):
        for instruction, selection, expected in (
            ("第2至3镜声音换个设计", None, {"shot-b", "shot-c"}),
            ("全文减少声音的重复", ["shot-c"], {"shot-c"}),
            ("第1镜声音不要改，第2至3镜声音换个设计", None, {"shot-b", "shot-c"}),
        ):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _response(
                    _op("shot-c", "sound", "同一BGM继续，操作声突出后自然收束"),
                ), selected_shot_ids=selection, assistant_locked_shot_ids=["shot-a"])
                self.assertEqual(result["type"], "rewrite_operations", result)
                data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
                self.assertEqual({item["shot_id"] for item in data["targets"]}, expected)
        for instruction, expected in (
            ("第1镜声音换个设计", "protected_target_selected"),
            ("第2至9镜声音换个设计", "ambiguous_shot_reference"),
        ):
            result, editor = self._analyze(instruction, _response(), assistant_locked_shot_ids=["shot-a"])
            self.assertEqual(result["analysis_code"], expected)
            self.assertEqual(editor.calls, [])

    def test_negative_constraints_are_not_mistaken_for_edit_permission(self):
        for instruction in ("不要减少航拍", "声音不要改", "保持声音不变", "不用把旁白改得更自然", "声音是什么", "请解释声音层次", "声音为什么重复", "帮我查天气"):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _response(_op("shot-c", "sound", "不应采用")))
                self.assertEqual(result["analysis_code"], "unsupported_instruction", result)
                self.assertEqual(editor.calls, [])
        result, editor = self._analyze("声音换个设计，画面和旁白不要改", _response(
            _op("shot-c", "sound", "延续背景乐，现场声先入后收"),
        ), selected_shot_ids=["shot-c"])
        self.assertEqual(result["type"], "rewrite_operations", result)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual({item["field"] for item in data["targets"]}, {"sound"})
        blocked, _ = self._analyze("让这一段更有张力，声音不要改", _response(
            _op("shot-c", "visual", "用现有产品演示串起前后信息。"),
            _op("shot-c", "sound", "不应顺改"),
        ), selected_shot_ids=["shot-c"])
        self.assertEqual(blocked["analysis_code"], "editor_scope_violation", blocked)
        self.assertNotIn("operations", blocked)

    def test_preserved_shot_and_technical_field_are_excluded_from_model_targets(self):
        result, editor = self._analyze("全文节奏更紧凑，第2镜不要改，不改变运镜", _response(
            _op("shot-c", "sound", "现场声直接衔接下一镜"),
        ), assistant_locked_shot_ids=["shot-a"])
        self.assertEqual(result["type"], "rewrite_operations", result)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual({item["shot_id"] for item in data["targets"]}, {"shot-c", "shot-d"})
        self.assertNotIn("camera_move", {item["field"] for item in data["targets"]})

    def test_preserving_count_duration_and_images_does_not_cancel_selected_sound_edit(self):
        shots = [dict(_shots()[1], shot_id=f"shot-{i}", shot_no=str(i), voiceover="") for i in range(1, 9)]
        result, editor = self._analyze(
            "保留第一镜原样。只修改第2镜和第6镜的声音：第2镜改为轻快木吉他伴奏，加入轻微杯子落桌声，无旁白；第6镜改为同一段木吉他伴奏，加入轻微拿杯声，无旁白。其他文字、图片、镜数与时长保持不变。",
            _response(_op("shot-2", "sound", "木吉他伴奏与杯子落桌声，无旁白。"),
                      _op("shot-6", "sound", "同一木吉他伴奏与拿杯声，无旁白。")),
            shots=shots, selected_shot_ids=["shot-2", "shot-6"], assistant_locked_shot_ids=["shot-1"],
        )
        self.assertEqual(result["type"], "rewrite_operations", result)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual({(item["shot_id"], item["field"]) for item in data["targets"]}, {
            ("shot-2", "sound"), ("shot-6", "sound"),
        })

    def test_coordinated_explicit_fields_keep_the_whole_requested_text_group(self):
        result, editor = self._analyze("第2至3镜画面、声音、旁白和字幕重新组织", _response(
            _op("shot-b", "visual", "拍摄建议：入口标识衔接演示场地。"),
            _op("shot-c", "subtitle", "产品操作细节"),
        ))
        self.assertEqual(result["type"], "rewrite_operations", result)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual({item["field"] for item in data["targets"]}, {"visual", "sound", "voiceover", "subtitle"})

    def test_distinct_scoped_fields_do_not_expand_to_the_other_shot(self):
        result, editor = self._analyze("第2镜声音换个设计，第3镜旁白换一种说法", _response(
            _op("shot-b", "sound", "延续上镜BGM并接入现场声"),
            _op("shot-c", "voiceover", "先看操作，再看细节。"),
        ))
        self.assertEqual(result["type"], "rewrite_operations", result)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual({(item["shot_id"], item["field"]) for item in data["targets"]}, {
            ("shot-b", "sound"), ("shot-c", "voiceover"),
        })

    def test_sound_empty_out_of_scope_and_no_change_have_distinct_atomic_results(self):
        for operations, expected in (
            ([_op("shot-b", "sound", "延续BGM"), _op("shot-c", "sound", "")], "editor_value_invalid"),
            ([_op("shot-b", "sound", "延续BGM"), _op("shot-c", "visual", "不应顺改画面")], "editor_scope_violation"),
            ([_op("shot-c", "sound", _shots()[2]["sound"])], "script_no_changes"),
            ([], "script_no_changes"),
        ):
            with self.subTest(expected=expected):
                result, editor = self._analyze("第2至3镜减少声音的重复", _response(*operations))
                self.assertEqual(result["analysis_code"], expected, result)
                self.assertEqual(len(editor.calls), 1)
                self.assertNotIn("operations", result)

    def test_sound_design_prompt_keeps_confirmed_music_and_neighbor_context(self):
        context = {
            "brief": {"confirmed_facts": ["指定音乐为匿名曲目A，连续铺底", "必须展示产品实际操作"]},
            "creative_standard": {"rhythm": "先交代环境，再展示操作"},
            "neighbor_shots": _shots(),
        }
        result, editor = self._analyze("第2至3镜声音换个设计", _response(
            _op("shot-b", "sound", "匿名曲目A延续，现场声渐入"),
            _op("shot-c", "sound", "匿名曲目A轻铺，突出产品操作声"),
        ), context=context)
        self.assertEqual(result["type"], "rewrite_operations", result)
        prompt = editor.calls[0]["prompt"]
        data = json.loads(prompt.split("\nDATA:", 1)[1])
        self.assertEqual(data["context"]["brief"], context["brief"])
        self.assertEqual(data["context"]["neighbor_shots"], context["neighbor_shots"])
        for evidence in ("BGM", "连续", "现场声", "重点音效", "承接", "必拍", "模板", "原创"):
            self.assertIn(evidence, prompt)

    def test_recorded_repetitive_sound_failure_gets_per_shot_rewrite_requirements(self):
        shots = _shots()
        for shot in shots[:3]:
            shot["sound"] = "匿名曲目A重新起乐，现场氛围与音乐推进"
        # This recorded failure is valid JSON; the regression checks the editor
        # contract, not an invented local semantic score or a quality certificate.
        recorded = _response(
            _op("shot-b", "sound", "承接上镜，匿名曲目A连续铺底，现场氛围与音乐推进"),
            _op("shot-c", "sound", "承接上镜，匿名曲目A连续铺底，现场氛围与音乐推进"),
        )
        _, editor = self._analyze("减少声音的重复", recorded, shots=shots, selected_shot_ids=["shot-b", "shot-c"])
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        for target in data["targets"]:
            self.assertEqual(target.get("rewrite_mode"), "complete_value")
            self.assertIn("本镜主声或留白", target.get("rewrite_requirements", ""))
            self.assertIn("不能只", target.get("rewrite_requirements", ""))
            self.assertEqual(target.get("visual_basis"), next(shot["visual"] for shot in shots if shot["shot_id"] == target["shot_id"]))
        self.assertIn("逐镜比较", editor.calls[0]["prompt"])

    def test_recorded_appended_closeup_failure_gets_complete_visual_rewrite_requirement(self):
        shots = _shots()
        shots[2]["visual"] = "广角建立场地关系。"
        _, editor = self._analyze("增加特写镜头", _response(
            _op("shot-c", "shot_size", "特写"),
            _op("shot-c", "visual", "广角建立场地关系。拍摄建议：以特写捕捉标识细节。"),
        ), shots=shots, selected_shot_ids=["shot-c"], context={
            "allow_original_adaptation": True, "technical_candidates": {"shot_size": ["特写"]},
        })
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        target = next(item for item in data["targets"] if item["field"] == "visual")
        self.assertEqual(target.get("rewrite_mode"), "complete_value")
        self.assertIn("不能保留旧句再追加", target.get("rewrite_requirements", ""))
        self.assertIn("相斥", target.get("rewrite_requirements", ""))
        self.assertIn("一次", target.get("rewrite_requirements", ""))

    def test_original_closeup_uses_legal_values_absent_from_current_inventory(self):
        shots = _shots()
        for shot in shots:
            shot.update(camera_type="实拍", shot_size="全景", camera_move="固定")
        context = {
            "allow_original_adaptation": True,
            "technical_candidates": {"camera_type": ["实拍"], "shot_size": ["全景", "特写"], "camera_move": ["固定"]},
            "source_summary": [{"shot_size": "全景", "source_role": "reference"}],
        }
        result, editor = self._analyze("第2至3镜增加特写镜头", _response(
            _op("shot-b", "shot_size", "特写"), _op("shot-b", "visual", "入口标识与门框细节清楚可见。"),
            _op("shot-c", "shot_size", "特写"), _op("shot-c", "visual", "产品按钮和手部操作细节清楚可见。"),
        ), shots=shots, context=context)
        self.assertEqual(result["type"], "rewrite_operations", result)
        self.assertEqual(len(editor.calls), 1)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertIs(data["context"].get("allow_original_adaptation"), True)
        self.assertEqual({op["field"] for op in result["operations"]}, {"shot_size", "visual"})
        for op in result["operations"]:
            self.assertEqual(set(op), {"operation", "shot_id", "field", "value"})
            if op["field"] == "visual":
                self.assertTrue(op["value"].startswith("拍摄建议："))
        for target in data["targets"]:
            if target["field"] == "visual":
                self.assertEqual(target["preserve_technical_fields"], {"camera_type": "实拍", "camera_move": "固定"})

    def test_original_permission_keeps_technical_validation_and_strict_context_compatibility(self):
        for context, value, expected in (
            ({"allow_original_adaptation": True, "technical_candidates": {"shot_size": ["特写"]}}, "", "editor_value_invalid"),
            ({"allow_original_adaptation": True, "technical_candidates": {"shot_size": ["特写"]}}, "虚构的超级特写", "editor_value_invalid"),
            ({"allow_original_adaptation": True, "technical_candidates": {"shot_size": []}}, "特写", "safe_value_required"),
            ({"allow_original_adaptation": False, "technical_candidates": {"shot_size": ["全景"]}}, "特写", "safe_value_required"),
            ({"allow_original_adaptation": "yes", "technical_candidates": {"shot_size": ["特写"]}}, "特写", "invalid_context"),
        ):
            with self.subTest(context=context, value=value):
                result, _ = self._analyze("增加特写镜头", _response(_op("shot-c", "shot_size", value)),
                    selected_shot_ids=["shot-c"], context=context)
                self.assertEqual(result["analysis_code"], expected, result)
                self.assertNotIn("operations", result)

    def test_rhythm_accepts_changed_subset_and_filters_unchanged_fields(self):
        for operations in (
            [_op("shot-c", "sound", "现场声直接接入下一镜")],
            [_op("shot-c", "camera_move", "固定"), _op("shot-c", "sound", "现场声直接接入下一镜")],
        ):
            with self.subTest(operations=operations):
                result, _ = self._analyze("节奏更紧凑", _response(*operations), selected_shot_ids=["shot-c"])
                self.assertEqual(result["type"], "rewrite_operations")
                self.assertEqual(result["operations"], [{"operation": "replace_field", **operations[-1]}])

    def test_empty_or_all_unchanged_operations_have_clear_no_change_feedback(self):
        for response in (_response(), _response(_op("shot-c", "voiceover", _shots()[2]["voiceover"]))):
            with self.subTest(response=response):
                result, _ = self._analyze("旁白更自然", response, selected_shot_ids=["shot-c"])
                self.assertEqual(result["analysis_code"], "script_no_changes")
                self.assertNotIn("越权", result["summary"])
                self.assertNotIn("operations", result)

    def test_scope_and_format_failures_are_distinct_and_batch_is_rejected(self):
        for response, expected in (
            ("not json", "editor_format_invalid"),
            (_response(_op("shot-c", "voiceover", "讲清产品的使用方式。"), _op("shot-b", "voiceover", "范围外")), "editor_scope_violation"),
            (_response(_op("shot-c", "source_file", "new.xlsx")), "editor_scope_violation"),
            (_response(_op("shot-c", "voiceover", "")), "editor_value_invalid"),
        ):
            with self.subTest(expected=expected):
                result, _ = self._analyze("旁白更自然", response, selected_shot_ids=["shot-c"])
                self.assertEqual(result["analysis_code"], expected)
                self.assertNotIn("operations", result)

    def test_all_seven_quick_instructions_can_form_preview_without_scope_roundtrip(self):
        cases = (
            ("减少航拍镜头", [_op("shot-b", "camera_type", "固定机位"), _op("shot-b", "visual", "地面机位呈现入口与主舞台的位置关系。")]),
            ("增加特写镜头", [_op("shot-c", "shot_size", "特写"), _op("shot-c", "visual", "产品表面的纹理与手部操作细节清楚可见。")]),
            ("旁白更自然", [_op("shot-c", "voiceover", "看看产品的这些细节。")]),
            ("字幕更简洁", [_op("shot-c", "subtitle", "产品细节")]),
            ("减少重复内容", [_op("shot-b", "voiceover", "沿着入口进入现场。")]),
            ("减少常用套话和AI味", [_op("shot-b", "voiceover", "活动现在开始。")]),
            ("节奏更紧凑", [_op("shot-c", "sound", "现场声直接衔接")]),
        )
        for instruction, operations in cases:
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _response(*operations), assistant_locked_shot_ids=["shot-a"])
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(len(editor.calls), 1)
                self.assertNotIn("shot-a", {op["shot_id"] for op in result["operations"]})

    def test_explicit_multishot_ranges_are_resolved_without_first_shot_truncation(self):
        for instruction, targets in (
            ("第2至4镜旁白更自然", {"shot-b", "shot-c", "shot-d"}),
            ("第2、4镜旁白更自然", {"shot-b", "shot-d"}),
            ("第2镜和第4镜旁白更自然", {"shot-b", "shot-d"}),
            ("第二到四镜旁白更自然", {"shot-b", "shot-c", "shot-d"}),
        ):
            with self.subTest(instruction=instruction):
                result, editor = self._analyze(instruction, _response(_op("shot-b", "voiceover", "进入活动现场。")))
                self.assertEqual(result["type"], "rewrite_operations", result)
                data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
                self.assertEqual({item["shot_id"] for item in data["targets"]}, targets)

    def test_conflicting_textual_range_is_not_silently_overridden_by_selection(self):
        result, editor = self._analyze("全文第1镜旁白更自然", _response(_op("shot-c", "voiceover", "展示具体的使用细节。")), selected_shot_ids=["shot-c"])
        self.assertEqual(result["type"], "needs_clarification")
        self.assertEqual(result["analysis_code"], "scope_conflict")
        self.assertNotIn("operations", result)
        self.assertEqual(editor.calls, [])

    def test_whole_script_excludes_protected_shots_and_unknown_explicit_shot_is_rejected(self):
        result, editor = self._analyze("全文旁白更自然", _response(_op("shot-c", "voiceover", "展示使用细节。")), assistant_locked_shot_ids=["shot-a"])
        self.assertEqual(result["type"], "rewrite_operations")
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual({item["shot_id"] for item in data["targets"]}, {"shot-b", "shot-c", "shot-d"})
        bad, bad_editor = self._analyze("第2至9镜旁白更自然", _response())
        self.assertEqual(bad["analysis_code"], "ambiguous_shot_reference")
        self.assertEqual(bad_editor.calls, [])

    def test_closeup_changes_only_size_and_visual_using_authoritative_candidates(self):
        shots = _shots()
        for shot in shots:
            shot.update(camera_type="实拍", shot_size="全景", camera_move="固定")
        result, editor = self._analyze("增加特写", _response(
            _op("shot-c", "shot_size", "产品特写"), _op("shot-c", "visual", "手指操作产品按钮，边缘材质清楚可见。"),
        ), shots=shots, selected_shot_ids=["shot-c"], context={"technical_candidates": {"shot_size": ["产品特写"]}})
        self.assertEqual(result["type"], "rewrite_operations", result)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual({item["field"] for item in data["targets"]}, {"shot_size", "visual"})
        self.assertEqual({op["field"] for op in result["operations"]}, {"shot_size", "visual"})
        self.assertEqual(shots[2]["camera_type"], "实拍")
        self.assertEqual(shots[2]["camera_move"], "固定")

    def test_authoritative_empty_values_do_not_fall_back_to_current_script(self):
        result, editor = self._analyze("增加特写", _response(), selected_shot_ids=["shot-c"], context={"technical_candidates": {"shot_size": []}})
        self.assertEqual(result["analysis_code"], "safe_value_required")
        self.assertEqual(editor.calls, [])

    def test_semantic_duplicates_do_not_require_literal_equal_text(self):
        shots = _shots()
        for index, shot in enumerate(shots):
            for field in ("visual", "sound", "voiceover", "subtitle"):
                shot[field] = f"第{index + 1}处表达产品细节，使用不同词句：{field}。"
        result, editor = self._analyze("减少重复内容", _response(_op("shot-c", "voiceover", "转向产品实际操作。")), shots=shots)
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(len(editor.calls), 1)
        self.assertIn("语义", editor.calls[0]["prompt"])

    def test_redundant_optional_text_can_be_cleared_through_canonical_operation(self):
        for instruction, field in (("减少重复内容", "voiceover"), ("减少常用套话和AI味", "subtitle")):
            with self.subTest(instruction=instruction):
                result, _ = self._analyze(instruction, _response(
                    _op("shot-b", field, ""),
                    _op("shot-c", field, _shots()[2][field]),
                ), selected_shot_ids=["shot-b", "shot-c"])
                self.assertEqual(result["type"], "rewrite_operations", result)
                self.assertEqual(result["operations"], [{
                    "operation": "clear_optional_field", "shot_id": "shot-b",
                    "field": field, "value": "",
                }])

    def test_duplicate_removal_cannot_clear_required_body(self):
        for field in ("visual", "sound"):
            with self.subTest(field=field):
                result, _ = self._analyze("减少重复内容", _response(
                    _op("shot-b", "voiceover", ""), _op("shot-c", field, ""),
                ), selected_shot_ids=["shot-b", "shot-c"])
                self.assertEqual(result["analysis_code"], "editor_value_invalid")
                self.assertNotIn("operations", result)

    def test_whole_paragraph_rewrite_receives_grounded_context_and_keeps_identity(self):
        context = {
            "brief": {"confirmed_facts": ["仅展示已确认的产品功能"]},
            "creative_standard": {"tone": "平实具体"},
            "neighbor_shots": [_shots()[1], _shots()[3]],
            "source_summary": {"function": "产品展示"},
            "recent_conversation": [{"role": "user", "text": "不要典礼化措辞"}],
        }
        result, editor = self._analyze("重写整段", _response(_op("shot-c", "voiceover", "先看操作，再看结果。")), selected_shot_ids=["shot-c"], context=context)
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(result["shot_count_before"], result["shot_count_after"])
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual(data["context"]["brief"], context["brief"])
        self.assertEqual(data["context"]["creative_standard"], context["creative_standard"])
        self.assertEqual(data["selected_shots"][0], _shots()[2])
        self.assertNotIn("structure", {item["field"] for item in data["targets"]})
        self.assertIn("事实", editor.calls[0]["prompt"])

    def test_explicit_protected_selection_and_total_count_keep_existing_boundaries(self):
        protected, editor = self._analyze("重写整段", _response(), selected_shot_ids=["shot-a"], assistant_locked_shot_ids=["shot-a"])
        self.assertEqual(protected["analysis_code"], "protected_target_selected")
        self.assertEqual(editor.calls, [])
        replan, editor = self._analyze("增加总镜数到6镜", _response())
        self.assertEqual(replan["type"], "replan_required")
        self.assertEqual(replan["shot_count_after"], 6)
        self.assertEqual(editor.calls, [])

    def test_empty_voiceover_is_only_written_when_explicitly_requested(self):
        shots = _shots()
        for shot in shots:
            shot["voiceover"] = ""
        natural, natural_editor = self._analyze("旁白更自然", _response(), shots=shots)
        self.assertEqual(natural["analysis_code"], "no_matching_shots")
        self.assertEqual(natural_editor.calls, [])
        rewrite, rewrite_editor = self._analyze("换个创意", _response(_op("shot-c", "visual", "拍摄建议：用产品局部与现场操作衔接。")), shots=shots)
        self.assertEqual(rewrite["type"], "rewrite_operations")
        rewrite_data = json.loads(rewrite_editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertNotIn("voiceover", {target["field"] for target in rewrite_data["targets"]})
        explicit, _ = self._analyze("第3镜新增旁白", _response(_op("shot-c", "voiceover", "从细节了解产品的操作。")), shots=shots)
        self.assertEqual(explicit["type"], "rewrite_operations", explicit)

    def test_authoritative_technical_values_cannot_be_expanded_by_model(self):
        result, _ = self._analyze("增加特写", _response(
            _op("shot-c", "shot_size", "极限微距特写"), _op("shot-c", "visual", "拍摄建议：展示产品表面。"),
        ), selected_shot_ids=["shot-c"], context={"technical_candidates": {"shot_size": ["特写"]}})
        self.assertEqual(result["analysis_code"], "editor_value_invalid")
        self.assertNotIn("operations", result)
        changed_type, _ = self._analyze("增加特写", _response(
            _op("shot-c", "shot_size", "特写"), _op("shot-c", "camera_type", "手持"),
        ), selected_shot_ids=["shot-c"])
        self.assertEqual(changed_type["analysis_code"], "editor_scope_violation")

    def test_selection_with_already_satisfied_shots_only_previews_remaining_changes(self):
        result, editor = self._analyze("第3至4镜增加特写", _response(
            _op("shot-c", "shot_size", "特写"), _op("shot-c", "visual", "产品按键与手部操作占据画面。"),
        ))
        self.assertEqual(result["type"], "rewrite_operations", result)
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual({item["shot_id"] for item in data["targets"]}, {"shot-c"})
        noop, noop_editor = self._analyze("增加特写", _response(), selected_shot_ids=["shot-d"])
        self.assertEqual(noop["analysis_code"], "script_no_changes")
        self.assertEqual(noop_editor.calls, [])

    def test_new_visual_concept_is_explicitly_a_shooting_suggestion(self):
        result, _ = self._analyze("换个创意", _response(
            _op("shot-c", "visual", "产品外观与现有操作细节在同一构图中呼应。"),
        ), selected_shot_ids=["shot-c"])
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(result["operations"][0]["value"], "拍摄建议：产品外观与现有操作细节在同一构图中呼应。")

    def test_all_28_shots_keep_dynamic_scope_and_only_recent_context(self):
        shots = []
        for index in range(28):
            shot = dict(_shots()[index % 4], shot_id=f"shot-{index + 1}", shot_no=str(index + 1))
            shots.append(shot)
        history = [{"role": "user", "text": f"第{i}次反馈"} for i in range(9)]
        result, editor = self._analyze("重写全文", _response(_op("shot-28", "subtitle", "操作演示结束")),
            shots=shots, assistant_locked_shot_ids=["shot-12"], context={"recent_conversation": history})
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual((result["shot_count_before"], result["shot_count_after"]), (28, 28))
        data = json.loads(editor.calls[0]["prompt"].split("\nDATA:", 1)[1])
        self.assertEqual(len({item["shot_id"] for item in data["targets"]}), 27)
        self.assertNotIn("shot-12", {item["shot_id"] for item in data["targets"]})
        self.assertEqual(data["context"]["recent_conversation"], history[-6:])
        self.assertGreater(editor.calls[0]["max_tokens"], 4096)


if __name__ == "__main__":
    unittest.main()
