"""Actual plan conflict UI, authority, persistence and refresh; no generation."""
import unittest

from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.version_delivery import VersionDeliveryService
from tests import test_mvp_v2_formal_generation_service as generation
from tests import test_mvp_v2_manual_receipt_browser as browser
from tests.test_mvp_v2_library_browse import library_confirmation_agent


class PlanConflictBrowserTests(unittest.TestCase):
    open_page = browser.ManualReceiptBrowserTests.open_page

    def test_brief_wins_conflict_replans_and_refreshes_without_generating(self):
        f = generation.FormalGenerationServiceTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f._confirmed_brief(expected_project_version=2,
            required_content='开头核心发言', event_titles=['开头核心发言', '品牌收束'])
        calls = []
        service = f._service(generation.FormalTextProviderPolicy(
            writer=f._provider('writer', calls), editor=f._provider('editor', calls),
            writer_provider='anonymous-writer', writer_model='anonymous-model',
            editor_provider='anonymous-editor', editor_model='anonymous-model'))
        agent = library_confirmation_agent(root=f.root, store=f.store, projects=f.projects, service=None)
        script = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(
            store=f.store, projects=f.projects,
            delivery=VersionDeliveryService(f.store, service.services.artifacts),
            commands=None, artifacts=service.services.artifacts),
            runs_root=f.root/'runs', source_root=f.source_root)
        app = local_ui.create_app(agent_api=agent, formal_generation_service=service,
            formal_script_service=script,
            formal_brief_adapter=FormalBriefInputAdapter(f.briefs, data_root=f.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        project = f.project['project_id']
        page.goto(origin + f'/projects/{project}/script?panel=structure&mode=text-only')
        page.locator('#formal-plan-create').click()
        expect(page.locator('#formal-plan-conflicts')).to_be_visible()
        expect(page.locator('#formal-plan-conflicts')).to_contain_text('开头核心发言')
        expect(page.locator('#formal-plan-coverage')).to_contain_text('需求与镜头对应')
        page.locator('#formal-plan-conflict-prepare').click()
        expect(page.locator('#formal-plan-conflict-confirmation')).to_be_hidden()
        for choice in page.locator('#formal-plan-conflict-list input[value="brief_wins"]').all():
            choice.check()
        page.locator('#formal-plan-conflict-prepare').click()
        expect(page.locator('#formal-plan-conflict-confirmation')).to_be_visible()
        expect(page.locator('#formal-plan-conflict-confirmation')).to_contain_text('模型调用 0 次')
        self.assertEqual(f._table_count('project_versions'), 0)
        page.locator('#formal-plan-conflict-check').check()
        with page.expect_response(lambda r: r.request.method == 'POST' and '/conflict-resolutions/execute' in r.url) as executed:
            page.locator('#formal-plan-conflict-execute').click()
        self.assertEqual(executed.value.status, 200, executed.value.text())
        value = executed.value.json()
        self.assertEqual(value['model_calls'], 0)
        self.assertEqual(value['script_versions_created'], 0)
        expect(page.locator('#formal-plan-conflicts')).to_be_hidden()
        expect(page.locator('#formal-plan-confirm-check')).to_be_enabled()
        page.reload()
        expect(page.locator('#formal-plan-shots')).to_contain_text('开头核心发言')
        expect(page.locator('#formal-plan-conflicts')).to_be_hidden()
        expect(page.locator('#formal-plan-coverage')).to_contain_text('需求与镜头对应')
        self.assertEqual(f._table_count('project_versions'), 0)
        self.assertEqual(calls, [])
        self.assertEqual(f._service().get_plan_preview(f.token, workspace_id=f.workspace_id,
            project_id=project, plan_id=value['new_plan']['plan_id'])['decision_counts']['unresolved_conflicts'], 0)
