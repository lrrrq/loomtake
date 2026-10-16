from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mvp_v2 import knowledge_service
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from tests.test_mvp_v2_knowledge_service import KnowledgeServiceFixture


class ProjectConventionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = KnowledgeServiceFixture(self.root)
        self.service = self.fixture.knowledge
        self.project_id = self.fixture.launch["project_id"]
        self.workspace_id = self.fixture.workspace_id
        self.member_auth = self.fixture.editor_auth
        self.viewer_auth = self._create_member("viewer", "匿名查看者")
        self.other_member_auth = self._create_member("editor", "匿名协作编辑")
        self.fixture.projects.add_brief_snapshot(
            self.fixture.auth, project_id=self.project_id,
            brief_ref="artifact://brief/anonymous-confirmed-source",
            brief_sha256="a" * 64, expected_project_version=0,
        )
        self.original_brief = self._brief_rows()

    def tearDown(self):
        self.temporary.cleanup()

    def _create_member(self, role, display_name):
        workspace = self.fixture.projects.get_workspace(
            self.fixture.auth, self.workspace_id,
        )
        actor = self.fixture.identity.create_actor(
            self.fixture.auth, workspace_id=self.workspace_id,
            project_id=self.project_id, display_name=display_name,
            auth_source="local", role=role,
            expected_workspace_version=workspace["workspace_version"],
        )
        return self.fixture.identity.issue_auth_session(actor_id=actor["actor_id"])["auth_token"]

    def _version(self):
        return self.service.get_state(
            self.member_auth, self.workspace_id, project_id=self.project_id,
        )["knowledge_version"]

    def _add(self, statement="本项目开头克制，减少口号式旁白", **changes):
        arguments = {
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "statement": statement,
            "evidence_refs": ["artifact://project-conversation/anonymous/message-1"],
            "expected_knowledge_version": self._version(),
            "confirmed": True,
        }
        arguments.update(changes)
        return self.service.add_project_convention(self.member_auth, **arguments)

    def _correct(self, convention, statement="开头克制，结尾可以使用一句品牌口号", **changes):
        arguments = {
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "convention_id": convention["convention_id"],
            "statement": statement,
            "evidence_refs": ["artifact://project-conversation/anonymous/message-5"],
            "expected_knowledge_version": self._version(),
            "reason": "项目成员明确修正适用范围",
            "confirmed": True,
        }
        arguments.update(changes)
        return self.service.correct_project_convention(self.other_member_auth, **arguments)

    def _stop(self, convention, status="disabled", **changes):
        arguments = {
            "workspace_id": self.workspace_id,
            "project_id": self.project_id,
            "convention_id": convention["convention_id"],
            "status": status,
            "expected_knowledge_version": self._version(),
            "reason": "用户要求不再用于后续创作",
        }
        arguments.update(changes)
        return self.service.set_project_convention_status(self.member_auth, **arguments)

    def _effective(self):
        return self.service.list_effective_rules(self.member_auth, self.project_id)["rules"]

    def _brief_rows(self):
        with self.fixture.store.connection() as connection:
            return {
                "project": dict(connection.execute(
                    "SELECT * FROM projects WHERE project_id = ?", (self.project_id,),
                ).fetchone()),
                "brief": [dict(row) for row in connection.execute(
                    "SELECT * FROM project_brief_snapshots WHERE project_id = ?",
                    (self.project_id,),
                ).fetchall()],
            }

    def _knowledge_rows(self):
        with self.fixture.store.connection() as connection:
            return {
                table: [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]
                for table in (
                    "knowledge_candidates", "knowledge_approvals", "knowledge_rules",
                    "knowledge_project_convention_events", "knowledge_states", "outbox",
                )
            }

    def test_project_editor_saves_shared_convention_with_sources_and_restart_readback(self):
        convention = self._add()

        self.assertEqual(convention["scope"], "project")
        self.assertEqual(convention["target_id"], self.project_id)
        self.assertEqual(convention["status"], "active")
        self.assertEqual(convention["knowledge_version"], 1)
        self.assertEqual(convention["approved_by_actor_id"], self.fixture.editor_actor_id)
        self.assertEqual(convention["evidence_refs"], ["artifact://project-conversation/anonymous/message-1"])
        self.assertEqual([item["rule_id"] for item in self._effective()], [convention["rule_id"]])

        restarted = KnowledgeService(WorkspaceStore(self.root / "workspace.sqlite3"))
        listed = restarted.list_project_conventions(self.other_member_auth, self.project_id)
        self.assertEqual(listed["conventions"], [convention])
        self.assertEqual(listed["knowledge_version"], 1)
        self.assertNotIn("inactive_statements", listed)
        self.assertEqual(self._brief_rows(), self.original_brief)

    def test_correction_retires_only_old_revision_and_retains_its_source(self):
        first = self._add()
        unrelated = self._add("本项目产品特写保持自然光")
        corrected = self._correct(first)

        self.assertEqual(corrected["convention_id"], first["convention_id"])
        self.assertNotEqual(corrected["rule_id"], first["rule_id"])
        self.assertEqual(corrected["created_at"], first["created_at"])
        self.assertEqual(corrected["knowledge_version"], 3)
        self.assertEqual(
            {item["rule_id"] for item in self._effective()},
            {corrected["rule_id"], unrelated["rule_id"]},
        )
        audit = self.service.list_project_conventions(
            self.member_auth, self.project_id, include_inactive=True,
        )
        self.assertEqual(len(audit["inactive_statements"]), 1)
        retired = audit["inactive_statements"][0]
        self.assertEqual(retired["statement"], first["statement"])
        self.assertEqual(retired["evidence_refs"], first["evidence_refs"])
        self.assertEqual(retired["status"], "superseded")
        self.assertEqual(retired["rule_id"], first["rule_id"])
        candidate_states = {
            item["candidate_id"]: item["status"]
            for item in self.service.list_candidates(self.fixture.auth, self.workspace_id)
        }
        self.assertEqual(candidate_states[first["candidate_id"]], "superseded")
        self.assertEqual(candidate_states[corrected["candidate_id"]], "active")
        self.assertEqual(self._brief_rows(), self.original_brief)

    def test_stop_then_delete_removes_every_revision_from_effective_context_and_preserves_audit(self):
        first = self._add()
        corrected = self._correct(first)
        latest = self._correct(corrected, statement="保留项目结尾，开头完全不出现口号")
        kept = self._add("本项目中段每个产品卖点都有实际演示")
        self._stop(latest)

        self.assertEqual([item["rule_id"] for item in self._effective()], [kept["rule_id"]])
        active = self.service.list_project_conventions(self.member_auth, self.project_id)
        self.assertEqual([item["convention_id"] for item in active["conventions"]], [kept["convention_id"]])
        disabled = self.service.list_project_conventions(
            self.member_auth, self.project_id, include_inactive=True,
        )
        self.assertEqual(len(disabled["inactive_statements"]), 3)
        self.assertEqual(disabled["inactive_statements"][-1]["status"], "disabled")

        deleted = self._stop(latest, "deleted")
        self.assertEqual(deleted["status"], "deleted")
        self.assertTrue(deleted["audit_preserved"])
        restarted = KnowledgeService(WorkspaceStore(self.root / "workspace.sqlite3"))
        audit = restarted.list_project_conventions(self.member_auth, self.project_id, include_inactive=True)
        self.assertEqual(audit["inactive_statements"][-1]["status"], "deleted")
        self.assertEqual(
            {item["statement"] for item in audit["inactive_statements"]},
            {first["statement"], corrected["statement"], latest["statement"]},
        )
        self.assertEqual([item["rule_id"] for item in self._effective()], [kept["rule_id"]])
        with self.fixture.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM knowledge_rules").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM knowledge_project_convention_events").fetchone()[0], 6)
            for table in ("knowledge_candidates", "knowledge_approvals", "knowledge_rules", "knowledge_project_convention_events"):
                with self.subTest(table=table):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                        connection.execute(f"UPDATE {table} SET created_at = created_at")
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                        connection.execute(f"DELETE FROM {table}")
        self.assertEqual(self._brief_rows(), self.original_brief)

    def test_viewer_can_read_but_cannot_add_correct_or_stop(self):
        convention = self._add()
        self.assertEqual(
            self.service.list_project_conventions(self.viewer_auth, self.project_id)["conventions"],
            [convention],
        )
        operations = (
            (self.service.add_project_convention, {"statement": "不能写", "evidence_refs": convention["evidence_refs"], "confirmed": True}),
            (self.service.correct_project_convention, {"convention_id": convention["convention_id"], "statement": "不能改", "evidence_refs": convention["evidence_refs"], "reason": "尝试修改", "confirmed": True}),
            (self.service.set_project_convention_status, {"convention_id": convention["convention_id"], "status": "deleted", "reason": "尝试删除"}),
        )
        before = self._knowledge_rows()
        for operation, fields in operations:
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                    operation(
                        self.viewer_auth, workspace_id=self.workspace_id,
                        project_id=self.project_id, expected_knowledge_version=1, **fields,
                    )
        self.assertEqual(self._knowledge_rows(), before)

    def test_project_scope_cannot_be_spoofed_or_read_from_another_project(self):
        convention = self._add()
        other_project_id = self.fixture.exhibition["project_id"]
        self.assertEqual(
            self.service.list_project_conventions(self.fixture.auth, other_project_id, include_inactive=True)["conventions"], [],
        )
        self.assertEqual(self.service.list_effective_rules(self.fixture.auth, other_project_id)["rules"], [])
        before = self._knowledge_rows()
        for operation in (
            lambda: self.service.list_project_conventions(self.member_auth, other_project_id, include_inactive=True),
            lambda: self._add(project_id=other_project_id),
            lambda: self._add(workspace_id="workspace-other"),
            lambda: self._correct(convention, project_id=other_project_id),
            lambda: self._stop(convention, project_id=other_project_id),
        ):
            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                operation()
        with self.assertRaisesRegex(WorkspaceServiceError, "NOT_FOUND"):
            self.service.set_project_convention_status(
                self.fixture.auth, workspace_id=self.workspace_id, project_id=other_project_id,
                convention_id=convention["convention_id"], status="deleted",
                expected_knowledge_version=1, reason="跨项目ID不可使用",
            )
        self.assertEqual(self._knowledge_rows(), before)

    def test_company_and_legacy_approved_project_rules_keep_existing_approval_semantics(self):
        company_candidate = self.fixture.propose(scope="company", statement="公司事实来源必须经过核对")
        company = self.fixture.approve(company_candidate)
        legacy_candidate = self.fixture.propose(statement="旧项目经验仍需要原审批流程")
        legacy = self.fixture.approve(legacy_candidate)
        pending = self.fixture.propose(scope="company", statement="待公司管理员批准的知识")
        company_rows = self._knowledge_rows()
        convention = self._add()
        self._correct(convention)
        self._stop(convention, "deleted")

        self.assertEqual({item["rule_id"] for item in self._effective()}, {company["rule_id"], legacy["rule_id"]})
        self.assertEqual(
            [item["rule_id"] for item in self.service.list_effective_rules(self.fixture.auth, self.fixture.exhibition["project_id"])["rules"]],
            [company["rule_id"]],
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.service.approve_candidate(
                self.member_auth, workspace_id=self.workspace_id, project_id=self.project_id,
                candidate_id=pending["candidate_id"], level="company", review_evidence="编辑不能批准公司知识",
                expected_knowledge_version=self._version(),
            )
        for existing_rule in (company, legacy):
            with self.assertRaisesRegex(WorkspaceServiceError, "NOT_FOUND"):
                self._stop({"convention_id": existing_rule["rule_id"]}, "deleted")
        after = self._knowledge_rows()
        for table in ("knowledge_candidates", "knowledge_approvals", "knowledge_rules"):
            self.assertEqual(after[table][:len(company_rows[table])], company_rows[table])
        self.assertTrue(all(item["scope"] == "project" for item in after["knowledge_rules"][2:]))
        self.assertEqual(self._brief_rows(), self.original_brief)

    def test_stale_write_and_unconfirmed_memory_write_do_not_leave_partial_rows(self):
        convention = self._add()
        before = self._knowledge_rows()
        for operation in (
            lambda: self._add(expected_knowledge_version=0),
            lambda: self._correct(convention, expected_knowledge_version=0),
            lambda: self._stop(convention, expected_knowledge_version=0),
        ):
            with self.assertRaisesRegex(WorkspaceServiceError, "KNOWLEDGE_VERSION_CONFLICT"):
                operation()
        for confirmation in (False, None, "true", 1):
            with self.subTest(confirmation=confirmation):
                with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_CONVENTION_CONFIRMATION_REQUIRED"):
                    self._add(confirmed=confirmation)
                with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_CONVENTION_CONFIRMATION_REQUIRED"):
                    self._correct(convention, confirmed=confirmation)
        self.assertEqual(self._knowledge_rows(), before)

    def test_failure_after_rule_creation_rolls_back_rule_event_state_and_outbox(self):
        convention = self._add()
        before = self._knowledge_rows()
        with patch("mvp_v2.knowledge_service.enqueue_outbox", side_effect=RuntimeError("anonymous storage failure")):
            for operation in (lambda: self._add("第二条约定"), lambda: self._correct(convention), lambda: self._stop(convention)):
                with self.assertRaisesRegex(RuntimeError, "storage failure"):
                    operation()
        self.assertEqual(self._knowledge_rows(), before)
        self.assertEqual(self._brief_rows(), self.original_brief)

    def test_inactive_convention_cannot_be_reenabled_by_correction_or_repeat_stop(self):
        convention = self._add()
        disabled = self._stop(convention)
        self._add("另一条有效约定")
        before = self._knowledge_rows()
        no_op = self._stop(convention)
        self.assertEqual(no_op["knowledge_version"], 3)
        self.assertEqual(no_op["revision_version"], disabled["revision_version"])
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_CONVENTION_INACTIVE"):
            self._correct(convention)
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self._stop(convention, status="active")
        self.assertEqual(self._knowledge_rows(), before)
        self._stop(convention, "deleted")
        before_deleted = self._knowledge_rows()
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_CONVENTION_INACTIVE"):
            self._stop(convention, "disabled")
        self.assertEqual(self._knowledge_rows(), before_deleted)

    def test_concurrent_stop_does_not_label_an_old_snapshot_with_the_new_knowledge_version(self):
        convention = self._add()
        read_rows = knowledge_service._latest_conventions

        def stop_after_rows(*args):
            rows = read_rows(*args)
            self._stop(convention)
            return rows

        with patch("mvp_v2.knowledge_service._latest_conventions", side_effect=stop_after_rows):
            previous = self.service.list_project_conventions(
                self.member_auth, self.project_id, include_inactive=True,
            )
        self.assertEqual(previous["knowledge_version"], 1)
        self.assertEqual(previous["conventions"][0]["status"], "active")
        self.assertEqual(previous["inactive_statements"], [])
        current = self.service.list_project_conventions(
            self.member_auth, self.project_id, include_inactive=True,
        )
        self.assertEqual(current["knowledge_version"], 2)
        self.assertEqual(current["conventions"][0]["status"], "disabled")
        self.assertEqual(current["inactive_statements"][0]["statement"], convention["statement"])
        self.assertEqual(self._effective(), [])


if __name__ == "__main__":
    unittest.main()
