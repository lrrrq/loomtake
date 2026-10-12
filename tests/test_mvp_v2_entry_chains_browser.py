"""Asset staging and knowledge proposals through the real local workbench."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from tests import test_mvp_v2_ui_workbench as asset_fixture
from tests import test_mvp_v2_ui_governance as rule_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class EntryChainsBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_archived_list_reopens_project_and_restores_without_new_project(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        f = rule_fixture.GovernanceUiFixture(Path(temporary.name)); self.addCleanup(f.client.close)
        app = local_ui.create_app(agent_api=f.app.state.agent_api,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root), formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.auth, f.workspace_id)
        project_id = f.project['project_id']
        with f.store.connection() as c:
            original_briefs = [tuple(r) for r in c.execute('SELECT * FROM project_brief_snapshots')]
            project_count = c.execute('SELECT COUNT(*) FROM projects').fetchone()[0]
        page.goto(origin + '/projects/' + project_id + '/overview')
        page.locator('#formal-project-lifecycle > summary').click()
        form = page.locator('#formal-lifecycle-form')
        form.locator('textarea').fill('匿名归档入口验证')
        form.locator('button[type="submit"]').click()
        page.locator('#formal-lifecycle-confirm-check').check()
        page.locator('#formal-lifecycle-execute').click()
        expect(page.locator('#formal-archive-banner')).to_be_visible()
        page.goto(origin + '/projects')
        page.locator('.project-archive-list > summary').click()
        archived = page.locator('#formal-archived-list a[href*="' + project_id + '"]')
        expect(archived).to_have_count(1)
        archived.click()
        expect(page.locator('#formal-archive-banner')).to_be_visible()
        page.locator('#formal-archive-manage').click()
        form.locator('textarea').fill('从归档列表恢复原项目')
        form.locator('button[type="submit"]').click()
        page.locator('#formal-lifecycle-confirm-check').check()
        page.locator('#formal-lifecycle-execute').click()
        expect(page.locator('#formal-archive-banner')).to_be_hidden()
        page.goto(origin + '/projects')
        expect(page.locator('#formal-project-list a[href*="' + project_id + '"]')).to_have_count(1)
        page.reload()
        expect(page.locator('#formal-project-list a[href*="' + project_id + '"]')).to_have_count(1)
        expect(page.locator('#formal-archived-list a[href*="' + project_id + '"]')).to_have_count(0)
        with f.store.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM projects').fetchone()[0], project_count)
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM project_brief_snapshots')], original_briefs)
        self.assertEqual(f.projects.get_project(f.auth, project_id)['status'], 'active')

    def test_asset_choose_freeze_cancel_stage_and_refresh_without_publication(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        f = asset_fixture.UiWorkbenchFixture(Path(temporary.name)); self.addCleanup(f.client.close)
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store, identity=f.identity,
            projects=f.projects, tasks=f.tasks, versions=f.versions, artifacts=RunArtifactResolver(f.runs_root)))
        app = local_ui.create_app(agent_api=agent, ui_workbench=f.workbench,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root), formal_brief_upload_factory=uploaded_document)
        source = f.root / 'anonymous-entry.png'; Image.new('RGB', (96, 64), 'cyan').save(source)
        original = source.read_bytes()
        page, origin = self.open_page(app, f.auth_token, f.workspace_id)
        errors = []; page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(origin + '/assets?project_id=' + f.project['project_id'])
        expect(page.locator('#asset-search')).to_be_enabled()
        page.locator('.asset-intake-panel > summary').click()
        with page.expect_file_chooser() as chooser:
            page.locator('#asset-choose').click()
        chooser.value.set_files(source)
        expect(page.locator('#asset-local-path')).to_have_value(source.name)
        page.locator('#asset-freeze').click()
        expect(page.locator('#asset-frozen-list')).to_contain_text(source.name)
        expect(page.locator('#asset-scope')).to_have_value('project')
        page.locator('#asset-stage').click()
        expect(page.locator('#asset-confirmation')).to_be_visible()
        page.locator('#asset-confirm-cancel').click()
        expect(page.locator('[data-import-id]')).to_have_count(0)
        expect(page.locator('#asset-frozen-list')).to_contain_text(source.name)
        page.locator('#asset-stage').click()
        page.locator('#asset-confirm-check').check()
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/assets/actions/execute')) as executed:
            page.locator('#asset-confirm-execute').click()
        self.assertEqual(executed.value.status, 200, executed.value.text())
        expect(page.locator('[data-import-id]')).to_have_count(1)
        record_id = page.locator('[data-import-id]').get_attribute('data-import-id')
        expect(page.locator('[data-import-id]')).to_contain_text('待审核')
        page.reload()
        expect(page.locator('[data-import-id]')).to_have_count(1)
        self.assertEqual(page.locator('[data-import-id]').get_attribute('data-import-id'), record_id)
        expect(page.locator('[data-import-id]')).to_contain_text(source.name)
        expect(page.locator('#asset-frozen-list')).not_to_contain_text(source.name)
        expect(page.locator('[data-asset-publish]')).to_have_count(0)
        self.assertEqual(source.read_bytes(), original)
        self.assertNotIn(str(f.root), page.locator('#page-assets').inner_text())
        self.assertEqual(errors, [])

    def test_rule_prepare_cancel_submit_refresh_remains_pending(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        f = rule_fixture.GovernanceUiFixture(Path(temporary.name)); self.addCleanup(f.client.close)
        app = local_ui.create_app(agent_api=f.app.state.agent_api, ui_governance=f.app.state.ui_governance,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.data_root), data_root=f.data_root), formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.auth, f.workspace_id)
        page.goto(origin + '/knowledge?project_id=' + f.project['project_id'])
        expect(page.locator('#knowledge-propose')).to_be_enabled()
        page.locator('.knowledge-proposal-panel > summary').click()
        statement = '匿名规则建议：先核对已确认事实，再调整表达。'
        page.locator('#knowledge-statement').fill(statement)
        page.locator('#knowledge-evidence').select_option(index=0)
        page.locator('#knowledge-propose').click()
        expect(page.locator('#governance-confirmation')).to_be_visible()
        page.locator('#governance-confirm-cancel').click()
        expect(page.locator('#knowledge-statement')).to_have_value(statement)
        with f.store.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM knowledge_candidates').fetchone()[0], 0)
        page.locator('#knowledge-propose').click()
        page.locator('#governance-confirm-check').check()
        with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/knowledge/candidates/execute')) as executed:
            page.locator('#governance-confirm-execute').click()
        self.assertEqual(executed.value.status, 200, executed.value.text())
        expect(page.locator('#pending-knowledge')).to_contain_text(statement)
        page.reload()
        expect(page.locator('#pending-knowledge')).to_contain_text(statement)
        expect(page.locator('#knowledge-rules')).not_to_contain_text(statement)
        with f.store.connection() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM knowledge_candidates').fetchone()[0], 1)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM knowledge_approvals').fetchone()[0], 0)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM knowledge_rules').fetchone()[0], 0)
