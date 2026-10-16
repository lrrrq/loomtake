"""Cancellation and continuation preserve real draft authority and receipts."""
from copy import deepcopy
import unittest

from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class _ContinuationProvider(workbench_fixture._WorkbenchProvider):
    def __init__(self):
        super().__init__(mixed=False, stage="creative")
        self.on_advisor = None

    def advisor(self, prompt, model, tokens):
        callback, self.on_advisor = self.on_advisor, None
        if callback:
            callback()
        return super().advisor(prompt, model, tokens)


class DirectorContinuationTests(unittest.TestCase):
    def setUp(self):
        self.base = workbench_fixture.CreativeWorkbenchTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.agent, self.token = self.base.agent, self.base.token
        self.tasks, self.scope = self.agent.services.tasks, self.base.scope
        self.session_id = self.base.session["agent_session_id"]
        self.provider = _ContinuationProvider()
        self.provider.install(self.agent)
        self.instruction = "修改所选两段声音，让现场交流自然衔接音乐，保留其他段落和全部画面。"

    def _start(self, key):
        return self.agent.start_workbench(
            self.token, self.session_id, content=self.instruction,
            context=self.base.context(), operation_key=key,
        )

    def _propose(self, key):
        started = self._start(key)
        result = self.agent.execute_workbench(self.token, started["task"]["task_id"])
        self.assertEqual(result["task"]["status"], "waiting_user", result["response"])
        self.assertEqual(result["response"]["type"], "workbench_proposal")
        self.assertEqual({group["group_id"] for group in result["response"]["groups"]}, {"block_1", "block_2"})
        return result

    def _confirm(self, result, retained):
        return self.agent.confirm_workbench(
            self.token, result["task"]["task_id"],
            proposal_digest=result["response"]["proposal_digest"], retained_group_ids=retained,
        )

    def _task_ids(self):
        return {item["task_id"] for item in self.tasks.list_agent_session_tasks(self.token, self.session_id)}

    def test_natural_stop_preserves_original_utterance_and_is_idempotent_without_model_call(self):
        pending = self._start('director-stop-pending')
        result = self.agent.start_workbench(self.token, self.session_id, content='先停一下。',
            context=None, operation_key='director-natural-stop')
        self.assertEqual(result['task']['status'], 'completed')
        self.assertEqual(result['response']['stopped_tasks'], [
            {'task_id': pending['task']['task_id'], 'status': 'cancelled'}])
        self.assertEqual(self.base.business(), self.base.initial)
        replay = self.agent.start_workbench(self.token, self.session_id, content='先停一下。',
            context=None, operation_key='director-natural-stop')
        self.assertEqual(replay['task']['task_id'], result['task']['task_id'])
        self.assertEqual(self.agent.execute_workbench(self.token, pending['task']['task_id'])['task']['status'], 'cancelled')
        history = self.agent.get_session(self.token, self.session_id)['messages']
        self.assertIn('先停一下。', [item['content'] for item in history if item['role'] == 'user'])

    def _workbench(self, task_id):
        events = self.tasks.list_task_events(self.token, task_id)
        return next(event["details"]["workbench"] for event in reversed(events) if event["details"].get("workbench"))

    def _mixed_handoff(self, key):
        self.provider.actions = [
            {"kind": "revise_text", "target_scope": "numbers", "target_numbers": [1],
             "fields": ["sound"], "instruction": "先把第一段现场声音衔接顺畅"},
            {"kind": "revise_brief", "target_scope": "selected", "fields": [],
             "instruction": "核对并确认需求中的风格调性"},
            {"kind": "revise_text", "target_scope": "numbers", "target_numbers": [2],
             "fields": ["visual"], "instruction": "按确认后的调性修改第二段画面", "depends_on": [1]},
        ]
        started = self._start(key)
        return self.agent.execute_workbench(self.token, started["task"]["task_id"])

    def test_mixed_handoff_previews_independent_work_and_keeps_dependent_action_after_adoption(self):
        proposal = self._mixed_handoff("director-mixed-handoff-independent")
        task_id = proposal["task"]["task_id"]
        self.assertEqual(proposal["response"]["type"], "workbench_proposal", proposal["response"])
        self.assertEqual([group["group_id"] for group in proposal["response"]["groups"]], ["block_1"])
        self.assertEqual({target["shot_id"] for target in self.provider.editor_inputs[-1]["targets"]}, {"block_1"})
        self.assertEqual(proposal["response"]["handoff"]["kind"], "brief")
        waiting = proposal["response"]["pending_actions"]
        self.assertEqual([(item["action_index"], item["depends_on"], item["target_ids"])
                          for item in waiting], [(2, [1], ["block_2"])])
        self.assertEqual(self.base.business(), self.base.initial)

        adopted = self._confirm(proposal, ["block_1"])
        self.assertEqual(adopted["response"]["pending_actions"], waiting)
        after = self.base.business()
        self.assertNotEqual(after["shots"][0]["fields"]["声音"], self.base.initial["shots"][0]["fields"]["声音"])
        self.assertEqual(after["shots"][1:], self.base.initial["shots"][1:])
        self.assertEqual(after["refs"], self.base.initial["refs"])
        fresh = self.base.new_agent()
        self.provider.install(fresh)
        task_ids = self._task_ids()
        blocked = fresh.resume_workbench(self.token, task_id)
        self.assertEqual(blocked["task"]["task_id"], task_id)
        self.assertEqual(blocked["response"]["pending_actions"], waiting)
        self.assertEqual(self._task_ids(), task_ids)
        self.assertEqual(len(self.provider.advisor_inputs), 1)
        self.assertEqual(len(self.provider.editor_inputs), 1)
        self.assertEqual(self.base.business(), after)

    def test_brief_handoff_requires_new_confirmed_version_then_resumes_remaining_action(self):
        proposal = self._mixed_handoff("director-brief-handoff-receipt")
        task_id = proposal["task"]["task_id"]
        self._confirm(proposal, ["block_1"])
        adopted = self.base.business()
        adapter = self.agent.script_revision.brief_adapter
        original_brief = adapter.service.read_current_formal(self.token, **self.scope)
        old_version_id = original_brief["brief_version_id"]
        before_invalid = self.base.database()
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self.agent.record_workbench_handoff(self.token, task_id,
                project_id=self.scope["project_id"], kind="brief", result={"brief_version_id": old_version_id})
        self.assertEqual(self.base.database(), before_invalid)
        self.assertNotIn("handoff_receipts", self._workbench(task_id))
        self.assertEqual(self.agent.resume_workbench(self.token, task_id)["task"]["task_id"], task_id)

        project = self.agent.services.projects.get_project(self.token, self.scope["project_id"])
        updated = adapter.update_field(self.token, **self.scope, brief_version_id=old_version_id,
            field_label="风格调性", value="克制真实，突出人与现场的自然交流",
            expected_project_version=project["project_version"])
        new_version_id = updated["brief_key"]
        self.assertNotEqual(new_version_id, old_version_id)
        with self.assertRaisesRegex(WorkspaceServiceError, "DRAFT_REVISION_CONFLICT"):
            self.agent.record_workbench_handoff(self.token, task_id,
                project_id=self.scope["project_id"], kind="brief", result={"brief_version_id": new_version_id})
        self.assertEqual(self.agent.resume_workbench(self.token, task_id)["task"]["task_id"], task_id)
        project = self.agent.services.projects.get_project(self.token, self.scope["project_id"])
        confirmed = adapter.confirm(self.token, **self.scope, brief_version_id=new_version_id,
            confirmed_at="2026-10-12T00:00:00Z", expected_project_version=project["project_version"])
        self.assertEqual(confirmed["brief_key"], new_version_id)
        receipt = self.agent.record_workbench_handoff(self.token, task_id,
            project_id=self.scope["project_id"], kind="brief", result={"brief_version_id": new_version_id})
        self.assertEqual(receipt["receipts"]["1"]["brief_version_id"], new_version_id)
        fresh = self.base.new_agent()
        self.provider.install(fresh)
        persisted = fresh.read_workbench(self.token, task_id)
        self.assertEqual(persisted["response"]["handoff_receipts"], receipt["receipts"])
        self.assertTrue(persisted["response"]["handoff"]["completed"])
        continued = fresh.resume_workbench(self.token, task_id)
        continuation_id = continued["task"]["task_id"]
        self.assertNotEqual(continuation_id, task_id)
        preview = fresh.execute_workbench(self.token, continuation_id)
        self.assertEqual(preview["response"]["type"], "workbench_proposal", preview["response"])
        self.assertEqual([group["group_id"] for group in preview["response"]["groups"]], ["block_2"])
        self.assertEqual({target["shot_id"] for target in self.provider.editor_inputs[-1]["targets"]}, {"block_2"})
        self.assertEqual(self.base.business(), adopted)
        fresh.confirm_workbench(self.token, continuation_id,
            proposal_digest=preview["response"]["proposal_digest"], retained_group_ids=["block_2"])
        final = self.base.business()
        self.assertEqual(final["shots"][0], adopted["shots"][0])
        self.assertNotEqual(final["shots"][1]["fields"]["画面描述"], adopted["shots"][1]["fields"]["画面描述"])
        self.assertEqual(final["shots"][2], adopted["shots"][2])
        self.assertEqual(final["refs"], adopted["refs"])

    def test_cancel_waiting_proposal_is_idempotent_keeps_history_and_cannot_apply(self):
        proposal = self._propose("director-cancel-waiting")
        task_id = proposal["task"]["task_id"]
        before = self.base.business()
        original_events = self.tasks.list_task_events(self.token, task_id)
        cancelled = self.agent.cancel_workbench(self.token, task_id)
        self.assertEqual(cancelled["task"]["status"], "cancelled")
        self.assertEqual(self.base.business(), before)
        events = self.tasks.list_task_events(self.token, task_id)
        self.assertEqual(events[:len(original_events)], original_events)
        self.assertGreater(len(events), len(original_events))
        self.assertEqual(self._workbench(task_id)["request"]["content"], self.instruction)
        fresh = self.base.new_agent()
        readback = fresh.read_workbench(self.token, task_id)
        self.assertEqual(readback["task"]["status"], "cancelled")
        self.assertEqual(readback["response"], cancelled["response"])
        after_cancel = self.base.database()
        self.assertEqual(fresh.cancel_workbench(self.token, task_id), readback)
        self.assertEqual(self.base.database(), after_cancel)
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_TASK_TRANSITION"):
            self._confirm(proposal, ["block_1", "block_2"])
        self.assertEqual(self.base.business(), before)
        self.assertEqual(self.base.database(), after_cancel)

    def test_resume_active_task_or_current_proposal_returns_original_without_duplicate_work(self):
        started = self._start("director-resume-active")
        task_id = started["task"]["task_id"]
        tasks_before = self._task_ids()
        resumed = self.agent.resume_workbench(self.token, task_id)
        self.assertEqual(resumed["task"]["task_id"], task_id)
        self.assertEqual(resumed["task"]["status"], "understanding")
        self.assertEqual(self._task_ids(), tasks_before)
        self.assertEqual(self.provider.advisor_inputs, [])
        self.assertEqual(self.provider.editor_inputs, [])
        active_responses = []
        self.provider.on_advisor = lambda: active_responses.append(self.agent.resume_workbench(self.token, task_id))
        proposal = self.agent.execute_workbench(self.token, task_id)
        self.assertEqual(proposal["response"]["type"], "workbench_proposal", proposal["response"])
        self.assertEqual(len(active_responses), 1)
        self.assertEqual(active_responses[0]["task"]["task_id"], task_id)
        self.assertEqual(active_responses[0]["task"]["status"], "executing")
        self.assertEqual(len(self.provider.advisor_inputs), 1)
        self.assertEqual(len(self.provider.editor_inputs), 1)
        tasks_after_preview = self._task_ids()
        original = self.agent.read_workbench(self.token, task_id)
        database = self.base.database()
        reread = self.base.new_agent().resume_workbench(self.token, task_id)
        self.assertEqual(reread, original)
        self.assertEqual(self._task_ids(), tasks_after_preview)
        self.assertEqual(self.base.database(), database)
        self.assertEqual(self.base.business(), self.base.initial)

    def test_partial_receipt_resumes_only_remaining_group_once_and_keeps_adopted_work(self):
        proposal = self._propose("director-partial-continuation")
        original_task_id = proposal["task"]["task_id"]
        applied = self._confirm(proposal, ["block_1"])
        response = applied["response"]
        self.assertEqual(response["applied_group_ids"], ["block_1"])
        self.assertEqual(response["remaining_group_ids"], ["block_2"])
        self.assertEqual([item["group_id"] for item in response["remaining_groups"]], ["block_2"])
        partially_applied = self.base.business()
        self.assertNotEqual(partially_applied["shots"][0]["fields"]["声音"], self.base.initial["shots"][0]["fields"]["声音"])
        self.assertEqual(partially_applied["shots"][1:], self.base.initial["shots"][1:])
        self.assertEqual(partially_applied["refs"], self.base.initial["refs"])

        fresh = self.base.new_agent()
        self.provider.install(fresh)
        self.assertEqual(fresh.read_workbench(self.token, original_task_id)["response"], response)
        continued = fresh.resume_workbench(self.token, original_task_id)
        continuation_id = continued["task"]["task_id"]
        self.assertNotEqual(continuation_id, original_task_id)
        self.assertEqual(continued["task"]["agent_session_id"], self.session_id)
        self.assertEqual(continued["task"]["status"], "understanding")
        self.assertEqual(self._workbench(continuation_id)["continuation_of_task_id"], original_task_id)
        tasks_after_resume = self._task_ids()
        replayed = fresh.resume_workbench(self.token, original_task_id)
        self.assertEqual(replayed["task"]["task_id"], continuation_id)
        self.assertEqual(self._task_ids(), tasks_after_resume)
        self.assertEqual(self.base.business(), partially_applied)
        self.assertEqual(len(self.provider.advisor_inputs), 1)
        self.assertEqual(len(self.provider.editor_inputs), 1)

        preview = fresh.execute_workbench(self.token, continuation_id)
        self.assertEqual(preview["response"]["type"], "workbench_proposal", preview["response"])
        self.assertEqual([group["group_id"] for group in preview["response"]["groups"]], ["block_2"])
        self.assertEqual({item["shot_id"] for item in self.provider.editor_inputs[-1]["targets"]}, {"block_2"})
        self.assertEqual(fresh.resume_workbench(self.token, original_task_id)["task"]["task_id"], continuation_id)
        self.assertEqual(self.base.business(), partially_applied)
        before_second_adoption = deepcopy(partially_applied["shots"][0])
        final = fresh.confirm_workbench(
            self.token, continuation_id, proposal_digest=preview["response"]["proposal_digest"], retained_group_ids=["block_2"],
        )
        self.assertEqual(final["response"]["applied_group_ids"], ["block_2"])
        self.assertEqual(final["response"]["remaining_group_ids"], [])
        self.assertEqual(final["response"]["remaining_groups"], [])
        actual = self.base.business()
        self.assertEqual(actual["shots"][0], before_second_adoption)
        self.assertNotEqual(actual["shots"][1]["fields"]["声音"], self.base.initial["shots"][1]["fields"]["声音"])
        self.assertEqual(actual["shots"][2], self.base.initial["shots"][2])
        self.assertEqual(actual["refs"], self.base.initial["refs"])

    def test_external_edit_requires_fresh_continuation_and_new_proposal_uses_current_before_values(self):
        proposal = self._propose("director-external-edit-continuation")
        original_task_id = proposal["task"]["task_id"]
        self.base.manual("block_1", "声音", "用户后来写入的当前环境声", "director-user-later-edit")
        edited = self.base.business()
        self.assertTrue(self.agent.read_workbench(self.token, original_task_id)["response"]["stale"])
        with self.assertRaisesRegex(WorkspaceServiceError, "DRAFT_REVISION_CONFLICT"):
            self._confirm(proposal, ["block_1", "block_2"])
        self.assertEqual(self.base.business(), edited)

        continued = self.agent.resume_workbench(self.token, original_task_id)
        continuation_id = continued["task"]["task_id"]
        self.assertNotEqual(continuation_id, original_task_id)
        workbench = self._workbench(continuation_id)
        self.assertEqual(workbench["continuation_of_task_id"], original_task_id)
        first_target = next(item for item in workbench["before"]["targets"] if item["id"] == "block_1")
        self.assertEqual(first_target["fields"]["声音"], "用户后来写入的当前环境声")
        preview = self.agent.execute_workbench(self.token, continuation_id)
        self.assertEqual(preview["response"]["type"], "workbench_proposal", preview["response"])
        first_group = next(item for item in preview["response"]["groups"] if item["group_id"] == "block_1")
        sound_change = next(item for item in first_group["changes"] if item["field"] == "声音")
        self.assertEqual(sound_change["before"], "用户后来写入的当前环境声")
        self.assertEqual(self.base.business(), edited)
        tasks_after_preview = self._task_ids()
        replay = self.base.new_agent().resume_workbench(self.token, original_task_id)
        self.assertEqual(replay["task"]["task_id"], continuation_id)
        self.assertEqual(self._task_ids(), tasks_after_preview)
        self.assertEqual(self.base.business(), edited)


if __name__ == "__main__":
    unittest.main()
