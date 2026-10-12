import json
import unittest
from tests import test_mvp_v2_formal_admin_service as fixture
from mvp_v2.formal_admin_service import FormalAdminService
from mvp_v2.workspace_store import WorkspaceServiceError

class AdminConnectionCheckTests(unittest.TestCase):
    setUp = fixture.FormalAdminServiceTests.setUp
    tearDown = fixture.FormalAdminServiceTests.tearDown
    _editor_token = fixture.FormalAdminServiceTests._editor_token

    def test_read_is_passive_request_is_once_and_restart_reads_same_receipt(self):
        calls = []
        self.admin.configure_connection_tests({'text': {'configured': True, 'invoke': lambda: calls.append('text') or True, 'method': '匿名文案样例', 'real_provider': False}, 'vision': {'configured': False}})
        before = self.admin.connection_status(self.owner_token, workspace_id=self.workspace_id)
        self.assertEqual(calls, [])
        self.assertEqual(before['services'][0]['verification']['status'], 'unverified')
        args = dict(workspace_id=self.workspace_id, kind='text', request_id='same-user-intent')
        first = self.admin.test_connection(self.owner_token, **args)
        second = self.admin.test_connection(self.owner_token, **args)
        self.assertEqual(calls, ['text'])
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'succeeded')
        restored = FormalAdminService(self.store).connection_status(self.owner_token, workspace_id=self.workspace_id)
        self.assertEqual(restored['services'][0]['verification'], first)
        self.assertEqual(restored['services'][1]['verification']['status'], 'unverified')
        self.assertEqual(restored['recent_calls'][0]['status'], 'failed')
        self.assertNotIn('客户原文', json.dumps(restored, ensure_ascii=False))
        with self.assertRaisesRegex(WorkspaceServiceError, 'PERMISSION_DENIED'):
            self.admin.test_connection(self._editor_token(), **args)
        with self.assertRaisesRegex(WorkspaceServiceError, 'IDEMPOTENCY_CONFLICT'):
            self.admin.test_connection(self.owner_token, workspace_id=self.workspace_id, kind='vision', request_id='same-user-intent')

    def test_failed_unknown_and_unconfigured_never_retry_or_report_success(self):
        calls = []
        def fail():
            calls.append(1)
            raise TimeoutError('api_key=private /Users/example/private-fixture.txt')
        self.admin.configure_connection_tests({'text': {'configured': True, 'invoke': fail, 'real_provider': False}, 'vision': {'configured': False}})
        args = dict(workspace_id=self.workspace_id, kind='text', request_id='timeout-request')
        result = self.admin.test_connection(self.owner_token, **args)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(self.admin.test_connection(self.owner_token, **args), result)
        self.assertEqual(len(calls), 1)
        self.assertNotIn('private', json.dumps(result))
        result = self.admin.test_connection(self.owner_token, workspace_id=self.workspace_id, kind='vision', request_id='unconfigured-request')
        self.assertEqual(result['status'], 'unconfigured')
        self.assertEqual(len(calls), 1)

    def test_http_rejects_arbitrary_provider_parameters_and_diagnostic_only_role(self):
        from fastapi.testclient import TestClient
        from mvp_v2.local_ui import create_app
        calls = []
        self.admin.configure_connection_tests({'text': {'configured': True, 'invoke': lambda: calls.append(1) or True}})
        client = TestClient(create_app(formal_admin_service=self.admin))
        path = f'/api/formal/admin/workspaces/{self.workspace_id}/connections'
        headers = {'X-MPlus-Agent-Auth': self.owner_token, 'X-MPlus-UI-Token': client.get('/api/ui/session').json()['token']}
        self.assertEqual(client.get(path, headers=headers).status_code, 200)
        bad = client.post(path, headers=headers, json={'kind': 'text', 'request_id': 'http-intent', 'url': 'https://example.invalid', 'model': 'arbitrary'})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(calls, [])
        with self.store.transaction() as db:
            db.execute("DELETE FROM role_permissions WHERE role_id='administrator' AND permission_id='system.verify'")
        # The existing admin diagnostic role alone cannot submit an active call.
        self.assertEqual(client.get(path, headers=headers).status_code, 200)
        denied = client.post(path, headers=headers, json={'kind': 'text', 'request_id': 'http-intent'})
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(calls, [])

    def test_simultaneous_same_intent_returns_unknown_without_a_second_call(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        entered, release = Event(), Event()
        calls = []
        def invoke():
            calls.append(1)
            entered.set()
            release.wait(5)
            return True
        self.admin.configure_connection_tests({'text': {'configured': True, 'invoke': invoke}})
        args = dict(workspace_id=self.workspace_id, kind='text', request_id='concurrent-intent')
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.admin.test_connection, self.owner_token, **args)
            self.assertTrue(entered.wait(2))
            try:
                during = self.admin.test_connection(self.owner_token, **args)
                self.assertEqual(during['status'], 'unknown')
                self.assertEqual(calls, [1])
            finally:
                release.set()
            self.assertEqual(first.result()['status'], 'succeeded')
        self.assertEqual(self.admin.test_connection(self.owner_token, **args)['status'], 'succeeded')
        self.assertEqual(calls, [1])
