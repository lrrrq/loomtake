"""Direct editors honor current conventions without rewriting frozen evidence.

Only editor responses are controlled. Convention writes, draft persistence and
the formal plan/version used below go through the existing authority services.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from mvp_v2.creative_revision import effective_brief_values
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.formal_script_revision import FormalRevisionService, FormalScriptRevisionService
from mvp_v2.run_contract import sha256_json
from mvp_v2.task_service import TaskService
from tests import test_mvp_v2_creative_formal_binding as creative_fixture
from tests import test_mvp_v2_formal_generation_service as generation_fixture
from tests import test_mvp_v2_formal_script_workspace as formal_fixture


class DirectRevisionConventionTests(unittest.TestCase):
    def _version(self, fixture):
        return fixture.knowledge.get_state(
            fixture.token, fixture.workspace_id,
            project_id=fixture.project["project_id"],
        )["knowledge_version"]

    def _seed_rules(self, fixture):
        knowledge, token = fixture.knowledge, fixture.token
        scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project["project_id"]}
        # Identical wording deliberately has different authority identities.
        # Retiring a project convention must not delete a company rule by text.
        shared_statement = "片尾保持自然环境声"
        candidate = knowledge.create_candidate(
            token, **scope, scope="company", target_id=fixture.workspace_id,
            statement=shared_statement, evidence_refs=["artifact://knowledge/anonymous-company"],
            expected_knowledge_version=self._version(fixture),
        )
        company = knowledge.approve_candidate(
            token, **scope, candidate_id=candidate["candidate_id"], level="company",
            review_evidence="管理员核对匿名来源后批准",
            expected_knowledge_version=self._version(fixture),
        )
        conventions = []
        for statement in ("开场保持声音克制", shared_statement, "结尾使用高昂弦乐"):
            conventions.append(knowledge.add_project_convention(
                token, **scope, statement=statement,
                evidence_refs=["artifact://project-conversation/anonymous/message-1"],
                expected_knowledge_version=self._version(fixture), confirmed=True,
            ))
        return {"company": company, "active": conventions[0],
                "disabled": conventions[1], "corrected_old": conventions[2]}

    def _retire_rules(self, fixture, rules):
        knowledge, token = fixture.knowledge, fixture.token
        scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project["project_id"]}
        knowledge.set_project_convention_status(
            token, **scope, convention_id=rules["disabled"]["convention_id"],
            status="disabled", reason="本项目不再沿用这条临时创作约定",
            expected_knowledge_version=self._version(fixture),
        )
        return knowledge.correct_project_convention(
            token, **scope, convention_id=rules["corrected_old"]["convention_id"],
            statement="结尾减少音乐，只保留人物脚步声",
            evidence_refs=["artifact://project-conversation/anonymous/message-2"],
            reason="项目成员更正结尾声音方向", confirmed=True,
            expected_knowledge_version=self._version(fixture),
        )

    @staticmethod
    def _editor_policy(calls):
        def editor(prompt, _model, _tokens):
            calls.append(json.loads(prompt.split("\nDATA:", 1)[1]))
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                               "operations": []})
        return FormalScriptAssistantEditorPolicy(editor, "anonymous", "anonymous")

    def _assert_current_context(self, context, fixture, rules, corrected, frozen):
        actual_rules = {rule["rule_id"]: rule for rule in context["effective_knowledge"]["rules"]}
        frozen_rules = {rule["rule_id"]: rule for rule in frozen["rules"]}
        retained = {rules["company"]["rule_id"], rules["active"]["rule_id"]}
        self.assertEqual(set(actual_rules), retained)
        for rule_id in retained:
            self.assertEqual(actual_rules[rule_id], frozen_rules[rule_id])
        self.assertEqual({rule["rule_id"] for rule in context["project_conventions"]},
                         {rules["active"]["rule_id"], corrected["rule_id"]})
        self.assertEqual(next(rule["statement"] for rule in context["project_conventions"]
                              if rule["rule_id"] == corrected["rule_id"]), corrected["statement"])
        self.assertEqual({rule["rule_id"] for rule in context["retired_conventions"]},
                         {rules["disabled"]["rule_id"], rules["corrected_old"]["rule_id"]})
        self.assertEqual(context["knowledge_version"], self._version(fixture))
        self.assertEqual(context["effective_knowledge"]["snapshot_sha256"], frozen["snapshot_sha256"])

    def test_creative_direct_revision_filters_retired_conventions_without_rewriting_basis(self):
        base = creative_fixture.CreativeFormalBindingTests("runTest")
        self.addCleanup(base.doCleanups)
        base.setUp()
        fixture, token, scope = base.fixture, base.fixture.token, base.scope
        rules = self._seed_rules(fixture)
        frozen = base.service._effective_knowledge(token, scope["project_id"])
        context = {"effective_knowledge": deepcopy(frozen)}
        basis = {"context": context, "snapshot_sha256": sha256_json(context)}
        current = base.repo.read()
        with base.repo.transaction(expected_project_version=current["project_version"],
                                   expected_revision=current["revision"]):
            base.repo.body["generation_basis"] = deepcopy(basis)
        before = base.repo.read()
        brief_before = fixture.briefs.read_current_formal(token, **scope)
        corrected = self._retire_rules(fixture, rules)
        calls = []
        revision = FormalRevisionService(
            projects=base.service.services.projects, tasks=base.service.services.tasks,
            workspace=None, writes_enabled=True,
            brief_adapter=FormalBriefInputAdapter(fixture.briefs, data_root=fixture.root / "runtime"),
            assistant_editor_policy=self._editor_policy(calls),
        )
        result = revision.propose_creative_draft_change(
            token, **scope, expected_project_version=before["project_version"],
            expected_revision=before["revision"], selected_block_ids=["block_1"],
            instruction="优化第1段声音，让环境声自然衔接音乐。",
            operation_key="direct-creative-current-conventions",
        )
        self.assertEqual(len(calls), 1)
        self._assert_current_context(calls[0]["context"], fixture, rules, corrected, frozen)
        self.assertEqual(calls[0]["context"]["brief"], effective_brief_values(brief_before))
        self.assertEqual(base.repo.read()["body"]["generation_basis"], basis)
        self.assertEqual(base.repo.read()["body"]["draft"], before["body"]["draft"])
        brief_after = fixture.briefs.read_current_formal(token, **scope)
        # This DTO includes projects.updated_at, which changes when the editor
        # starts a task. The Brief identity, status and complete content must not.
        self.assertEqual({key: value for key, value in brief_after.items() if key != "updated_at"},
                         {key: value for key, value in brief_before.items() if key != "updated_at"})
        self.assertEqual(revision.tasks.get_task(token, result["task_id"])["status"], "completed")

    def test_formal_direct_revision_filters_retired_conventions_without_rewriting_plan(self):
        original_setup = generation_fixture.FormalGenerationServiceTests.setUp

        def setup_with_conventions(fixture):
            original_setup(fixture)
            fixture.direct_revision_rules = self._seed_rules(fixture)

        base = formal_fixture.FormalScriptWorkspaceTests("runTest")
        self.addCleanup(base.doCleanups)
        # Seed before the fixture creates its real immutable plan and version.
        # The editor still reads assistant_context through its real authority.
        with patch.object(generation_fixture.FormalGenerationServiceTests,
                          "setUp", setup_with_conventions):
            base.setUp()
        fixture, token = base.generated, base.generated.token
        scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project["project_id"]}
        rules = fixture.direct_revision_rules
        frozen_context = base.workspace.assistant_context(token, **scope)
        frozen = deepcopy(frozen_context["effective_knowledge"])
        _, version_context = base.workspace._current_context(token, scope["workspace_id"], scope["project_id"])
        plan_path = base.workspace.services.artifacts.resolve_stage_artifact(
            version_context.run_id, "script_plan",
            expected_manifest_sha256=version_context.manifest.sha256,
        ).path
        plan_bytes = plan_path.read_bytes()
        brief_before = fixture.briefs.read_current_formal(token, **scope)
        before = base.workspace.read_current(token, **scope)
        corrected = self._retire_rules(fixture, rules)
        calls = []
        revision = FormalScriptRevisionService(
            projects=base.projects, tasks=TaskService(base.store), workspace=base.workspace,
            writes_enabled=True, assistant_editor_policy=self._editor_policy(calls),
        )
        result = revision.propose(
            token, agent_session_id="", project_id=scope["project_id"],
            instruction="优化第1镜声音，让环境声自然衔接音乐。",
            selected_shot_ids=[before["shots"][0]["shot_id"]],
            expected_project_version=before["project_version"], expected_revision=before["revision"],
            idempotency_key="direct-formal-current-conventions", draft_only=True, entrypoint="assistant",
        )
        self.assertEqual(len(calls), 1)
        self._assert_current_context(calls[0]["context"], fixture, rules, corrected, frozen)
        self.assertEqual(calls[0]["context"]["brief"], frozen_context["brief"])
        self.assertEqual(base.workspace.assistant_context(token, **scope), frozen_context)
        self.assertEqual(plan_path.read_bytes(), plan_bytes)
        self.assertEqual(base.workspace.read_current(token, **scope), before)
        self.assertEqual(fixture.briefs.read_current_formal(token, **scope), brief_before)
        self.assertEqual(result["response"]["type"], "no_changes")


if __name__ == "__main__":
    unittest.main()
