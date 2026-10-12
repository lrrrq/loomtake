"""RV06-02/04: manual receipts commit with domain writes, replay original token."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from mvp_v2.workspace_store import WorkspaceServiceError
from tests.test_mvp_v2_ui_workbench import UiWorkbenchFixture


class ManualReceiptRecoveryTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.f = UiWorkbenchFixture(Path(root.name))
        self.addCleanup(self.f.client.close)
        self.service = self.f.workbench.services.manual_versions
        version = self.f.add_version('匿名回执版本', human_review_status='quality_pass')
        self.parameters = dict(project_id=self.f.project['project_id'], version_id=version['version_id'],
            expected_project_version=self.f.projects.get_project(self.f.auth_token, self.f.project['project_id'])['project_version'],
            idempotency_key='original-manual-selection')
        self.prepared = self.service.prepare(self.f.auth_token, tool_name='select_candidate_version', parameters=self.parameters)

    def execute(self, parameters=None):
        return self.service.execute(self.f.auth_token, tool_name='select_candidate_version',
            parameters=parameters or self.parameters, command_token=self.prepared['command_token'])

    def test_domain_failure_rolls_back_consumption_then_same_token_recovers_once(self):
        original = self.service._domain_action
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('simulated interruption before commit')
        with patch.object(self.service, '_domain_action', side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, 'before commit'):
                self.execute()
        with self.f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT status FROM manual_command_confirmations WHERE command_id=?',
                (self.prepared['command_id'],)).fetchone()[0], 'pending')
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM candidate_selections').fetchone()[0], 0)
        first = self.execute()
        self.assertEqual(self.execute(), first)
        with self.f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM candidate_selections').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM manual_command_results').fetchone()[0], 1)
        with self.assertRaisesRegex(WorkspaceServiceError, 'PERMISSION_DENIED'):
            self.execute({**self.parameters, 'idempotency_key': 'another-request'})

    def test_legacy_consumed_without_receipt_requires_reconciliation(self):
        with self.f.store.transaction() as connection:
            connection.execute("UPDATE manual_command_confirmations SET status='consumed',consumed_at='2026-09-18T00:00:00Z' WHERE command_id=?",
                (self.prepared['command_id'],))
        with self.assertRaisesRegex(WorkspaceServiceError, 'MANUAL_RESULT_RECONCILIATION_REQUIRED'):
            self.execute()
        with self.f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM candidate_selections').fetchone()[0], 0)

    def test_overlapping_original_commands_return_one_domain_result(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.execute(), range(2)))
        self.assertEqual(results[0], results[1])
        with self.f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM candidate_selections').fetchone()[0], 1)

    def test_receipt_does_not_bypass_revoked_identity(self):
        self.execute()
        with self.f.store.transaction() as connection:
            connection.execute("UPDATE auth_sessions SET revoked_at='2026-09-18T00:00:00Z'")
        with self.assertRaises(WorkspaceServiceError):
            self.execute()

    def test_knowledge_conflict_keeps_original_confirmation_and_rejects_scope_promotion(self):
        from tests.test_mvp_v2_knowledge_service import KnowledgeServiceFixture
        from mvp_v2.manual_knowledge_commands import (
            ManualKnowledgeApprovalCommandService, ManualKnowledgeApprovalCommandServices,
        )
        with tempfile.TemporaryDirectory() as root:
            f = KnowledgeServiceFixture(Path(root))
            first = f.propose(statement='第一条待确认规则')
            second = f.propose(statement='另一窗口先确认规则')
            service = ManualKnowledgeApprovalCommandService(
                ManualKnowledgeApprovalCommandServices(f.store, f.projects, f.knowledge))
            parameters = dict(candidate_id=first['candidate_id'], level='project',
                expected_knowledge_version=f.knowledge.get_state(f.auth, f.workspace_id,
                    project_id=f.launch['project_id'])['knowledge_version'],
                review_evidence='保留原人工核对依据', idempotency_key='knowledge-conflict-original')
            prepared = service.prepare(f.auth, project_id=f.launch['project_id'], parameters=parameters)
            f.approve(second)
            with f.store.connection() as connection:
                before = list(connection.iterdump())
            with self.assertRaisesRegex(WorkspaceServiceError, 'KNOWLEDGE_VERSION_CONFLICT'):
                service.execute(f.auth, project_id=f.launch['project_id'], parameters=parameters,
                    command_token=prepared['command_token'])
            with self.assertRaisesRegex(WorkspaceServiceError, 'PERMISSION_DENIED'):
                service.prepare(f.auth, project_id=f.launch['project_id'],
                    parameters={**parameters, 'level': 'company', 'idempotency_key': 'scope-spoof'})
            with f.store.connection() as connection:
                self.assertEqual(list(connection.iterdump()), before)
                self.assertEqual(connection.execute(
                    'SELECT status FROM manual_command_confirmations WHERE command_id=?',
                    (prepared['command_id'],)).fetchone()[0], 'pending')
            revised = {**parameters, 'idempotency_key': 'knowledge-conflict-reconfirmed',
                'expected_knowledge_version': f.knowledge.get_state(f.auth, f.workspace_id,
                    project_id=f.launch['project_id'])['knowledge_version']}
            confirmed = service.prepare(f.auth, project_id=f.launch['project_id'], parameters=revised)
            result = service.execute(f.auth, project_id=f.launch['project_id'], parameters=revised,
                command_token=confirmed['command_token'])
            self.assertEqual(service.execute(f.auth, project_id=f.launch['project_id'], parameters=revised,
                command_token=confirmed['command_token']), result)
            with f.store.connection() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM knowledge_approvals').fetchone()[0], 2)
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM knowledge_rules').fetchone()[0], 2)
