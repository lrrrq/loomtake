"""Independent work survives failures; adoption respects cross-group dependencies.

Only the editor provider is failed deliberately. Existing draft, image search,
proposal, adoption and refresh behavior use their real authority services.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class DirectorDependencyTests(unittest.TestCase):
    def setUp(self):
        self.base = workbench_fixture.CreativeWorkbenchTests("runTest")
        self.addCleanup(self.base.doCleanups)
        self.base.setUp()
        self.agent, self.token = self.base.agent, self.base.token
        self.session_id = self.base.session["agent_session_id"]
        self.before = self._saved_business()

    def _saved_business(self):
        body = self.base.repo.read()["body"]
        return {key: deepcopy(body.get(key)) for key in (
            "draft", "image_references", "patches", "inputs", "generation_basis",
            "assistant_protected_ids",
        )}

    @staticmethod
    def _action(kind, number, instruction, fields=(), depends_on=()):
        return {"kind": kind, "target_scope": "numbers", "target_numbers": [number],
                "fields": list(fields), "instruction": instruction,
                "depends_on": list(depends_on)}

    def _propose(self, content, key):
        context = {**self.base.context(),
                   "selected_target_ids": ["block_1", "block_2", "block_3"]}
        started = self.agent.start_workbench(
            self.token, self.session_id, content=content,
            context=context, operation_key=key,
        )
        return self.agent.execute_workbench(self.token, started["task"]["task_id"])

    def _confirm(self, proposal, groups):
        return self.agent.confirm_workbench(
            self.token, proposal["task"]["task_id"],
            proposal_digest=proposal["response"]["proposal_digest"],
            retained_group_ids=groups,
        )

    def test_failed_text_keeps_independent_images_and_persists_blocked_dependency(self):
        self.base.provider.actions = [
            self._action("revise_text", 1, "调整第1段画面", fields=["visual"]),
            self._action("find_images", 2, "依据第1段的新画面给第2段重新配图", depends_on=[0]),
            self._action("find_images", 3, "独立给第3段重新配图，更换主图"),
        ]
        editor_inputs = []

        def unavailable_editor(prompt, _model, _tokens):
            editor_inputs.append(json.loads(prompt.split("\nDATA:", 1)[1]))
            raise RuntimeError("anonymous editor transport failure")

        self.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=unavailable_editor, provider="anonymous-dependencies", model="anonymous")
        with patch.object(self.base.library, "candidates", wraps=self.base.library.candidates) as retrieval:
            proposal = self._propose(
                "先调整第1段画面；第2段根据第1段的新画面重新配图。另独立给第3段重新配图，更换主图。",
                "director-independent-images-after-editor-failure",
            )
        self.assertEqual(len(editor_inputs), 1)
        self.assertEqual({row["shot_id"] for row in editor_inputs[0]["targets"]}, {"block_1"})
        self.assertEqual([str(call.kwargs["shot_no"]) for call in retrieval.call_args_list],
                         ["3"], proposal["response"])
        self.assertEqual(self._saved_business(), self.before)
        self.assertEqual(proposal["response"]["type"], "workbench_proposal")
        task_id = proposal["task"]["task_id"]
        fresh = self.base.new_agent().read_workbench(self.token, task_id)
        self.assertEqual(fresh["response"], proposal["response"])
        pending = {row["action_index"]: row for row in fresh["response"]["pending_actions"]}
        self.assertEqual(set(pending), {0, 1})
        self.assertEqual(pending[0]["status"], "failed")
        self.assertTrue(pending[0]["error_code"])
        self.assertTrue(pending[0]["summary"])
        self.assertEqual(pending[0]["target_ids"], ["block_1"])
        self.assertEqual(pending[0]["depends_on"], [])
        self.assertEqual(pending[1]["status"], "awaiting_dependency")
        self.assertEqual(pending[1]["target_ids"], ["block_2"])
        self.assertEqual(pending[1]["depends_on"], [0])
        images = {group["target_id"]: group["image"] for group in fresh["response"]["groups"]
                  if group.get("image")}
        self.assertEqual(set(images), {"block_3"})
        self.assertGreater(len(images["block_3"]["candidates"]), 1)
        self.assertTrue(all(row["content_sha256"] and row["candidate_key"]
                            for row in images["block_3"]["candidates"]))
        self.assertEqual(self._saved_business(), self.before)

    def test_cross_group_dependency_rejects_orphan_adoption_but_independent_group_can_apply(self):
        # Ensure adopting group 2 would actually write a real matching image;
        # an unselected reference-only candidate would be a harmless no-op.
        self.base.manual("block_2", "画面描述", "外场入口人群在展板前交流，固定中景记录现场互动。",
                         "existing-second-block-matching-scene")
        self.before = self._saved_business()
        business_before = self.base.business()
        self.base.provider.actions = [
            self._action("revise_text", 1, "调整第1段画面", fields=["visual"]),
            self._action("find_images", 2, "依据第1段的新画面给第2段重新配图", depends_on=[0]),
            self._action("revise_text", 3, "独立调整第3段声音", fields=["sound"]),
        ]
        proposal = self._propose(
            "调整第1段画面，然后第2段根据第1段的新画面重新配图；独立调整第3段声音，保留其他内容。",
            "director-cross-group-dependency-adoption",
        )
        self.assertEqual(proposal["response"]["type"], "workbench_proposal", proposal["response"])
        groups = {group["group_id"]: group for group in proposal["response"]["groups"]}
        self.assertEqual(set(groups), {"block_1", "block_2", "block_3"}, proposal["response"])
        self.assertEqual(groups["block_2"]["depends_on"], ["block_1"])
        self.assertEqual(groups["block_3"]["depends_on"], [])
        self.assertTrue(groups["block_2"]["image"]["candidates"])
        self.assertTrue(groups["block_2"]["image"]["selected_candidate_key"])
        self.assertTrue(groups["block_2"]["image"]["included"])
        self.assertEqual(self._saved_business(), self.before)
        database = self.base.database()
        with self.assertRaisesRegex(WorkspaceServiceError, "WORKBENCH_DEPENDENCY_REQUIRED"):
            self._confirm(proposal, ["block_2"])
        self.assertEqual(self.base.database(), database)
        self.assertEqual(self._saved_business(), self.before)

        applied = self._confirm(proposal, ["block_3"])
        self.assertEqual(applied["response"]["applied_group_ids"], ["block_3"])
        self.assertEqual(set(applied["response"]["remaining_group_ids"]), {"block_1", "block_2"})
        after = self.base.business()
        self.assertEqual(after["shots"][:2], business_before["shots"][:2])
        self.assertEqual(after["refs"], business_before["refs"])
        previous_third, current_third = business_before["shots"][2], after["shots"][2]
        self.assertNotEqual(current_third["fields"]["声音"], previous_third["fields"]["声音"])
        expected_third = deepcopy(previous_third)
        expected_third["fields"]["声音"] = current_third["fields"]["声音"]
        self.assertEqual(current_third, expected_third)
        fresh = self.base.new_agent()
        self.assertEqual(fresh.read_workbench(self.token, proposal["task"]["task_id"])["response"],
                         applied["response"])
        reread = fresh.generation.read_creative_draft(self.token, **self.base.scope)
        self.assertEqual(reread["blocks"][2]["fields"], current_third["fields"])
        self.assertEqual(self.base.business(), after)

    def _recoverable_failure(self, key):
        # Keep two real existing references, away from the highest-ranked
        # matching images, so the independent result is a genuine new choice.
        catalog = self.base.library.candidates(
            self.token, **self.base.scope,
            expected_project_version=self.base.repo.read()["project_version"], view="library")
        prior_images = catalog["library_candidates"][20:22]
        self.assertEqual(len(prior_images), 2)
        self.base.base._save([
            {"block_id": "block_3", "library_candidate_key": row["candidate_key"], "primary": index == 0}
            for index, row in enumerate(prior_images)
        ], replace=["block_3"], operation=key + "-existing-images")
        self.base.manual("block_3", "画面描述", "外场入口人群在展板前交流，固定中景记录现场互动。",
                         key + "-existing-third-scene")
        self.base.manual("block_2", "画面描述", "外场入口人群在展板前交流，固定中景记录现场互动。",
                         key + "-existing-second-scene")
        self.before = self._saved_business()
        self.base.provider.actions = [
            self._action("revise_text", 1, "调整第1段画面", fields=["visual"]),
            self._action("find_images", 2, "依据第1段的新画面给第2段重新配图", depends_on=[0]),
            self._action("find_images", 3, "独立给第3段重新配图，更换主图"),
        ]
        editor_calls = []

        def unavailable_editor(prompt, _model, _tokens):
            editor_calls.append(json.loads(prompt.split("\nDATA:", 1)[1]))
            raise RuntimeError("anonymous recoverable editor failure")

        self.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=unavailable_editor, provider="anonymous-dependencies", model="anonymous")
        proposal = self._propose(
            "先调整第1段画面；第2段根据第1段的新画面重新配图。另独立给第3段重新配图，更换主图。", key)
        self.assertEqual(len(editor_calls), 1)
        self.assertEqual(proposal["response"]["type"], "workbench_proposal", proposal["response"])
        self.assertTrue(proposal["response"]["can_resume"])
        image = next(group["image"] for group in proposal["response"]["groups"]
                     if group["target_id"] == "block_3")
        self.assertTrue(image["selected_candidate_key"], image)
        self.assertTrue(image["included"], image)
        self.assertEqual(self._saved_business(), self.before)
        return proposal

    @staticmethod
    def _retrieved_shots(retrieval):
        return [str(call.kwargs["shot_no"]) for call in retrieval.call_args_list
                if call.kwargs.get("shot_no") is not None]

    @staticmethod
    def _candidate_identities(image):
        return {row["candidate_key"]: (row.get("media_id"), row["content_sha256"])
                for row in image["candidates"]}

    def test_resume_before_adoption_retries_failed_dependency_and_reuses_successful_candidates(self):
        with patch.object(self.base.library, "candidates", wraps=self.base.library.candidates) as retrieval:
            proposal = self._recoverable_failure("director-recover-before-adoption")
            parent_id = proposal["task"]["task_id"]
            original_image = next(group["image"] for group in proposal["response"]["groups"]
                                  if group["target_id"] == "block_3")
            fresh = self.base.new_agent()
            self.base.provider.install(fresh)
            resumed = fresh.resume_workbench(self.token, parent_id)
            child_id = resumed["task"]["task_id"]
            self.assertNotEqual(child_id, parent_id,
                                "Pending failures must not be trapped behind the existing usable proposal")
            self.assertEqual(resumed["task"]["agent_session_id"], self.session_id)
            self.assertEqual(fresh.resume_workbench(self.token, parent_id)["task"]["task_id"], child_id)
            recovered = fresh.execute_workbench(self.token, child_id)
            self.assertEqual(recovered["response"]["type"], "workbench_proposal", recovered["response"])
            self.assertEqual(self._retrieved_shots(retrieval), ["3", "2"])
            groups = {group["target_id"]: group for group in recovered["response"]["groups"]}
            self.assertEqual(set(groups), {"block_1", "block_2", "block_3"})
            self.assertTrue(groups["block_1"]["changes"])
            self.assertTrue(groups["block_2"]["image"]["candidates"])
            self.assertEqual(groups["block_2"]["depends_on"], ["block_1"])
            self.assertEqual(self._candidate_identities(groups["block_3"]["image"]),
                             self._candidate_identities(original_image))
            self.assertEqual(recovered["response"]["pending_actions"], [])
            self.assertEqual(self._saved_business(), self.before)
            self.assertEqual(len(self.base.provider.advisor_inputs), 1)
            self.assertEqual(len(self.base.provider.editor_inputs), 1)
            self.assertEqual({row["shot_id"] for row in self.base.provider.editor_inputs[0]["targets"]},
                             {"block_1"})
            restarted = self.base.new_agent()
            readback = restarted.read_workbench(self.token, child_id)["response"]
            self.assertEqual({key: readback[key] for key in recovered["response"]}, recovered["response"])
            self.assertEqual(readback["continuation_of_task_id"], parent_id)
            self.assertEqual(restarted.resume_workbench(self.token, parent_id)["task"]["task_id"], child_id)
            self.assertEqual(self._retrieved_shots(retrieval), ["3", "2"])
            self.assertEqual(len(self.base.provider.editor_inputs), 1)
            self.assertEqual(self._saved_business(), self.before)

            selected_images = {}
            for target_id in ("block_2", "block_3"):
                image = groups[target_id]["image"]
                self.assertTrue(image["included"], image)
                selected_images[target_id] = next(row for row in image["candidates"]
                    if row["candidate_key"] == image["selected_candidate_key"])
            applied = restarted.confirm_workbench(
                self.token, child_id, proposal_digest=recovered["response"]["proposal_digest"],
                retained_group_ids=["block_1", "block_2", "block_3"])
            self.assertEqual(set(applied["response"]["applied_group_ids"]),
                             {"block_1", "block_2", "block_3"})
            self.assertEqual(applied["response"]["remaining_group_ids"], [])
            actual = self.base.business()
            self.assertEqual(actual["shots"][1:], self.before["draft"]["shots"][1:])
            expected_first = deepcopy(self.before["draft"]["shots"][0])
            for change in groups["block_1"]["changes"]:
                expected_first["fields"][change["field"]] = change["after"]
            # Adopting a new visual correctly asks for a fresh image review.
            expected_first["image_reconfirmation_required"] = True
            self.assertEqual(actual["shots"][0], expected_first)
            for target_id, candidate in selected_images.items():
                primary = next(row for row in actual["refs"]
                               if row["block_id"] == target_id and row["primary"])
                self.assertEqual(primary["content_sha256"], candidate["content_sha256"])
                self.assertEqual(primary["media_id"], candidate["media_id"])
            saved = self.base.new_agent().read_workbench(self.token, child_id)["response"]
            self.assertEqual({key: saved[key] for key in applied["response"]}, applied["response"])
            self.assertEqual(self.base.business(), actual)
            self.assertEqual(self._retrieved_shots(retrieval), ["3", "2"])

    def test_resume_after_adopting_independent_images_preserves_receipt_and_only_retries_remaining(self):
        with patch.object(self.base.library, "candidates", wraps=self.base.library.candidates) as retrieval:
            proposal = self._recoverable_failure("director-recover-after-independent-adoption")
            parent_id = proposal["task"]["task_id"]
            original_image = next(group["image"] for group in proposal["response"]["groups"]
                                  if group["target_id"] == "block_3")
            selected = next(row for row in original_image["candidates"]
                            if row["candidate_key"] == original_image["selected_candidate_key"])
            applied = self._confirm(proposal, ["block_3"])
            self.assertEqual(applied["response"]["applied_group_ids"], ["block_3"])
            self.assertTrue(applied["response"]["can_resume"],
                            "Failed pending actions must remain resumable after the only ready group is adopted")
            after_adoption = self._saved_business()
            self.assertEqual(after_adoption["draft"], self.before["draft"])
            primary = next(row for row in after_adoption["image_references"]
                           if row["block_id"] == "block_3" and row["primary"])
            self.assertEqual(primary["content_sha256"], selected["content_sha256"])
            self.assertEqual(primary["media_id"], selected["media_id"])
            self.assertNotEqual(after_adoption["image_references"], self.before["image_references"])
            fresh = self.base.new_agent()
            self.base.provider.install(fresh)
            resumed = fresh.resume_workbench(self.token, parent_id)
            child_id = resumed["task"]["task_id"]
            self.assertNotEqual(child_id, parent_id)
            recovered = fresh.execute_workbench(self.token, child_id)
            self.assertEqual(recovered["response"]["type"], "workbench_proposal", recovered["response"])
            self.assertEqual(self._retrieved_shots(retrieval), ["3", "2"])
            groups = {group["target_id"]: group for group in recovered["response"]["groups"]}
            self.assertEqual(set(groups), {"block_1", "block_2"})
            self.assertTrue(groups["block_1"]["changes"])
            self.assertTrue(groups["block_2"]["image"]["candidates"])
            self.assertEqual(groups["block_2"]["depends_on"], ["block_1"])
            self.assertEqual(recovered["response"]["pending_actions"], [])
            self.assertEqual(self._saved_business(), after_adoption)
            self.assertEqual(len(self.base.provider.advisor_inputs), 1)
            self.assertEqual(len(self.base.provider.editor_inputs), 1)
            self.assertEqual({row["shot_id"] for row in self.base.provider.editor_inputs[0]["targets"]},
                             {"block_1"})
            restarted = self.base.new_agent()
            original_receipt = restarted.read_workbench(self.token, parent_id)["response"]
            self.assertEqual({key: original_receipt[key] for key in applied["response"]}, applied["response"])
            self.assertEqual(original_receipt["continuation_task_id"], child_id)
            readback = restarted.read_workbench(self.token, child_id)["response"]
            self.assertEqual({key: readback[key] for key in recovered["response"]}, recovered["response"])
            self.assertEqual(readback["continuation_of_task_id"], parent_id)
            self.assertEqual(restarted.resume_workbench(self.token, parent_id)["task"]["task_id"], child_id)
            self.assertEqual(self._retrieved_shots(retrieval), ["3", "2"])
            self.assertEqual(self._saved_business(), after_adoption)


if __name__ == "__main__":
    unittest.main()
