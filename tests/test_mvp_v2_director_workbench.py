"""Director discussion and editing over real persisted anonymous services.

Only model responses are controlled. Drafts, identities, conventions, task
events, proposals and partial adoption use their existing authority services.
"""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.director_conversation import project_context
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.package_publication import AssetPackagePublicationService
from mvp_v2.package_registry import LocalPackageRegistry
from mvp_v2.run_contract import sha256_json
from mvp_v2.template_editing_methods import EVIDENCE_SCHEMA_VERSION, read_method_source
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_workbench as workbench_fixture


class _DirectorProvider(workbench_fixture._WorkbenchProvider):
    def __init__(self, modes):
        super().__init__(mixed=False, stage="creative")
        self.modes = list(modes)
        self.choices = [
            {"label": "方向一 · 现场纪实", "instruction": "选择方向一，声音以现场交流为主", "description": "先建立真实现场，再用短音乐衔接。"},
            {"label": "方向二 · 克制递进", "instruction": "选择方向二，以低声音乐自然衔接现场交流与掌声", "description": "前两段逐步推进，原结尾的人工声音保持完整。"},
            {"label": "方向三 · 节奏对照", "instruction": "选择方向三，以环境声与短暂停顿形成对照", "description": "用停顿区分两段重点，原结尾保持完整。"},
        ]
        self.long_content = "三个方向都以已确认需求为依据；以下只是声音设计讨论，还没有修改稿件。\n" + "\n".join(
            f"讨论细节 {index}：方向一强调现场氛围，方向二让低声音乐接住交流并逐渐过渡到掌声，"
            "方向三用短暂停顿形成节奏差异。原结尾继续保留已有人工剪辑备注与环境声，未选段落不改。"
            for index in range(1, 24)
        )

    def advisor(self, prompt, _model, _tokens):
        context, message = prompt.split("\nCONTEXT:", 1)[1].rsplit("\nUSER:", 1)
        self.advisor_inputs.append(json.loads(context))
        index = len(self.advisor_inputs) - 1
        mode = self.modes[index]
        if mode == "discussion":
            return json.dumps({
                "intent": "discussion", "summary": "先比较三个声音方向，保留原结尾。",
                "content": self.long_content if index == 0 else "第二个方向能让声音逐步推进，原结尾保留；本轮仍只讨论。",
                "candidate_action": "none", "actions": [], "choices": self.choices,
                "constraints": {"preserve": ["原结尾与未选段落"], "prohibit": ["虚构已确认需求中的事实"], "unknown": []},
            }, ensure_ascii=False)
        return json.dumps({
            "intent": "workbench_revision", "summary": "按第二个方向修改所选两段的声音，结尾保留。",
            "content": "沿用前面讨论的第二个方向，为当前所选两段预览声音修改；原结尾和画面均保持。",
            "actions": [{"kind": "revise_text", "source_clause": message, "target_scope": "selected",
                "fields": ["sound"], "instruction": "按第二个方向让低声音乐自然衔接现场交流与掌声，保留原结尾", "depends_on": []}],
            "constraints": {"preserve": ["原结尾与未选段落", "全部原画面"], "prohibit": ["虚构已确认需求中的事实"], "unknown": []},
        }, ensure_ascii=False)


class DirectorWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.base = workbench_fixture.CreativeWorkbenchTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.agent = self.base.agent
        self.fixture, self.repo = self.base.fixture, self.base.repo
        self.token, self.scope = self.base.token, self.base.scope
        self.session_id = self.base.session["agent_session_id"]

    def _install(self, modes):
        provider = _DirectorProvider(modes)
        provider.install(self.agent)
        return provider

    def _turn(self, content, key, *, context=None, token=None, session_id=None):
        token, session_id = token or self.token, session_id or self.session_id
        started = self.agent.start_workbench(
            token, session_id, content=content, context=context, operation_key=key,
        )
        self.assertEqual(started["task"]["status"], "understanding")
        return self.agent.execute_workbench(token, started["task"]["task_id"])

    def _business_rows(self):
        with self.fixture.store.connection() as connection:
            return {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in ("projects", "creative_draft_heads", "project_versions", "project_brief_snapshots")
            }

    def _method_templates(self):
        """Create two immutable method revisions through the real package service."""
        registry = LocalPackageRegistry(self.fixture.root / "director-method-registry.json")
        packages = AssetPackagePublicationService(registry=registry,
            storage=LocalAssetStorage(self.fixture.root / "director-method-assets"),
            packages_root=self.fixture.root / "director-method-packages")
        workbook = next(self.fixture.source_root.glob("*.xlsx"))
        source = read_method_source(workbook)
        cells = next(sheet["cells"] for sheet in source["sheets"] if sheet["source_sheet"] == "模版")
        seed = packages.import_template(category_id="launch", filename=workbook.name,
            content=workbook.read_bytes(), display_name="匿名编导参考模板")
        records = []
        for label in ("甲", "乙"):
            evidence = {
                "schema_version": EVIDENCE_SCHEMA_VERSION, "category_id": "launch",
                "source_workbook": {"file_name": workbook.name, "sha256": source["workbook_sha256"],
                                    "source_content_sha256": source["source_content_sha256"]},
                "methods": [{
                    "method_id": "launch.anonymous_method_" + label,
                    "purpose": "匿名方法" + label + "用于检验讨论依据的版本绑定。",
                    "audience_change": "从观察现场转为理解相邻画面的关系。",
                    "applies_when": ["当前活动有对应的现场画面。"],
                    "material_conditions": ["两个匿名来源镜头可作为编排参考。"],
                    "guidance": ["方法" + label + "：根据当前项目事实安排信息变化。"],
                    "exceptions": ["案例事实不自动成为当前项目事实。"],
                    "duration_adaptation": {"short": "保留必要关系。", "long": "增加真实过程。",
                                            "boundary": "不规定固定镜数或秒数。"},
                    "review_questions": ["是否保留原事件与信息变化？"],
                    "sequence": [{"role": role, "source_sheet": "模版", "source_rows": [row],
                                  "information_change": "呈现该来源画面的观察重点。"}
                                 for row, role in ((7, "建立"), (8, "承接"))],
                    "evidence_refs": [{"source_sheet": "模版", "cell": f"I{row}", "quote": cells[f"I{row}"]}
                                      for row in (7, 8)],
                    "visual_evidence": [], "evidence_kind": "source_interpretation",
                    "case_fact_boundary": ["仅测试冻结依据，不认证创作质量。"], "external_basis": [],
                }], "source_issues": [], "coverage": {"read_sheets": ["模版"]},
            }
            records.append(packages.create_semantic_revision(category_id="launch",
                source_ref=seed["resolved_ref"], editing_evidence=evidence))
        first, second = records
        registry.promote("launch", first["package_id"], first["package_version"])
        self.agent.generation.template_packages = packages
        self.agent.generation.asset_package_resolver = lambda _: {**registry.resolve_formal("launch"), "alias": "formal"}
        return packages, first, second

    def _select_template(self, record):
        project = self.agent.services.projects.get_project(self.token, self.scope["project_id"])
        return self.agent.generation.select_template(self.token, **self.scope,
            expected_project_version=project["project_version"], template_ref=record["resolved_ref"], confirmed=True)

    def _freeze_existing_basis(self):
        # The pre-existing anonymous draft is seeded by the shared fixture.
        # Persist its historical context through the same repository transaction.
        context = self.agent.generation.read_template_method_context(self.token, **self.scope)
        state = self.repo.read()
        with self.repo.transaction(expected_project_version=state["project_version"], expected_revision=state["revision"]):
            self.repo.body["generation_basis"] = {"context": context, "snapshot_sha256": sha256_json(context)}
        return context

    def _remove_anonymous_draft_head(self):
        with self.fixture.store.transaction() as connection:
            connection.execute("DELETE FROM creative_draft_heads WHERE project_id=?", (self.scope["project_id"],))

    def test_continuous_discussion_full_content_and_convention_reach_both_models_then_partial_adoption(self):
        provider = self._install(["discussion", "discussion", "revise_text"])
        first_message = "先讨论三个声音方向，不要改稿。本项目结尾保留已有人工声音。"
        before = deepcopy(self.repo.read())
        first = self._turn(first_message, "director-long-discussion", context=self.base.context())
        self.assertEqual(first["task"]["status"], "completed", first["response"])
        self.assertEqual(first["response"]["type"], "read_response")
        self.assertGreater(len(provider.long_content), 1000)
        self.assertEqual(first["response"]["content"], provider.long_content)
        self.assertEqual(first["response"]["choices"], provider.choices)
        self.assertEqual(provider.editor_inputs, [])
        self.assertEqual(self.repo.read(), before)

        convention = self.agent.save_project_convention(
            self.token, self.session_id, statement="本项目结尾保留已有人工声音",
            expected_knowledge_version=self.fixture.knowledge.get_state(
                self.token, self.scope["workspace_id"], project_id=self.scope["project_id"],
            )["knowledge_version"], confirmed=True,
            source_message_id=first["task"]["task_id"] + ":user",
        )
        self.assertEqual(convention["status"], "active")
        second_message = "先不要改，解释第二个方向为什么适合，同时记得保留刚才说的结尾。"
        second = self._turn(second_message, "director-followup-discussion", context=self.base.context())
        self.assertEqual(second["response"]["type"], "read_response", second["response"])
        self.assertEqual(self.repo.read(), before)

        third_message = "按刚才第二个方向修改所选两段的声音，保留刚才说的结尾和全部画面。"
        proposal = self._turn(third_message, "director-apply-second-direction", context=self.base.context())
        self.assertEqual(proposal["task"]["status"], "waiting_user", proposal["response"])
        self.assertEqual(proposal["response"]["type"], "workbench_proposal")
        self.assertEqual({group["target_id"] for group in proposal["response"]["groups"]}, {"block_1", "block_2"})
        self.assertEqual(self.base.business(), self.base.initial)
        self.assertEqual(len(provider.advisor_inputs), 3)
        self.assertEqual(len(provider.editor_inputs), 1)
        advisor_context = provider.advisor_inputs[-1]
        editor_context = provider.editor_inputs[0]["context"]
        history = advisor_context["conversation"]
        self.assertEqual([item["content"] for item in history], [
            first_message, provider.long_content, second_message, second["response"]["content"],
        ])
        self.assertEqual(history[1]["choices"], provider.choices)
        self.assertEqual(editor_context["conversation"], history)
        self.assertEqual(editor_context["recent_conversation"], history)
        self.assertEqual(editor_context["project_conventions"], advisor_context["project_conventions"])
        self.assertEqual(editor_context["project_conventions"][0]["convention_id"], convention["convention_id"])
        self.assertEqual(editor_context["project_conventions"][0]["statement"], convention["statement"])
        self.assertEqual(editor_context["project_conventions"][0]["evidence_refs"], convention["evidence_refs"])
        self.assertIn(third_message, provider.editor_inputs[0]["instruction"])

        applied = self.agent.confirm_workbench(
            self.token, proposal["task"]["task_id"],
            proposal_digest=proposal["response"]["proposal_digest"], retained_group_ids=["block_1"],
        )
        self.assertEqual(applied["response"]["type"], "workbench_applied")
        actual = self.base.business()
        previous = {item["shot_id"]: item for item in self.base.initial["shots"]}
        current = {item["shot_id"]: item for item in actual["shots"]}
        self.assertNotEqual(current["block_1"]["fields"]["声音"], previous["block_1"]["fields"]["声音"])
        for key in ("画面描述", "段落", "时长"):
            self.assertEqual(current["block_1"]["fields"][key], previous["block_1"]["fields"][key])
        self.assertEqual(current["block_2"], previous["block_2"])
        self.assertEqual(current["block_3"], previous["block_3"])
        self.assertEqual(actual["refs"], self.base.initial["refs"])
        fresh = self.base.new_agent()
        self.assertEqual(fresh.read_workbench(self.token, proposal["task"]["task_id"])["response"], applied["response"])
        reread = fresh.get_session(self.token, self.session_id)
        self.assertEqual(len(reread["messages"]), 6, "Child editor tasks must not duplicate conversation messages")
        self.assertEqual(reread["messages"][0]["content"], first_message)
        self.assertEqual(reread["messages"][1]["content"], provider.long_content)
        self.assertEqual(reread["messages"][1]["response"]["choices"], provider.choices)
        self.assertEqual(reread["project_decisions"][0]["convention_id"], convention["convention_id"])
        refreshed = fresh.generation.read_creative_draft(self.token, **self.scope)
        self.assertEqual(refreshed["blocks"][0]["fields"]["声音"], current["block_1"]["fields"]["声音"])
        self.assertEqual(refreshed["blocks"][2]["fields"], previous["block_3"]["fields"])

    def test_existing_project_without_draft_can_discuss_without_creating_a_business_revision(self):
        # This removes only the disposable anonymous fixture's head. Its project,
        # confirmed Brief and immutable source history remain the same project.
        with self.fixture.store.transaction() as connection:
            connection.execute("DELETE FROM creative_draft_heads WHERE project_id=?", (self.scope["project_id"],))
        provider = self._install(["discussion"])
        before = self._business_rows()
        empty = self.repo.read()
        self.assertIsNone(empty["body"]["draft"])
        result = self._turn("还没有初稿，先讨论三种声音叙事方向。", "director-no-draft-discussion")
        self.assertEqual(result["task"]["status"], "completed", result["response"])
        self.assertEqual(result["response"]["type"], "read_response")
        self.assertEqual(result["response"]["content"], provider.long_content)
        self.assertEqual(provider.advisor_inputs[0]["snapshot"]["stage"], "discussion")
        self.assertEqual(provider.advisor_inputs[0]["snapshot"]["targets"], [])
        self.assertEqual(provider.editor_inputs, [])
        self.assertEqual(self.repo.read(), empty)
        self.assertEqual(self._business_rows(), before)
        fresh = self.base.new_agent()
        refreshed = fresh.get_session(self.token, self.session_id)
        self.assertEqual(fresh.director_context(self.token, self.session_id)["stage"], "discussion")
        self.assertEqual(refreshed["messages"][-1]["content"], provider.long_content)

    def test_viewer_and_disabled_write_switch_allow_discussion_without_script_changes(self):
        identity = LocalIdentityAdapter(self.fixture.store)
        workspace = self.agent.services.projects.get_workspace(self.token, self.scope["workspace_id"])
        viewer = identity.create_actor(
            self.token, **self.scope, display_name="匿名讨论查看者", auth_source="local", role="viewer",
            expected_workspace_version=workspace["workspace_version"],
        )
        viewer_token = identity.issue_auth_session(actor_id=viewer["actor_id"])["auth_token"]
        viewer_session = self.agent.services.tasks.create_agent_session(viewer_token, project_id=self.scope["project_id"])
        provider = self._install(["discussion", "discussion"])
        before = self._business_rows()
        draft = deepcopy(self.repo.read())
        for token, session_id, writes_enabled in (
            (viewer_token, viewer_session["agent_session_id"], True),
            (self.token, self.session_id, False),
        ):
            with self.subTest(viewer=token == viewer_token, writes_enabled=writes_enabled):
                self.agent.writes_enabled = writes_enabled
                result = self._turn(
                    "只讨论声音方向，不要修改稿件。", "director-readonly-" + str(writes_enabled),
                    token=token, session_id=session_id,
                )
                self.assertEqual(result["task"]["status"], "completed", result["response"])
                self.assertEqual(result["response"]["type"], "read_response")
                self.assertTrue(result["response"]["content"])
                self.assertEqual(self.repo.read(), draft)
                self.assertEqual(self._business_rows(), before)
        self.assertEqual(len(provider.advisor_inputs), 2)
        self.assertEqual(provider.editor_inputs, [])

    def test_no_draft_discussion_reads_current_selected_template_methods_without_business_write(self):
        _, first, second = self._method_templates()
        self._select_template(second)
        self._remove_anonymous_draft_head()
        expected = self.agent.generation.read_template_method_context(self.token, **self.scope)
        self.assertEqual(expected["template_ref"], second["resolved_ref"])
        self.assertNotEqual(expected["template_ref"], first["resolved_ref"])
        self.assertEqual(expected["template_semantics"]["editing_methods"]["status"], "evidence_bound")
        before, empty = self._business_rows(), deepcopy(self.repo.read())
        provider = self._install(["discussion"])
        with patch.object(self.agent.generation, "read_template_method_context",
                          wraps=self.agent.generation.read_template_method_context) as reader:
            result = self._turn("没有稿件，先按当前模板讨论剪辑方向。", "director-selected-method-discussion")
        self.assertGreater(reader.call_count, 0)
        self.assertEqual(result["response"]["type"], "read_response")
        context = provider.advisor_inputs[0]
        self.assertEqual(context["snapshot"]["stage"], "discussion")
        self.assertEqual(context["basis_kind"], "selected_template_reference")
        self.assertEqual(context["template_semantics"], expected["template_semantics"])
        self.assertEqual(context["frozen_basis"]["template_rules"], expected["template_rules"])
        self.assertEqual(self.repo.read(), empty)
        self.assertEqual(self._business_rows(), before)
        self.assertEqual(provider.editor_inputs, [])
        fresh = self.base.new_agent()
        self.assertEqual(project_context(fresh, self.token, self.session_id, for_model=True)["creative_basis"]["template_semantics"],
                         expected["template_semantics"])

    def test_existing_draft_uses_frozen_method_basis_after_new_template_selection(self):
        _, first, second = self._method_templates()
        self._select_template(first)
        frozen = self._freeze_existing_basis()
        self._select_template(second)
        current = self.agent.generation.read_template_method_context(self.token, **self.scope)
        self.assertNotEqual(frozen["template_semantics"], current["template_semantics"])
        before, business = deepcopy(self.repo.read()), self._business_rows()
        provider = self._install(["discussion"])
        with patch.object(self.agent.generation, "read_template_method_context",
                          wraps=self.agent.generation.read_template_method_context) as reader:
            result = self._turn("讨论当前稿的声音关系，保留已有创作依据。", "director-frozen-method-discussion")
        reader.assert_not_called()
        self.assertEqual(result["response"]["type"], "read_response")
        context = provider.advisor_inputs[0]
        self.assertEqual(context["template_semantics"], frozen["template_semantics"])
        self.assertEqual(context["frozen_basis"]["template_rules"], frozen["template_rules"])
        self.assertNotEqual(context["template_semantics"], current["template_semantics"])
        self.assertEqual(self.repo.read(), before)
        self.assertEqual(self._business_rows(), business)
        self.assertEqual(project_context(self.base.new_agent(), self.token, self.session_id, for_model=True)["creative_basis"]["template_semantics"],
                         frozen["template_semantics"])

    def test_template_selection_invalidates_proposal_but_never_replaces_saved_draft_basis(self):
        _, first, second = self._method_templates()
        self._select_template(first)
        frozen = self._freeze_existing_basis()
        provider = self._install(["revise_text"])
        proposal = self._turn("调整所选两段声音，原结尾保留。", "director-method-stale-proposal", context=self.base.context())
        self.assertEqual(proposal["response"]["type"], "workbench_proposal", proposal["response"])
        body = deepcopy(self.repo.read()["body"])
        self._select_template(second)
        fresh = self.base.new_agent()
        self.assertTrue(fresh.read_workbench(self.token, proposal["task"]["task_id"])["response"]["stale"])
        database = self.base.database()
        with self.assertRaisesRegex(WorkspaceServiceError, "DRAFT_REVISION_CONFLICT"):
            fresh.confirm_workbench(self.token, proposal["task"]["task_id"],
                proposal_digest=proposal["response"]["proposal_digest"], retained_group_ids=["block_1"])
        self.assertEqual(self.base.database(), database)
        self.assertEqual(self.repo.read()["body"], body)
        self.assertEqual(self.repo.read()["body"]["generation_basis"]["context"], frozen)
        self.assertEqual(provider.editor_inputs[0]["context"]["template_semantics"], frozen["template_semantics"])

    def test_default_template_change_invalidates_empty_project_analysis_without_version_change(self):
        packages, first, second = self._method_templates()
        self._remove_anonymous_draft_head()
        provider = self._install(["discussion"])
        started = self.agent.start_workbench(self.token, self.session_id,
            content="先按当前默认模板讨论剪辑思路。", context=None, operation_key="director-default-method-stale")
        before, empty = self._business_rows(), deepcopy(self.repo.read())
        self.assertEqual(self.agent.generation.read_template_method_context(self.token, **self.scope)["template_ref"],
                         first["resolved_ref"])
        packages.registry.promote("launch", second["package_id"], second["package_version"])
        self.assertEqual(self._business_rows(), before)
        with self.assertLogs("mvp_v2.creative_workbench", level="ERROR"):
            result = self.agent.execute_workbench(self.token, started["task"]["task_id"])
        self.assertEqual(result["task"]["status"], "failed", result["response"])
        self.assertEqual(result["response"]["error_code"], "DRAFT_REVISION_CONFLICT")
        self.assertEqual(provider.advisor_inputs, [])
        self.assertEqual(provider.editor_inputs, [])
        self.assertEqual(self.repo.read(), empty)
        self.assertEqual(self._business_rows(), before)
        current = project_context(self.base.new_agent(), self.token, self.session_id, for_model=True)
        self.assertEqual(current["creative_basis"]["template_ref"], second["resolved_ref"])


if __name__ == "__main__":
    unittest.main()
