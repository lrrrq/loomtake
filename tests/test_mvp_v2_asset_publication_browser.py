"""Real asset publication HTTP response loss and original-command browser recovery."""
from dataclasses import replace
import unittest
import threading

from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from tests import test_mvp_v2_asset_publication_recovery as fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class AssetPublicationBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_late_publication_response_preserves_other_project_and_single_package(self):
        support = fixture.AssetPublicationRecoveryTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.f
        f.asset_tools.services = replace(f.asset_tools.services, packages=support.service.services.packages)
        f.workbench.services = replace(f.workbench.services, manual_asset_publish=support.service)
        ws = f.projects.get_workspace(f.auth_token, f.workspace_id)
        other = f.projects.create_project(f.auth_token, workspace_id=f.workspace_id,
            name='匿名资产迟到项目乙', category_id='launch',
            expected_workspace_version=ws['workspace_version'])
        other_before = f.projects.get_project(f.auth_token, other['project_id'])
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store, identity=f.identity,
            projects=f.projects, tasks=f.tasks, versions=f.versions, artifacts=RunArtifactResolver(f.runs_root)))
        app = local_ui.create_app(agent_api=agent, ui_workbench=f.workbench,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root), formal_brief_upload_factory=uploaded_document)
        def current_files():
            return {str(path): path.read_bytes() for root in (f.root, support.asset.root)
                for path in root.rglob('CURRENT') if path.is_file()}
        unchanged_current = current_files()
        from starlette.concurrency import run_in_threadpool
        committed, release = threading.Event(), threading.Event()
        requests = []
        @app.middleware('http')
        async def hold_publication(request, call_next):
            response = await call_next(request)
            if request.url.path == '/api/manual/assets/publish/execute':
                requests.append(response.status_code)
                committed.set()
                await run_in_threadpool(release.wait, 25)
            return response
        page, origin = self.open_page(app, f.auth_token, f.workspace_id)
        self.addCleanup(release.set)
        page.goto(origin + '/assets?project_id=' + f.project['project_id'])
        page.locator('[data-asset-publish]').click()
        page.locator('#asset-confirm-check').check()
        page.locator('#asset-confirm-execute').click()
        self.assertTrue(committed.wait(20), 'Publication must commit before project switch')
        self.assertEqual(support.registry.resolve_formal('launch')['package_version'], 4)
        page.locator('#asset-project-select').select_option(other['project_id'])
        expect(page.locator('#asset-project-select')).to_have_value(other['project_id'])
        expect(page.locator('#asset-search')).to_be_enabled()
        expect(page.locator('#asset-confirmation')).to_be_hidden()
        with page.expect_response(lambda r: r.url.endswith('/api/manual/assets/publish/execute')):
            release.set()
        page.wait_for_timeout(200)
        expect(page.locator('#asset-project-select')).to_have_value(other['project_id'])
        expect(page.locator('#asset-confirmation')).to_be_hidden()
        page.locator('#asset-project-select').select_option(f.project['project_id'])
        expect(page.locator('#asset-search')).to_be_enabled()
        expect(page.locator('#asset-confirmation')).to_be_hidden()
        expect(page.locator('[data-asset-publish]')).to_have_count(0)
        page.reload()
        expect(page.locator('#asset-search')).to_be_enabled()
        expect(page.locator('#asset-confirmation')).to_be_hidden()
        page.locator('#asset-search-query').fill('receipt')
        page.locator('#asset-search-kind').select_option('image')
        page.locator('#asset-search').click()
        expect(page.locator('#asset-search-results')).to_contain_text('receipt.png')
        self.assertEqual(requests, [200])
        self.assertEqual(len(support.registry.list()['packages']), 2)
        events = support.center.list_events(support.parameters['import_id'])
        self.assertEqual(sum(e['event_type']=='asset_import.published' for e in events), 1)
        self.assertEqual(sum(e['event_type']=='asset_publication.completed' for e in events), 1)
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM manual_command_results').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_publications').fetchone()[0], 0)
        self.assertEqual(f.projects.get_project(f.auth_token, other['project_id']), other_before)
        self.assertEqual(current_files(), unchanged_current)

    def test_publish_response_loss_refresh_recovers_one_immutable_package(self):
        support = fixture.AssetPublicationRecoveryTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.f
        f.asset_tools.services = replace(f.asset_tools.services, packages=support.service.services.packages)
        f.workbench.services = replace(f.workbench.services, manual_asset_publish=support.service)
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store, identity=f.identity,
            projects=f.projects, tasks=f.tasks, versions=f.versions, artifacts=RunArtifactResolver(f.runs_root)))
        app = local_ui.create_app(agent_api=agent, ui_workbench=f.workbench,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root), formal_brief_upload_factory=uploaded_document)
        def current_files():
            return {str(path): path.read_bytes() for root in (f.root, support.asset.root)
                for path in root.rglob('CURRENT') if path.is_file()}
        unchanged_current = current_files()
        page, origin = self.open_page(app, f.auth_token, f.workspace_id)
        requests = []
        def lose_response(route):
            requests.append(route.request.post_data_json)
            response = route.fetch()
            self.assertEqual(response.status, 200, response.text())
            if len(requests) == 1:
                route.abort('connectionreset')
            else:
                route.fulfill(response=response)
        page.route('**/api/manual/assets/publish/execute', lose_response)
        page.goto(origin + '/assets?project_id=' + f.project['project_id'])
        page.locator('[data-asset-publish]').click()
        expect(page.locator('#asset-confirm-copy')).to_contain_text('同类型共享资产范围')
        expect(page.locator('#asset-confirm-copy')).to_contain_text('receipt.png')
        page.locator('#asset-confirm-evidence summary').click()
        digest = support.center.get_import(support.parameters['import_id'])['items'][0]['content_sha256']
        expect(page.locator('#asset-confirm-evidence')).to_contain_text(digest)
        page.locator('#asset-confirm-check').check()
        page.locator('#asset-confirm-execute').click()
        expect(page.locator('#asset-confirm-expiry')).to_contain_text('原请求已保留')
        self.assertEqual(support.registry.resolve_formal('launch')['package_version'], 4)
        page.reload()
        expect(page.locator('#asset-confirm-risk')).to_have_text('上次操作结果待核对')
        expect(page.locator('#asset-confirm-copy')).to_contain_text('receipt.png')
        page.locator('#asset-confirm-check').check()
        page.locator('#asset-confirm-execute').click()
        expect(page.locator('#asset-confirmation')).to_be_hidden()
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(len(support.registry.list()['packages']), 2)
        events = support.center.list_events(support.parameters['import_id'])
        self.assertEqual(sum(e['event_type']=='asset_import.published' for e in events), 1)
        self.assertEqual(sum(e['event_type']=='asset_publication.completed' for e in events), 1)
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM manual_command_results').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM project_versions').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_publications').fetchone()[0], 0)
        page.reload()
        expect(page.locator('#asset-confirmation')).to_be_hidden()
        self.assertEqual(current_files(), unchanged_current)
