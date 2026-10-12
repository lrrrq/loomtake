import json
import unittest

from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.task_service import TaskService
from tests import test_mvp_v2_formal_script_workspace as script_fixture


class RevisionUsabilityTests(unittest.TestCase):
    def setUp(self):
        self.base = script_fixture.FormalScriptWorkspaceTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)

    def _propose(self, invoke, instruction):
        base = self.base
        project_id = base.generated.project["project_id"]
        tasks = TaskService(base.store)
        service = FormalScriptRevisionService(
            projects=base.projects, tasks=tasks, workspace=base.workspace, writes_enabled=True,
            assistant_editor_policy=FormalScriptAssistantEditorPolicy(invoke, "anonymous", "anonymous-editor"),
        )
        current = base.workspace.read_current(base.generated.token, workspace_id=base.generated.workspace_id, project_id=project_id)
        proposal = service.propose(
            base.generated.token, agent_session_id="", project_id=project_id,
            instruction=instruction, expected_project_version=current["project_version"],
            expected_revision=current["revision"], idempotency_key="anonymous-edit",
            draft_only=True, selected_shot_ids=[current["shots"][0]["shot_id"]],
        )
        return service, tasks, current, proposal

    def test_visual_expression_reaches_single_draft_with_frozen_context_and_source(self):
        prompts = []
        def editor(prompt, model, budget):
            data = json.loads(prompt.split("\nDATA:", 1)[1])
            prompts.append(data)
            visual = next(item for item in data["targets"] if item["field"] == "visual")
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1", "operations": [{
                "shot_id": visual["shot_id"], "field": "visual", "value": "拍摄建议：用一个清楚的现场动作带出开场，减少重复描述。",
            }]}, ensure_ascii=False)
        service, tasks, before, proposal = self._propose(editor, "节奏更紧凑")
        self.assertEqual(proposal["response"]["type"], "action_proposal")
        self.assertGreater(len(prompts[0]["context"]["neighbor_shots"]), 2)
        self.assertIn("项目名称", prompts[0]["context"]["brief"])
        self.assertIn("creative_standard", prompts[0]["context"])
        self.assertIn("technical_candidates", prompts[0]["context"])
        self.assertEqual(proposal["response"]["affected_fields"], ["画面描述"])
        parameters = next(event["details"]["internal_formal_parameters"] for event in reversed(
            tasks.list_task_events(self.base.generated.token, proposal["task_id"])
        ) if event["details"].get("internal_formal_parameters"))
        self.assertFalse(any(item["operation"] == "source_rebind" for item in parameters["operations"]))
        result = service.execute(
            self.base.generated.token,
            project=self.base.projects.get_project(self.base.generated.token, self.base.generated.project["project_id"]),
            parameters=parameters,
        )
        after = self.base.workspace.read_current(
            self.base.generated.token, workspace_id=self.base.generated.workspace_id,
            project_id=self.base.generated.project["project_id"],
        )
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["shots"][0]["source"], before["shots"][0]["source"])
        self.assertEqual(after["shots"][1:], before["shots"][1:])
        self.assertTrue(result)

    def test_no_changes_finishes_without_confirmation_or_draft_write(self):
        def editor(prompt, model, budget):
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1", "operations": []})
        _, _, before, proposal = self._propose(editor, "节奏更紧凑")
        self.assertEqual(proposal["status"], "completed")
        self.assertEqual(proposal["response"]["type"], "no_changes")
        self.assertFalse(proposal["response"]["confirmation_required"])
        after = self.base.workspace.read_current(
            self.base.generated.token, workspace_id=self.base.generated.workspace_id,
            project_id=self.base.generated.project["project_id"],
        )
        self.assertEqual(after["revision"], before["revision"])
        self.assertFalse(after["can_save_version"])

    def test_remove_duplicate_caption_uses_existing_optional_clear_operation(self):
        base = self.base
        project_id = base.generated.project["project_id"]
        for index in range(2):
            current = base.workspace.read_current(base.generated.token, workspace_id=base.generated.workspace_id, project_id=project_id)
            shot = current["shots"][index]
            base.workspace.apply_table_change(
                base.generated.token, workspace_id=base.generated.workspace_id, project_id=project_id,
                expected_project_version=current["project_version"], expected_revision=current["revision"],
                shot_no=shot["shot_no"], shot_id=shot["shot_id"], field="subtitle", value="匿名现场活动",
                idempotency_key=f"duplicate-subtitle-{index}",
            )
        def editor(prompt, model, budget):
            data = json.loads(prompt.split("\nDATA:", 1)[1])
            target = next(item for item in data["targets"] if item["field"] == "subtitle")
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1", "operations": [{
                "shot_id": target["shot_id"], "field": "subtitle", "value": "",
            }]})
        service, tasks, before, proposal = self._propose(editor, "减少重复")
        self.assertEqual(proposal["response"]["type"], "action_proposal")
        parameters = next(event["details"]["internal_formal_parameters"] for event in reversed(
            tasks.list_task_events(base.generated.token, proposal["task_id"])
        ) if event["details"].get("internal_formal_parameters"))
        self.assertEqual(parameters["operations"][0]["operation"], "clear_optional_field")
        service.execute(base.generated.token, project=base.projects.get_project(base.generated.token, project_id), parameters=parameters)
        after = base.workspace.read_current(base.generated.token, workspace_id=base.generated.workspace_id, project_id=project_id)
        self.assertEqual(after["revision"], before["revision"] + 1)
        self.assertEqual(after["shots"][0]["subtitle"], "")
        self.assertEqual(after["shots"][1:], before["shots"][1:])
