from __future__ import annotations

import json
import unittest

from mvp_v2.formal_script_assistant import (
    FormalScriptAssistantEditorPolicy,
    analyze_formal_script_instruction,
)


class _Editor:
    def __init__(self, response, *, provider="deepseek", model="deepseek-chat"):
        self.response = response
        self.provider = provider
        self.model = model
        self.calls = []

    def invoke(self, prompt, model, max_tokens):
        self.calls.append({"prompt": prompt, "model": model, "max_tokens": max_tokens})
        return self.response(prompt) if callable(self.response) else self.response

    def policy(self):
        return FormalScriptAssistantEditorPolicy(
            invoke=self.invoke, provider=self.provider, model=self.model,
        )


def _shots():
    return [
        {
            "shot_id": "shot-a", "shot_no": "1", "structure": "开场",
            "camera_type": "航拍", "shot_size": "大全景", "camera_move": "缓慢推进",
            "visual": "匿名会场外观与入口关系。", "sound": "克制音乐",
            "voiceover": "欢迎来到匿名活动。", "subtitle": "匿名活动",
            "notes": "",
        },
        {
            "shot_id": "shot-b", "shot_no": "2", "structure": "场地关系",
            "camera_type": "航拍", "shot_size": "全景", "camera_move": "横移",
            "visual": "匿名会场外观与入口关系。", "sound": "克制音乐",
            "voiceover": "欢迎来到匿名活动。", "subtitle": "匿名活动",
            "notes": "",
        },
        {
            "shot_id": "shot-c", "shot_no": "3", "structure": "特写",
            "camera_type": "固定机位", "shot_size": "中景", "camera_move": "固定",
            "visual": "匿名嘉宾展示产品细节。", "sound": "现场声",
            "voiceover": "产品细节清晰呈现。", "subtitle": "产品亮点",
            "notes": "保留真实演示",
        },
        {
            "shot_id": "shot-d", "shot_no": "4", "structure": "收束",
            "camera_type": "手持", "shot_size": "特写", "camera_move": "跟随",
            "visual": "匿名嘉宾完成分享。", "sound": "掌声",
            "voiceover": "分享自然结束。", "subtitle": "感谢观看",
            "notes": "",
        },
    ]


