from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.knowledge_service import KnowledgeService
from mvp_v2.knowledge_tools import (
    KnowledgeToolRecoveryRequired,
    KnowledgeToolService,
    KnowledgeToolServices,
)
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class KnowledgeToolFixture:
    def __init__(self, root: Path, *, checkpoint=None):
        self.store = WorkspaceStore(root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="anonymous-knowledge-tool-owner-token",
        )
        self.auth = owner["auth_token"]
        self.owner_actor_id = owner["actor_id"]
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
        self.tasks = TaskService(self.store)
        self.owner_launch_session = self.tasks.create_agent_session(
            self.auth, project_id=self.launch["project_id"]
        )["agent_session_id"]
        self.owner_exhibition_session = self.tasks.create_agent_session(
            self.auth, project_id=self.exhibition["project_id"]
        )["agent_session_id"]
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
            actor_id=self.editor_actor_id,
            token="anonymous-knowledge-tool-editor-token",
        )["auth_token"]
        self.editor_session = self.tasks.create_agent_session(
            self.editor_auth, project_id=self.launch["project_id"]
        )["agent_session_id"]
        self.knowledge = KnowledgeService(self.store)
        self.confirmations = ActionConfirmationService(self.store, b"k" * 32)
        self.services = KnowledgeToolServices(
            store=self.store,
            projects=self.projects,
            knowledge=self.knowledge,
            tasks=self.tasks,
            confirmations=self.confirmations,
            idempotency=ToolIdempotencyService(self.store),
        )
        self.tools = KnowledgeToolService(
            self.services, checkpoint=checkpoint
        )

    def knowledge_version(self, *, auth=None, project=None) -> int:
        project = project or self.launch
        return self.knowledge.get_state(
            auth or self.auth,
            self.workspace_id,
            project_id=project["project_id"],
        )["knowledge_version"]

    def create_parameters(
        self,
        *,
        key="knowledge-create-key-0001",
        scope="project",
        target_id=None,
        statement="匿名项目经验候选",
        expected=None,
        actor_id=None,
        project=None,
    ) -> dict:
        project = project or self.launch
        actor_id = actor_id or self.owner_actor_id
        target = {
            "company": self.workspace_id,
            "category": project["category_id"],
            "project": project["project_id"],
            "personal": actor_id,
        }[scope]
        return {
            "idempotency_key": key,
            "expected_knowledge_version": (
                self.knowledge_version(project=project)
                if expected is None
                else expected
            ),
            "scope": scope,
            "target_id": target if target_id is None else target_id,
            "statement": statement,
            "evidence_refs": ["artifact://knowledge/evidence/anonymous"],
        }

    def execute_create(
        self,
        parameters: dict,
        *,
        auth=None,
        session=None,
    ) -> dict:
        auth = auth or self.auth
        session = session or self.owner_launch_session
        confirmation = self.tools.issue_confirmation(
            auth,
            agent_session_id=session,
            tool_name="create_knowledge_candidate",
            parameters=parameters,
        )
        return self.tools.create_knowledge_candidate(
            auth,
            agent_session_id=session,
            parameters=parameters,
            confirmation_token=confirmation["confirmation_token"],
        )

    def approve_parameters(
        self,
        candidate: dict,
        *,
        key="knowledge-approve-key-0001",
        expected=None,
        level=None,
    ) -> dict:
        return {
            "idempotency_key": key,
            "expected_knowledge_version": (
                self.knowledge_version() if expected is None else expected
            ),
            "candidate_id": candidate["candidate_id"],
            "level": level or candidate["scope"],
            "review_evidence": "管理员核对匿名证据后批准",
        }

    def execute_approve(
        self,
        parameters: dict,
        *,
        auth=None,
        session=None,
    ) -> tuple[dict, dict]:
        auth = auth or self.auth
        session = session or self.owner_launch_session
        confirmation = self.tools.issue_confirmation(
            auth,
            agent_session_id=session,
            tool_name="approve_knowledge",
            parameters=parameters,
        )
        execution = {
            **parameters,
            "confirmation_token": confirmation["confirmation_token"],
        }
        return (
            self.tools.approve_knowledge(
                auth,
                agent_session_id=session,
                parameters=execution,
            ),
            execution,
        )


class KnowledgeToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = KnowledgeToolFixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_candidate_requires_confirmation_and_stays_out_of_effective_rules(self):
        parameters = self.fixture.create_parameters()
        confirmation = self.fixture.tools.issue_confirmation(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            tool_name="create_knowledge_candidate",
            parameters=parameters,
        )
        with self.fixture.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_candidates"
                ).fetchone()[0],
                0,
            )

        created = self.fixture.tools.create_knowledge_candidate(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            parameters=parameters,
            confirmation_token=confirmation["confirmation_token"],
        )
        replayed = self.fixture.tools.create_knowledge_candidate(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            parameters=parameters,
            confirmation_token=confirmation["confirmation_token"],
        )
        effective = self.fixture.knowledge.list_effective_rules(
            self.fixture.auth, self.fixture.launch["project_id"]
        )

        self.assertEqual(created["status"], "completed")
        self.assertEqual(created["result"]["status"], "pending_approval")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["task_id"], created["task_id"])
        self.assertEqual(effective["rules"], [])
        self.assertFalse(effective["pending_candidates_included"])
        with self.fixture.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_candidates"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM action_confirmations"
                ).fetchone()[0],
                "consumed",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM tool_idempotency_records"
                ).fetchone()[0],
                "completed",
            )
            self.assertEqual(
                connection.execute("SELECT status FROM tasks").fetchone()[0],
                "completed",
            )

    def test_administrator_approval_activates_once_and_replays_from_task_evidence(self):
        candidate = self.fixture.execute_create(
            self.fixture.create_parameters(key="knowledge-create-for-approval")
        )["result"]
        before = self.fixture.knowledge.list_effective_rules(
            self.fixture.auth, self.fixture.launch["project_id"]
        )
        parameters = self.fixture.approve_parameters(candidate)
        approved, execution = self.fixture.execute_approve(parameters)
        replayed = self.fixture.tools.approve_knowledge(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            parameters=execution,
        )
        after = self.fixture.knowledge.list_effective_rules(
            self.fixture.auth, self.fixture.launch["project_id"]
        )

        self.assertEqual(before["rules"], [])
        self.assertEqual(approved["result"]["status"], "active")
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["result"], approved["result"])
        self.assertEqual(
            [item["rule_id"] for item in after["rules"]],
            [approved["result"]["rule_id"]],
        )
        with self.fixture.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_approvals"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_rules"
                ).fetchone()[0],
                1,
            )

    def test_session_authority_rejects_spoofed_scope_targets_before_confirmation(self):
        wrong_targets = (
            ("company", self.fixture.exhibition["project_id"]),
            ("category", "exhibition"),
            ("project", self.fixture.exhibition["project_id"]),
            ("personal", self.fixture.editor_actor_id),
        )
        for index, (scope, target_id) in enumerate(wrong_targets):
            with self.subTest(scope=scope):
                parameters = self.fixture.create_parameters(
                    key=f"knowledge-spoof-key-{index:04d}",
                    scope=scope,
                    target_id=target_id,
                )
                with self.assertRaisesRegex(
                    WorkspaceServiceError, "PERMISSION_DENIED"
                ):
                    self.fixture.tools.issue_confirmation(
                        self.fixture.auth,
                        agent_session_id=self.fixture.owner_launch_session,
                        tool_name="create_knowledge_candidate",
                        parameters=parameters,
                    )
        with self.fixture.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM action_confirmations"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_candidates"
                ).fetchone()[0],
                0,
            )

    def test_project_or_category_candidate_cannot_be_approved_from_wrong_session(self):
        project_candidate = self.fixture.execute_create(
            self.fixture.create_parameters(key="knowledge-project-bound-create")
        )["result"]
        category_candidate = self.fixture.execute_create(
            self.fixture.create_parameters(
                key="knowledge-category-bound-create",
                scope="category",
            )
        )["result"]

        project_parameters = self.fixture.approve_parameters(
            project_candidate, key="knowledge-project-bound-approve"
        )
        category_parameters = self.fixture.approve_parameters(
            category_candidate, key="knowledge-category-bound-approve"
        )

        for parameters in (project_parameters, category_parameters):
            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                self.fixture.tools.issue_confirmation(
                    self.fixture.auth,
                    agent_session_id=self.fixture.owner_exhibition_session,
                    tool_name="approve_knowledge",
                    parameters=parameters,
                )
        listed = self.fixture.knowledge.list_candidates(
            self.fixture.auth, self.fixture.workspace_id
        )
        self.assertEqual(
            [item["status"] for item in listed],
            ["pending_approval", "pending_approval"],
        )

    def test_editor_can_propose_but_only_administrator_can_approve(self):
        candidate = self.fixture.execute_create(
            self.fixture.create_parameters(
                key="knowledge-editor-proposal",
                actor_id=self.fixture.editor_actor_id,
            ),
            auth=self.fixture.editor_auth,
            session=self.fixture.editor_session,
        )["result"]
        parameters = self.fixture.approve_parameters(
            candidate, key="knowledge-editor-cannot-approve"
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.tools.issue_confirmation(
                self.fixture.editor_auth,
                agent_session_id=self.fixture.editor_session,
                tool_name="approve_knowledge",
                parameters=parameters,
            )
        effective = self.fixture.knowledge.list_effective_rules(
            self.fixture.editor_auth, self.fixture.launch["project_id"]
        )
        self.assertEqual(effective["rules"], [])

    def test_stale_version_failure_is_audited_and_idempotently_replayed(self):
        expected = self.fixture.knowledge_version()
        first = self.fixture.create_parameters(
            key="knowledge-version-first", expected=expected
        )
        stale = self.fixture.create_parameters(
            key="knowledge-version-stale", expected=expected
        )
        self.fixture.execute_create(first)
        confirmation = self.fixture.tools.issue_confirmation(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            tool_name="create_knowledge_candidate",
            parameters=stale,
        )
        failed = self.fixture.tools.create_knowledge_candidate(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            parameters=stale,
            confirmation_token=confirmation["confirmation_token"],
        )
        replayed = self.fixture.tools.create_knowledge_candidate(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            parameters=stale,
            confirmation_token=confirmation["confirmation_token"],
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "KNOWLEDGE_VERSION_CONFLICT")
        self.assertTrue(failed["error"]["retryable"])
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["task_id"], failed["task_id"])
        with self.fixture.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_candidates"
                ).fetchone()[0],
                1,
            )

    def test_same_idempotency_key_rejects_changed_statement_without_duplicate(self):
        parameters = self.fixture.create_parameters(key="knowledge-conflict-key")
        self.fixture.execute_create(parameters)
        changed = {
            **parameters,
            "statement": "同一幂等键下被篡改的候选",
        }
        confirmation = self.fixture.tools.issue_confirmation(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            tool_name="create_knowledge_candidate",
            parameters=changed,
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.fixture.tools.create_knowledge_candidate(
                self.fixture.auth,
                agent_session_id=self.fixture.owner_launch_session,
                parameters=changed,
                confirmation_token=confirmation["confirmation_token"],
            )
        with self.fixture.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_candidates"
                ).fetchone()[0],
                1,
            )

    def test_crash_after_domain_write_blocks_automatic_replay_for_reconciliation(self):
        fired = {"value": False}

        def checkpoint(name):
            if name == "after_domain_write" and not fired["value"]:
                fired["value"] = True
                raise RuntimeError("simulated crash")

        fixture = KnowledgeToolFixture(self.root / "crash", checkpoint=checkpoint)
        parameters = fixture.create_parameters(key="knowledge-crash-key-0001")
        confirmation = fixture.tools.issue_confirmation(
            fixture.auth,
            agent_session_id=fixture.owner_launch_session,
            tool_name="create_knowledge_candidate",
            parameters=parameters,
        )
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            fixture.tools.create_knowledge_candidate(
                fixture.auth,
                agent_session_id=fixture.owner_launch_session,
                parameters=parameters,
                confirmation_token=confirmation["confirmation_token"],
            )

        restarted = KnowledgeToolService(fixture.services)
        with self.assertRaises(KnowledgeToolRecoveryRequired) as recovery:
            restarted.create_knowledge_candidate(
                fixture.auth,
                agent_session_id=fixture.owner_launch_session,
                parameters=parameters,
                confirmation_token=confirmation["confirmation_token"],
            )
        self.assertEqual(
            recovery.exception.code, "RECOVERY_RECONCILIATION_REQUIRED"
        )
        self.assertFalse(recovery.exception.retryable)
        self.assertFalse(recovery.exception.recovery["automatic_retry"])
        effective = fixture.knowledge.list_effective_rules(
            fixture.auth, fixture.launch["project_id"]
        )
        self.assertEqual(effective["rules"], [])
        with fixture.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_candidates"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM tool_idempotency_records"
                ).fetchone()[0],
                "in_progress",
            )
            self.assertEqual(
                connection.execute("SELECT status FROM tasks").fetchone()[0],
                "executing",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM action_confirmations"
                ).fetchone()[0],
                "consumed",
            )

    def test_approval_crash_also_blocks_replay_without_duplicate_rule(self):
        candidate = self.fixture.execute_create(
            self.fixture.create_parameters(key="knowledge-before-approve-crash")
        )["result"]
        fired = {"value": False}

        def checkpoint(name):
            if name == "after_domain_write" and not fired["value"]:
                fired["value"] = True
                raise RuntimeError("simulated approval crash")

        crashing = KnowledgeToolService(
            self.fixture.services, checkpoint=checkpoint
        )
        parameters = self.fixture.approve_parameters(
            candidate, key="knowledge-approve-crash-key"
        )
        confirmation = crashing.issue_confirmation(
            self.fixture.auth,
            agent_session_id=self.fixture.owner_launch_session,
            tool_name="approve_knowledge",
            parameters=parameters,
        )
        execution = {
            **parameters,
            "confirmation_token": confirmation["confirmation_token"],
        }
        with self.assertRaisesRegex(RuntimeError, "simulated approval crash"):
            crashing.approve_knowledge(
                self.fixture.auth,
                agent_session_id=self.fixture.owner_launch_session,
                parameters=execution,
            )

        restarted = KnowledgeToolService(self.fixture.services)
        with self.assertRaises(KnowledgeToolRecoveryRequired):
            restarted.approve_knowledge(
                self.fixture.auth,
                agent_session_id=self.fixture.owner_launch_session,
                parameters=execution,
            )
        with self.fixture.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_approvals"
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_rules"
                ).fetchone()[0],
                1,
            )


if __name__ == "__main__":
    unittest.main()
