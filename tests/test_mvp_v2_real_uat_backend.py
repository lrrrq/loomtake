from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as SpreadsheetImage
from PIL import Image

from mvp_v2 import formal_script_workspace as script_workspace_module
from mvp_v2.excel_io import write_template_output_workbook
from mvp_v2.formal_image_workspace import FormalImageWorkspaceService, ImageShotSelection
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService
from mvp_v2.generation_stages import read_script_plan
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_revision_union as union_fixture
from tests import test_mvp_v2_formal_image_workspace as image_fixture
from tests import test_mvp_v2_formal_generation_service as generation_fixture


class RealUatBackendRegressionTests(unittest.TestCase):
    """Anonymous multi-shot reproductions of the saved-version UAT failures."""

    def setUp(self):
        self.fixture = union_fixture.FormalRevisionUnionTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.workspace = self.fixture.base.workspace
        self.token = self.fixture.token
        self.scope = {
            "workspace_id": self.fixture.workspace_id,
            "project_id": self.fixture.project_id,
        }
        self.initial = self.workspace.read_current(self.token, **self.scope)
        self.assertGreaterEqual(len(self.initial["shots"]), 3)

    def _edit_third_sound(self):
        return self.workspace.apply_table_change(
            self.token, **self.scope,
            expected_project_version=self.initial["project_version"],
            expected_revision=self.initial["revision"],
            shot_no="3", shot_id=self.initial["shots"][2]["shot_id"],
            field="sound", value="现场掌声落下后保留轻音乐",
            idempotency_key="real-uat-third-sound",
        )

    def _history(self, action, current):
        return getattr(self.workspace, action)(
            self.token, **self.scope,
            expected_project_version=self.initial["project_version"],
            expected_revision=current["revision"],
            idempotency_key=f"real-uat-{action}-{current['revision']}",
        )

    def _review_rows(self):
        result = self.workspace.build_review_workbook(
            self.token, **self.scope, version_no=1,
        )
        workbook = load_workbook(BytesIO(result["content"]), data_only=True)
        try:
            sheet = workbook["脚本"]
            self.assertIn("复核稿，非正式交付", sheet["A1"].value)
            return tuple(sheet.iter_rows(values_only=True))
        finally:
            workbook.close()

    def _save_proposal(self, changed, *, key="real-uat-save"):
        return self.fixture.revision.propose_draft_save(
            self.token, project_id=self.fixture.project_id,
            expected_project_version=self.initial["project_version"],
            expected_revision=changed["revision"],
            reason="只保存第3镜声音调整", idempotency_key=key,
        )

    def test_undo_to_saved_text_allows_review_workbook_and_redo_stays_dirty(self):
        baseline_rows = self._review_rows()
        versions_before = self.fixture.projects.list_project_versions(
            self.token, self.fixture.project_id,
        )
        changed = self._edit_third_sound()
        undone = self._history("undo", changed)
        self.assertGreater(undone["revision"], 0)
        self.assertEqual(undone["state"], "ready")
        self.assertFalse(undone["can_save_version"])
        self.assertEqual(undone["shots"], self.initial["shots"])
        self.assertEqual(self._review_rows(), baseline_rows)

        redone = self._history("redo", undone)
        self.assertEqual(redone["state"], "editing")
        self.assertTrue(redone["can_save_version"])
        with self.assertRaises(WorkspaceServiceError) as caught:
            self._review_rows()
        self.assertEqual(caught.exception.code, "DRAFT_NOT_SAVED")
        self.assertEqual(
            self.fixture.projects.list_project_versions(self.token, self.fixture.project_id),
            versions_before,
        )

    def test_discard_restores_review_workbook_without_creating_a_version(self):
        baseline_rows = self._review_rows()
        versions_before = self.fixture.projects.list_project_versions(
            self.token, self.fixture.project_id,
        )
        changed = self._edit_third_sound()
        discarded = self._history("discard_changes", changed)
        self.assertGreater(discarded["revision"], 0)
        self.assertEqual(discarded["state"], "ready")
        self.assertFalse(discarded["can_save_version"])
        self.assertEqual(discarded["shots"], self.initial["shots"])
        self.assertEqual(self._review_rows(), baseline_rows)
        self.assertEqual(
            self.fixture.projects.list_project_versions(self.token, self.fixture.project_id),
            versions_before,
        )

    def test_unexpected_save_failure_finishes_task_idempotency_and_cannot_reexecute(self):
        changed = self._edit_third_sound()
        proposal = self._save_proposal(changed)
        task_id = proposal["task_id"]
        versions_before = self.fixture.projects.list_project_versions(
            self.token, self.fixture.project_id,
        )
        confirm = {
            "proposal_digest": proposal["response"]["proposal_digest"],
        }
        failure = ValueError("anonymous materialization failure /private/source.xlsx")
        with patch.object(self.workspace.materializer, "materialize", side_effect=failure) as materialize:
            # Capture the boundary error so the regression reports the persistent
            # stuck task, rather than stopping at an incidental uncaught exception.
            try:
                self.fixture.agent.confirm_action(self.token, task_id, **confirm)
            except ValueError:
                pass
            task = self.fixture.tasks.get_task(self.token, task_id)
            self.assertEqual(task["status"], "failed")
            self.assertTrue(task["error_code"])
            replay = self.fixture.agent.confirm_action(self.token, task_id, **confirm)
            self.assertEqual(replay["task"]["status"], "failed")
            self.assertEqual(materialize.call_count, 1)
            self.assertNotIn("/private/source.xlsx", json.dumps(replay, ensure_ascii=False))

        with self.fixture.store.connection() as connection:
            attempts = connection.execute(
                "SELECT attempt_no FROM task_attempts WHERE task_id=? ORDER BY attempt_no",
                (task_id,),
            ).fetchall()
            records = connection.execute(
                "SELECT status, response_json FROM tool_idempotency_records WHERE agent_session_id=?",
                (task["agent_session_id"],),
            ).fetchall()
        self.assertEqual([item["attempt_no"] for item in attempts], [1])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "failed")
        self.assertEqual(json.loads(records[0]["response_json"])["status"], "failed")
        events = self.fixture.tasks.list_task_events(self.token, task_id)
        self.assertEqual(events[-1]["status"], "failed")
        self.assertEqual(
            self.fixture.projects.list_project_versions(self.token, self.fixture.project_id),
            versions_before,
        )
        self.assertEqual(self.workspace.read_current(self.token, **self.scope)["shots"], changed["shots"])

    def test_draft_save_proposal_reports_only_the_changed_shot_and_field(self):
        changed = self._edit_third_sound()
        proposal = self._save_proposal(changed)
        response = proposal["response"]
        self._assert_third_sound_preview(response)

    def _assert_third_sound_preview(self, response):
        self.assertEqual(response["affected_shots"], ["3"])
        self.assertEqual(response["preview"]["affected_shots"], ["3"])
        self.assertEqual(response["affected_fields"], ["sound"])
        changes = response["preview"]["changes"]
        self.assertEqual(len(changes), 1)
        change = changes[0]
        self.assertEqual(change["shot_id"], self.initial["shots"][2]["shot_id"])
        self.assertEqual(change["shot_no"], "3")
        self.assertEqual(change["field"], "sound")
        self.assertEqual(change["before"], self.initial["shots"][2]["sound"])
        self.assertEqual(change["after"], "现场掌声落下后保留轻音乐")
        self.assertEqual(change["label"], "第 3 镜 · 声音")

    def test_save_preview_excludes_the_second_shot_edit_after_undo(self):
        first = self._edit_third_sound()
        second = self.workspace.apply_table_change(
            self.token, **self.scope,
            expected_project_version=self.initial["project_version"],
            expected_revision=first["revision"],
            shot_no="5", shot_id=self.initial["shots"][4]["shot_id"],
            field="subtitle", value="随后撤销的第5镜字幕",
            idempotency_key="real-uat-fifth-subtitle",
        )
        self.assertNotEqual(second["shots"][4]["subtitle"], self.initial["shots"][4]["subtitle"])
        undone = self._history("undo", second)
        self.assertGreater(undone["revision"], first["revision"])
        self.assertEqual(undone["shots"], first["shots"])
        self.assertTrue(undone["can_redo"])
        proposal = self._save_proposal(undone, key="real-uat-net-changes-save")
        self._assert_third_sound_preview(proposal["response"])

    def test_save_preview_preserves_deleted_identity_and_every_surviving_renumber(self):
        changed = self.workspace.apply_revision_operation(
            self.token, **self.scope,
            expected_project_version=self.initial["project_version"], expected_revision=0,
            operation={
                "operation": "delete_shot", "shot_no": "1",
                "shot_id": self.initial["shots"][0]["shot_id"],
            },
            idempotency_key="real-uat-delete-first-shot",
        )
        self.assertEqual(len(changed["shots"]), len(self.initial["shots"]) - 1)
        proposal = self.fixture.revision.propose_draft_save(
            self.token, project_id=self.fixture.project_id,
            expected_project_version=self.initial["project_version"],
            expected_revision=changed["revision"], reason="删除首镜并保留其余镜头内容与来源",
            idempotency_key="real-uat-delete-first-preview",
        )
        response = proposal["response"]
        changes = response["preview"]["changes"]
        deleted = [item for item in changes if item["field"] == "delete_shot"]
        self.assertEqual(len(deleted), 1)
        self.assertEqual(deleted[0]["shot_id"], self.initial["shots"][0]["shot_id"])
        self.assertEqual(deleted[0]["shot_no"], "1")
        self.assertEqual(deleted[0]["label"], "第 1 镜 · 整镜")
        self.assertEqual(deleted[0]["before"], "镜头内容与来源")
        self.assertIsNone(deleted[0]["after"])
        renumbered = {item["shot_id"]: item for item in changes if item["field"] == "shot_no"}
        self.assertEqual(set(renumbered), {shot["shot_id"] for shot in self.initial["shots"][1:]})
        for index, shot in enumerate(self.initial["shots"][1:], start=1):
            with self.subTest(original_shot=shot["shot_no"]):
                item = renumbered[shot["shot_id"]]
                self.assertEqual(item["before"], shot["shot_no"])
                self.assertEqual(item["after"], str(index))
                self.assertEqual(item["shot_no"], str(index))
                self.assertEqual(item["label"], f"第 {index} 镜 · 镜号")
        self.assertEqual(len(changes), len(self.initial["shots"]))
        self.assertEqual(set(response["affected_fields"]), {"delete_shot", "shot_no"})
        self.assertEqual(set(response["affected_shots"]), {item["shot_no"] for item in changes})
        self.assertEqual(response["preview"]["affected_shots"], response["affected_shots"])
        self.assertIn(f"影响 {len(self.initial['shots'])} 镜", response["summary"])

    def test_receipt_error_after_completed_task_recovers_without_second_execution(self):
        changed = self._edit_third_sound()
        proposal = self._save_proposal(changed, key="real-uat-completed-receipt-error")
        task_id = proposal["task_id"]
        before = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        record_result = self.fixture.agent.services.idempotency.record_result
        receipt_calls = 0

        def fail_first_receipt(*args, **kwargs):
            nonlocal receipt_calls
            receipt_calls += 1
            if receipt_calls == 1:
                self.assertEqual(self.fixture.tasks.get_task(self.token, task_id)["status"], "completed")
                self.assertEqual(
                    len(self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)),
                    len(before) + 1,
                )
                raise ValueError("anonymous failure before receipt commit")
            return record_result(*args, **kwargs)

        confirmation = {"proposal_digest": proposal["response"]["proposal_digest"]}
        with patch.object(
            self.fixture.agent.services.idempotency, "record_result", side_effect=fail_first_receipt,
        ), patch.object(self.fixture.revision, "execute", wraps=self.fixture.revision.execute) as execute:
            confirmed = self.fixture.agent.confirm_action(self.token, task_id, **confirmation)
            self.assertEqual(confirmed["task"]["status"], "completed")
            replay = self.fixture.agent.confirm_action(self.token, task_id, **confirmation)
            self.assertEqual(replay["task"]["status"], "completed")
            self.assertEqual(replay["response"], confirmed["response"])
            self.assertEqual(execute.call_count, 1)
        after = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[-1]["parent_version_id"], self.initial["base_version_id"])
        task = self.fixture.tasks.get_task(self.token, task_id)
        with self.fixture.store.connection() as connection:
            records = connection.execute(
                "SELECT status, response_json FROM tool_idempotency_records WHERE agent_session_id=?",
                (task["agent_session_id"],),
            ).fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "completed")
        self.assertEqual(json.loads(records[0]["response_json"])["status"], "completed")
        self.assertEqual(
            [event["status"] for event in self.fixture.tasks.list_task_events(self.token, task_id)].count("completed"),
            1,
        )

    def test_sync_repairs_interrupted_receipt_for_already_completed_task(self):
        changed = self._edit_third_sound()
        proposal = self._save_proposal(changed, key="real-uat-completed-interrupted-receipt")
        task_id = proposal["task_id"]
        before = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        record_result = self.fixture.agent.services.idempotency.record_result
        receipt_calls = 0

        def interrupt_first_receipt(*args, **kwargs):
            nonlocal receipt_calls
            receipt_calls += 1
            if receipt_calls == 1:
                raise KeyboardInterrupt("anonymous interruption before receipt commit")
            return record_result(*args, **kwargs)

        with patch.object(
            self.fixture.agent.services.idempotency, "record_result", side_effect=interrupt_first_receipt,
        ), patch.object(self.fixture.revision, "execute", wraps=self.fixture.revision.execute) as execute:
            with self.assertRaises(KeyboardInterrupt):
                self.fixture.agent.confirm_action(
                    self.token, task_id, proposal_digest=proposal["response"]["proposal_digest"],
                )
            task = self.fixture.tasks.get_task(self.token, task_id)
            self.assertEqual(task["status"], "completed")
            after_commit = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
            self.assertEqual(len(after_commit), len(before) + 1)
            self.assertEqual(after_commit[-1]["parent_version_id"], self.initial["base_version_id"])
            with self.fixture.store.connection() as connection:
                initial_receipt = connection.execute(
                    "SELECT status FROM tool_idempotency_records WHERE agent_session_id=?",
                    (task["agent_session_id"],),
                ).fetchone()
            self.assertEqual(initial_receipt["status"], "in_progress")
            events = self.fixture.tasks.list_task_events(self.token, task_id)
            first = self.fixture.agent.sync_task(self.token, task_id)
            self.assertEqual(first["task"]["status"], "completed")
            second = self.fixture.agent.sync_task(self.token, task_id)
            self.assertEqual(second["response"], first["response"])
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(self.fixture.tasks.list_task_events(self.token, task_id), events)
        with self.fixture.store.connection() as connection:
            records = connection.execute(
                "SELECT status, response_json FROM tool_idempotency_records WHERE agent_session_id=?",
                (task["agent_session_id"],),
            ).fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "completed")
        self.assertEqual(json.loads(records[0]["response_json"])["status"], "completed")
        self.assertEqual(
            self.fixture.projects.list_project_versions(self.token, self.fixture.project_id), after_commit,
        )

    def test_session_read_during_workbook_render_preserves_active_save_and_final_receipt(self):
        changed = self._edit_third_sound()
        proposal = self._save_proposal(changed, key="real-uat-active-session-read")
        task_id = proposal["task_id"]
        session_id = self.fixture.tasks.get_task(self.token, task_id)["agent_session_id"]
        before = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        entered, release = threading.Event(), threading.Event()
        render = script_workspace_module.run_workbook_render_stage

        def pause_real_workbook_render(*args, **kwargs):
            entered.set()
            if not release.wait(20):
                raise TimeoutError("anonymous render was not released")
            return render(*args, **kwargs)

        lock = self.fixture.base.generated.root / "runs" / self.fixture.project_id / ".pipeline.lock"
        with patch.object(
            script_workspace_module, "run_workbook_render_stage", side_effect=pause_real_workbook_render,
        ) as rendering, ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                self.fixture.client.post,
                f"/api/agent/tasks/{task_id}/confirm", headers=self.fixture.headers,
                json={"proposal_digest": proposal["response"]["proposal_digest"]},
            )
            try:
                self.assertTrue(entered.wait(20), "confirm must reach actual workbook rendering")
                self.assertTrue(lock.is_file(), "the existing pipeline must still own its render lock")
                self.assertEqual(self.fixture.tasks.get_task(self.token, task_id)["status"], "executing")
                self.assertEqual(self.fixture.projects.list_project_versions(self.token, self.fixture.project_id), before)
                # This is the ordinary endpoint used by loadAgentSession when a
                # second browser tab opens or refreshes the same conversation.
                read = self.fixture.client.get(
                    f"/api/agent/sessions/{session_id}", headers=self.fixture.headers,
                )
                during_status = self.fixture.tasks.get_task(self.token, task_id)["status"]
            finally:
                release.set()
            confirmed = pending.result(timeout=30)
            self.assertEqual(rendering.call_count, 1)
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(during_status, "executing")
        self.assertEqual(
            next(item for item in read.json()["tasks"] if item["task_id"] == task_id)["status"],
            "executing",
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertEqual(confirmed.json()["task"]["status"], "completed")
        after = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[-1]["parent_version_id"], self.initial["base_version_id"])
        events = self.fixture.tasks.list_task_events(self.token, task_id)
        self.assertNotIn("blocked", [item["status"] for item in events])
        self.assertEqual([item["status"] for item in events].count("completed"), 1)
        for _ in range(2):
            self.assertEqual(self.fixture.agent.sync_task(self.token, task_id)["task"]["status"], "completed")
        self.assertEqual(self.fixture.tasks.list_task_events(self.token, task_id), events)
        self.assertEqual(self.fixture.projects.list_project_versions(self.token, self.fixture.project_id), after)
        with self.fixture.store.connection() as connection:
            receipts = connection.execute(
                "SELECT status, response_json FROM tool_idempotency_records WHERE agent_session_id=?", (session_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT attempt_no FROM task_attempts WHERE task_id=? ORDER BY attempt_no", (task_id,),
            ).fetchall()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "completed")
        self.assertEqual(json.loads(receipts[0]["response_json"])["status"], "completed")
        self.assertEqual([item["attempt_no"] for item in attempts], [1])
        self.assertFalse(lock.exists())

    def test_error_after_durable_child_reconciles_completed_without_second_version(self):
        changed = self._edit_third_sound()
        proposal = self._save_proposal(changed, key="real-uat-after-commit")
        task_id = proposal["task_id"]
        versions_before = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        execute = self.fixture.revision.execute

        def execute_then_lose_response(*args, **kwargs):
            execute(*args, **kwargs)
            raise ValueError("anonymous failure after durable child")

        confirmation = {"proposal_digest": proposal["response"]["proposal_digest"]}
        with patch.object(self.fixture.revision, "execute", side_effect=execute_then_lose_response) as execution:
            try:
                result = self.fixture.agent.confirm_action(self.token, task_id, **confirmation)
            except ValueError:
                result = None
            versions_after = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
            self.assertEqual(len(versions_after), len(versions_before) + 1)
            child_id = versions_after[-1]["version_id"]
            self.assertEqual(versions_after[-1]["parent_version_id"], self.initial["base_version_id"])
            task = self.fixture.tasks.get_task(self.token, task_id)
            self.assertEqual(task["status"], "completed")
            self.assertIsNotNone(result)
            self.assertEqual(result["response"]["version_id"], child_id)
            replay = self.fixture.agent.confirm_action(self.token, task_id, **confirmation)
            self.assertEqual(replay["task"]["status"], "completed")
            self.assertEqual(replay["response"]["version_id"], child_id)
            self.assertEqual(execution.call_count, 1)
        self.assertEqual(
            self.fixture.projects.list_project_versions(self.token, self.fixture.project_id),
            versions_after,
        )
        with self.fixture.store.connection() as connection:
            records = connection.execute(
                "SELECT status, response_json FROM tool_idempotency_records WHERE agent_session_id=?",
                (task["agent_session_id"],),
            ).fetchall()
            attempts = connection.execute(
                "SELECT attempt_no FROM task_attempts WHERE task_id=? ORDER BY attempt_no", (task_id,),
            ).fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "completed")
        self.assertEqual(json.loads(records[0]["response_json"])["status"], "completed")
        self.assertEqual([item["attempt_no"] for item in attempts], [1])

    def test_sync_unreconciled_historical_execution_blocks_idempotency_without_retry(self):
        changed = self._edit_third_sound()
        proposal = self._save_proposal(changed, key="real-uat-interrupted-save")
        task_id = proposal["task_id"]
        versions_before = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        # A process interruption escapes ordinary Exception handling and leaves
        # exactly the persisted pre-write state that restart recovery must read.
        with patch.object(
            self.workspace.materializer, "materialize",
            side_effect=KeyboardInterrupt("anonymous interrupted process"),
        ) as execution:
            with self.assertRaises(KeyboardInterrupt):
                self.fixture.agent.confirm_action(
                    self.token, task_id,
                    proposal_digest=proposal["response"]["proposal_digest"],
                )
            interrupted = self.fixture.tasks.get_task(self.token, task_id)
            self.assertEqual(interrupted["status"], "executing")
            first = self.fixture.agent.sync_task(self.token, task_id)
            self.assertEqual(first["task"]["status"], "blocked")
            self.assertEqual(first["task"]["error_code"], "RECOVERY_RECONCILIATION_REQUIRED")
            events_before_replay = self.fixture.tasks.list_task_events(self.token, task_id)
            second = self.fixture.agent.sync_task(self.token, task_id)
            self.assertEqual(second["task"]["status"], "blocked")
            self.assertEqual(execution.call_count, 1)
            self.assertEqual(self.fixture.tasks.list_task_events(self.token, task_id), events_before_replay)
        with self.fixture.store.connection() as connection:
            records = connection.execute(
                "SELECT status, response_json FROM tool_idempotency_records WHERE agent_session_id=?",
                (interrupted["agent_session_id"],),
            ).fetchall()
            attempts = connection.execute(
                "SELECT attempt_no FROM task_attempts WHERE task_id=? ORDER BY attempt_no", (task_id,),
            ).fetchall()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "blocked")
        self.assertEqual(json.loads(records[0]["response_json"])["status"], "blocked")
        self.assertEqual([item["attempt_no"] for item in attempts], [1])
        self.assertEqual(
            self.fixture.projects.list_project_versions(self.token, self.fixture.project_id),
            versions_before,
        )
        self.assertEqual(self.workspace.read_current(self.token, **self.scope)["shots"], changed["shots"])

    def _workspace_with_upgraded_source_alias(self):
        generated = self.fixture.base.generated
        new_source = generated.root / "packages" / "launch-core" / "6" / "sources"
        shutil.copytree(generated.source_root, new_source)
        workspace = FormalScriptWorkspaceService(
            self.workspace.services,
            runs_root=generated.root / "runs", source_root=generated.source_root,
            source_root_resolver=lambda _: new_source,
        )
        _, context = workspace._current_context(self.token, **self.scope)
        plan_path = self.fixture.base.resolver.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        ).path
        self.assertFalse(read_script_plan(plan_path).constraints.get("formal_asset_package"))
        return workspace, context, new_source

    def test_legacy_unpinned_parent_save_uses_frozen_source_after_same_bytes_alias_upgrade(self):
        workspace, parent_context, new_source = self._workspace_with_upgraded_source_alias()
        self.workspace = workspace
        changed = self._edit_third_sound()
        parent_manifest = self.fixture.base.resolver.read_manifest(
            parent_context.run_id, expected_sha256=parent_context.manifest.sha256,
        )
        frozen_source = parent_manifest["template_workbook"]
        self.assertNotEqual(frozen_source["path"], str(new_source / "M+发布会类通用脚本库.xlsx"))
        self.assertEqual(sha256(Path(frozen_source["path"]).read_bytes()).hexdigest(), frozen_source["sha256"])
        self.assertEqual(
            (new_source / "M+发布会类通用脚本库.xlsx").read_bytes(),
            Path(frozen_source["path"]).read_bytes(),
        )
        saved = workspace.save_version(
            self.token, **self.scope,
            expected_project_version=self.initial["project_version"],
            expected_revision=changed["revision"],
            reason="保持旧版本来源，只保存第3镜声音", idempotency_key="real-uat-legacy-save",
        )
        self.assertEqual(saved["save_status"], "completed")
        _, child_context = workspace._current_context(self.token, **self.scope)
        child_manifest = self.fixture.base.resolver.read_manifest(
            child_context.run_id, expected_sha256=child_context.manifest.sha256,
        )
        self.assertEqual(child_manifest["template_workbook"], frozen_source)
        self.assertEqual(child_manifest["parent_run_id"], parent_context.run_id)
        current = workspace.read_current(self.token, **self.scope)
        self.assertEqual(current["shots"][2]["sound"], changed["shots"][2]["sound"])
        for before, after in zip(self.initial["shots"], current["shots"]):
            self.assertEqual(after["source"], before["source"])
            for field in ("visual", "voiceover", "subtitle", "duration_seconds"):
                self.assertEqual(after[field], before[field])

    def test_legacy_frozen_workbook_content_drift_remains_rejected(self):
        workspace, parent_context, _ = self._workspace_with_upgraded_source_alias()
        self.workspace = workspace
        changed = self._edit_third_sound()
        versions_before = self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)
        source = self.fixture.base.generated.source_root / "M+发布会类通用脚本库.xlsx"
        workbook = load_workbook(source)
        try:
            workbook["模版"]["A3"] = "changed frozen source content"
            workbook.save(source)
        finally:
            workbook.close()
        with self.assertRaises((WorkspaceServiceError, ValueError)):
            workspace.save_version(
                self.token, **self.scope,
                expected_project_version=self.initial["project_version"],
                expected_revision=changed["revision"],
                reason="不得沿用内容被修改的冻结来源", idempotency_key="real-uat-legacy-drift",
            )
        self.assertEqual(
            self.fixture.projects.list_project_versions(self.token, self.fixture.project_id),
            versions_before,
        )
        _, current_context = workspace._current_context(self.token, **self.scope)
        self.assertEqual(current_context.version_id, parent_context.version_id)


