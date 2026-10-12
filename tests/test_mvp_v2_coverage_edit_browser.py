"""RV04-02 UI edit, image reconfirmation, unified save and coverage reread."""
import json
import unittest

from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.task_service import TaskService
from mvp_v2.version_delivery import resolve_version_shot_ids
from tests import test_mvp_v2_frozen_version_recovery as recovery_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests.test_mvp_v2_library_browse import library_confirmation_agent


class CoverageEditBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_edit_reconfirm_save_review_and_refresh_keep_evidence_in_its_version(self):
        support = recovery_fixture.FrozenVersionRecoveryTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.f
        f._confirm_current()
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        parent_model = f.review.read_current(f.token, **scope)
        parent = f.review.services.delivery.resolve(f.token, f.project_id, parent_model['base_version_id'])
        paths = {stage: f.base.resolver.resolve_stage_artifact(parent.run_id, stage).path
            for stage in ('script_plan', 'script_draft', 'visual_binding')}
        parent_bytes = {stage: path.read_bytes() for stage, path in paths.items()}
        raw = json.loads(parent_bytes['script_draft'])
        affected = next(row for row in raw['traceability'] if row.get('output_evidence'))
        shot_no = affected['output_evidence'][0]['shot_no']
        ids = resolve_version_shot_ids(f.base.resolver, parent)
        shot_id = ids[shot_no]
        with f.store.connection() as connection:
            approvals = [tuple(row) for row in connection.execute('SELECT * FROM version_quality_final_confirmations')]
        versions_before = len(f.base.projects.list_project_versions(f.token, f.project_id))
        agent = library_confirmation_agent(root=f.root, store=f.store, projects=f.base.projects, service=None)
        agent.script_revision = FormalScriptRevisionService(projects=f.base.projects,
            tasks=TaskService(f.store), workspace=f.base.base.workspace,
            image_workspace=f.base.workspace, writes_enabled=True)
        app = local_ui.create_app(agent_api=agent, formal_script_service=f.base.base.workspace,
            formal_image_service=f.base.workspace, formal_review_service=f.review,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.root/'runtime'), data_root=f.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{f.project_id}/script')
        page.locator('[data-creative-edit-mode="table"]').click()
        cell = page.locator(f'[data-script-shot-key="{shot_id}"][data-script-field="visual"]')
        new_visual = '匿名来宾沿通道进入会场。'
        with page.expect_response(lambda r: '/script/table?' in r.url and r.request.method == 'POST', timeout=60000) as edited:
            cell.fill(new_visual)
            cell.blur()
        self.assertEqual(edited.value.status, 200, edited.value.text())
        page.goto(origin + f'/projects/{f.project_id}/script?panel=images&focus_shot={shot_no}&shot_id={shot_id}')
        page.locator(f'[data-image-shot-use="{int(shot_no)-1}"]').click(timeout=60000)
        page.locator('#creative-open-save').click()
        page.locator('#formal-script-save-reason').fill('匿名覆盖证据界面回归')
        page.locator('#formal-script-save').click()
        confirm = page.locator('#formal-script-save-proposal [data-confirm-script-revision]')
        expect(confirm).to_be_visible(timeout=60000)
        with page.expect_response(lambda r: '/api/agent/tasks/' in r.url and r.url.endswith('/confirm')
                and r.request.method == 'POST', timeout=60000) as saved:
            confirm.click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        self.assertEqual(saved.value.json()['task']['status'], 'completed')
        next_link = page.locator('#formal-script-save-proposal [data-script-next-href]')
        expect(next_link).to_be_visible(timeout=60000)
        with page.expect_response(lambda r: '/api/formal/projects/' in r.url and '/review?' in r.url, timeout=60000) as reviewed:
            next_link.click()
        self.assertEqual(reviewed.value.status, 200, reviewed.value.text())
        model = reviewed.value.json()
        self.assertNotEqual(model['state'], 'confirmed')
        self.assertNotEqual(model['base_version_id'], parent.version_id)
        row = next(item for item in model['traceability'] if item['requirement_id'] == affected['requirement_id'])
        evidence = next(item for item in row['output_evidence'] if item['shot_no'] == shot_no)
        self.assertEqual(evidence['visual_description'], new_visual)
        self.assertEqual(evidence['content_evidence']['shot_id'], shot_id)
        self.assertEqual(evidence['verification_status'], 'pending_review')
        page.locator('#formal-review-trace-title').click()
        trace_row = page.locator('#formal-review-traceability tbody tr').filter(
            has=page.get_by_text(affected['brief_text'], exact=True)).first
        trace_row.locator('summary').click()
        expect(trace_row.locator('li').filter(has_text=new_visual)).to_be_visible(timeout=60000)
        with page.expect_response(lambda r: '/api/formal/projects/' in r.url and '/review?' in r.url, timeout=60000) as reread:
            page.reload()
        self.assertEqual(reread.value.json()['base_version_id'], model['base_version_id'])
        self.assertEqual(reread.value.json()['traceability'], model['traceability'])
        self.assertEqual(len(f.base.projects.list_project_versions(f.token, f.project_id)), versions_before+1)
        self.assertEqual(parent_bytes, {stage: path.read_bytes() for stage, path in paths.items()})
        with f.store.connection() as connection:
            self.assertEqual(approvals, [tuple(row) for row in connection.execute('SELECT * FROM version_quality_final_confirmations')])
