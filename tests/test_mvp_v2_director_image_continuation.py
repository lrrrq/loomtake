"""Image handoff and continuation through the real director and image services.

Only model/image responses are deterministic fixtures. Task relationships,
drafts, image identities, adoption receipts and history use their authorities.
"""
from copy import deepcopy
import unittest

from mvp_v2 import director_history
from mvp_v2.image_providers import ImageProviderError, ImageProviderResult
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_workbench as workbench_fixture
from tests.test_mvp_v2_image_creation import photo


class DirectorImageContinuationTests(unittest.TestCase):
    def setUp(self):
        self.base = workbench_fixture.CreativeWorkbenchTests("runTest")
        self.addCleanup(self.base.doCleanups)
        self.base.setUp()
        self.agent, self.service = self.base.agent, self.base.generation
        self.token, self.scope = self.base.token, self.base.scope
        self.session_id = self.base.session["agent_session_id"]
        self.calls = []

        def provider(prompt, reference_images=(), **_kwargs):
            self.calls.append((prompt, reference_images))
            content = photo("orange" if len(self.calls) == 1 else "blue")
            return ImageProviderResult(content, "image/png", 2560, 1440,
                "anonymous-director-image", None, "2560x1440", "anonymous-receipt", {})

        self.service.image_provider = provider

    @staticmethod
    def _action(kind, numbers, dependencies=()):
        return {"kind": kind, "target_scope": "numbers", "target_numbers": list(numbers),
            "fields": ["sound"] if kind == "revise_text" else [],
            "instruction": {"create_image": "生成匿名活动空间参考图，保留现场事实边界",
                "revise_text": "让声音自然衔接", "find_images": "按当前画面查找配图"}[kind],
            "depends_on": list(dependencies)}

    def _turn(self, actions, key):
        self.base.provider.actions = deepcopy(actions)
        started = self.agent.start_workbench(self.token, self.session_id,
            content="先为指定段落生成并采用参考图，完成后再处理明确依赖它的声音与配图。",
            context=None, operation_key=key)
        return self.agent.execute_workbench(self.token, started["task"]["task_id"])

    def _start(self, request, key):
        current = self.service.read_creative_draft(self.token, **self.scope)
        return self.service.start_image_creation(self.token, **self.scope,
            expected_project_version=current["project_version"],
            expected_creative_revision=current["revision"],
            target_kind=request["target_kind"], target_id=request["target_id"],
            expected_target_basis=request["target_basis"], prompt=request["instruction"],
            operation_key=key, workbench_task_id=request["task_id"],
            workbench_action_index=request["action_index"], start_background=False)

    def _generate(self, request, key):
        item = self._start(request, key)
        self.service.execute_image_creation(self.token, **self.scope, task_id=item["task_id"])
        return self.service.read_image_creation(self.token, **self.scope, task_id=item["task_id"])

    def _adopt(self, item, key):
        current = self.service.read_creative_draft(self.token, **self.scope)
        return self.service.adopt_image_creation(self.token, **self.scope,
            task_id=item["task_id"], target_kind=item["target"]["kind"], target_id=item["target"]["id"],
            expected_project_version=current["project_version"],
            expected_creative_revision=current["revision"], operation_key=key)

    def _read(self, task_id):
        return self.base.new_agent().read_workbench(self.token, task_id)

    def _assert_blocked(self, task_id):
        current = self._read(task_id)
        self.assertFalse(current["response"]["can_resume"], current)
        resumed = self.base.new_agent().resume_workbench(self.token, task_id)
        self.assertEqual(resumed["task"]["task_id"], task_id)
        self.assertFalse(resumed["response"]["can_resume"])
        self.assertEqual(self.base.provider.editor_inputs, [])
        return current

    def _history(self, task_id):
        history = self.base.new_agent().get_session(self.token, self.session_id)["messages"]
        replies = [item for item in history if item["task_id"] == task_id and item["role"] == "assistant"]
        self.assertEqual(len(replies), 1)
        return history, replies[0]

    def test_all_targets_must_be_adopted_before_dependent_text_and_images_resume_once(self):
        first = self._turn([self._action("create_image", [1, 2]),
            self._action("revise_text", [3], [0]), self._action("find_images", [3], [0, 1])],
            "director-two-image-continuation")
        parent_id = first["task"]["task_id"]
        requests = first["response"]["image_creation_requests"]
        self.assertEqual({item["target_id"] for item in requests}, {"block_1", "block_2"})
        self.assertEqual(self.base.business(), self.base.initial)
        first_image = self._generate(requests[0], "director-generated-first")
        self.assertEqual(first_image["adoption_receipts"], [])
        self._assert_blocked(parent_id)
        self._adopt(first_image, "director-adopt-first")
        self._assert_blocked(parent_id)
        second_image = self._generate(requests[1], "director-generated-second")
        self._assert_blocked(parent_id)
        self._adopt(second_image, "director-adopt-second")
        ready = self._read(parent_id)
        self.assertTrue(ready["response"]["can_resume"])
        receipts = ready["response"]["image_creation_results"]
        self.assertEqual({item["task_id"] for item in receipts}, {first_image["task_id"], second_image["task_id"]})
        self.assertTrue(all(item["adoption_receipts"][-1]["currently_applied"] for item in receipts))
        self.assertEqual(self.base.business()["shots"], self.base.initial["shots"])
        adopted_refs = deepcopy([r for r in self.base.business()["refs"] if r["block_id"] != "block_3"])
        self.assertEqual(len(adopted_refs), 2)
        history, reply = self._history(parent_id)
        self.assertEqual(reply["response"]["image_creation_results"], receipts)
        self.assertIn("2 张当前用于目标", reply["operation_receipt"])
        self.assertFalse({first_image["task_id"], second_image["task_id"]} & {m["task_id"] for m in history})
        self.assertEqual(len(director_history._model_message(reply)["image_creation_results"]), 2)
        resumed = self.base.new_agent().resume_workbench(self.token, parent_id)
        child_id = resumed["task"]["task_id"]
        self.assertNotEqual(child_id, parent_id)
        result = self.agent.execute_workbench(self.token, child_id)
        self.assertEqual(result["response"]["type"], "workbench_proposal", result)
        self.assertEqual({g["target_id"] for g in result["response"]["groups"]}, {"block_3"})
        self.assertEqual(len(self.base.provider.editor_inputs), 1)
        self.assertEqual(len(self.base.provider.advisor_inputs), 1)
        self.assertEqual(len(self.calls), 2)
        self.agent.confirm_workbench(self.token, child_id,
            proposal_digest=result["response"]["proposal_digest"], retained_group_ids=["block_3"])
        final = self.base.business()
        self.assertEqual(final["shots"][:2], self.base.initial["shots"][:2])
        self.assertEqual([r for r in final["refs"] if r["block_id"] != "block_3"], adopted_refs)
        self.assertNotEqual(final["shots"][2]["fields"]["声音"], self.base.initial["shots"][2]["fields"]["声音"])
        self.assertEqual(self.base.new_agent().resume_workbench(self.token, parent_id)["task"]["task_id"], child_id)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(item["adoption_receipts"][-1]["currently_applied"]
            for item in self._history(parent_id)[1]["response"]["image_creation_results"]))
        with self.base.fixture.store.connection() as connection:
            for table in ("project_versions", "version_review_human_decisions"):
                self.assertEqual(connection.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)

    def test_uncertain_result_is_visible_and_cannot_release_or_repeat_paid_work(self):
        def uncertain(*_args, **_kwargs):
            self.calls.append("uncertain")
            raise ImageProviderError("IMAGE_TRANSPORT_FAILED", "匿名响应结果未知", request_may_have_succeeded=True)

        self.service.image_provider = uncertain
        first = self._turn([self._action("create_image", [1]), self._action("revise_text", [2], [0])],
            "director-unknown-image")
        parent_id = first["task"]["task_id"]
        item = self._generate(first["response"]["image_creation_requests"][0], "director-unknown-generated")
        self.assertTrue(item["result_uncertain"])
        for _ in range(2):
            current = self._assert_blocked(parent_id)
            self.assertTrue(current["response"]["image_creation_results"][0]["result_uncertain"])
            self.service.execute_image_creation(self.token, **self.scope, task_id=item["task_id"])
        _, reply = self._history(parent_id)
        projected = director_history._model_message(reply)["image_creation_results"][0]
        self.assertEqual(projected["status"], "failed")
        self.assertIn("未知", projected["error_message"])
        self.assertEqual(self.calls, ["uncertain"])
        self.assertEqual(self.base.business(), self.base.initial)

    def test_changed_target_preserves_generated_result_without_false_adoption_or_resume(self):
        first = self._turn([self._action("create_image", [1]), self._action("revise_text", [2], [0])],
            "director-image-stale-target")
        parent_id = first["task"]["task_id"]
        item = self._generate(first["response"]["image_creation_requests"][0], "director-stale-generated")
        self.base.manual("block_1", "画面描述", "用户后来改为室内观众交流画面", "manual-after-image-generation")
        before = self.base.business()
        with self.assertRaises(WorkspaceServiceError) as caught:
            self._adopt(item, "director-stale-adoption")
        self.assertEqual(caught.exception.code, "DRAFT_REVISION_CONFLICT")
        current = self._assert_blocked(parent_id)
        result = current["response"]["image_creation_results"][0]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["adoption_receipts"], [])
        self.assertTrue(result["preview_url"])
        self.assertEqual(self.base.business(), before)
        self.assertEqual(len(self.calls), 1)

    def test_protected_creation_target_does_not_release_dependent_edit(self):
        state = self.base.repo.read()
        self.base.revision.set_creative_protection(self.token, **self.scope,
            expected_project_version=state["project_version"], expected_revision=state["revision"],
            protected_block_ids=["block_1"], operation_key="director-protect-image-target")
        before = self.base.business()
        first = self._turn([self._action("create_image", [1]), self._action("revise_text", [2], [0])],
            "director-image-protected-dependency")
        self.assertEqual(first["response"].get("image_creation_requests", []), [])
        continued = self.base.new_agent().resume_workbench(self.token, first["task"]["task_id"])
        if continued["task"]["status"] == "understanding":
            continued = self.agent.execute_workbench(self.token, continued["task"]["task_id"])
        self.assertEqual(self.base.provider.editor_inputs, [], continued)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.base.business(), before)


if __name__ == "__main__":
    unittest.main()
