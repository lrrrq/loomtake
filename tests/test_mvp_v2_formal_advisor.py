from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from mvp_v2.formal_advisor import (
    FORMAL_ADVISOR_CACHE_NAMESPACE,
    FORMAL_ADVISOR_MAX_OUTPUT_TOKENS,
    FORMAL_ADVISOR_PROMPT_VERSION,
    FormalAdvisorPolicy,
    FormalAdvisorService,
)
from mvp_v2.workspace_store import WorkspaceServiceError


class FormalAdvisorTests(unittest.TestCase):
    def test_image_intent_keeps_only_bounded_semantic_query_and_no_action_parameters(self):
        service = FormalAdvisorService(FormalAdvisorPolicy(
            invoke=lambda *_args: json.dumps({
                "intent": "inspect_image_candidates",
                "summary": "检索第 3 镜生活方式候选。",
                "evidence_ids": [],
                "questions": [],
                "candidate_action": "apply_formal_script_revision",
                "image_query": "更偏生活方式，人物自然互动",
            }),
            provider="deepseek-http", model="deepseek-v4-flash",
        ))

        decision = service.decide("给第 3 镜找几张更偏生活方式的候选", {"evidence_ids": []})

        self.assertEqual(decision.intent, "inspect_image_candidates")
        self.assertEqual(decision.candidate_action, "none")
        self.assertEqual(decision.image_query, "更偏生活方式，人物自然互动")

    def test_valid_decision_uses_owned_route_limit_and_prompt_namespace(self):
        calls = []

        def invoke(prompt, model, max_tokens):
            calls.append((prompt, model, max_tokens))
            return json.dumps({
                "intent": "prepare_plan",
                "summary": "可以准备当前项目规划。",
                "evidence_ids": ["project:project_12345678:v0"],
                "questions": [],
                "candidate_action": "prepare_formal_plan",
            })

        service = FormalAdvisorService(FormalAdvisorPolicy(
            invoke=invoke,
            provider="deepseek-http",
            model="deepseek-v4-flash",
        ))
        decision = service.decide("请准备规划", {
            "evidence_ids": ["project:project_12345678:v0"],
            "project_version": 0,
        })

        self.assertEqual(decision.intent, "prepare_plan")
        self.assertEqual(calls[0][1:], ("deepseek-v4-flash", FORMAL_ADVISOR_MAX_OUTPUT_TOKENS))
        self.assertIn(f"PROMPT_VERSION:{FORMAL_ADVISOR_PROMPT_VERSION}", calls[0][0])
        self.assertIn(f"CACHE_NAMESPACE:{FORMAL_ADVISOR_CACHE_NAMESPACE}", calls[0][0])

    def test_unknown_intent_extra_parameters_and_cross_project_evidence_are_rejected(self):
        invalid = (
            ({
                "intent": "publish_version", "summary": "越权", "evidence_ids": [],
                "questions": [], "candidate_action": "none",
            }, "FORMAL_ADVISOR_INVALID_RESPONSE"),
            ({
                "intent": "prepare_plan", "summary": "伪造写参数", "evidence_ids": [],
                "questions": [], "candidate_action": "prepare_formal_plan",
                "path": "/tmp/forged", "project_id": "project_other0000",
            }, "FORMAL_ADVISOR_INVALID_RESPONSE"),
            ({
                "intent": "prepare_plan", "summary": "跨项目证据",
                "evidence_ids": ["project:project_other0000:v0"],
                "questions": [], "candidate_action": "prepare_formal_plan",
            }, "FORMAL_ADVISOR_EVIDENCE_INVALID"),
        )
        for payload, expected_code in invalid:
            with self.subTest(expected_code=expected_code):
                service = FormalAdvisorService(FormalAdvisorPolicy(
                    invoke=lambda *_args, value=payload: json.dumps(value),
                    provider="deepseek-http", model="deepseek-v4-flash",
                ))
                with self.assertRaises(WorkspaceServiceError) as caught:
                    service.decide("执行", {
                        "evidence_ids": ["project:project_12345678:v0"],
                    })
                self.assertEqual(caught.exception.code, expected_code)

    def test_candidate_action_is_ignored_and_rebuilt_from_valid_intent(self):
        service = FormalAdvisorService(FormalAdvisorPolicy(
            invoke=lambda *_args: json.dumps({
                "intent": "explain_status", "summary": "查看当前状态",
                "evidence_ids": [], "questions": [],
                "candidate_action": "confirm_formal_plan_generation",
            }),
            provider="deepseek-http", model="deepseek-v4-flash",
        ))

        decision = service.decide("当前状态", {"evidence_ids": []})

        self.assertEqual(decision.intent, "explain_status")
        self.assertEqual(decision.candidate_action, "none")

    def test_runtime_key_is_redacted_from_prompt_and_sensitive_output_is_discarded(self):
        secret = "deepseek-secret-for-formal-advisor-test"
        prompts = []

        def safe_invoke(prompt, _model, _tokens):
            prompts.append(prompt)
            return json.dumps({
                "intent": "explain_status", "summary": "已读取状态",
                "evidence_ids": [], "questions": [], "candidate_action": "none",
            })

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": secret}):
            service = FormalAdvisorService(FormalAdvisorPolicy(
                invoke=safe_invoke, provider="deepseek-http", model="deepseek-v4-flash",
            ))
            service.decide(f"不要记录 {secret}", {"evidence_ids": []})
            self.assertNotIn(secret, prompts[0])
            self.assertIn("[REDACTED]", prompts[0])

            leaking = FormalAdvisorService(FormalAdvisorPolicy(
                invoke=lambda *_args: json.dumps({
                    "intent": "explain_status", "summary": secret,
                    "evidence_ids": [], "questions": [], "candidate_action": "none",
                }),
                provider="deepseek-http", model="deepseek-v4-flash",
            ))
            with self.assertRaises(WorkspaceServiceError) as caught:
                leaking.decide("状态", {"evidence_ids": []})
            self.assertEqual(caught.exception.code, "FORMAL_ADVISOR_SENSITIVE_OUTPUT")
            self.assertNotIn(secret, str(caught.exception))

    def test_provider_exception_is_replaced_with_non_sensitive_error(self):
        secret = "provider-exception-secret"

        def unavailable(*_args):
            raise RuntimeError(secret)

        service = FormalAdvisorService(FormalAdvisorPolicy(
            invoke=unavailable, provider="deepseek-http", model="deepseek-v4-flash",
        ))
        with self.assertRaises(WorkspaceServiceError) as caught:
            service.decide("状态", {"evidence_ids": []})
        self.assertEqual(caught.exception.code, "FORMAL_ADVISOR_UNAVAILABLE")
        self.assertNotIn(secret, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