class RealUatReferenceImageCountRegressionTests(unittest.TestCase):
    def _fixture_with_reference_pairs(self):
        write_library = generation_fixture.write_launch_library

        def write_library_with_reference_pairs(path, template_rows, creative_rows):
            write_library(path, template_rows, creative_rows)
            workbook = load_workbook(path)
            try:
                for row in range(7, 7 + len(template_rows)):
                    for alternative in range(2):
                        content = BytesIO()
                        color = ((row * 11) % 256, 40 + alternative * 160, (row * 19) % 256)
                        with Image.new("RGB", (128, 72), color) as image:
                            image.save(content, format="PNG")
                        content.seek(0)
                        workbook["模版"].add_image(SpreadsheetImage(content), f"C{row}")
                workbook.save(path)
            finally:
                workbook.close()

        fixture = union_fixture.FormalRevisionUnionTests("runTest")
        self.addCleanup(fixture.doCleanups)
        with patch.object(
            generation_fixture, "write_launch_library", side_effect=write_library_with_reference_pairs,
        ):
            fixture.setUp()
        return fixture

    def test_sound_save_preserves_first_reference_per_cell_with_multiple_source_images(self):
        fixture = self._fixture_with_reference_pairs()
        workspace, resolver = fixture.base.workspace, fixture.base.resolver
        scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project_id}
        initial = workspace.read_current(fixture.token, **scope)
        _, parent = workspace._current_context(fixture.token, **scope)
        parent_binding_path = resolver.resolve_stage_artifact(
            parent.run_id, "visual_binding", expected_manifest_sha256=parent.manifest.sha256,
        ).path
        parent_binding = json.loads(parent_binding_path.read_text(encoding="utf-8"))
        parent_workbook = resolver.resolve_artifact(parent.run_id, "workbook").path
        parent_bytes = parent_workbook.read_bytes()
        expected_images = {}
        paired_shots = []
        for row_index, row in enumerate(parent_binding["script_rows"], start=7):
            paths = [Path(value.strip()) for value in str(row.get("画面示意") or "").split(";") if value.strip()]
            if len(paths) == 2:
                paired_shots.append(str(row["镜号"]))
                self.assertTrue(all(path.is_file() for path in paths))
                with Image.open(paths[0]) as first, Image.open(paths[1]) as second:
                    first_pixels = first.convert("RGB").tobytes()
                    self.assertNotEqual(first_pixels, second.convert("RGB").tobytes())
                    expected_images[(row_index, 3)] = (first.size, sha256(first_pixels).hexdigest())
        self.assertGreaterEqual(len(paired_shots), 3)
        self.assertEqual(len(paired_shots), len(initial["shots"]))
        self.assertGreater(len(initial["shots"]), 3)
        self.assertEqual(self._workbook_images(parent_workbook), expected_images)

        changed = workspace.apply_table_change(
            fixture.token, **scope, expected_project_version=initial["project_version"],
            expected_revision=initial["revision"], shot_no="3", shot_id=initial["shots"][2]["shot_id"],
            field="sound", value="掌声收束后保留轻音乐", idempotency_key="reference-pairs-sound-edit",
        )
        saved = workspace.save_version(
            fixture.token, **scope, expected_project_version=initial["project_version"],
            expected_revision=changed["revision"], reason="只保存第3镜声音，保留源参考图",
            idempotency_key="reference-pairs-sound-save",
        )
        self.assertEqual(saved["save_status"], "completed")
        _, child = workspace._current_context(fixture.token, **scope)
        child_binding = json.loads(resolver.resolve_stage_artifact(
            child.run_id, "visual_binding", expected_manifest_sha256=child.manifest.sha256,
        ).path.read_text(encoding="utf-8"))
        self.assertEqual(len(child_binding["script_rows"]), len(parent_binding["script_rows"]))
        differences = [
            (str(after["镜号"]), field)
            for before, after in zip(parent_binding["script_rows"], child_binding["script_rows"])
            for field in set(before) | set(after)
            if before.get(field) != after.get(field)
        ]
        self.assertEqual(differences, [("3", "声音")])
        self.assertEqual(
            self._workbook_images(resolver.resolve_artifact(child.run_id, "workbook").path),
            expected_images,
        )
        self.assertEqual(parent_workbook.read_bytes(), parent_bytes)
        self.assertEqual(json.loads(parent_binding_path.read_text(encoding="utf-8")), parent_binding)
        versions = fixture.projects.list_project_versions(fixture.token, fixture.project_id)
        self.assertEqual(len(versions), 2)
        self.assertEqual(versions[-1]["parent_version_id"], parent.version_id)

    def test_sound_save_preserves_visual_detail_evidence_and_export_columns(self):
        fixture = self._fixture_with_reference_pairs()
        workspace, resolver = fixture.base.workspace, fixture.base.resolver
        scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project_id}
        initial = workspace.read_current(fixture.token, **scope)
        self.assertEqual(len(initial["shots"]), 17)
        _, parent = workspace._current_context(fixture.token, **scope)
        parent_paths = {
            stage: resolver.resolve_stage_artifact(
                parent.run_id, stage, expected_manifest_sha256=parent.manifest.sha256,
            ).path for stage in ("script_plan", "script_draft", "visual_binding")
        }
        parent_paths["workbook"] = resolver.resolve_artifact(parent.run_id, "workbook").path
        parent_paths["manifest"] = resolver.resolve_manifest(
            parent.run_id, expected_sha256=parent.manifest.sha256,
        ).path
        parent_files = {path: path.read_bytes() for path in parent_paths.values()}
        parent_binding = json.loads(parent_paths["visual_binding"].read_text(encoding="utf-8"))
        parent_draft = json.loads(parent_paths["script_draft"].read_text(encoding="utf-8"))
        parent_plan = json.loads(parent_paths["script_plan"].read_text(encoding="utf-8"))
        for detail in parent_binding["detail_rows"]:
            for field in ("原图路径", "图片绑定方式", "图片绑定说明", "视觉资产ID"):
                self.assertTrue(detail.get(field), (detail["模板镜号"], field))
        self.assertNotEqual(parent_binding["detail_rows"], parent_draft["detail_rows"])
        before_workbook = load_workbook(parent_paths["workbook"])
        try:
            expected_detail_rows = tuple(before_workbook["抽调明细"].iter_rows(values_only=True))
        finally:
            before_workbook.close()

        changed = workspace.apply_table_change(
            fixture.token, **scope, expected_project_version=initial["project_version"],
            expected_revision=initial["revision"], shot_no="3", shot_id=initial["shots"][2]["shot_id"],
            field="sound", value="掌声收束后保留轻音乐", idempotency_key="visual-detail-sound-edit",
        )
        saved = workspace.save_version(
            fixture.token, **scope, expected_project_version=initial["project_version"],
            expected_revision=changed["revision"], reason="只修改声音并完整保留视觉依据",
            idempotency_key="visual-detail-sound-save",
        )
        self.assertEqual(saved["save_status"], "completed")
        _, child = workspace._current_context(fixture.token, **scope)
        child_payloads = {
            stage: json.loads(resolver.resolve_stage_artifact(
                child.run_id, stage, expected_manifest_sha256=child.manifest.sha256,
            ).path.read_text(encoding="utf-8"))
            for stage in ("script_plan", "script_draft", "visual_binding")
        }
        self.assertEqual(child_payloads["visual_binding"]["detail_rows"], parent_binding["detail_rows"])
        self.assertEqual(child_payloads["script_draft"]["detail_rows"], parent_draft["detail_rows"])
        self.assertEqual(child_payloads["script_plan"]["detail_rows"], parent_plan["detail_rows"])
        child_workbook = load_workbook(resolver.resolve_artifact(child.run_id, "workbook").path)
        try:
            self.assertEqual(
                tuple(child_workbook["抽调明细"].iter_rows(values_only=True)), expected_detail_rows,
            )
        finally:
            child_workbook.close()
        expected_scripts = [dict(row) for row in parent_binding["script_rows"]]
        expected_scripts[2]["声音"] = "掌声收束后保留轻音乐"
        self.assertEqual(child_payloads["visual_binding"]["script_rows"], expected_scripts)
        for path, content in parent_files.items():
            self.assertEqual(path.read_bytes(), content)

    def test_sound_save_rejects_actual_missing_embedded_image_without_creating_child(self):
        fixture = self._fixture_with_reference_pairs()
        workspace, resolver = fixture.base.workspace, fixture.base.resolver
        scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project_id}
        initial = workspace.read_current(fixture.token, **scope)
        _, parent = workspace._current_context(fixture.token, **scope)
        parent_workbook = resolver.resolve_artifact(parent.run_id, "workbook").path
        parent_binding = resolver.resolve_stage_artifact(
            parent.run_id, "visual_binding", expected_manifest_sha256=parent.manifest.sha256,
        ).path
        parent_manifest = resolver.resolve_manifest(parent.run_id, expected_sha256=parent.manifest.sha256).path
        parent_files = {
            path: path.read_bytes() for path in (parent_workbook, parent_binding, parent_manifest)
        }
        parent_images = self._workbook_images(parent_workbook)
        self.assertEqual(len(parent_images), len(initial["shots"]))
        self.assertGreater(len(parent_images), 3)
        versions_before = fixture.projects.list_project_versions(fixture.token, fixture.project_id)
        changed = workspace.apply_table_change(
            fixture.token, **scope, expected_project_version=initial["project_version"],
            expected_revision=initial["revision"], shot_no="3", shot_id=initial["shots"][2]["shot_id"],
            field="sound", value="掌声收束后保留轻音乐", idempotency_key="missing-embedded-image-edit",
        )
        render = script_workspace_module.write_template_output_workbook
        removed_anchor = min(parent_images)

        def render_without_first_image(path, *args, **kwargs):
            render(path, *args, **kwargs)
            workbook = load_workbook(path)
            try:
                images = workbook["脚本"]._images
                self.assertEqual(len(images), len(parent_images))
                removed = images.pop(0)
                self.assertEqual(
                    (removed.anchor._from.row + 1, removed.anchor._from.col + 1), removed_anchor,
                )
                workbook.save(path)
            finally:
                workbook.close()

        with patch.object(
            script_workspace_module, "write_template_output_workbook", side_effect=render_without_first_image,
        ), self.assertRaises(WorkspaceServiceError) as caught:
            workspace.save_version(
                fixture.token, **scope, expected_project_version=initial["project_version"],
                expected_revision=changed["revision"], reason="漏嵌图片必须阻断保存",
                idempotency_key="missing-embedded-image-save",
            )
        self.assertEqual(caught.exception.code, "SCRIPT_VERSION_AUDIT_FAILED")
        audits = [
            path for path in (fixture.base.generated.root / "runs").glob(
                f"{fixture.project_id}/runs/*/formal-script.audit.json"
            ) if path.parent.name != parent.run_id
        ]
        self.assertEqual(len(audits), 1)
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "fail")
        self.assertEqual(audit["blocking_items"], ["embedded_image_count_mismatch"])
        image_check = next(item for item in audit["checks"] if item["检查项"] == "embedded_image_count_mismatch")
        self.assertEqual(image_check["级别"], "hard")
        self.assertIn(
            f"expected={len(parent_images)}, actual={len(parent_images) - 1}", image_check["详情"],
        )
        expected_remaining = {anchor: image for anchor, image in parent_images.items() if anchor != removed_anchor}
        self.assertEqual(self._workbook_images(audits[0].with_name("formal-script.xlsx")), expected_remaining)
        self.assertEqual(
            fixture.projects.list_project_versions(fixture.token, fixture.project_id), versions_before,
        )
        _, current = workspace._current_context(fixture.token, **scope)
        self.assertEqual(current.version_id, parent.version_id)
        self.assertEqual(workspace.read_current(fixture.token, **scope)["shots"], changed["shots"])
        for path, content in parent_files.items():
            self.assertEqual(path.read_bytes(), content)

    @staticmethod
    def _workbook_images(path):
        workbook = load_workbook(path)
        try:
            images = {}
            for image in workbook["脚本"]._images:
                anchor = (image.anchor._from.row + 1, image.anchor._from.col + 1)
                if anchor in images:
                    raise AssertionError(f"multiple embedded images at {anchor}")
                with Image.open(BytesIO(image._data())) as pixels:
                    images[anchor] = (pixels.size, sha256(pixels.convert("RGB").tobytes()).hexdigest())
            return images
        finally:
            workbook.close()


