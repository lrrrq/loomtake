import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from mvp_v2.formal_admin_service import FormalAdminService
from mvp_v2.formal_runtime import build_formal_runtime
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    enqueue_outbox,
    new_id,
    utc_now,
)
from mvp_v2.local_ui import create_app


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FormalAdminServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "runtime" / "workspace.sqlite3"
        self.store = WorkspaceStore(self.database_path)
        self.store.initialize()
        self.identity = LocalIdentityAdapter(self.store)
        bootstrap = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="本机管理员",
            confirmed=True,
            token="admin-session-secret",
        )
        self.workspace_id = bootstrap["workspace_id"]
        self.owner_token = bootstrap["auth_token"]
        self.projects = ProjectService(self.store)
        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        self.project = self.projects.create_project(
            self.owner_token,
            workspace_id=self.workspace_id,
            name="匿名诊断项目",
            category_id="launch",
            expected_workspace_version=workspace["workspace_version"],
        )
        self.tasks = TaskService(self.store)
        session = self.tasks.create_agent_session(
            self.owner_token, project_id=self.project["project_id"],
        )
        self.task = self.tasks.create_task(
            self.owner_token,
            agent_session_id=session["agent_session_id"],
            task_type="formal_generate",
            payload_sha256=_sha("anonymous-task"),
            expected_project_version=self.project["project_version"],
        )
        self.tasks.transition_task(
            self.owner_token,
            task_id=self.task["task_id"],
            expected_task_version=self.task["task_version"],
            status="failed",
            details={"summary": "客户原文不应进入管理员 DTO"},
            error_code="PROVIDER_TIMEOUT",
        )
        with self.store.transaction() as connection:
            attempt = connection.execute(
                "SELECT * FROM task_attempts WHERE task_id=?", (self.task["task_id"],),
            ).fetchone()
            self.attempt_id = attempt["attempt_id"]
            self.checkpoint_id = new_id("checkpoint")
            connection.execute(
                """INSERT INTO stage_checkpoints(
                checkpoint_id, attempt_id, task_id, stage_id, status,
                input_ref, input_sha256, output_ref, output_sha256,
                error_code, created_at) VALUES (?, ?, ?, ?, 'failed', NULL, NULL, NULL, NULL, ?, ?)""",
                (
                    self.checkpoint_id, self.attempt_id, self.task["task_id"],
                    "script_generation", "PROVIDER_TIMEOUT", utc_now(),
                ),
            )
            connection.execute(
                """INSERT INTO provider_call_diagnostics(
                provider_call_id, attempt_id, task_id, stage_id, provider,
                status, duration_ms, error_code, input_summary, error_summary,
                output_ref, created_at) VALUES (?, ?, ?, ?, ?, 'failed', ?, ?, ?, ?, NULL, ?)""",
                (
                    new_id("provider_call"), self.attempt_id, self.task["task_id"],
                    "script_generation", "configured-provider", 1250, "PROVIDER_TIMEOUT",
                    "客户原文 /Users/example/private-fixture.txt",
                    "password=visible /Users/example/private-fixture.txt " + _sha("secret"),
                    utc_now(),
                ),
            )
            connection.execute(
                """INSERT INTO dead_letters(
                dead_letter_id, task_id, attempt_id, reason_code,
                payload_ref, payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("dead_letter"), self.task["task_id"], self.attempt_id,
                    "RETRY_EXHAUSTED", "artifact://dead-letter/anonymous",
                    _sha("dead-letter"), utc_now(),
                ),
            )
        self.admin = FormalAdminService(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def _editor_token(self) -> str:
        workspace = self.projects.get_workspace(self.owner_token, self.workspace_id)
        actor = self.identity.create_actor(
            self.owner_token,
            workspace_id=self.workspace_id,
            display_name="普通编辑",
            auth_source="local",
            role="editor",
            expected_workspace_version=workspace["workspace_version"],
        )
        return self.identity.issue_auth_session(
            actor_id=actor["actor_id"], token="editor-session-secret",
        )["auth_token"]

    def test_admin_diagnostics_are_scoped_and_redacted(self):
        overview = self.admin.overview(self.owner_token, workspace_id=self.workspace_id)
        detail = self.admin.task_detail(
            self.owner_token, workspace_id=self.workspace_id, task_id=self.task["task_id"],
        )

        self.assertEqual(overview["schema_version"], 27)
        self.assertEqual(overview["services"]["dead_letter"], 1)
        self.assertEqual(overview["services"]["provider_failures"], 1)
        self.assertEqual(detail["attempts"][0]["attempt_no"], 1)
        self.assertEqual(detail["checkpoints"][0]["stage_id"], "script_generation")
        serialized = json.dumps(detail, ensure_ascii=False)
        self.assertNotIn("客户原文", serialized)
        self.assertNotIn("/Users/example/private-fixture.txt", serialized)
        self.assertNotIn(_sha("secret"), serialized)
        self.assertNotIn("visible", serialized)
        self.assertIn("[REDACTED]", serialized)
        self.assertIn("[LOCAL_PATH]", serialized)

    def test_editor_cannot_read_or_recover_admin_tasks(self):
        token = self._editor_token()
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.admin.overview(token, workspace_id=self.workspace_id)
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.admin.propose_recovery(
                token,
                workspace_id=self.workspace_id,
                task_id=self.task["task_id"],
                action="retry_current_stage",
                reason="人工确认重试",
                expected_project_version=self.project["project_version"],
                idempotency_key="editor-retry",
            )

    def test_editor_retry_uses_same_attempt_authority_without_admin_recovery(self):
        token = self._editor_token()
        proposal = self.admin.propose_stage_retry(
            token,
            workspace_id=self.workspace_id,
            project_id=self.project["project_id"],
            requested_stage_id="script_generation",
            reason="普通阶段重试",
            expected_project_version=self.project["project_version"],
            idempotency_key="editor-stage-retry",
        )
        result = self.admin.confirm_stage_retry(
            token,
            workspace_id=self.workspace_id,
            proposal_id=proposal["proposal_id"],
            proposal_digest=proposal["proposal_digest"],
        )

        self.assertEqual(proposal["type"], "formal_stage_retry_proposal")
        self.assertEqual(result["type"], "formal_stage_retry_attempt_created")
        self.assertTrue(result["old_task_unchanged"])
        with self.store.connection() as connection:
            attempts = connection.execute(
                "SELECT origin, attempt_no FROM task_attempts WHERE task_id=? ORDER BY attempt_no",
                (self.task["task_id"],),
            ).fetchall()
            events = connection.execute(
                "SELECT event_type FROM outbox WHERE aggregate_id=? ORDER BY created_at",
                (self.task["task_id"],),
            ).fetchall()
        self.assertEqual([(row["origin"], row["attempt_no"]) for row in attempts], [
            ("initial", 1), ("retry", 2),
        ])
        self.assertIn("task.stage_retry_attempt_created", [row["event_type"] for row in events])

    def test_editor_retry_cannot_confirm_admin_checkpoint_recovery(self):
        proposal = self.admin.propose_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            task_id=self.task["task_id"],
            action="resume_checkpoint",
            reason="管理员检查点恢复",
            expected_project_version=self.project["project_version"],
            idempotency_key="admin-checkpoint-only",
            checkpoint_id=self.checkpoint_id,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.admin.confirm_stage_retry(
                self._editor_token(),
                workspace_id=self.workspace_id,
                proposal_id=proposal["proposal_id"],
                proposal_digest=proposal["proposal_digest"],
            )

    def test_admin_http_routes_enforce_role_and_return_real_recovery(self):
        self.admin.build_recovery_executor({})
        client = TestClient(create_app(formal_admin_service=self.admin))
        headers = {"X-MPlus-Agent-Auth": self.owner_token}
        access = client.get(
            f"/api/formal/admin/workspaces/{self.workspace_id}/access", headers=headers,
        )
        overview = client.get(
            f"/api/formal/admin/workspaces/{self.workspace_id}/overview", headers=headers,
        )
        proposal = client.post(
            f"/api/formal/admin/workspaces/{self.workspace_id}/tasks/{self.task['task_id']}/recovery-proposals",
            headers=headers,
            json={
                "action": "resume_checkpoint",
                "reason": "HTTP 管理员确认从检查点继续",
                "expected_project_version": self.project["project_version"],
                "idempotency_key": "http-recover-once",
                "checkpoint_id": self.checkpoint_id,
            },
        )
        confirmed = client.post(
            f"/api/formal/admin/workspaces/{self.workspace_id}/recovery-proposals/{proposal.json()['proposal_id']}/confirm",
            headers=headers,
            json={"proposal_digest": proposal.json()["proposal_digest"]},
        )
        editor_headers = {"X-MPlus-Agent-Auth": self._editor_token()}
        blocked = client.get(
            f"/api/formal/admin/workspaces/{self.workspace_id}/access", headers=editor_headers,
        )
        forbidden = client.get(
            f"/api/formal/admin/workspaces/{self.workspace_id}/overview", headers=editor_headers,
        )

        self.assertEqual(access.status_code, 200)
        self.assertEqual(access.json(), {"admin_console": "available"})
        self.assertEqual(overview.status_code, 200)
        self.assertEqual(proposal.status_code, 200)
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(confirmed.json()["status"], "accepted")
        self.assertEqual(confirmed.json()["execution"]["terminal_status"], "failed")
        self.assertEqual(
            confirmed.json()["execution"]["error_code"], "HANDLER_NOT_REGISTERED",
        )
        self.assertTrue(confirmed.json()["old_task_unchanged"])
        self.assertEqual(blocked.status_code, 200)
        self.assertEqual(blocked.json(), {"admin_console": "blocked"})
        self.assertEqual(forbidden.status_code, 403)

    def test_recovery_creates_immutable_child_attempt_without_mutating_task(self):
        proposal = self.admin.propose_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            task_id=self.task["task_id"],
            action="resume_checkpoint",
            reason="已确认 Provider 恢复，继续检查点",
            expected_project_version=self.project["project_version"],
            idempotency_key="recover-once",
            checkpoint_id=self.checkpoint_id,
        )
        result = self.admin.confirm_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            proposal_id=proposal["proposal_id"],
            proposal_digest=proposal["proposal_digest"],
        )
        replay = self.admin.confirm_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            proposal_id=proposal["proposal_id"],
            proposal_digest=proposal["proposal_digest"],
        )

        self.assertEqual(result["new_attempt_id"], replay["new_attempt_id"])
        self.assertFalse(result["replayed"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(result["old_task_status"], "failed")
        with self.store.connection() as connection:
            attempts = connection.execute(
                "SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no",
                (self.task["task_id"],),
            ).fetchall()
            task = connection.execute(
                "SELECT status, lock_version FROM tasks WHERE task_id=?", (self.task["task_id"],),
            ).fetchone()
            outbox = connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_type='task.recovery_attempt_created'",
            ).fetchone()[0]
        self.assertEqual([row["attempt_no"] for row in attempts], [1, 2])
        self.assertEqual(attempts[1]["parent_attempt_id"], attempts[0]["attempt_id"])
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["lock_version"], 1)
        self.assertEqual(outbox, 1)

    def test_recovery_digest_lock_idempotency_and_immutability(self):
        proposal = self.admin.propose_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            task_id=self.task["task_id"],
            action="retry_current_stage",
            reason="重试当前阶段",
            expected_project_version=self.project["project_version"],
            idempotency_key="same-key",
        )
        replay = self.admin.propose_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            task_id=self.task["task_id"],
            action="retry_current_stage",
            reason="重试当前阶段",
            expected_project_version=self.project["project_version"],
            idempotency_key="same-key",
        )
        self.assertEqual(proposal["proposal_id"], replay["proposal_id"])
        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.admin.propose_recovery(
                self.owner_token,
                workspace_id=self.workspace_id,
                task_id=self.task["task_id"],
                action="retry_current_stage",
                reason="不同原因",
                expected_project_version=self.project["project_version"],
                idempotency_key="same-key",
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROPOSAL_DIGEST_MISMATCH"):
            self.admin.confirm_recovery(
                self.owner_token,
                workspace_id=self.workspace_id,
                proposal_id=proposal["proposal_id"],
                proposal_digest="0" * 64,
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
            with self.store.transaction() as connection:
                connection.execute(
                    "UPDATE task_attempts SET reason='mutated' WHERE attempt_id=?",
                    (self.attempt_id,),
                )

    def test_attempt_checkpoint_and_provider_scope_cannot_cross_tasks(self):
        session = self.tasks.create_agent_session(
            self.owner_token, project_id=self.project["project_id"],
        )
        other = self.tasks.create_task(
            self.owner_token,
            agent_session_id=session["agent_session_id"],
            task_type="other_stage",
            payload_sha256=_sha("other-task"),
            expected_project_version=self.project["project_version"],
        )
        with self.store.connection() as connection:
            other_attempt = connection.execute(
                "SELECT attempt_id FROM task_attempts WHERE task_id=?", (other["task_id"],),
            ).fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "STAGE_CHECKPOINT_SCOPE_MISMATCH"):
            with self.store.transaction() as connection:
                connection.execute(
                    """INSERT INTO stage_checkpoints(
                    checkpoint_id, attempt_id, task_id, stage_id, status,
                    input_ref, input_sha256, output_ref, output_sha256,
                    error_code, created_at) VALUES (?, ?, ?, 'cross', 'failed', NULL, NULL, NULL, NULL, NULL, ?)""",
                    (new_id("checkpoint"), other_attempt, self.task["task_id"], utc_now()),
                )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "PROVIDER_CALL_SCOPE_MISMATCH"):
            with self.store.transaction() as connection:
                connection.execute(
                    """INSERT INTO provider_call_diagnostics(
                    provider_call_id, attempt_id, task_id, stage_id, provider,
                    status, duration_ms, error_code, input_summary, error_summary,
                    output_ref, created_at) VALUES (?, ?, ?, 'cross', 'configured-provider',
                    'failed', 1, 'CROSS_SCOPE', '', '', NULL, ?)""",
                    (new_id("provider_call"), other_attempt, self.task["task_id"], utc_now()),
                )

    def _confirmed_recovery_event(self, *, action="resume_checkpoint"):
        proposal = self.admin.propose_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            task_id=self.task["task_id"],
            action=action,
            reason="专项执行器测试",
            expected_project_version=self.project["project_version"],
            idempotency_key=f"executor-{action}",
            checkpoint_id=self.checkpoint_id if action == "resume_checkpoint" else "",
        )
        confirmed = self.admin.confirm_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            proposal_id=proposal["proposal_id"],
            proposal_digest=proposal["proposal_digest"],
        )
        with self.store.connection() as connection:
            event = connection.execute(
                """SELECT event_id, payload_json FROM outbox
                WHERE event_type='task.recovery_attempt_created'
                  AND json_extract(payload_json, '$.attempt_id')=?""",
                (confirmed["new_attempt_id"],),
            ).fetchone()
        self.assertIsNotNone(event)
        return confirmed["new_attempt_id"], event["event_id"]

    def test_recovery_executor_calls_injected_handler_and_records_success_terminal(self):
        attempt_id, event_id = self._confirmed_recovery_event()
        calls = []

        def handler(context):
            calls.append(context)
            return {
                "status": "succeeded",
                "output_ref": f"artifact://recovery/{context.attempt_id}",
                "output_sha256": "a" * 64,
            }

        executor = self.admin.build_recovery_executor(
            {"script_generation": handler},
        )
        result = executor.consume_outbox_event(event_id)

        self.assertEqual(result["terminal_status"], "succeeded")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].attempt_id, attempt_id)
        self.assertEqual(calls[0].stage_id, "script_generation")
        terminal = executor.terminal_for_attempt(attempt_id)
        self.assertEqual(terminal["status"], "succeeded")
        self.assertEqual(terminal["output_ref"], f"artifact://recovery/{attempt_id}")
        with self.store.connection() as connection:
            checkpoint = connection.execute(
                "SELECT attempt_id, status, output_ref, output_sha256 FROM stage_checkpoints WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            event = connection.execute(
                "SELECT status FROM outbox WHERE event_id=?", (event_id,),
            ).fetchone()
        self.assertEqual(tuple(checkpoint), (attempt_id, "completed", f"artifact://recovery/{attempt_id}", "a" * 64))
        self.assertEqual(event["status"], "published")

    def test_recovery_executor_records_failed_terminal_and_never_promotes_handler_error(self):
        attempt_id, event_id = self._confirmed_recovery_event()

        def handler(_context):
            raise RuntimeError("stage provider failed")

        executor = self.admin.build_recovery_executor(
            {"script_generation": handler},
        )
        result = executor.consume_outbox_event(event_id)

        self.assertEqual(result["terminal_status"], "failed")
        terminal = executor.terminal_for_attempt(attempt_id)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["error_code"], "HANDLER_ERROR")
        with self.store.connection() as connection:
            checkpoint = connection.execute(
                "SELECT attempt_id, status, error_code, output_ref FROM stage_checkpoints WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            task_event = connection.execute(
                """SELECT details_json FROM task_events WHERE task_id=?
                ORDER BY sequence_no DESC LIMIT 1""",
                (self.task["task_id"],),
            ).fetchone()
        self.assertEqual(tuple(checkpoint), (attempt_id, "failed", "HANDLER_ERROR", None))
        self.assertEqual(json.loads(task_event["details_json"])["recovery_terminal"], "failed")

    def test_recovery_executor_replay_and_crash_reconcile_do_not_repeat_side_effect(self):
        attempt_id, event_id = self._confirmed_recovery_event()
        calls = []

        def handler(context):
            calls.append(context.attempt_id)
            return {
                "status": "succeeded",
                "output_ref": f"artifact://recovery/{context.attempt_id}",
                "output_sha256": "b" * 64,
            }

        executor = self.admin.build_recovery_executor(
            {"script_generation": handler},
        )
        def crash_after_domain_write(*args, **kwargs):
            raise SystemExit("consumer crashed after domain write")

        with patch.object(executor, "_ack_event", side_effect=crash_after_domain_write):
            with self.assertRaises(SystemExit):
                executor.consume_outbox_event(event_id)

        replay = executor.consume_outbox_event(event_id)
        duplicate = executor.consume_outbox_event(event_id)

        self.assertEqual(replay["terminal_status"], "succeeded")
        self.assertTrue(replay["replayed"])
        self.assertTrue(duplicate["replayed"])
        self.assertEqual(calls, [attempt_id])
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM stage_checkpoints WHERE attempt_id=?", (attempt_id,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM outbox WHERE event_id=?", (event_id,),
                ).fetchone()[0],
                "published",
            )
            old_task = connection.execute(
                "SELECT status, lock_version FROM tasks WHERE task_id=?", (self.task["task_id"],),
            ).fetchone()
            old_attempt = connection.execute(
                "SELECT attempt_id, attempt_no, reason FROM task_attempts WHERE attempt_id=?",
                (self.attempt_id,),
            ).fetchone()
        self.assertEqual(tuple(old_task), ("failed", 1))
        self.assertEqual(old_attempt["attempt_id"], self.attempt_id)
        self.assertEqual(old_attempt["attempt_no"], 1)
        self.assertEqual(old_attempt["reason"], "初始执行")

    def test_bound_recovery_executor_consumes_confirmed_attempt_before_response(self):
        calls = []

        def handler(context):
            calls.append(context.attempt_id)
            return {
                "status": "succeeded",
                "output_ref": f"artifact://recovery/{context.attempt_id}",
                "output_sha256": "c" * 64,
            }

        self.admin.build_recovery_executor({"script_generation": handler})
        proposal = self.admin.propose_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            task_id=self.task["task_id"],
            action="resume_checkpoint",
            reason="运行中真实消费",
            expected_project_version=self.project["project_version"],
            idempotency_key="runtime-bound-consume",
            checkpoint_id=self.checkpoint_id,
        )

        confirmed = self.admin.confirm_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            proposal_id=proposal["proposal_id"],
            proposal_digest=proposal["proposal_digest"],
        )

        self.assertEqual(confirmed["status"], "accepted")
        self.assertEqual(confirmed["execution"]["terminal_status"], "succeeded")
        self.assertTrue(confirmed["execution"]["checkpoint_id"])
        self.assertEqual(calls, [confirmed["new_attempt_id"]])

    def test_runtime_restart_consumes_pending_duplicate_events_once(self):
        attempt_id, event_id = self._confirmed_recovery_event()
        with self.store.transaction() as connection:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM outbox WHERE event_id=?", (event_id,),
            ).fetchone()[0])
            duplicate_event_id = enqueue_outbox(
                connection,
                aggregate_type="task",
                aggregate_id=self.task["task_id"],
                event_type="task.recovery_attempt_created",
                payload=payload,
            )
        calls = []

        def handler(context):
            calls.append(context.attempt_id)
            return {
                "status": "succeeded",
                "output_ref": f"artifact://recovery/{context.attempt_id}",
                "output_sha256": "d" * 64,
            }

        data_root = self.database_path.parent
        runtime = build_formal_runtime(
            {
                "data_root": str(data_root),
                "runs_root": str(data_root / "runs"),
                "source_root": str(data_root / "source"),
                "formal_agent_read_only": True,
            },
            recovery_handlers={"script_generation": handler},
        )

        self.assertEqual(calls, [attempt_id])
        self.assertEqual(runtime.recovery.terminal_for_attempt(attempt_id)["status"], "succeeded")
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM stage_checkpoints WHERE attempt_id=?", (attempt_id,),
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM outbox WHERE event_id IN (?, ?) AND status='published'",
                (event_id, duplicate_event_id),
            ).fetchone()[0], 2)

        restarted = build_formal_runtime(
            {
                "data_root": str(data_root),
                "runs_root": str(data_root / "runs"),
                "source_root": str(data_root / "source"),
                "formal_agent_read_only": True,
            },
            recovery_handlers={"script_generation": lambda _context: self.fail("重启不得重复执行")},
        )
        self.assertEqual(restarted.recovery.terminal_for_attempt(attempt_id)["status"], "succeeded")

    def test_stable_runtime_without_stage_handler_records_failed_terminal(self):
        data_root = self.database_path.parent
        runtime = build_formal_runtime({
            "data_root": str(data_root),
            "runs_root": str(data_root / "runs"),
            "source_root": str(data_root / "source"),
            "formal_agent_read_only": True,
        })
        proposal = runtime.admin.propose_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            task_id=self.task["task_id"],
            action="resume_checkpoint",
            reason="无 handler 时必须返回真实失败终态",
            expected_project_version=self.project["project_version"],
            idempotency_key="runtime-handler-missing",
            checkpoint_id=self.checkpoint_id,
        )

        confirmed = runtime.admin.confirm_recovery(
            self.owner_token,
            workspace_id=self.workspace_id,
            proposal_id=proposal["proposal_id"],
            proposal_digest=proposal["proposal_digest"],
        )

        self.assertEqual(confirmed["status"], "accepted")
        self.assertEqual(confirmed["execution"]["terminal_status"], "failed")
        self.assertEqual(confirmed["execution"]["error_code"], "HANDLER_NOT_REGISTERED")
        terminal = runtime.recovery.terminal_for_attempt(confirmed["new_attempt_id"])
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["error_code"], "HANDLER_NOT_REGISTERED")
        with self.store.connection() as connection:
            checkpoint = connection.execute(
                "SELECT status, error_code FROM stage_checkpoints WHERE attempt_id=?",
                (confirmed["new_attempt_id"],),
            ).fetchone()
        self.assertEqual(tuple(checkpoint), ("failed", "HANDLER_NOT_REGISTERED"))


if __name__ == "__main__":
    unittest.main()
