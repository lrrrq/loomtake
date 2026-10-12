"""RV07-03 actual asset lookup and project scope switching."""
from pathlib import Path
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
from tests.test_mvp_v2_ui_workbench import UiWorkbenchFixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class AssetSearchBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_shared_and_project_assets_follow_current_scope_and_read_only_lookup(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        f = UiWorkbenchFixture(Path(directory.name)); self.addCleanup(f.client.close)
        ws = f.projects.get_workspace(f.auth_token, f.workspace_id)
        b = f.projects.create_project(f.auth_token, workspace_id=f.workspace_id,
            name='匿名素材项目乙', category_id='launch', expected_workspace_version=ws['workspace_version'])
        resolved = f.asset_tools.services.packages.resolve_active('launch')
        center = resolved.center
        with f.store.connection() as connection:
            actor_id = connection.execute('SELECT actor_id FROM actors').fetchone()[0]
        for name, color, project_id in [('shared-search','yellow',None),
                ('alpha-search','red',f.project['project_id']), ('beta-search','blue',b['project_id'])]:
            path = f.root/(name+'-合影.png')
            Image.new('RGB',(48,32),color).save(path)
            upload = f.asset_tools.services.uploads.freeze(path)
            staged = center.stage_import(import_id='import_'+name.replace('-','_'),
                category_id='launch', scope='project' if project_id else 'shared', project_id=project_id,
                files=[f.asset_tools.services.uploads.as_import_file(upload['artifact_ref'])],
                requested_by_actor_id=actor_id, expected_package_version=resolved.package_version)
            reviewed = center.review_import(staged['import_id'], reviewed_by_actor_id=actor_id,
                expected_revision=staged['revision'])
            center.publish_import(reviewed['import_id'], published_by_actor_id=actor_id,
                expected_revision=reviewed['revision'])
        before = {name: center.list_events('import_'+name.replace('-','_')) for name in
            ['shared-search','alpha-search','beta-search']}
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store, identity=f.identity,
            projects=f.projects, tasks=f.tasks, versions=f.versions, artifacts=RunArtifactResolver(f.runs_root)))
        app = local_ui.create_app(agent_api=agent, ui_workbench=f.workbench,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root), formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.auth_token, f.workspace_id)
        pending = []
        def hold_context(route):
            pending.append(route)
        page.route('**/api/agent/workbench/assets?*', hold_context)
        page.goto(origin+'/assets?project_id='+f.project['project_id'])
        results = page.locator('#asset-search-results')
        for project_id, own, excluded in [(f.project['project_id'],'alpha-search','beta-search'),
                (b['project_id'],'beta-search','alpha-search')]:
            if project_id != f.project['project_id']:
                page.locator('#asset-project-select').select_option(project_id)
            expect(page.locator('#asset-project-select')).to_have_value(project_id)
            expect(page.locator('#asset-package-label')).to_have_text('正在读取资源…')
            expect(page.locator('#asset-search')).to_be_disabled()
            for _ in range(100):
                if pending:
                    break
                page.wait_for_timeout(20)
            self.assertTrue(pending, 'Actual asset context request must start')
            pending.pop(0).continue_()
            expect(page.locator('#asset-search')).to_be_enabled()
            page.locator('#asset-search-query').fill('search')
            page.locator('#asset-search-kind').select_option('image')
            with page.expect_response(lambda r: r.url.endswith('/assets/search') and r.request.method=='POST') as found:
                page.locator('#asset-search').click()
            self.assertEqual(found.value.status,200,found.value.text())
            expect(results.locator('article')).to_have_count(2)
            expect(results).to_contain_text('shared-search')
            expect(results).to_contain_text(own)
            expect(results).not_to_contain_text(excluded)
            expect(results).to_contain_text('同类型共享')
            expect(results).to_contain_text('仅当前项目')
            expect(results).not_to_contain_text(str(f.root))
            original_ids = {item['asset_id'] for item in found.value.json()['results']}
            page.locator('#asset-search-query').fill('合照')
            with page.expect_response(lambda r: r.url.endswith('/assets/search') and r.request.method=='POST') as alternate:
                page.locator('#asset-search').click()
            self.assertEqual(alternate.value.status, 200, alternate.value.text())
            self.assertEqual({item['asset_id'] for item in alternate.value.json()['results']}, original_ids)
            expect(results.locator('article')).to_have_count(2)
            expect(results).to_contain_text(own)
            expect(results).not_to_contain_text(excluded)
            results.locator('article').first.locator('summary').click()
            expect(results.locator('article').first.locator('details')).to_have_attribute('open','')
        page.unroute('**/api/agent/workbench/assets?*', hold_context)
        page.reload()
        expect(page.locator('#asset-project-select')).to_have_value(b['project_id'])
        expect(page.locator('#asset-search')).to_be_enabled()
        page.locator('#asset-search-query').fill('search')
        page.locator('#asset-search-kind').select_option('image')
        page.locator('#asset-search').click()
        expect(results).to_contain_text('beta-search')
        expect(results).not_to_contain_text('alpha-search')
        page.route('**/api/agent/workbench/assets?*', lambda route: route.abort('connectionreset'))
        page.reload()
        expect(page.locator('#asset-package-label')).to_have_text('资源读取失败')
        expect(page.locator('#asset-search')).to_be_disabled()
        page.unroute('**/api/agent/workbench/assets?*')
        page.locator('[data-assets-retry]').click()
        expect(page.locator('#asset-search')).to_be_enabled()
        page.locator('#asset-search-query').fill('search')
        page.locator('#asset-search-kind').select_option('image')
        page.locator('#asset-search').click()
        expect(results).to_contain_text('beta-search')
        expect(results).not_to_contain_text('alpha-search')
        self.assertEqual({name:center.list_events('import_'+name.replace('-','_')) for name in before},before)
