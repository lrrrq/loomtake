"""Browser bytes enter the existing asset import workflow; no server picker."""
from io import BytesIO
from pathlib import Path
import hashlib
import tempfile
import unittest
from PIL import Image
from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from tests import test_mvp_v2_ui_workbench as fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class AssetUploadTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.f = fixture.UiWorkbenchFixture(Path(tmp.name)); self.addCleanup(self.f.client.close)
        image = BytesIO(); Image.new('RGB', (16, 16), 'blue').save(image, 'PNG')
        self.content = image.getvalue()
        self.url = '/api/agent/workbench/assets/upload?agent_session_id=' + self.f.session_id

    def upload(self, name, content, headers=None):
        return self.f.client.post(self.url, headers=self.f.headers if headers is None else headers,
                                  files={'file': (name, content)})

    def test_bytes_replay_type_limits_and_unauthorized_leave_no_import(self):
        bad = self.upload('wrong.jpg', self.content)
        self.assertEqual(bad.status_code, 422, bad.text)
        self.assertFalse(self.f.asset_tools.services.uploads.storage.root.exists())
        too_large = self.upload('large.png', b'x' * (5 * 1024 * 1024 + 1))
        self.assertEqual(too_large.status_code, 409, too_large.text)
        self.assertEqual(too_large.json()["detail"]["code"], "FILE_TOO_LARGE")
        denied = self.upload('valid.png', self.content,
            {'X-MPlus-UI-Token': self.f.app.state.session_token})
        self.assertIn(denied.status_code, (401, 403), denied.text)
        first = self.upload('valid.png', self.content)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.upload('valid.png', self.content).json(), first.json())
        self.assertEqual(first.json()['content_sha256'], hashlib.sha256(self.content).hexdigest())
        self.assertNotIn(str(self.f.root), str(first.json()))
        self.assertEqual(self.f.workbench.asset_context(self.f.auth_token,
            agent_session_id=self.f.session_id)['imports'], [])
        root = self.f.asset_tools.services.uploads.storage.root
        self.assertEqual(len([p for p in root.rglob('*') if p.is_file()]), 1)

    def test_editor_can_upload_but_cannot_prepare_shared_publication(self):
        f = self.f
        ws = f.projects.get_workspace(f.auth_token, f.workspace_id)
        actor = f.identity.create_actor(f.auth_token, workspace_id=f.workspace_id,
            project_id=f.project['project_id'], display_name='匿名资产编辑',
            auth_source='local', role='editor', expected_workspace_version=ws['workspace_version'])
        token = f.identity.issue_auth_session(actor_id=actor['actor_id'], token='asset-editor')['auth_token']
        session = f.tasks.create_agent_session(token, project_id=f.project['project_id'])['agent_session_id']
        self.url = '/api/agent/workbench/assets/upload?agent_session_id=' + session
        headers = {**f.headers, 'X-MPlus-Agent-Auth': token}
        frozen = self.upload('editor.png', self.content, headers)
        self.assertEqual(frozen.status_code, 200, frozen.text)
        parameters = {'package_id':f.package_id, 'expected_active_package_version':f.package_version,
            'category_id':'launch', 'scope':'shared', 'file_refs':[frozen.json()['artifact_ref']],
            'idempotency_key':'editor-shared-asset-import'}
        prepared = f.client.post('/api/agent/workbench/assets/actions/prepare', headers=headers,
            json={'agent_session_id':session, 'tool_name':'create_asset_import', 'parameters':parameters})
        self.assertEqual(prepared.status_code, 200, prepared.text)
        created = f.client.post('/api/agent/workbench/assets/actions/execute', headers=headers,
            json={'agent_session_id':session, 'tool_name':'create_asset_import', 'parameters':parameters,
                  'confirmation_token':prepared.json()['confirmation_token']})
        self.assertEqual(created.status_code, 200, created.text)
        record = created.json()['result']
        denied = f.client.post('/api/manual/assets/publish/prepare',
            headers={'X-MPlus-UI-Token':f.app.state.session_token, 'X-MPlus-Manual-Auth':token},
            json={'project_id':f.project['project_id'], 'package_id':f.package_id,
                'expected_active_package_version':f.package_version, 'import_id':record['import_id'],
                'expected_import_revision':record['revision'], 'idempotency_key':'editor-cannot-publish-shared'})
        self.assertEqual(denied.status_code, 403, denied.text)
        self.assertEqual(denied.json()['detail']['code'], 'PERMISSION_DENIED')
        current = f.workbench.asset_context(token, agent_session_id=session)['imports']
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]['state'], 'staged')
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM manual_command_confirmations').fetchone()[0], 0)

    def test_browser_upload_cancel_reselect_import_and_refresh(self):
        f = self.f
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store, identity=f.identity,
            projects=f.projects, tasks=f.tasks, versions=f.versions, artifacts=RunArtifactResolver(f.runs_root)))
        app = local_ui.create_app(agent_api=agent, ui_workbench=f.workbench,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root), formal_brief_upload_factory=uploaded_document)
        page, origin = browser_fixture.ManualReceiptBrowserTests.open_page(self, app, f.auth_token, f.workspace_id)
        requests = []
        page.on('request', lambda r: requests.append(r.url))
        page.goto(origin + '/assets?project_id=' + f.project['project_id'])
        expect(page.locator('#asset-search')).to_be_enabled()
        page.locator('.asset-intake-panel summary').click()
        for iteration in range(2):
            page.locator('#asset-upload-file').set_input_files(
                {'name':'browser-only.png', 'mimeType':'image/png', 'buffer':self.content})
            page.locator('#asset-freeze').click()
            expect(page.locator('#asset-frozen-list')).to_contain_text('browser-only.png')
            if iteration == 0:
                page.locator('[data-remove-frozen]').click()
                expect(page.locator('#asset-stage')).to_be_disabled()
        executions = []
        def lose_receipt(route):
            executions.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 200, response.text())
            if len(executions) == 1:
                route.abort('connectionreset')
            else:
                route.fulfill(response=response)
        page.route('**/api/agent/workbench/assets/actions/execute', lose_receipt)
        page.locator('#asset-stage').click()
        expect(page.locator('#asset-confirmation')).to_be_visible()
        page.locator('#asset-confirm-check').check()
        page.locator('#asset-confirm-execute').click()
        expect(page.locator('#asset-confirm-expiry')).to_contain_text('原请求已保留')
        page.reload()
        expect(page.locator('#asset-confirm-risk')).to_have_text('上次操作结果待核对')
        page.locator('#asset-confirm-check').check()
        page.locator('#asset-confirm-execute').click()
        expect(page.locator('#asset-confirmation')).to_be_hidden()
        self.assertEqual(len(executions), 2)
        self.assertEqual(executions[0], executions[1])
        page.reload()
        expect(page.locator('#asset-imports')).to_contain_text('browser-only.png')
        records = f.workbench.asset_context(f.auth_token, agent_session_id=f.session_id)['imports']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['state'], 'staged')
        self.assertFalse(any('/api/dialog/asset' in url or '/assets/freeze' in url for url in requests))
        root = f.asset_tools.services.uploads.storage.root
        self.assertEqual(len([p for p in root.rglob('*') if p.is_file()]), 1)
