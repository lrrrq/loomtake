from __future__ import annotations

import sqlite3
from pathlib import Path
import tempfile
import unittest

from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.project_service import ProjectService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class KnowledgeServiceFixture:
    def __init__(self, root: Path):
        self.store = WorkspaceStore(root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="anonymous-knowledge-owner-token",
        )
        self.auth = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.launch = self.projects.create_project(
            self.auth,
            workspace_id=self.workspace_id,
            name="匿名发布会",
            category_id="launch",
            expected_workspace_version=0,
        )
        self.exhibition = self.projects.create_project(
            self.auth,
            workspace_id=self.workspace_id,
            name="匿名展会",
            category_id="exhibition",
            expected_workspace_version=1,
        )
        workspace = self.projects.get_workspace(self.auth, self.workspace_id)
        editor = self.identity.create_actor(
            self.auth,
            workspace_id=self.workspace_id,
            project_id=self.launch["project_id"],
            display_name="匿名编辑",
            auth_source="local",
            role="editor",
            expected_workspace_version=workspace["workspace_version"],
        )
        self.editor_actor_id = editor["actor_id"]
        self.editor_auth = self.identity.issue_auth_session(
            actor_id=editor["actor_id"], token="anonymous-knowledge-editor-token"
        )["auth_token"]
        self.knowledge = KnowledgeService(self.store)

    def propose(
        self,
        *,
        scope="project",
        target_id=None,
        statement="匿名经验候选",
        expected=None,
        auth=None,
        project=None,
    ):
        project = project or self.launch
        auth = auth or self.auth
        if target_id is None:
            target_id = {
                "company": self.workspace_id,
                "category": project["category_id"],
                "project": project["project_id"],
                "personal": (
                    self.editor_actor_id if auth == self.editor_auth else self.owner_actor_id
                ),
            }[scope]
        if expected is None:
            expected = self.knowledge.get_state(
                auth,
                self.workspace_id,
                project_id=project["project_id"],
            )["knowledge_version"]
        return self.knowledge.create_candidate(
            auth,
            workspace_id=self.workspace_id,
            project_id=project["project_id"],
            scope=scope,
            target_id=target_id,
            statement=statement,
            evidence_refs=["artifact://knowledge/evidence/anonymous"],
            expected_knowledge_version=expected,
        )

    @property
    def owner_actor_id(self):
        return self.knowledge.get_state(
            self.auth,
            self.workspace_id,
            project_id=self.launch["project_id"],
        )["actor_id"]

    def approve(self, candidate, *, level=None, expected=None):
        if expected is None:
            expected = self.knowledge.get_state(
                self.auth,
                self.workspace_id,
                project_id=self.launch["project_id"],
            )["knowledge_version"]
        return self.knowledge.approve_candidate(
            self.auth,
            workspace_id=self.workspace_id,
            project_id=self.launch["project_id"],
            candidate_id=candidate["candidate_id"],
            level=level or candidate["scope"],
            review_evidence="管理员核对匿名证据后批准",
            expected_knowledge_version=expected,
        )


class KnowledgeServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = KnowledgeServiceFixture(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_pending_candidate_never_enters_effective_rules_before_approval(self):
        candidate = self.fixture.propose()

        before = self.fixture.knowledge.list_effective_rules(
            self.fixture.auth, self.fixture.launch["project_id"]
        )
        approved = self.fixture.approve(candidate)
        after = self.fixture.knowledge.list_effective_rules(
            self.fixture.auth, self.fixture.launch["project_id"]
        )

        self.assertEqual(before["rules"], [])
        self.assertFalse(before["pending_candidates_included"])
        self.assertEqual([item["rule_id"] for item in after["rules"]], [approved["rule_id"]])
        self.assertEqual(after["knowledge_version"], 2)

    def test_company_category_project_personal_hierarchy_is_explicit(self):
        candidates = []
        for scope, target, statement in (
            ("company", self.fixture.workspace_id, "公司层匿名规则"),
            ("category", "launch", "类别层匿名规则"),
            ("project", self.fixture.launch["project_id"], "项目层匿名规则"),
            ("personal", self.fixture.owner_actor_id, "个人层匿名规则"),
        ):
            candidate = self.fixture.propose(
                scope=scope, target_id=target, statement=statement
            )
            self.fixture.approve(candidate)
            candidates.append(candidate)

        result = self.fixture.knowledge.list_effective_rules(
            self.fixture.auth, self.fixture.launch["project_id"]
        )
        other = self.fixture.knowledge.list_effective_rules(
            self.fixture.auth, self.fixture.exhibition["project_id"]
        )

        self.assertEqual(
            [item["scope"] for item in result["rules"]],
            ["company", "category", "project", "personal"],
        )
        self.assertEqual(result["precedence"], ["company", "category", "project", "personal"])
        self.assertEqual([item["scope"] for item in other["rules"]], ["company", "personal"])

    def test_editor_can_propose_but_cannot_approve(self):
        candidate = self.fixture.propose(
            auth=self.fixture.editor_auth,
            target_id=self.fixture.launch["project_id"],
        )
        version = self.fixture.knowledge.get_state(
            self.fixture.editor_auth,
            self.fixture.workspace_id,
            project_id=self.fixture.launch["project_id"],
        )["knowledge_version"]

        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.knowledge.approve_candidate(
                self.fixture.editor_auth,
                workspace_id=self.fixture.workspace_id,
                project_id=self.fixture.launch["project_id"],
                candidate_id=candidate["candidate_id"],
                level="project",
                review_evidence="编辑不能自批",
                expected_knowledge_version=version,
            )

    def test_scope_target_spoofing_and_cross_level_promotion_are_rejected(self):
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.propose(scope="category", target_id="exhibition")

        candidate = self.fixture.propose(scope="project")
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.approve(candidate, level="company")
        listed = self.fixture.knowledge.list_candidates(
            self.fixture.auth, self.fixture.workspace_id
        )
        self.assertEqual(listed[-1]["status"], "pending_approval")

    def test_stale_knowledge_version_rejects_without_partial_write(self):
        first = self.fixture.propose(expected=0, statement="第一条匿名候选")
        with self.assertRaisesRegex(WorkspaceServiceError, "KNOWLEDGE_VERSION_CONFLICT"):
            self.fixture.propose(expected=0, statement="过期版本候选")

        candidates = self.fixture.knowledge.list_candidates(
            self.fixture.auth, self.fixture.workspace_id
        )
        self.assertEqual([item["candidate_id"] for item in candidates], [first["candidate_id"]])

    def test_candidate_approval_and_rule_records_are_immutable(self):
        candidate = self.fixture.propose()
        approved = self.fixture.approve(candidate)

        with self.fixture.store.connection() as connection:
            for table, key, value in (
                ("knowledge_candidates", "candidate_id", candidate["candidate_id"]),
                ("knowledge_approvals", "approval_id", approved["approval_id"]),
                ("knowledge_rules", "rule_id", approved["rule_id"]),
            ):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                    connection.execute(
                        f"UPDATE {table} SET created_at = created_at WHERE {key} = ?",
                        (value,),
                    )


if __name__ == "__main__":
    unittest.main()
