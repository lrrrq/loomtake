from io import BytesIO
import json
import unittest

from mvp_v2.formal_brief_input_adapter import UploadedDocument, FormalBriefUnderstandingPolicy
from tests import test_mvp_v2_formal_console as console_tests


class CreativeInputQuestionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = console_tests.FormalConsoleTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture._project()
        self.scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.project["project_id"]}

    def questions(self):
        return self.fixture.adapter.input_questions(self.fixture.token, **self.scope)

    def test_only_missing_authoritative_fields_are_asked_without_blocking_creative_work(self):
        before = self.questions()
        self.assertEqual({row["field"] for row in before}, {"脚本类型", "目标时长", "风格调性", "视频目标"})
        self.assertTrue(all(row["can_defer"] for row in before))
        self.assertEqual(next(row for row in before if row["field"] == "脚本类型")["initial_value"], "发布会")
        text = "脚本类型：发布会\n目标时长：90秒\n风格调性：克制自然\n视频目标：呈现产品使用体验"
        self.fixture.adapter.import_uploads(self.fixture.token, **self.scope,
            uploads=[UploadedDocument("匿名要求.txt", BytesIO(text.encode()), True)], expected_project_version=0)
        self.assertEqual(self.questions(), [])

    def test_inline_answer_uses_frozen_input_and_does_not_confirm_brief(self):
        result = self.fixture.adapter.import_inline_answer(self.fixture.token, **self.scope,
            field="目标时长", value="60秒", expected_project_version=0)
        self.assertEqual(result["confirmation_status"], "pending")
        self.assertNotIn("目标时长", {row["field"] for row in self.questions()})
        self.assertEqual(self.fixture._table_count("brief_confirmations"), 0)
        stored = self.fixture.adapter.service.read_current_formal(self.fixture.token, **self.scope)
        self.assertTrue(stored["brief"]["source_manifest"]["documents"])
        self.assertTrue(all(fact["evidence_refs"] for fact in stored["brief"]["facts"]))

    def test_natural_language_tone_is_consumed_without_asking_it_again(self):
        text = "请写新品发布会短片。风格真实、克制，有现场感。"
        def provider(prompt, model, tokens):
            return json.dumps({"facts": [{"field": "风格调性", "value": "真实、克制，有现场感",
                "evidence": [{"key": "E0001", "quote": "风格真实、克制，有现场感"}]}]}, ensure_ascii=False)
        self.fixture.adapter.understanding_policy = FormalBriefUnderstandingPolicy(provider, "anonymous", "anonymous")
        self.fixture.adapter.import_uploads(self.fixture.token, **self.scope,
            uploads=[UploadedDocument("匿名自然要求.txt", BytesIO(text.encode()), True)], expected_project_version=0)
        self.assertNotIn("风格调性", {row["field"] for row in self.questions()})


if __name__ == "__main__":
    unittest.main()
