import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from mvp_v2.formal_admin_service import FormalAdminService
from mvp_v2.formal_generation_service import FormalGenerationBlocked, FormalTextProviderPolicy
from tests import test_mvp_v2_formal_generation_service as fixture_module


class GenerationRecoveryTests(unittest.TestCase):
    def test_confirmed_retry_generates_real_workbook_and_preserves_failed_task(self):
        self._exercise_retry(interrupt_receipt=False)

    def test_retry_reconciles_saved_version_after_receipt_interruption(self):
        self._exercise_retry(interrupt_receipt=True)

    def _exercise_retry(self, *, interrupt_receipt):
        f = fixture_module.FormalGenerationServiceTests("runTest")
        f.setUp()
        self.addCleanup(f.doCleanups)
        calls = []
        writer = f._provider("writer", calls)
        failed_once = []

        def flaky_writer(*args):
            if not failed_once:
                failed_once.append(True)
                raise RuntimeError("anonymous transient provider failure")
            return writer(*args)

        service = f._service(FormalTextProviderPolicy(
            writer=flaky_writer, editor=f._provider("editor", calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        ))
        plan = service.prepare_plan(f.token, workspace_id=f.workspace_id,
            project_id=f.project["project_id"], expected_project_version=2, generation_mode="text_only_draft")
        f.expected_shot_count = plan["shot_count"]
        with self.assertRaises(FormalGenerationBlocked):
            service.confirm_plan_and_generate(f.token, workspace_id=f.workspace_id,
                project_id=f.project["project_id"], plan_id=plan["plan_id"], expected_project_version=3)
        failed_task = service.services.tasks.get_task(f.token, plan["plan_id"])
        self.assertEqual(failed_task["status"], "blocked")
        self.assertEqual(f._table_count("project_versions"), 0)
        operations = FormalAdminService(f.store)
        recovery = operations.build_recovery_executor(
            {"formal_generation_plan": service.recover_confirmed_generation},
            authenticated_stages=frozenset({"formal_generation_plan"}),
        )
        proposed = operations.propose_stage_retry(f.token, workspace_id=f.workspace_id,
            project_id=f.project["project_id"], requested_stage_id="formal_generation_plan",
            reason="重试刚才失败的生成", expected_project_version=3, idempotency_key="anonymous-retry")
        def confirm():
            return operations.confirm_stage_retry(f.token, workspace_id=f.workspace_id,
                proposal_id=proposed["proposal_id"], proposal_digest=proposed["proposal_digest"])
        if interrupt_receipt:
            with patch.object(recovery, "_persist_terminal", side_effect=RuntimeError("anonymous receipt interruption")):
                with self.assertRaisesRegex(RuntimeError, "receipt interruption"):
                    confirm()
            self.assertEqual(f._table_count("project_versions"), 1)
            # A restart does not invent credentials or mark pending work failed.
            self.assertEqual(recovery.consume_pending(), [])
            self.assertEqual(len(calls), 2)
        confirmed = confirm()
        self.assertEqual(confirmed["execution"]["terminal_status"], "succeeded", confirmed)
        terminal = recovery.terminal_for_attempt(confirmed["new_attempt_id"])
        project = f.projects.get_project(f.token, f.project["project_id"])
        version = f.projects.get_project_version(f.token, project["current_version_id"])
        artifact = service.services.artifacts.resolve_artifact(version["run_id"], "workbook")
        self.assertEqual((terminal["output_ref"], terminal["output_sha256"]), (artifact.ref, artifact.sha256))
        book = load_workbook(artifact.path)
        self.assertGreater(book["脚本"].max_row, 8)
        book.close()
        self.assertEqual(service.services.tasks.get_task(f.token, plan["plan_id"]), failed_task)
        replay = recovery.consume_outbox_event(confirmed["execution"]["event_id"], auth_token=f.token)
        self.assertTrue(replay["replayed"])
        self.assertEqual(f._table_count("project_versions"), 1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(confirm()["execution"]["replayed"])
