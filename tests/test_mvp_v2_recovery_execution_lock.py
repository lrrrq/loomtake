"""Two executor instances must never run the same recovery attempt twice."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
from threading import Event
import unittest

from mvp_v2.formal_recovery_executor import FormalRecoveryExecutor
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from tests import test_mvp_v2_formal_admin_service as fixture_module


class RecoveryExecutionLockTests(unittest.TestCase):
    def test_two_executors_exclude_live_attempt_then_replay_one_terminal(self):
        fixture = fixture_module.FormalAdminServiceTests("runTest")
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        attempt_id, event_id = fixture._confirmed_recovery_event()
        entered, release = Event(), Event()
        calls = []

        def handler(context):
            calls.append(context.attempt_id)
            if len(calls) == 1:
                entered.set()
                if not release.wait(timeout=10):
                    raise RuntimeError("anonymous recovery test did not release handler")
            return {"status": "succeeded", "output_ref": "artifact://run/anonymous/recovered",
                    "output_sha256": hashlib.sha256(b"anonymous output").hexdigest()}

        first = FormalRecoveryExecutor(fixture.store, handlers={"script_generation": handler})
        second = FormalRecoveryExecutor(WorkspaceStore(fixture.database_path), handlers={"script_generation": handler})
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(first.consume_outbox_event, event_id)
            try:
                self.assertTrue(entered.wait(timeout=5))
                with self.assertRaises(WorkspaceServiceError) as rejected:
                    second.consume_outbox_event(event_id)
                self.assertEqual(rejected.exception.code, "RECOVERY_EXECUTION_IN_PROGRESS")
                self.assertEqual(calls, [attempt_id])
                self.assertIsNone(second.terminal_for_attempt(attempt_id))
            finally:
                release.set()
            result = running.result(timeout=10)
        self.assertEqual(result["terminal_status"], "succeeded")
        replay = second.consume_outbox_event(event_id)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["terminal_status"], "succeeded")
        self.assertEqual(replay["checkpoint_id"], result["checkpoint_id"])
        self.assertEqual(calls, [attempt_id])
        with fixture.store.connection() as connection:
            rows = connection.execute("SELECT * FROM stage_checkpoints WHERE attempt_id=?", (attempt_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "completed")
        self.assertIsNone(rows[0]["error_code"])


if __name__ == "__main__":
    unittest.main()