class RealUatTemplateColumnPreservationRegressionTests(unittest.TestCase):
    def test_original_eleven_columns_preserve_optional_text_intent_and_reference_gaps(self):
        # The expected contract comes from an explicit 11-column source, never
        # from a workbook produced by the renderer under test.
        headers = (
            None, None, "画面示意", "效果示意", "效果", "类型", "景别",
            "运镜", "画面描述", "声音", "备注",
        )
        temporary = tempfile.TemporaryDirectory(prefix="mplus-real-uat-template-columns-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "anonymous-eleven-column-template.xlsx"
        template_rows = [
            ["匿名结构", number, None, None, "直接切换", "地面", "全景", "固定",
             f"匿名第{number}镜现场", "轻音乐", f"匿名来源说明{number}"]
            for number in range(1, 18)
        ]
        generation_fixture.write_launch_library(source, template_rows, [])
        script_rows, detail_rows, expected_images = [], [], {}
        blank_shots = {1, 9, 17}
        workbook = load_workbook(source)
        try:
            sheet = workbook["模版"]
            self.assertEqual(sheet.max_column, 11)
            self.assertEqual(tuple(cell.value for cell in sheet[6]), headers)
            for number, row in enumerate(template_rows, start=1):
                image_path = ""
                if number not in blank_shots:
                    image_path = str(root / f"anonymous-reference-{number}.png")
                    with Image.new("RGB", (128, 72), (number * 11, 70, number * 13)) as image:
                        image.save(image_path)
                        expected_images[(number + 6, 3)] = (
                            image.size, sha256(image.tobytes()).hexdigest(),
                        )
                    sheet.add_image(SpreadsheetImage(image_path), f"C{number + 6}")
                script_rows.append({
                    "结构": row[0], "镜号": str(number), "画面示意": image_path,
                    "效果示意": "", "效果": row[4], "类型": row[5], "景别": row[6],
                    "运镜": row[7], "画面描述": row[8], "声音": row[9], "备注": row[10],
                    "时长(秒)": 3,
                })
                detail_rows.append({
                    "项目名称": "匿名原格式回归", "模板镜号": str(number),
                    "采用来源文件": source.name, "采用来源 sheet": "模版",
                    "采用来源行": number + 6,
                })
            workbook.save(source)
        finally:
            workbook.close()
        source_bytes = source.read_bytes()
        self.assertEqual(len(script_rows), 17)
        self.assertEqual(len(expected_images), 14)
        self.assertTrue(all("旁白" not in row and "字幕" not in row for row in script_rows))
        script_rows[2]["声音"] = "掌声收束后保留轻音乐"

        for phase, subtitle, expected_headers in (
            ("sound_only", None, headers),
            ("explicit_subtitle", "匿名第三镜字幕", (*headers, "字幕")),
            ("cleared_subtitle", "", (*headers, "字幕")),
        ):
            if subtitle is not None:
                script_rows[2]["字幕"] = subtitle
            with self.subTest(phase=phase):
                self.assertTrue(all("旁白" not in row for row in script_rows))
                self.assertEqual("字幕" in script_rows[2], subtitle is not None)
                output = root / f"{phase}.xlsx"
                write_template_output_workbook(
                    output, script_rows, detail_rows,
                    template_workbook_path=source, duration_seconds=51,
                )
                rendered = load_workbook(output)
                try:
                    sheet = rendered["脚本"]
                    self.assertEqual(sheet.max_column, len(expected_headers))
                    self.assertEqual(tuple(cell.value for cell in sheet[6]), expected_headers)
                    self.assertEqual(sheet.max_row, 23)
                    self.assertEqual(
                        [sheet.cell(row, 2).value for row in range(7, 24)],
                        [str(number) for number in range(1, 18)],
                    )
                    for number, script in enumerate(script_rows, start=1):
                        self.assertEqual(sheet.cell(number + 6, 9).value, script["画面描述"])
                        self.assertEqual(sheet.cell(number + 6, 10).value, script["声音"])
                        self.assertEqual(sheet.cell(number + 6, 11).value, script["备注"])
                    if subtitle is not None:
                        self.assertEqual(sheet["L9"].value, subtitle or None)
                        self.assertTrue(all(
                            sheet.cell(row, 12).value is None
                            for row in range(7, 24) if row != 9
                        ))
                finally:
                    rendered.close()
                actual_images = RealUatReferenceImageCountRegressionTests._workbook_images(output)
                self.assertEqual(actual_images, expected_images)
                self.assertEqual(
                    {number for number in range(1, 18) if (number + 6, 3) not in actual_images},
                    blank_shots,
                )
                self.assertEqual(source.read_bytes(), source_bytes)


class RealUatImageSourceRegressionTests(unittest.TestCase):
    def test_legacy_image_child_reuses_frozen_source_after_same_bytes_alias_upgrade(self):
        fixture = image_fixture.FormalImageWorkspaceTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        generated = fixture.base.generated
        new_source = generated.root / "packages" / "launch-core" / "6" / "sources"
        shutil.copytree(generated.source_root, new_source)
        workspace = FormalImageWorkspaceService(
            fixture.workspace.services,
            runs_root=generated.root / "runs", source_root=generated.source_root,
            source_root_resolver=lambda _: new_source,
        )
        scope = {"workspace_id": fixture.workspace_id, "project_id": fixture.project_id}
        current = workspace.read_current(fixture.token, **scope)
        self.assertGreaterEqual(len(current["shots"]), 3)
        project = fixture.projects.get_project(fixture.token, fixture.project_id)
        parent = fixture.delivery.resolve(
            fixture.token, fixture.project_id, project["current_version_id"],
            expected_project_version=current["project_version"],
        )
        manifest = fixture.resolver.read_manifest(parent.run_id, expected_sha256=parent.manifest.sha256)
        plan = read_script_plan(fixture.resolver.resolve_stage_artifact(
            parent.run_id, "script_plan", expected_manifest_sha256=parent.manifest.sha256,
        ).path)
        self.assertFalse(plan.constraints.get("formal_asset_package"))
        frozen_source = manifest["template_workbook"]
        self.assertNotEqual(Path(frozen_source["path"]).parent, new_source)
        self.assertEqual(
            Path(frozen_source["path"]).read_bytes(),
            (new_source / Path(frozen_source["path"]).name).read_bytes(),
        )
        saved = workspace.save_version(
            fixture.token, **scope,
            expected_project_version=current["project_version"],
            selections=tuple(
                ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                for item in current["shots"]
            ),
            idempotency_key="real-uat-image-legacy-alias",
        )
        self.assertEqual(saved["save_status"], "completed")
        self.assertTrue(all(item["confirmed"] for item in saved["shots"]))
        self.assertEqual(
            [item["selected_image_key"] for item in saved["shots"]],
            [item["selected_image_key"] for item in current["shots"]],
        )
        project = fixture.projects.get_project(fixture.token, fixture.project_id)
        child = fixture.delivery.resolve(fixture.token, fixture.project_id, project["current_version_id"])
        child_manifest = fixture.resolver.read_manifest(child.run_id, expected_sha256=child.manifest.sha256)
        self.assertEqual(child_manifest["template_workbook"], frozen_source)
        self.assertEqual(child_manifest["parent_run_id"], parent.run_id)
        workbook = load_workbook(fixture.resolver.resolve_artifact(child.run_id, "workbook").path)
        try:
            self.assertEqual(len(workbook["脚本"]._images), len(current["shots"]))
        finally:
            workbook.close()


if __name__ == "__main__":
    unittest.main()
