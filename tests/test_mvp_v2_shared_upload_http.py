"""Overlapping real HTTP imports must not delete another project's frozen input."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch

import httpx
from playwright.sync_api import expect

from mvp_v2 import local_ui
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_brief_domain import ArtifactPayload
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from tests import test_mvp_v2_formal_brief_input_adapter as input_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class SharedUploadHttpTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_failed_project_import_preserves_concurrent_project_input_and_retry(self):
        self._overlapping_uploads(same_project=False)

    def test_same_project_concurrent_upload_rejects_stale_writer_without_deleting_blob(self):
        self._overlapping_uploads(same_project=True)

    def _overlapping_uploads(self, *, same_project):
        f = input_fixture.FormalBriefInputAdapterTests('runTest')
        f.setUp()
        self.addCleanup(f.tearDown)
        agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=f.store, identity=LocalIdentityAdapter(f.store), projects=f.projects,
            tasks=TaskService(f.store), versions=VersionService(f.store),
            artifacts=RunArtifactResolver(f.root / 'runs')))
        app = local_ui.create_app(agent_api=agent, formal_brief_adapter=f.adapter,
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        published, committed = threading.Event(), threading.Event()
        original_import = f.adapter.service.import_frozen
        gate = threading.Lock()
        entered = []
        successful_project = f.first if same_project else f.second

        def delayed_failure(auth_token, **kwargs):
            with gate:
                first_request = not entered
                entered.append(kwargs['project_id'])
            if first_request:
                # This boundary is after input promotion and before DB transaction.
                published.set()
                if not committed.wait(15):
                    raise AssertionError('second project did not commit')
                if not same_project:
                    raise WorkspaceServiceError('PROJECT_VERSION_CONFLICT', '匿名并发故障注入')
            return original_import(auth_token, **kwargs)

        text = '项目名称: 匿名共享资料\n风格调性: 克制\n视频目标: 说明重点\n必须出现的内容: 产品\n关键事件与流程: 发布\n禁止或不能出现的内容: 夸张'
        headers = {'X-MPlus-Agent-Auth': f.token, 'Origin': origin}
        def upload(project):
            return httpx.post(origin + '/api/formal/projects/' + project['project_id'] + '/brief/files',
                params={'workspace_id': f.workspace_id, 'expected_project_version': 0},
                headers=headers, files={'files': ('共同需求.txt', text.encode(), 'text/plain')}, timeout=30)

        with patch.object(f.adapter.service, 'import_frozen', side_effect=delayed_failure):
            with ThreadPoolExecutor(2) as pool:
                failed = pool.submit(upload, f.first)
                try:
                    self.assertTrue(published.wait(10))
                    success = pool.submit(upload, successful_project).result(20)
                    self.assertEqual(success.status_code, 200, success.text)
                finally:
                    committed.set()
                rejected = failed.result(20)
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertIn('PROJECT_VERSION_CONFLICT', rejected.text)
        second_before = f.adapter.read_structured(f.token, workspace_id=f.workspace_id,
            project_id=successful_project['project_id'], brief_version_id=success.json()['brief_key'])
        inputs = f.adapter.service.list_imported_sources(f.token, workspace_id=f.workspace_id,
            project_id=successful_project['project_id'])
        self.assertEqual(len(inputs), 1)
        self.assertEqual(f.adapter.store.read(ArtifactPayload(inputs[0]['artifact_ref'], inputs[0]['sha256']), suffix='.txt'), text.encode())
        if not same_project:
            self.assertEqual(f.adapter.service.list_imported_sources(f.token, workspace_id=f.workspace_id,
                project_id=f.first['project_id']), [])
        page.goto(origin + '/projects/' + successful_project['project_id'] + '/brief')
        expect(page.locator('[data-edit-formal-fact="风格调性"]')).to_be_visible()
        page.reload()
        expect(page.locator('[data-edit-formal-fact="风格调性"]')).to_be_visible()
        if same_project:
            reopened = FormalBriefInputAdapter(FormalBriefWorkspaceService(
                WorkspaceStore(f.store.database_path), artifact_root=f.root / 'data'), data_root=f.root / 'data')
            sources = reopened.service.list_imported_sources(f.token, workspace_id=f.workspace_id,
                project_id=f.first['project_id'])
            self.assertEqual(sources, inputs)
            self.assertEqual(reopened.read_structured(f.token, workspace_id=f.workspace_id,
                project_id=f.first['project_id'], brief_version_id=success.json()['brief_key']), second_before)
            self.assertEqual(reopened.store.read(ArtifactPayload(sources[0]['artifact_ref'], sources[0]['sha256']),
                suffix='.txt'), text.encode())
            return
        retry = upload(f.first)
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(f.adapter.read_structured(f.token, workspace_id=f.workspace_id,
            project_id=f.second['project_id'], brief_version_id=success.json()['brief_key']), second_before)
        reopened = FormalBriefInputAdapter(FormalBriefWorkspaceService(
            WorkspaceStore(f.store.database_path), artifact_root=f.root / 'data'), data_root=f.root / 'data')
        for project in (f.first, f.second):
            sources = reopened.service.list_imported_sources(f.token, workspace_id=f.workspace_id,
                project_id=project['project_id'])
            self.assertEqual(len(sources), 1)
            self.assertEqual(sources[0]['artifact_ref'], inputs[0]['artifact_ref'])
            self.assertEqual(sources[0]['sha256'], inputs[0]['sha256'])
            self.assertEqual(reopened.store.read(ArtifactPayload(sources[0]['artifact_ref'], sources[0]['sha256']), suffix='.txt'), text.encode())
