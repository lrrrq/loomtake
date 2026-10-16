from __future__ import annotations

from copy import deepcopy
import json
import os
import unittest
from unittest.mock import patch

from mvp_v2.formal_advisor import (
    CONVERSATION_SUMMARY_MAX_CHARACTERS,
    CONVERSATION_SUMMARY_PROMPT_VERSION,
    FormalAdvisorPolicy,
    FormalAdvisorService,
)
from mvp_v2.workspace_store import WorkspaceServiceError


class DirectorAdvisorTests(unittest.TestCase):
    """Response and citation contracts; controlled Provider, not creative quality evidence."""

    def setUp(self):
        self.calls = []
        self.discussion = {
            "mode": "creative_workbench", "evidence_ids": ["current-brief"],
            "snapshot": {"stage": "discussion", "targets": [],
                         "ui_scope": {"mode": "all", "selected_numbers": []}},
            "conversation": [
                {"message_id": "m1", "role": "user", "content": "结尾保留人物回望，不要口号。"},
                {"message_id": "m2", "role": "assistant", "content": "方向一：群像。方向二：单人观察。"},
            ],
            "history_summary": {"content": "曾讨论高反差方向，后来否定。", "source_message_ids": ["old-m1"]},
            "project_conventions": [{"content": "克制的结尾"}],
            "retired_conventions": [{"content": "每镜加品牌标志"}],
            "sources": [{"id": "current-brief", "label": "已确认需求"}],
        }

    def service(self, output):
        def invoke(prompt, model, tokens):
            self.calls.append((prompt, model, tokens))
            if isinstance(output, Exception):
                raise output
            return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return FormalAdvisorService(FormalAdvisorPolicy(
            invoke=invoke, provider="controlled-director", model="owned-route", max_output_tokens=8192))

    def answer(self, **updates):
        return {"intent": "discussion", "summary": "三种方向的主要取舍如下。",
                "content": "从一个人的动作展开。", "actions": [], "candidate_action": "none", **updates}

    def test_no_draft_compares_three_directions_with_complete_content_without_actions(self):
        body = "方向一采用平行群像，方向二跟随一位观察者，方向三用物件变化串联时间。\n" * 35
        choices = [{"label": label, "description": description, "instruction": instruction}
                   for label, description, instruction in [
                       ("群像", "信息覆盖广，人物情感较分散。", "按群像方向继续讨论"),
                       ("单人观察", "情感集中，需要连贯人物行动。", "按单人观察方向继续讨论"),
                       ("物件线索", "适合克制表达，需要物件前后变化。", "按物件线索方向继续讨论"),
                   ]]
        before = deepcopy(self.discussion)
        decision = self.service(self.answer(content=body, choices=choices, evidence_ids=["current-brief"])).decide(
            "比较三种叙事方向及取舍，先不改稿。", self.discussion)
        self.assertEqual(decision.content, body)
        self.assertGreater(len(decision.content), 1000)
        self.assertEqual([choice.model_dump() for choice in decision.choices], choices)
        self.assertEqual(decision.actions, [])
        self.assertEqual(decision.candidate_action, "none")
        self.assertEqual(self.discussion, before)
        prompt, model, tokens = self.calls[0]
        self.assertEqual((model, tokens), ("owned-route", 8192))
        context_text = prompt.split("\nCONTEXT:", 1)[1].rsplit("\nUSER:", 1)[0]
        self.assertEqual(json.loads(context_text), before)
        self.assertIn("无稿讨论时actions=[]", prompt)
        self.assertIn("不固定为两种", prompt)
        self.assertIn("不得继续将已撤回约定作为约束", prompt)

    def test_current_reference_can_use_historical_direction_but_cannot_reuse_historical_authorization(self):
        context = deepcopy(self.discussion)
        context["snapshot"] = {"stage": "creative", "targets": [
            {"number": number, "fields": {"画面描述": "人物交流", "声音": "现场声"}, "protected": False}
            for number in [1, 2, 3]],
            "ui_scope": {"mode": "selected", "selected_numbers": [2], "current_number": 2}}
        action = {"kind": "revise_text", "source_clause": "按第二个", "target_scope": "numbers",
                  "target_numbers": [2], "fields": ["visual"],
                  "instruction": "采用单人观察方向，通过人物停步和目光推进，只改第2段画面，保留结尾回望。",
                  "depends_on": []}
        payload = self.answer(intent="workbench_revision", actions=[action],
                              constraints={"preserve": ["结尾人物回望"], "prohibit": ["不要口号"]})
        decision = self.service(payload).decide("按第二个，只改第2段，保留刚才说的结尾。", context)
        self.assertEqual(decision.actions[0].source_clause, "按第二个")
        self.assertIn("单人观察", decision.actions[0].instruction)
        self.assertEqual(decision.constraints.prohibit, ["不要口号"])
        forged = deepcopy(payload)
        forged["actions"][0]["source_clause"] = "结尾保留人物回望，不要口号。"
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.service(forged).decide("按第二个，只改第2段，保留刚才说的结尾。", context)
        self.assertEqual(caught.exception.code, "FORMAL_ADVISOR_SOURCE_INVALID")

    def test_discussion_cannot_smuggle_action_and_no_draft_cannot_target_nonexistent_shot(self):
        action = {"kind": "revise_text", "source_clause": "改第1段", "target_scope": "numbers",
                  "target_numbers": [1], "fields": ["visual"], "instruction": "人物回望", "depends_on": []}
        for intent, code in [("discussion", "FORMAL_ADVISOR_INVALID_RESPONSE"),
                             ("workbench_revision", "FORMAL_ADVISOR_SCOPE_INVALID")]:
            with self.subTest(intent=intent), self.assertRaises(WorkspaceServiceError) as caught:
                self.service(self.answer(intent=intent, actions=[action])).decide("改第1段", self.discussion)
            self.assertEqual(caught.exception.code, code)

    def test_old_response_can_omit_content_and_choice_description(self):
        result = self.service({"intent": "needs_clarification", "summary": "选择一种方向。",
                               "choices": [{"label": "克制", "instruction": "保持事实，减少口号"}]}).decide(
            "更克制些", self.discussion)
        self.assertEqual(result.content, "")
        self.assertEqual(result.choices[0].description, "")

    def test_complete_content_cannot_cite_unknown_evidence_or_leak_runtime_secret(self):
        for payload, code in [
            (self.answer(evidence_ids=["another-project"]), "FORMAL_ADVISOR_EVIDENCE_INVALID"),
            (self.answer(content="anonymous-director-secret"), "FORMAL_ADVISOR_SENSITIVE_OUTPUT"),
        ]:
            with self.subTest(code=code), patch.dict(os.environ, {"DEEPSEEK_API_KEY": "anonymous-director-secret"}):
                with self.assertRaises(WorkspaceServiceError) as caught:
                    self.service(payload).decide("讨论方向", self.discussion)
                self.assertEqual(caught.exception.code, code)

    def test_summary_keeps_original_message_ids_and_does_not_mutate_history(self):
        messages = deepcopy(self.discussion["conversation"])
        messages += [{"message_id": "m3", "role": "user", "content": "否定第二种，保留结尾；时长还未确认。"}]
        before = deepcopy(messages)
        expected = {"content": "用户保留结尾回望、禁止口号；否定第二种方向；时长待确认。",
                    "source_message_ids": ["m1", "m2", "m3"]}
        actual = self.service(expected).summarize_conversation(messages)
        self.assertEqual(actual, expected)
        self.assertEqual(messages, before)
        prompt = self.calls[0][0]
        self.assertEqual(json.loads(prompt.split("\nMESSAGES:", 1)[1]), messages)
        self.assertIn(CONVERSATION_SUMMARY_PROMPT_VERSION, prompt)
        self.assertIn("否定、例外和适用条件必须原义保留", prompt)
        self.assertIn("未完成动作", prompt)

    def test_summary_rejects_unsupported_missing_duplicate_or_extra_sources(self):
        invalid = [
            {"content": "摘要", "source_message_ids": ["other-project-message"]},
            {"content": "摘要", "source_message_ids": []},
            {"content": "摘要", "source_message_ids": ["m1", "m1"]},
            {"content": "摘要", "source_message_ids": [True]},
            {"content": "摘要", "source_message_ids": [{"id": "m1"}]},
            {"content": "摘要", "source_message_ids": ["m1"], "approved": True},
            {"content": "", "source_message_ids": ["m1"]},
            {"content": "长" * (CONVERSATION_SUMMARY_MAX_CHARACTERS + 1), "source_message_ids": ["m1"]},
            "not json",
        ]
        for payload in invalid:
            with self.subTest(payload=str(payload)[:80]), self.assertRaises(WorkspaceServiceError) as caught:
                self.service(payload).summarize_conversation(self.discussion["conversation"])
            self.assertEqual(caught.exception.code, "FORMAL_ADVISOR_SUMMARY_INVALID")

    def test_summary_requires_valid_original_identity_before_calling_provider(self):
        service = self.service({"content": "摘要", "source_message_ids": ["m1"]})
        for messages in [[], [{"role": "user", "content": "没有引用"}],
                         [{"message_id": "m1"}, {"message_id": "m1"}], [{"message_id": []}]]:
            with self.subTest(messages=messages), self.assertRaises(WorkspaceServiceError) as caught:
                service.summarize_conversation(messages)
            self.assertEqual(caught.exception.code, "FORMAL_ADVISOR_SUMMARY_INVALID")
        self.assertEqual(self.calls, [])

    def test_summary_unavailable_raises_without_inventing_fallback_or_leaking_exception(self):
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.service(RuntimeError("private transport detail")).summarize_conversation(self.discussion["conversation"])
        self.assertEqual(caught.exception.code, "FORMAL_ADVISOR_SUMMARY_UNAVAILABLE")
        self.assertNotIn("private transport", str(caught.exception))

    def test_summary_redacts_provider_input_and_discards_sensitive_output(self):
        secret = "anonymous-summary-runtime-secret"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            messages = [{"message_id": "m1", "role": "user", "content": f"不要记录 {secret}"}]
            result = self.service({"content": "用户要求不记录敏感内容。", "source_message_ids": ["m1"]}).summarize_conversation(messages)
            self.assertNotIn(secret, self.calls[-1][0])
            self.assertEqual(result["source_message_ids"], ["m1"])
            with self.assertRaises(WorkspaceServiceError) as caught:
                self.service({"content": secret, "source_message_ids": ["m1"]}).summarize_conversation(messages)
            self.assertEqual(caught.exception.code, "FORMAL_ADVISOR_SUMMARY_SENSITIVE_OUTPUT")


if __name__ == "__main__":
    unittest.main()
