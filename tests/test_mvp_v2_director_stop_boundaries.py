"""Natural stop prevents the next nested call and retains completed results.

Providers are gated with events; drafts, searches, tasks and readback use the
real services over an existing edited three-block project with saved images.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from threading import Event
import unittest
from unittest.mock import patch

from mvp_v2 import creative_workbench, formal_image_batch
from mvp_v2.formal_advisor import FormalAdvisorPolicy, FormalAdvisorService
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class DirectorStopBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.base = workbench_fixture.CreativeWorkbenchTests("runTest")
        self.addCleanup(self.base.doCleanups)
        self.base.setUp()
        self.agent, self.token = self.base.agent, self.base.token
        self.session_id = self.base.session["agent_session_id"]
        self.tasks = self.agent.services.tasks
        self.before = self._saved_business()

    def _saved_business(self):
        body = self.base.repo.read()["body"]
        # Proposals record task links and recovery receipts in this repository;
        # the saved draft, manual edits, image choices and their bases stay put.
        return {key: deepcopy(body.get(key)) for key in (
            "draft", "image_references", "patches", "inputs", "generation_basis",
            "assistant_protected_ids",
        )}

    def _stop_during_call(self, entered, release, key):
        started = self.agent.start_workbench(
            self.token, self.session_id,
            content="修改第1、2段画面和声音，再给这两段寻找配图，保留其他内容。",
            context=self.base.context(), operation_key=key,
        )
        task_id = started["task"]["task_id"]
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(self.agent.execute_workbench, self.token, task_id)
            try:
                self.assertTrue(entered.wait(20), "The nested call did not reach its gate")
                stopped = self.agent.start_workbench(
                    self.token, self.session_id, content="先停一下。", context=None,
                    operation_key=key + "-stop",
                )
                self.assertEqual(stopped["task"]["status"], "completed")
            finally:
                release.set()
            result = future.result(timeout=20)
        self.assertEqual(result["task"]["status"], "cancelled", result["response"])
        self.assertEqual(result["response"]["type"], "workbench_cancelled")
        fresh = self.base.new_agent().read_workbench(self.token, task_id)
        self.assertEqual(fresh["task"]["status"], "cancelled")
        self.assertEqual(fresh["response"]["type"], "workbench_cancelled")
        self.assertEqual(self._saved_business(), self.before,
                         "Stopping must preserve the whole saved draft and its image choices")
        return task_id

    def _seed_long_history(self):
        for index in range(1, 5):
            project = self.agent.services.projects.get_project(
                self.token, self.base.scope["project_id"])
            task = self.tasks.create_task(
                self.token, agent_session_id=self.session_id,
                task_type="formal_production_advisor", payload_sha256=str(index) * 64,
                expected_project_version=project["project_version"],
            )
            for status, details in (
                ("understanding", {"workbench": {"request": {
                    "content": f"第{index}轮讨论声音方向。" + "甲" * 4900}}}),
                ("executing", {}),
                ("completed", {"response": {"type": "read_response",
                    "content": "人物声音创作讨论。" + "乙" * 4900}}),
            ):
                task = self.tasks.transition_task(
                    self.token, task_id=task["task_id"],
                    expected_task_version=task["task_version"], status=status,
                    details=details,
                )

    def test_stop_during_history_summary_does_not_start_advisor_after_summary_returns(self):
        self._seed_long_history()
        entered, release, calls = Event(), Event(), []

        def provider(prompt, model, tokens):
            if "\nMESSAGES:" in prompt:
                messages = json.loads(prompt.split("\nMESSAGES:", 1)[1])
                calls.append("summary")
                entered.set()
                if not release.wait(20):
                    raise AssertionError("The summary gate was not released")
                return json.dumps({"content": "历史讨论涉及声音方向。",
                    "source_message_ids": [row["message_id"] for row in messages]},
                    ensure_ascii=False)
            calls.append("advisor")
            return self.base.provider.advisor(prompt, model, tokens)

        self.agent.advisor = FormalAdvisorService(FormalAdvisorPolicy(
            invoke=provider, provider="anonymous-stop-boundary", model="anonymous"))
        with patch.dict(os.environ, {"MPLUS_DIRECTOR_HISTORY_CHARS": "16000"}):
            self._stop_during_call(entered, release, "stop-during-history-summary")
        self.assertEqual(calls, ["summary"],
                         "A completed in-flight summary must not launch the advisor after stop")
        self.assertEqual(self.base.provider.editor_inputs, [])

    def test_stop_during_first_library_call_keeps_candidates_without_starting_second_target(self):
        entered, release, calls, catalogs = Event(), Event(), [], []
        original = self.base.library.candidates

        def candidates(*args, **kwargs):
            calls.append(str(kwargs["shot_no"]))
            catalog = original(*args, **kwargs)
            if len(calls) == 1:
                catalogs.append(deepcopy(catalog))
                entered.set()
                if not release.wait(20):
                    raise AssertionError("The image retrieval gate was not released")
            return catalog

        with patch.object(self.base.library, "candidates", side_effect=candidates):
            task_id = self._stop_during_call(entered, release, "stop-during-first-library-call")
        self.assertEqual(calls, ["1"],
                         "Stop must prevent retrieval for the next selected target")
        data = creative_workbench._load(self.agent, self.token, task_id)[1]
        child_id = data["image_task_id"]
        readback = self.base.new_agent().script_revision.read_image_batch(
            self.token, **self.base.scope, task_id=child_id)
        response = readback["response"]
        self.assertEqual(readback["task"]["status"], "cancelled")
        self.assertTrue(response["stop_requested"])
        self.assertEqual(response["remaining_target_ids"], ["block_2"])
        self.assertEqual(response["progress"]["completed"], 1)
        self.assertEqual(response["progress"]["total"], 2)
        self.assertEqual([row["target_id"] for row in response["shots"]], ["block_1"])
        expected = next(row["candidates"] for row in catalogs[0]["shots"]
                        if str(row["shot_no"]) == "1")
        self.assertTrue(expected, "The first real retrieval must produce candidates")
        actual = [row for row in response["shots"][0]["candidates"]
                  if row["source_label"] == "创意库"]
        # Public proposals retain a bounded shortlist from the full catalog.
        self.assertGreater(len(actual), 1)
        self.assertIn(expected[0]["content_sha256"],
                      {row["content_sha256"] for row in actual})
        parameters = formal_image_batch._load(self.base.revision, self.token, child_id)[1]
        expected_by_key = {row["candidate_key"]: row for row in expected}
        for candidate in actual:
            original_key = parameters["choices"][candidate["candidate_key"]]["library_candidate_key"]
            source = expected_by_key[original_key]
            self.assertEqual(candidate["content_sha256"], source["content_sha256"])
            self.assertEqual(candidate["media_id"], source["media_id"])
        parent = self.base.new_agent().read_workbench(self.token, task_id)
        group = next(row for row in parent["response"]["groups"]
                     if row["target_id"] == "block_1")
        self.assertEqual(group["image"]["candidates"], response["shots"][0]["candidates"])
        self.assertEqual(self._saved_business(), self.before)


if __name__ == "__main__":
    unittest.main()
