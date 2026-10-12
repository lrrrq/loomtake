"""Original shooting suggestions retain accurate evidence across real draft operations."""

from __future__ import annotations

from io import BytesIO
import json
import unittest

from openpyxl import load_workbook

from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_script_workspace as workspace_tests
from tests.test_mvp_v2_formal_browser_journey import _assert_review_workbook_matches_saved_version


class OriginalAdaptationIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = workspace_tests.FormalScriptWorkspaceTests("runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.generated = self.fixture.generated
        self.workspace = self.fixture.workspace
        self.token = self.generated.token
        self.scope = {
            "workspace_id": self.generated.workspace_id,
            "project_id": self.generated.project["project_id"],
        }
        self.tasks = TaskService(self.fixture.store)
        self.revisions = FormalScriptRevisionService(
            projects=self.fixture.projects, tasks=self.tasks,
            workspace=self.workspace, writes_enabled=True,
        )
        self.provider_calls_after_generation = len(self.fixture.calls)
        self.assertGreaterEqual(len(self._current()["shots"]), 3)

    def tearDown(self):
        # The generation fixture uses anonymous stubs; revisions use no editor.
        self.assertEqual(len(self.fixture.calls), self.provider_calls_after_generation)

    def _current(self, workspace=None):
        return (workspace or self.workspace).read_current(self.token, **self.scope)

    def _propose(self, instruction, key):
        before = self._current()
        return self.revisions.propose(
            self.token, agent_session_id="", project_id=self.scope["project_id"],
            instruction=instruction,
            expected_project_version=before["project_version"],
            expected_revision=before["revision"], idempotency_key=key,
            draft_only=True,
        )

    def _apply(self, instruction, key):
        before = self._current()
        proposal = self._propose(instruction, key)
        self.assertEqual(proposal["response"]["type"], "action_proposal", proposal)
        preview = self._current()
        self.assertEqual(preview["shots"], before["shots"])
        self.assertEqual(preview["revision"], before["revision"])
        parameters = next(
            event["details"]["internal_formal_parameters"]
            for event in reversed(self.tasks.list_task_events(self.token, proposal["task_id"]))
            if event["details"].get("internal_formal_parameters")
        )
        self.revisions.execute(
            self.token,
            project=self.fixture.projects.get_project(self.token, self.scope["project_id"]),
            parameters=parameters,
        )
        after = self._current()
        self.assertEqual(after["revision"], before["revision"] + 1)
        return after

    def _basis(self):
        _, context = self.workspace._current_context(self.token, **self.scope)
        draft, _, _ = self.workspace._repository(self.token, context).draft_state(
            self.workspace._open_draft_id(context),
        )
        return json.loads(draft.shots[0]["source_detail_override"]["原创改编依据"])

    def _first_original(self):
        before = self._current()
        first = self._apply(
            "把第1镜运镜修改为跟随；"
            "把第1镜画面描述修改为拍摄建议：展示主会场入口处的指引标识。",
            "original-initial",
        )
        self.assertEqual(first["shots"][0]["adaptation"]["origin"], "original_suggestion")
        self.assertEqual(first["shots"][0]["source"], before["shots"][0]["source"])
        self.assertEqual(first["shots"][1:], before["shots"][1:])
        return first

    def _save_restart_and_export(self, current, *, absent_text=()):
        versions_before = self.fixture.projects.list_project_versions(
            self.token, self.scope["project_id"],
        )
        saved = self.workspace.save_version(
            self.token, **self.scope,
            expected_project_version=current["project_version"],
            expected_revision=current["revision"], reason="匿名原创依据复核",
            idempotency_key="original-save",
        )
        self.assertEqual(saved["save_status"], "completed")
        versions_after = self.fixture.projects.list_project_versions(
            self.token, self.scope["project_id"],
        )
        self.assertEqual(versions_after[:-1], versions_before)
        self.assertEqual(len(versions_after), len(versions_before) + 1)
        restarted = type(self.workspace)(
            self.workspace.services, runs_root=self.generated.root / "runs",
            source_root=self.generated.source_root,
        )
        restored = self._current(restarted)
        self.assertEqual(restored["shots"], current["shots"])
        exported = restarted.build_review_workbook(
            self.token, **self.scope, version_no=len(versions_after),
        )
        source = self.fixture.resolver.resolve_artifact(versions_after[-1]["run_id"], "workbook").path
        _assert_review_workbook_matches_saved_version(
            BytesIO(exported["content"]), source, len(current["shots"]), len(versions_after),
        )
        workbook = load_workbook(BytesIO(exported["content"]), data_only=True)
        try:
            text = "\n".join(
                str(cell.value or "") for sheet in workbook for row in sheet for cell in row
            )
        finally:
            workbook.close()
        self.assertIn(current["shots"][0]["visual"], text)
        self.assertIn("原创拍摄建议", text)
        for stale in absent_text:
            self.assertNotIn(stale, text)
        for private in (
            "artifact://", str(self.generated.root), self.token,
            "原创改编依据", "source_catalog_sha256", "formal_fact_evidence",
        ):
            self.assertNotIn(private, text)
        self.assertEqual(
            self.fixture.projects.list_project_versions(self.token, self.scope["project_id"]),
            versions_after,
        )

    def _assert_visual_followup(self, *, through_table):
        first = self._first_original()
        first_basis = self._basis()
        visual = "拍摄建议：展示主会场入口处的地面纹理。"
        if through_table:
            self.workspace.apply_table_change(
                self.token, **self.scope,
                expected_project_version=first["project_version"],
                expected_revision=first["revision"],
                shot_no="1", shot_id=first["shots"][0]["shot_id"],
                field="visual", value=visual, idempotency_key="table-visual-followup",
            )
            current = self._current()
        else:
            current = self._apply(
                f"把第1镜画面描述修改为{visual}", "canonical-visual-followup",
            )
        self.assertEqual(current["shots"][0]["visual"], visual)
        self.assertEqual(current["shots"][1:], first["shots"][1:])
        self.assertEqual(current["shots"][0]["source"], first["shots"][0]["source"])
        self.assertEqual(self._basis()["fields"]["visual"], visual)
        self.assertEqual(self._basis()["reference_source_refs"], first_basis["reference_source_refs"])
        summary = current["shots"][0]["adaptation"]["summary"]
        self.assertIn(visual, summary)
        self.assertNotIn(first["shots"][0]["visual"], summary)
        self._save_restart_and_export(current, absent_text=(first["shots"][0]["visual"],))

    def test_canonical_visual_followup_refreshes_original_summary_and_saved_export(self):
        self._assert_visual_followup(through_table=False)

    def test_table_visual_followup_refreshes_original_summary_and_saved_export(self):
        self._assert_visual_followup(through_table=True)

    def test_undo_then_new_branch_excludes_discarded_original_evidence_after_restart(self):
        first = self._first_original()
        first_basis = self._basis()
        discarded = self._apply("把第1镜运镜修改为推", "discarded-push")
        discarded_basis = self._basis()
        self.workspace.undo(
            self.token, **self.scope,
            expected_project_version=discarded["project_version"],
            expected_revision=discarded["revision"], idempotency_key="undo-push",
        )
        self.assertEqual(self._current()["shots"], first["shots"])
        self.assertEqual(self._basis(), first_basis)
        branch = self._apply("把第1镜运镜修改为拉", "new-pull")
        branch_basis = self._basis()
        self.assertEqual(branch_basis["fields"]["camera_move"], "拉")
        self.assertEqual(branch_basis["previous"], first_basis)
        self.assertNotEqual(branch_basis["previous"], discarded_basis)
        self.assertEqual(branch["shots"][1:], first["shots"][1:])
        self._save_restart_and_export(branch, absent_text=("运镜=推",))

    def _draft_snapshot(self):
        with self.fixture.store.connection() as connection:
            return {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in (
                    "projects", "project_versions", "version_review_drafts",
                    "version_review_draft_states", "version_review_draft_heads",
                    "version_review_cell_patches", "version_review_undo_checkpoints",
                )
            }

    def test_invalid_technical_value_in_canonical_proposal_leaves_no_draft(self):
        before = self._current()
        snapshot = self._draft_snapshot()
        for field, label in (("camera_type", "类型"), ("shot_size", "景别"), ("camera_move", "运镜")):
            with self.subTest(field=field):
                with self.assertRaises(WorkspaceServiceError) as caught:
                    self._propose(
                        f"把第1镜字幕修改为不应写入的字幕；把第2镜{label}修改为量子跳跃",
                        f"invalid-canonical-{field}",
                    )
                self.assertEqual(caught.exception.code, "INVALID_INPUT")
                self.assertEqual(self._draft_snapshot(), snapshot)
                self.assertEqual(self._current()["shots"], before["shots"])
                self.assertEqual(self._current()["revision"], before["revision"])

    def test_invalid_technical_value_in_table_leaves_no_draft(self):
        before = self._current()
        snapshot = self._draft_snapshot()
        for field in ("camera_type", "shot_size", "camera_move"):
            with self.subTest(field=field):
                with self.assertRaises(WorkspaceServiceError) as caught:
                    self.workspace.apply_table_change(
                        self.token, **self.scope,
                        expected_project_version=before["project_version"],
                        expected_revision=before["revision"],
                        shot_no="2", shot_id=before["shots"][1]["shot_id"],
                        field=field, value="量子跳跃", idempotency_key=f"invalid-table-{field}",
                    )
                self.assertEqual(caught.exception.code, "INVALID_INPUT")
                self.assertEqual(self._draft_snapshot(), snapshot)
                self.assertEqual(self._current()["shots"], before["shots"])
                self.assertEqual(self._current()["revision"], before["revision"])


if __name__ == "__main__":
    unittest.main()