class FormalScriptAssistantTests(unittest.TestCase):
    def test_reduce_aerial_uses_joint_visual_fields_and_never_touches_locked_shots(self):
        analysis = analyze_formal_script_instruction(
            instruction="减少航拍镜头数量，优先保留最能交代场地关系的航拍镜头",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
        )
        self.assertEqual(analysis["type"], "scope_required")
        self.assertEqual(analysis["analysis_code"], "script_scope_required")
        self.assertEqual(
            [item["shot_id"] for item in analysis["suggested_targets"]], ["shot-b"],
        )
        self.assertEqual(analysis["suggested_targets"][0]["fields"], [
            "camera_type", "shot_size", "camera_move", "visual",
        ])
        self.assertNotIn("shot-c", json.dumps(analysis["suggested_targets"], ensure_ascii=False))

        locked_editor = _Editor("must not be called")
        locked = analyze_formal_script_instruction(
            instruction="减少航拍镜头",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
            selected_shot_ids=["shot-a"],
            editor_policy=locked_editor.policy(),
        )
        self.assertEqual(locked["type"], "needs_clarification")
        self.assertEqual(locked["analysis_code"], "protected_target_selected")
        self.assertEqual(locked_editor.calls, [])

        editor = _Editor(json.dumps({
            "schema_version": "mplus-formal-script-assistant-editor/v1",
            "operations": [
                {"shot_id": "shot-b", "field": "camera_type", "value": "固定机位"},
                {"shot_id": "shot-b", "field": "shot_size", "value": "中景"},
                {"shot_id": "shot-b", "field": "camera_move", "value": "缓慢推进"},
                {"shot_id": "shot-b", "field": "visual", "value": "匿名嘉宾从入口走向主舞台。"},
            ],
        }, ensure_ascii=False))
        result = analyze_formal_script_instruction(
            instruction="减少航拍镜头",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
            selected_shot_ids=["shot-b"],
            editor_policy=editor.policy(),
        )
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(result["shot_count_before"], 4)
        self.assertEqual(result["shot_count_after"], 4)
        self.assertEqual(
            {item["field"] for item in result["operations"]},
            {"camera_type", "shot_size", "camera_move", "visual"},
        )
        self.assertEqual(result["model_evidence"], {
            "provider": "deepseek", "model": "deepseek-chat",
            "purpose": "formal_script_semantic_rewrite", "call_count": 1,
        })
        self.assertEqual(len(editor.calls), 1)
        self.assertNotIn("shot-a", editor.calls[0]["prompt"])

    def test_reduce_aerial_requires_visual_to_follow_new_camera_semantics(self):
        def response(prompt):
            visual = (
                "匿名嘉宾从入口走向主舞台，地面机位缓慢推进。"
                if "哪怕主体内容不变也必须重写 visual" in prompt
                else _shots()[1]["visual"]
            )
            return json.dumps({
                "schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [
                    {"shot_id": "shot-b", "field": "camera_type", "value": "固定机位"},
                    {"shot_id": "shot-b", "field": "shot_size", "value": "中景"},
                    {"shot_id": "shot-b", "field": "camera_move", "value": "缓慢推进"},
                    {"shot_id": "shot-b", "field": "visual", "value": visual},
                ],
            }, ensure_ascii=False)

        result = analyze_formal_script_instruction(
            instruction="减少航拍镜头",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
            selected_shot_ids=["shot-b"],
            editor_policy=_Editor(response).policy(),
        )

        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(result["shot_count_before"], result["shot_count_after"])

    def test_increase_closeup_changes_size_and_visual_not_structure_and_total_change_requires_replan(self):
        analysis = analyze_formal_script_instruction(
            instruction="增加特写镜头",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
        )
        self.assertEqual(analysis["type"], "scope_required")
        suggested = {item["shot_id"]: item for item in analysis["suggested_targets"]}
        self.assertIn("shot-c", suggested, "structure=特写 不能冒充 shot_size=特写")
        self.assertNotIn("shot-d", suggested, "已有特写不应成为增加目标")
        self.assertEqual(suggested["shot-c"]["fields"], [
            "shot_size", "visual",
        ])

        editor = _Editor(json.dumps({
            "schema_version": "mplus-formal-script-assistant-editor/v1",
            "operations": [
                {"shot_id": "shot-c", "field": "shot_size", "value": "特写"},
                {"shot_id": "shot-c", "field": "visual", "value": "匿名嘉宾手持产品，展示材质和操作细节。"},
            ],
        }, ensure_ascii=False))
        result = analyze_formal_script_instruction(
            instruction="增加特写镜头",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
            selected_shot_ids=["shot-c"],
            editor_policy=editor.policy(),
        )
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(result["shot_count_before"], result["shot_count_after"])
        self.assertEqual(
            {item["field"] for item in result["operations"]},
            {"shot_size", "visual"},
        )
        self.assertEqual(result["model_evidence"]["call_count"], 1)

        preserved_count = analyze_formal_script_instruction(
            instruction="增加产品特写，保持总镜数不变",
            shots=_shots(), assistant_locked_shot_ids=["shot-a"],
        )
        self.assertEqual(preserved_count["type"], "scope_required")

        for instruction in ("新增一个镜头", "增加总镜数到 6 镜", "删除第2镜"):
            with self.subTest(instruction=instruction):
                replan = analyze_formal_script_instruction(
                    instruction=instruction, shots=_shots(),
                    assistant_locked_shot_ids=["shot-a"],
                )
                self.assertEqual(replan["type"], "replan_required")
                self.assertEqual(replan["analysis_code"], "script_replan_required")
        target = analyze_formal_script_instruction(
            instruction="增加总镜数到 6 镜",
            shots=_shots(), assistant_locked_shot_ids=["shot-a"],
        )
        self.assertEqual(target["shot_count_after"], 6)
        self.assertEqual(target["assistant_locked_shot_ids"], ["shot-a"])

    def test_voiceover_and_subtitle_style_require_scope_then_strict_server_owned_rewrite(self):
        scope = analyze_formal_script_instruction(
            instruction="旁白更自然，去掉AI味",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
        )
        self.assertEqual(scope["type"], "scope_required")
        self.assertTrue(scope["suggested_targets"])
        self.assertTrue(all(item["fields"] == ["voiceover"] for item in scope["suggested_targets"]))
        self.assertNotIn("shot-a", {item["shot_id"] for item in scope["suggested_targets"]})

        editor = _Editor(json.dumps({
            "schema_version": "mplus-formal-script-assistant-editor/v1",
            "operations": [{
                "shot_id": "shot-b", "field": "subtitle", "value": "匿名活动启幕",
            }],
        }, ensure_ascii=False))
        result = analyze_formal_script_instruction(
            instruction="第2镜字幕更简洁",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
            editor_policy=editor.policy(),
        )
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(result["operations"][0]["value"], "匿名活动启幕")
        self.assertNotEqual(result["operations"][0]["value"], "第2镜字幕更简洁")

    def test_reduce_duplicates_allows_semantic_candidates_and_only_changes_returned_fields(self):
        scope = analyze_formal_script_instruction(
            instruction="减少重复内容",
            shots=_shots(),
            assistant_locked_shot_ids=["shot-a"],
        )
        self.assertEqual(scope["type"], "scope_required")
        by_shot = {item["shot_id"]: item for item in scope["suggested_targets"]}
        self.assertEqual(
            by_shot["shot-b"]["fields"],
            ["sound", "voiceover", "subtitle", "visual"],
        )
        self.assertNotIn("shot-a", by_shot)
        self.assertIn("shot-c", by_shot, "语义重复不应被逐字匹配提前排除")

        editor = _Editor(json.dumps({
            "schema_version": "mplus-formal-script-assistant-editor/v1",
            "operations": [
                {"shot_id": "shot-b", "field": "sound", "value": "现场环境声渐入"},
                {"shot_id": "shot-b", "field": "voiceover", "value": "匿名活动现在开始。"},
                {"shot_id": "shot-b", "field": "subtitle", "value": "活动启幕"},
                {"shot_id": "shot-b", "field": "visual", "value": "匿名入口连接主舞台的动线。"},
            ],
        }, ensure_ascii=False))
        result = analyze_formal_script_instruction(
            instruction="减少重复内容", shots=_shots(),
            assistant_locked_shot_ids=["shot-a"], selected_shot_ids=["shot-b"],
            editor_policy=editor.policy(),
        )
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(len(result["operations"]), 4)
        self.assertEqual({item["field"] for item in result["operations"]}, {
            "sound", "voiceover", "subtitle", "visual",
        })

    def test_reduce_cliches_previews_semantic_rewrite_without_scope_roundtrip(self):
        shots = _shots()
        shots[1]["voiceover"] = "此刻，让我们共同见证匿名活动的精彩启幕。"
        editor = _Editor(json.dumps({
            "schema_version": "mplus-formal-script-assistant-editor/v1",
            "operations": [{
                "shot_id": "shot-b", "field": "voiceover",
                "value": "匿名活动现在开始。",
            }],
        }, ensure_ascii=False))
        result = analyze_formal_script_instruction(
            instruction="减少常用套话和AI味", shots=shots,
            assistant_locked_shot_ids=["shot-a"], editor_policy=editor.policy(),
        )
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual(result["operations"], [{
            "operation": "replace_field", "shot_id": "shot-b",
            "field": "voiceover", "value": "匿名活动现在开始。",
        }])
        self.assertEqual(result["model_evidence"]["call_count"], 1)
        self.assertEqual(len(editor.calls), 1)
        self.assertNotIn("shot-a", editor.calls[0]["prompt"])

    def test_reduce_cliches_rejects_out_of_scope_model_rewrite_without_operations(self):
        editor = _Editor(json.dumps({
            "schema_version": "mplus-formal-script-assistant-editor/v1",
            "operations": [{
                "shot_id": "shot-a", "field": "voiceover", "value": "越权修改保护镜头",
            }],
        }, ensure_ascii=False))
        result = analyze_formal_script_instruction(
            instruction="减少套话", shots=_shots(),
            assistant_locked_shot_ids=["shot-a"], editor_policy=editor.policy(),
        )
        self.assertEqual(result["type"], "needs_clarification")
        self.assertEqual(result["analysis_code"], "editor_scope_violation")
        self.assertNotIn("operations", result)
        self.assertEqual(result["model_evidence"]["call_count"], 1)

    def test_tighter_rhythm_uses_selected_existing_fields_and_keeps_n(self):
        scope = analyze_formal_script_instruction(
            instruction="节奏更紧凑",
            shots=_shots(), assistant_locked_shot_ids=["shot-a"],
        )
        self.assertEqual(scope["type"], "scope_required")
        target = next(item for item in scope["suggested_targets"] if item["shot_id"] == "shot-c")
        self.assertEqual(target["fields"], ["camera_move", "sound", "visual"])

        editor = _Editor(json.dumps({
            "schema_version": "mplus-formal-script-assistant-editor/v1",
            "operations": [
                {"shot_id": "shot-c", "field": "camera_move", "value": "快速推进"},
                {"shot_id": "shot-c", "field": "sound", "value": "现场声快速衔接"},
                {"shot_id": "shot-c", "field": "visual", "value": "匿名嘉宾快速展示核心细节。"},
            ],
        }, ensure_ascii=False))
        result = analyze_formal_script_instruction(
            instruction="节奏更紧凑", shots=_shots(),
            assistant_locked_shot_ids=["shot-a"], selected_shot_ids=["shot-c"],
            editor_policy=editor.policy(),
        )
        self.assertEqual(result["type"], "rewrite_operations")
        self.assertEqual((result["shot_count_before"], result["shot_count_after"]), (4, 4))

    def test_model_format_scope_value_and_noop_have_distinct_results(self):
        current = _shots()[1]["subtitle"]
        invalid_outputs = [
            "```json\n{}\n```",
            json.dumps({
                "schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [
                    {"shot_id": "shot-b", "field": "subtitle", "value": "活动启幕"},
                    {"shot_id": "shot-a", "field": "subtitle", "value": "越权多改"},
                ],
            }, ensure_ascii=False),
            json.dumps({
                "schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [],
            }, ensure_ascii=False),
            json.dumps({
                "schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{"shot_id": "shot-b", "field": "subtitle", "value": ""}],
            }, ensure_ascii=False),
            json.dumps({
                "schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{"shot_id": "shot-b", "field": "subtitle", "value": current}],
            }, ensure_ascii=False),
            json.dumps({
                "schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{"shot_id": "shot-b", "field": "source", "value": "越权"}],
            }, ensure_ascii=False),
        ]
        expected_codes = (
            "editor_format_invalid", "editor_scope_violation", "script_no_changes",
            "editor_value_invalid", "script_no_changes", "editor_scope_violation",
        )
        for index, output in enumerate(invalid_outputs, start=1):
            with self.subTest(index=index):
                editor = _Editor(
                    output,
                    provider="deepseek://secret" if index == 1 else "deepseek",
                    model="model?token=secret" if index == 1 else "deepseek-chat",
                )
                result = analyze_formal_script_instruction(
                    instruction="第2镜字幕更简洁", shots=_shots(),
                    assistant_locked_shot_ids=["shot-a"], editor_policy=editor.policy(),
                )
                self.assertEqual(result["type"], "needs_clarification")
                self.assertEqual(result["analysis_code"], expected_codes[index - 1])
                self.assertNotIn("operations", result)
                self.assertEqual(result["model_evidence"]["call_count"], 1)
                if index == 1:
                    self.assertEqual(result["model_evidence"]["provider"], "redacted")
                    self.assertEqual(result["model_evidence"]["model"], "redacted")

    def test_read_only_question_or_invalid_context_does_not_invoke_model(self):
        editor = _Editor("should not be called")
        invalid = analyze_formal_script_instruction(
            instruction="旁白更自然", shots=_shots(),
            assistant_locked_shot_ids=["unknown-shot"], editor_policy=editor.policy(),
        )
        self.assertEqual(invalid["type"], "needs_clarification")
        self.assertEqual(invalid["analysis_code"], "invalid_context")
        question = analyze_formal_script_instruction(
            instruction="请解释特写镜头", shots=_shots(),
            assistant_locked_shot_ids=["shot-a"], editor_policy=editor.policy(),
        )
        self.assertEqual(question["type"], "needs_clarification")
        self.assertEqual(question["analysis_code"], "unsupported_instruction")
        self.assertEqual(editor.calls, [])


if __name__ == "__main__":
    unittest.main()
