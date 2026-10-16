"""Reassess the original goal after a confirmed handoff omitted later actions."""
from copy import deepcopy
import json
import unittest

from tests import test_mvp_v2_creative_workbench as workbench_fixture


class _HandoffReassessmentProvider(workbench_fixture._WorkbenchProvider):
    def __init__(self):
        super().__init__(mixed=False, stage="creative")

    def advisor(self, prompt, model, tokens):
        first = not self.advisor_inputs
        self.actions = ([
            {"kind": "revise_brief", "target_scope": "selected", "fields": [],
             "instruction": "将需求风格改为克制真实，先在需求页确认。"},
        ] if first else [
            {"kind": "revise_text", "target_scope": "numbers", "target_numbers": [2],
             "fields": ["sound"], "instruction": "只将第二段声音改得克制自然，保留画面和其他段落。"},
        ])
        response = json.loads(super().advisor(prompt, model, tokens))
        response["content"] = ("先确认新的需求风格，确认后继续调整第二段声音。" if first
                               else "需求已经确认，接着准备第二段声音修改，保留其他内容。")
        return json.dumps(response, ensure_ascii=False)


class DirectorHandoffReassessmentTests(unittest.TestCase):
    def setUp(self):
        self.base = workbench_fixture.CreativeWorkbenchTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.agent, self.token, self.scope = self.base.agent, self.base.token, self.base.scope
        self.session_id = self.base.session["agent_session_id"]
        self.provider = _HandoffReassessmentProvider()
        self.provider.install(self.agent)

    def test_confirmed_brief_reassesses_omitted_sound_action_then_adopts_and_rereads(self):
        instruction = "先把需求风格改成克制真实，确认后只改第二段声音，保留所有画面和其他段落。"
        before = deepcopy(self.base.business())
        started = self.agent.start_workbench(
            self.token, self.session_id, content=instruction,
            context=self.base.context(), operation_key="director-handoff-reassess-omitted-action")
        parent_id = started["task"]["task_id"]
        handoff = self.agent.execute_workbench(self.token, parent_id)
        self.assertEqual(handoff["task"]["status"], "waiting_user", handoff["response"])
        self.assertEqual(handoff["response"]["type"], "workbench_handoff")
        self.assertEqual(handoff["response"]["pending_actions"], [])
        self.assertEqual(len(self.provider.advisor_inputs), 1)
        self.assertEqual(self.provider.editor_inputs, [])
        self.assertEqual(self.base.business(), before)
        self.assertEqual(self.agent.resume_workbench(self.token, parent_id)["task"]["task_id"], parent_id)
        self.assertEqual(len(self.provider.advisor_inputs), 1)

        adapter = self.agent.script_revision.brief_adapter
        old_brief = adapter.service.read_current_formal(self.token, **self.scope)
        project = self.agent.services.projects.get_project(self.token, self.scope["project_id"])
        new_style = "克制真实，突出人与现场的自然交流"
        updated = adapter.update_field(
            self.token, **self.scope, brief_version_id=old_brief["brief_version_id"],
            field_label="风格调性", value=new_style,
            expected_project_version=project["project_version"])
        new_version_id = updated["brief_key"]
        self.assertNotEqual(new_version_id, old_brief["brief_version_id"])
        project = self.agent.services.projects.get_project(self.token, self.scope["project_id"])
        confirmed = adapter.confirm(
            self.token, **self.scope, brief_version_id=new_version_id,
            confirmed_at="2026-10-12T00:00:00Z", expected_project_version=project["project_version"])
        self.assertEqual(confirmed["brief_key"], new_version_id)
        receipt = self.agent.record_workbench_handoff(
            self.token, parent_id, project_id=self.scope["project_id"], kind="brief",
            result={"brief_version_id": new_version_id})
        self.assertEqual(receipt["receipts"]["0"]["brief_version_id"], new_version_id)

        fresh = self.base.new_agent()
        self.provider.install(fresh)
        self.assertEqual(fresh.read_workbench(self.token, parent_id)["response"]["handoff_receipts"],
                         receipt["receipts"])
        continued = fresh.resume_workbench(self.token, parent_id)
        continuation_id = continued["task"]["task_id"]
        self.assertNotEqual(continuation_id, parent_id,
                            "A confirmed handoff must reassess the original goal before declaring it complete")
        preview = fresh.execute_workbench(self.token, continuation_id)
        self.assertEqual(preview["response"]["type"], "workbench_proposal", preview["response"])
        self.assertEqual(preview["task"]["status"], "waiting_user")
        self.assertEqual(len(self.provider.advisor_inputs), 2)
        context = self.provider.advisor_inputs[1]
        self.assertEqual([action["kind"] for action in context["continuation"]["completed_actions"]],
                         ["revise_brief"])
        self.assertEqual(context["continuation"]["handoff_receipts"], receipt["receipts"])
        self.assertEqual(context["latest_project_brief"]["brief_version_id"], new_version_id)
        self.assertEqual(context["latest_project_brief"]["confirmation_status"], "confirmed")
        self.assertIn(new_style, json.dumps(context["latest_project_brief"], ensure_ascii=False))
        self.assertEqual([group["group_id"] for group in preview["response"]["groups"]], ["block_2"])
        self.assertEqual({target["shot_id"] for target in self.provider.editor_inputs[-1]["targets"]},
                         {"block_2"})
        self.assertEqual({target["field"] for target in self.provider.editor_inputs[-1]["targets"]}, {"sound"})
        self.assertEqual(self.base.business(), before, "Confirmation of a Brief does not itself edit the draft")

        applied = fresh.confirm_workbench(
            self.token, continuation_id, proposal_digest=preview["response"]["proposal_digest"],
            retained_group_ids=["block_2"])
        self.assertEqual(applied["response"]["type"], "workbench_applied")
        after = self.base.business()
        self.assertNotEqual(after["shots"][1]["fields"]["声音"], before["shots"][1]["fields"]["声音"])
        expected = deepcopy(before)
        expected["shots"][1]["fields"]["声音"] = after["shots"][1]["fields"]["声音"]
        self.assertEqual(after, expected, "Only the authorized second-block sound may change")

        restarted = self.base.new_agent()
        self.assertEqual(restarted.read_workbench(self.token, continuation_id)["response"],
                         {**applied["response"], "continuation_of_task_id": parent_id})
        self.assertEqual(restarted.resume_workbench(self.token, parent_id)["task"]["task_id"], continuation_id)
        self.assertEqual(len(self.provider.advisor_inputs), 2)
        self.assertEqual(self.base.business(), after)


if __name__ == "__main__":
    unittest.main()
