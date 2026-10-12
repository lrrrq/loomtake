import unittest
from tests import test_mvp_v2_formal_admin_service as fixture

class AdminAttemptProjectionTests(unittest.TestCase):
    setUp = fixture.FormalAdminServiceTests.setUp
    tearDown = fixture.FormalAdminServiceTests.tearDown

    def detail(self):
        return self.admin.task_detail(self.owner_token, workspace_id=self.workspace_id, task_id=self.task['task_id'])

    def test_registered_recovery_and_actual_terminal_survive_readback(self):
        self.assertEqual(self.detail()['recovery_actions'], [])
        calls = []
        self.admin.build_recovery_executor({'formal_generate': lambda ctx: calls.append(ctx.attempt_id) or {'status': 'succeeded', 'output_ref': 'artifact://anonymous/output', 'output_sha256': 'a' * 64}})
        detail = self.detail()
        self.assertIn('retry_current_stage', [a['action'] for a in detail['recovery_actions']])
        self.assertFalse(detail['checkpoints'][0]['recoverable'])
        proposal = self.admin.propose_recovery(self.owner_token, workspace_id=self.workspace_id, task_id=self.task['task_id'], action='retry_current_stage', reason='核对后重试', expected_project_version=self.project['project_version'], idempotency_key='projection-retry')
        args = dict(workspace_id=self.workspace_id, proposal_id=proposal['proposal_id'], proposal_digest=proposal['proposal_digest'])
        first = self.admin.confirm_recovery(self.owner_token, **args)
        second = self.admin.confirm_recovery(self.owner_token, **args)
        self.assertEqual(first['new_attempt_id'], second['new_attempt_id'])
        self.assertEqual(len(calls), 1)
        detail = self.detail()
        self.assertEqual(detail['task']['status'], 'failed')
        self.assertEqual([a['execution_status'] for a in detail['attempts']], ['failed', 'succeeded'])
        self.assertTrue(detail['task']['title'])
        self.assertNotIn('output_ref', str(detail['attempts']))
