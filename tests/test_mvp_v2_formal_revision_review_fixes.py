from __future__ import annotations

import sqlite3
import unittest

from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_script_workspace as workspace_tests


class FormalRevisionReviewFixesTests(unittest.TestCase):
    def setUp(self):
        self.case = workspace_tests.FormalScriptWorkspaceTests("runTest")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.workspace = self.case.workspace
        self.token = self.case.generated.token
        self.workspace_id = self.case.generated.workspace_id
        self.project_id = self.case.generated.project["project_id"]
        self.tasks = TaskService(self.case.store)
        self.revisions = FormalRevisionService(
            projects=self.case.projects, tasks=self.tasks,
            workspace=self.workspace, writes_enabled=True,
        )

    def _current(self):
        return self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )

    def _parameters(self, proposal):
        return next(
            event["details"]["internal_formal_parameters"]
            for event in reversed(self.tasks.list_task_events(self.token, proposal["task_id"]))
            if event["details"].get("internal_formal_parameters")
        )

    def _project(self):
        return self.case.projects.get_project(self.token, self.project_id)

    def _publication_snapshot(self):
        with self.case.store.connection() as connection:
            snapshot = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
                for table in (
                    "project_runs", "project_versions", "version_review_draft_submissions",
                    "formal_version_feedback_records",
                )
            }
            snapshot["publication_outbox"] = [tuple(row) for row in connection.execute(
                """SELECT * FROM outbox WHERE event_type IN
                ('project.run_bound', 'project.version_created')"""
            )]
            return snapshot

    def test_feedback_failure_does_not_commit_child_or_submission(self):
        current = self._current()
        proposal = self.revisions.propose_change(
            self.token, project_id=self.project_id,
            expected_project_version=current["project_version"],
            expected_revision=current["revision"],
            operation={
                "operation": "replace_field", "shot_no": "1",
                "shot_id": current["shots"][0]["shot_id"],
                "field": "subtitle", "value": "匿名反馈事务回滚验证",
            },
            idempotency_key="feedback-atomicity",
        )
        parameters = self._parameters(proposal)
        before_project = self._project()
        before = self._publication_snapshot()
        with self.case.store.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER feedback_abort BEFORE INSERT ON formal_version_feedback_records
                BEGIN SELECT RAISE(ABORT, 'TEST_FEEDBACK_ABORT'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "TEST_FEEDBACK_ABORT"):
            self.revisions.execute(self.token, project=before_project, parameters=parameters)
        self.assertEqual(self._project()["current_version_id"], before_project["current_version_id"])
        self.assertEqual(self._project()["project_version"], before_project["project_version"])
        self.assertEqual(self._publication_snapshot(), before)
        # The failed submission retains the editable draft, so the user can save it again.
        self.assertEqual(self._current()["shots"][0]["subtitle"], "匿名反馈事务回滚验证")
        self.assertTrue(self._current()["can_save_version"])

    def test_feedback_evidence_belongs_to_the_committed_child(self):
        current = self._current()
        proposal = self.revisions.propose_change(
            self.token, project_id=self.project_id,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            operation={
                "operation": "replace_field", "shot_no": "1",
                "shot_id": current["shots"][0]["shot_id"],
                "field": "subtitle", "value": "匿名反馈正确关联验证",
            }, idempotency_key="feedback-child-evidence",
        )
        parent = self._project()["current_version_id"]
        result = self.revisions.execute(
            self.token, project=self._project(), parameters=self._parameters(proposal),
        )
        self.assertEqual(result["status"], "completed")
        child = self._project()["current_version_id"]
        self.assertNotEqual(parent, child)
        with self.case.store.connection() as connection:
            rows = connection.execute(
                """SELECT f.base_version_id, f.child_version_id, f.payload_ref, f.payload_sha256,
                s.revision_report_ref, s.revision_report_sha256
                FROM formal_version_feedback_records f
                JOIN version_review_draft_submissions s
                ON s.materialized_version_id=f.child_version_id"""
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), (parent, child, rows[0][4], rows[0][5], rows[0][4], rows[0][5]))

    def test_proposal_cannot_replace_a_shot_protected_after_proposal(self):
        current = self._current()
        proposal = self.revisions.propose(
            self.token, agent_session_id="", project_id=self.project_id,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            instruction="把第1镜字幕修改为旧提案不应覆盖受保护镜头",
            entrypoint="assistant", draft_only=True, idempotency_key="late-lock-proposal",
        )
        self._protect(current)
        with self.assertRaises(WorkspaceServiceError) as raised:
            self.revisions.execute(
                self.token, project=self._project(), parameters=self._parameters(proposal),
            )
        self.assertEqual(raised.exception.code, "SCRIPT_TARGET_LOCKED")
        self.assertEqual(self._current()["shots"][0]["subtitle"], current["shots"][0]["subtitle"])
        self.assertEqual(self._current()["revision"], current["revision"])

    def test_assistant_cannot_clear_or_delete_protected_shot(self):
        current = self._current()
        current = self.workspace.apply_table_change(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            shot_no="1", shot_id=current["shots"][0]["shot_id"],
            field="subtitle", value="用户已确认的字幕", idempotency_key="manual-before-protection",
        )
        self._protect(current)
        protected = self._current()
        for kind in ("clear_optional_field", "delete_shot"):
            with self.subTest(kind=kind):
                with self.assertRaises(WorkspaceServiceError) as raised:
                    self.workspace.apply_revision_operation(
                        self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                        expected_project_version=current["project_version"],
                        expected_revision=current["revision"],
                        operation={
                            "operation": kind, "shot_no": "1",
                            "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
                        }, idempotency_key=f"protected-{kind}",
                    )
                self.assertEqual(raised.exception.code, "SCRIPT_TARGET_LOCKED")
                self.assertEqual(self._current()["shots"], protected["shots"])
                self.assertEqual(self._current()["revision"], current["revision"])

    def _protect(self, current):
        self.workspace.set_assistant_locks(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=current["project_version"],
            locked_shot_ids=[current["shots"][0]["shot_id"]], idempotency_key="protect-shot",
        )
