import json
import unittest
from openpyxl import load_workbook

from mvp_v2.creative_generation import repository
from mvp_v2.formal_generation_service import FormalTextProviderPolicy
from mvp_v2.generation_stages import read_script_plan
from mvp_v2.excel_io import ADJACENT_REVIEW_SHEET_NAME
from mvp_v2.version_review_service import VersionReviewService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_generation_service as formal_tests


class CreativeFormalBindingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = formal_tests.FormalGenerationServiceTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.calls = []
        self.service = self.fixture._service(FormalTextProviderPolicy(
            writer=self.provider, editor=self.provider, writer_provider="anonymous", writer_model="anonymous",
            editor_provider="anonymous", editor_model="anonymous"))
        self.scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.fixture.project["project_id"]}
        self.repo = repository(self.service, self.fixture.token, **self.scope)
        current = self.repo.read()
        rows = [{"shot_id": f"block_{i}", "shot_no": str(i), "fields": {
            "段落": title, "画面描述": visual, "声音": sound, "时长": "建议5秒"},
            "source_refs": [{"type": "user_instruction", "input_sha256": "a" * 64}]}
            for i, (title, visual, sound) in enumerate([
                ("开场", "活动现场的品牌标识与空间入口依次入画，观众在入口停留观看", "现场氛围与音乐推进"),
                ("核心展示", "核心宣讲与仪式在活动现场展开，观众的注视与现场动作形成呼应", "现场氛围与音乐推进"),
                ("结尾", "活动空间逐渐拉远，品牌标识与现场观众共同构成完整的片尾画面", "现场氛围与音乐推进"),
            ], 1)]
        with self.repo.transaction(expected_project_version=current["project_version"], expected_revision=0):
            VersionReviewService(self.repo).create_draft(workspace_id=self.scope["workspace_id"],
                project_id=self.scope["project_id"], base_version_id=None, actor_id=self.repo.actor_id, shots=rows)

    def provider(self, prompt, model, tokens):
        if "\nINPUT:" in prompt:
            return self.fixture._provider("planning", [])(prompt, model, tokens)
        self.calls.append(prompt)
        fallback = self.fixture._provider("writer", [])(prompt, model, tokens)
        payload = json.loads(fallback)
        source = json.loads(prompt.split("\nDATA:", 1)[1].split("\nDRAFT:", 1)[0])
        for shot, basis in zip(payload["shots"], source["shots"]):
            shot.update(basis.get("accepted_creative_fields") or {})
        return json.dumps(payload, ensure_ascii=False)

    def plan(self):
        state = self.repo.read()
        plan = self.service.prepare_plan(self.fixture.token, **self.scope,
            expected_project_version=state["project_version"], expected_creative_revision=state["revision"],
            generation_mode="text_only_draft")
        plan["plan_ref"] = self.service._plan_event(self.fixture.token, plan["plan_id"])["plan_ref"]
        return plan

    def test_plan_preserves_order_content_real_sources_and_exposes_added_shots(self):
        original = self.repo.read()["body"]["draft"]["shots"]
        plan = self.plan()
        self.assertEqual(self.calls, [])
        self.assertEqual(plan["creative_conversion"]["mapped_count"], 3)
        mapped = [shot for shot in plan["shots"] if shot.get("creative_block")]
        self.assertEqual([shot["creative_block"]["block_id"] for shot in mapped], [row["shot_id"] for row in original])
        self.assertEqual([shot["visual"] for shot in mapped], [row["fields"]["画面描述"] for row in original])
        # The anonymous library has 17 template slots; three draft blocks do not request a new count.
        self.assertEqual(plan["shot_count"], 17)
        self.assertIsNone(plan["capacity_report"]["requested_shot_count"])
        self.assertEqual(plan["capacity_report"]["hard_slot_quantity_mode"], "template_default")
        for shot in plan["shots"]:
            self.assertTrue(shot["template_source"]["file"])
            self.assertTrue(shot["template_source"]["sheet"])
            self.assertGreater(shot["template_source"]["row"], 0)
        frozen = read_script_plan(self.service.services.artifacts.resolve_stage_artifact(
            self.service._run_id_from_ref(plan["plan_ref"]), "script_plan").path)
        for planned, row in zip(frozen.shot_plans, frozen.script_rows):
            if planned.get("creative_block"):
                self.assertIn("初稿段落：" + planned["creative_block"]["fields"]["段落"], row["备注"])
        self.assertEqual(frozen.constraints["creative_draft_binding"]["draft_id"], self.repo.read()["body"]["draft"]["draft_id"])
        creative_events = [event for event in frozen.events if event["event_id"].startswith("creative_")]
        self.assertTrue(all(event["fact_status"] == "creative_expression" for event in creative_events))

    def test_protected_blocks_are_frozen_mapped_and_later_protection_invalidates_plan(self):
        state = self.repo.read()
        with self.repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            self.repo.body['assistant_protected_ids'] = ['block_2']
        plan = self.plan()
        mapped = next(shot for shot in plan['shots'] if shot.get('creative_block', {}).get('block_id') == 'block_2')
        self.assertTrue(mapped['creative_block']['assistant_protected'])
        self.assertEqual(plan['creative_conversion']['protected_count'], 1)
        self.assertTrue(plan['creative_conversion']['mappings'])
        state = self.repo.read()
        with self.repo.transaction(expected_project_version=state['project_version'], expected_revision=state['revision']):
            self.repo.body['assistant_protected_ids'] = []
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.service.generate_from_confirmed_plan(self.fixture.token, **self.scope,
                task_id=plan['plan_id'], plan_ref=plan['plan_ref'], plan_sha256=plan['plan_sha256'],
                expected_project_version=self.repo.read()['project_version'])
        self.assertEqual(caught.exception.code, 'CREATIVE_PLAN_STALE')
        self.assertFalse(self.calls)

    def test_edited_draft_invalidates_plan_before_any_provider_call(self):
        plan = self.plan()
        state = self.repo.read()
        with self.repo.transaction(expected_project_version=state["project_version"], expected_revision=state["revision"]):
            VersionReviewService(self.repo).apply_table_patch(state["body"]["draft"]["draft_id"],
                expected_revision=0, actor_id=self.repo.actor_id, shot_no="2", field="声音", value="用户改过的音乐")
        with self.assertRaises(WorkspaceServiceError) as error:
            self.service.generate_from_confirmed_plan(self.fixture.token, **self.scope,
                task_id=plan["plan_id"], plan_ref=plan["plan_ref"], plan_sha256=plan["plan_sha256"],
                expected_project_version=self.repo.read()["project_version"])
        self.assertEqual(error.exception.code, "CREATIVE_PLAN_STALE")
        self.assertEqual(self.calls, [])

    def test_formal_generation_preserves_accepted_text_without_creating_review_evidence(self):
        original = self.repo.read()["body"]["draft"]
        plan = self.plan()
        result = self.service.generate_from_confirmed_plan(self.fixture.token, **self.scope,
            task_id=plan["plan_id"], plan_ref=plan["plan_ref"], plan_sha256=plan["plan_sha256"],
            expected_project_version=self.repo.read()["project_version"])
        self.assertEqual(len(self.calls), 2)
        for shot in plan["shots"]:
            if shot.get("creative_block"):
                row = next(row for row in result["script_rows"] if row["镜号"] == shot["shot_no"])
                self.assertEqual(row["画面描述"], shot["visual"])
                self.assertEqual(row["声音"], shot["sound"])
                self.assertIn("初稿段落：" + shot["creative_block"]["section"], row["备注"])
        self.assertEqual(self.repo.read()["body"]["draft"], original)
        self.assertTrue(result["version_id"])
        with self.fixture.store.connection() as connection:
            version = connection.execute("SELECT * FROM project_versions WHERE version_id=?", (result["version_id"],)).fetchone()
            self.assertIsNone(version["parent_version_id"])
            self.assertEqual(connection.execute("SELECT count(*) FROM version_review_human_decisions").fetchone()[0], 0)
            completion = connection.execute("SELECT * FROM version_completion_states WHERE version_id=?", (result["version_id"],)).fetchone()
            self.assertFalse(completion["delivery_eligible"])
            self.assertFalse(completion["review_eligible"])
        workbook = self.service.services.artifacts.resolve_artifact(version["run_id"], "workbook")
        wb = load_workbook(workbook.path)
        try:
            edges = [row for row in wb[ADJACENT_REVIEW_SHEET_NAME].iter_rows(min_row=2, values_only=True) if any(value is not None for value in row)]
            self.assertEqual(len(edges), len(result["script_rows"]) - 1)
        finally:
            wb.close()

    def test_provider_rewrite_cannot_replace_frozen_accepted_expressions(self):
        original_provider = self.provider
        def rewriting(prompt, model, tokens):
            payload = json.loads(original_provider(prompt, model, tokens))
            payload["shots"][0]["visual_description"] = "模型擅自改写用户已经保存的第一段画面"
            return json.dumps(payload, ensure_ascii=False)
        self.service.provider_policy = FormalTextProviderPolicy(
            writer=rewriting, editor=rewriting, writer_provider="anonymous", writer_model="anonymous",
            editor_provider="anonymous", editor_model="anonymous")
        plan = self.plan()
        result = self.service.generate_from_confirmed_plan(self.fixture.token, **self.scope,
                task_id=plan["plan_id"], plan_ref=plan["plan_ref"], plan_sha256=plan["plan_sha256"],
                expected_project_version=self.repo.read()["project_version"])
        first = next(row for row in result["script_rows"] if row["镜号"] == plan["shots"][0]["shot_no"])
        self.assertEqual(first["画面描述"], plan["shots"][0]["visual"])
        self.assertEqual(first["声音"], plan["shots"][0]["sound"])
        self.assertNotIn("模型擅自改写", json.dumps(result["script_rows"], ensure_ascii=False))
        with self.fixture.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM project_versions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM version_review_human_decisions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
