"""RV02 scope isolation against actual delayed image proposal responses."""
import threading
import unittest
from playwright.sync_api import expect
from starlette.concurrency import run_in_threadpool
from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from tests import test_mvp_v2_image_assistant_batch as batch_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests.test_mvp_v2_library_browse import library_confirmation_agent


class ImageScopeRecoveryBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def setup_scope(self):
        support = batch_fixture.ImageAssistantBatchTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.f
        script = f.base.workspace
        current = script.read_current(f.token, **support.base.scope)
        target = current['shots'][1]['shot_id']
        before = f.workspace.read_progress(f.token, **support.base.scope)
        ws = f.projects.get_workspace(f.token, f.workspace_id)
        b = f.projects.create_project(f.token, workspace_id=f.workspace_id,
            name='匿名配图项目乙', expected_workspace_version=ws['workspace_version'])
        agent = library_confirmation_agent(root=f.base.generated.root, store=f.store,
            projects=f.projects, service=None)
        agent.script_revision = support.revision
        app = local_ui.create_app(agent_api=agent, formal_script_service=script,
            formal_image_service=f.workspace, formal_brief_adapter=FormalBriefInputAdapter(
                FormalBriefWorkspaceService(f.store, artifact_root=f.base.generated.root/'runtime'),
                data_root=f.base.generated.root/'runtime'), formal_brief_upload_factory=uploaded_document)
        return support, f, script, current, target, before, b, app

    def test_selected_recommendation_late_response_stays_in_original_project(self):
        support, f, script, current, target, before, b, app = self.setup_scope()
        held, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        @app.middleware('http')
        async def hold_proposal(request, call_next):
            response = await call_next(request)
            if request.method == 'POST' and request.url.path.endswith('/image-workspace/assistant'):
                held.set()
                await run_in_threadpool(release.wait, 60)
            return response
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{f.project_id}/script?panel=images')
        page.locator('[data-creative-batch-toggle]').click()
        page.locator(f'[data-creative-select-shot="{target}"]').check()
        page.locator('#creative-image-scope').select_option('all')
        with page.expect_request(lambda r: r.method == 'POST' and '/image-workspace/assistant?' in r.url) as request:
            page.locator('[data-creative-match-images][data-image-batch-mode="replace_selected"]').click()
        self.assertEqual(request.value.post_data_json['selected_shot_ids'], [target])
        self.assertTrue(held.wait(20))
        page.locator('.project-exit').click()
        page.locator(f'#formal-project-list a[href*="/projects/{b["project_id"]}/"]').click()
        page.locator('.project-nav [data-route="script"]').click()
        expect(page.locator('#formal-nav-project-name')).to_have_text('匿名配图项目乙')
        with page.expect_response(lambda r: r.request.method == 'POST' and '/image-workspace/assistant?' in r.url) as response:
            release.set()
        self.assertEqual(response.value.status, 200, response.value.text())
        proposal = response.value.json()
        self.assertEqual(proposal['response']['affected_shot_ids'], [target])
        page.wait_for_timeout(500)
        self.assertIn('/projects/' + b['project_id'] + '/script', page.url)
        expect(page.locator('[data-image-batch]')).to_have_count(0)
        self.assertEqual(f.workspace.read_progress(f.token, **support.base.scope), before)
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tasks WHERE project_id=?',
                (b['project_id'],)).fetchone()[0], 0)
        page.locator('.project-exit').click()
        page.locator(f'#formal-project-list a[href*="/projects/{f.project_id}/"]').click()
        page.locator('.project-nav [data-route="script"]').click()
        page.locator('[data-creative-panel="images"]').click()
        expect(page.locator('[data-image-batch]')).to_be_visible(timeout=30000)
        self.assertEqual(f.workspace.read_progress(f.token, **support.base.scope), before)

    def test_frozen_target_survives_browser_selection_change_and_cancel_then_apply(self):
        support, f, script, current, target, before, b, app = self.setup_scope()
        page, origin = self.open_page(app, f.token, f.workspace_id)
        url = origin + f'/projects/{f.project_id}/script?panel=images'
        other = current['shots'][2]['shot_id']
        original_shots = script.read_current(f.token, **support.base.scope)['shots']
        original_versions = f.projects.list_project_versions(f.token, f.project_id)

        def propose():
            page.goto(url)
            page.locator('[data-creative-batch-toggle]').click()
            page.locator(f'[data-creative-select-shot="{other}"]').uncheck()
            page.locator(f'[data-creative-select-shot="{target}"]').check()
            page.locator('#creative-image-scope').select_option('all')
            with page.expect_response(lambda r: r.request.method == 'POST'
                    and '/image-workspace/assistant?' in r.url) as response:
                page.locator('[data-creative-match-images][data-image-batch-mode="replace_selected"]').click()
            self.assertEqual(response.value.status, 200, response.value.text())
            self.assertEqual(response.value.json()['response']['affected_shot_ids'], [target])
            expect(page.locator('[data-image-batch-save]')).to_be_visible()
            # Changing the current selection must not retarget a frozen proposal.
            page.locator(f'[data-creative-select-shot="{target}"]').uncheck()
            page.locator(f'[data-creative-select-shot="{other}"]').check()

        propose()
        page.reload()
        expect(page.locator('[data-image-batch-cancel]')).to_be_visible()
        self.assertEqual(f.workspace.read_progress(f.token, **support.base.scope), before)
        with page.expect_response(lambda r: r.request.method == 'POST' and '/cancel' in r.url) as cancelled:
            page.locator('[data-image-batch-cancel]').click()
        self.assertEqual(cancelled.value.status, 200, cancelled.value.text())
        self.assertEqual(f.workspace.read_progress(f.token, **support.base.scope), before)

        propose()
        with page.expect_response(lambda r: r.request.method == 'POST'
                and '/image-workspace/assistant/' in r.url) as applied:
            page.locator('[data-image-batch-save]').click()
        self.assertEqual(applied.value.status, 200, applied.value.text())
        after = f.workspace.read_progress(f.token, **support.base.scope)
        previous = {row['shot_id']: row for row in before['selections']}
        selected = {row['shot_id']: row for row in after['selections']}
        self.assertIn(target, selected)
        self.assertTrue(selected[target]['image_key'])
        self.assertEqual({k: v for k, v in selected.items() if k != target},
                         {k: v for k, v in previous.items() if k != target})
        page.reload()
        self.assertEqual(f.workspace.read_progress(f.token, **support.base.scope), after)
        self.assertEqual(script.read_current(f.token, **support.base.scope)['shots'], original_shots)
        self.assertEqual(f.projects.list_project_versions(f.token, f.project_id), original_versions)

    def test_leaving_during_save_cancels_rerecommendation_before_post(self):
        support, f, script, current, target, before, b, app = self.setup_scope()
        held, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        proposals = []
        @app.middleware('http')
        async def hold_save(request, call_next):
            if request.method == 'POST' and request.url.path.endswith('/image-workspace/assistant'):
                proposals.append(request.url.path)
            response = await call_next(request)
            if request.method == 'POST' and request.url.path.endswith('/script/table'):
                held.set()
                await run_in_threadpool(release.wait, 60)
            return response
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{f.project_id}/script?panel=images')
        page.locator('[data-creative-batch-toggle]').click()
        page.locator(f'[data-creative-select-shot="{target}"]').check()
        page.locator('[data-creative-panel="script"]').click()
        page.locator('[data-creative-edit-mode="table"]').click()
        cell = page.locator(f'[data-script-shot-key="{target}"][data-script-field="subtitle"]')
        cell.fill('保存等待期间保留的字幕')
        cell.blur()
        self.assertTrue(held.wait(20))
        page.locator('[data-creative-panel="images"]').click()
        page.locator('[data-creative-match-images][data-image-batch-mode="replace_selected"]').click()
        page.locator('.project-exit').click()
        with page.expect_response(lambda r: '/script/table?' in r.url and r.request.method == 'POST') as saved:
            release.set()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        expect(page).to_have_url(origin + '/projects')
        page.locator(f'#formal-project-list a[href*="/projects/{b["project_id"]}/"]').click()
        page.locator('.project-nav [data-route="script"]').click()
        expect(page.locator('#formal-nav-project-name')).to_have_text('匿名配图项目乙')
        page.wait_for_timeout(500)
        self.assertEqual(proposals, [])
        persisted = script.read_current(f.token, **support.base.scope)
        self.assertEqual(next(s for s in persisted['shots'] if s['shot_id']==target)['subtitle'],
            '保存等待期间保留的字幕')
        after = f.workspace.read_progress(f.token, **support.base.scope)
        self.assertEqual(after['script_revision'], persisted['revision'])
        self.assertEqual({k: v for k, v in after.items() if k != 'script_revision'},
            {k: v for k, v in before.items() if k != 'script_revision'})


    def test_other_window_deletes_target_while_save_waits_rejects_stale_recommendation(self):
        support, f, script, current, target, before, b, app = self.setup_scope()
        held, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        @app.middleware('http')
        async def hold_saved_response(request, call_next):
            response = await call_next(request)
            if request.method == 'POST' and request.url.path.endswith('/script/table'):
                held.set()
                await run_in_threadpool(release.wait, 60)
            return response
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{f.project_id}/script?panel=images')
        page.wait_for_load_state('networkidle')
        page.locator('[data-creative-batch-toggle]').click()
        page.locator(f'[data-creative-select-shot="{target}"]').check()
        page.locator('[data-creative-panel="script"]').click()
        page.locator('[data-creative-edit-mode="table"]').click()
        survivor = current['shots'][0]['shot_id']
        cell = page.locator(f'[data-script-shot-key="{survivor}"][data-script-field="subtitle"]')
        cell.fill('跨窗口删除时仍应保存的字幕')
        cell.blur()
        self.assertTrue(held.wait(20))
        page.locator('[data-creative-panel="images"]').click()
        page.locator('[data-creative-match-images][data-image-batch-mode="replace_selected"]').click()
        other_context = page.context.browser.new_context(storage_state=page.context.storage_state())
        self.addCleanup(other_context.close)
        other = other_context.new_page()
        other.set_default_timeout(15000)
        other.goto(origin + f'/projects/{f.project_id}/script')
        other.wait_for_load_state('networkidle')
        other.locator('[data-creative-edit-mode="table"]').click()
        other.locator('#creative-assistant > summary').click()
        other.locator('#creative-assistant-scope').select_option('all')
        other.locator('#formal-script-instruction').fill('删除第2镜')
        other.locator('#formal-script-instruction-submit').click()
        with other.expect_response(lambda r: r.request.method == 'POST' and '/confirm' in r.url) as removed:
            other.locator('[data-confirm-script-revision]').click()
        self.assertEqual(removed.value.status, 200, removed.value.text())
        expect(other.locator('#creative-shot-count')).to_have_text(f"{len(current['shots']) - 1} 镜")
        with page.expect_response(lambda r: r.request.method == 'POST' and '/image-workspace/assistant?' in r.url) as stale:
            release.set()
        self.assertEqual(stale.value.request.post_data_json['selected_shot_ids'], [target])
        self.assertIn(stale.value.status, (400, 409), stale.value.text())
        self.assertEqual(stale.value.json()['detail']['code'], 'IMAGE_SCOPE_MISMATCH')
        print('Stale recommendation:', stale.value.status, stale.value.text())
        persisted = script.read_current(f.token, **support.base.scope)
        self.assertNotIn(target, {s['shot_id'] for s in persisted['shots']})
        self.assertEqual(persisted['shots'][0]['subtitle'], '跨窗口删除时仍应保存的字幕')
        self.assertEqual(persisted['shots'][1]['shot_id'], current['shots'][2]['shot_id'])
        after = f.workspace.read_progress(f.token, **support.base.scope)
        before_by_id = {s['shot_id']: s for s in before['selections']}
        self.assertEqual(after['selections'], [before_by_id[s['shot_id']] for s in after['selections']])
        self.assertEqual(persisted['base_version_id'], current['base_version_id'])
        expect(page.locator('[data-image-batch]')).to_have_count(0)
        page.reload()
        expect(page.locator('[data-image-shot]')).to_have_count(len(current['shots']) - 1)
        self.assertEqual(script.read_current(f.token, **support.base.scope)['shots'], persisted['shots'])


    def test_frozen_proposal_deleted_in_other_window_cannot_apply_and_can_cancel(self):
        support, f, script, current, target, before, b, app = self.setup_scope()
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{f.project_id}/script?panel=images')
        page.wait_for_load_state('networkidle')
        page.locator('[data-creative-batch-toggle]').click()
        page.locator(f'[data-creative-select-shot="{target}"]').check()
        with page.expect_response(lambda r: r.request.method == 'POST' and '/image-workspace/assistant?' in r.url) as proposed:
            page.locator('[data-creative-match-images][data-image-batch-mode="replace_selected"]').click()
        self.assertEqual(proposed.value.status, 200, proposed.value.text())
        proposal = proposed.value.json()
        self.assertEqual(proposal['response']['affected_shot_ids'], [target])
        expect(page.locator('[data-image-batch-save]')).to_be_visible()
        other_context = page.context.browser.new_context(storage_state=page.context.storage_state())
        self.addCleanup(other_context.close)
        other = other_context.new_page()
        other.set_default_timeout(15000)
        other.goto(origin + f'/projects/{f.project_id}/script')
        other.wait_for_load_state('networkidle')
        other.locator('[data-creative-edit-mode="table"]').click()
        other.locator('#creative-assistant > summary').click()
        other.locator('#creative-assistant-scope').select_option('all')
        other.locator('#formal-script-instruction').fill('删除第2镜')
        other.locator('#formal-script-instruction-submit').click()
        with other.expect_response(lambda r: r.request.method == 'POST' and '/confirm' in r.url) as removed:
            other.locator('[data-confirm-script-revision]').click()
        self.assertEqual(removed.value.status, 200, removed.value.text())
        expect(other.locator('#creative-shot-count')).to_have_text(f"{len(current['shots']) - 1} 镜")
        before_apply = f.workspace.read_progress(f.token, **support.base.scope)
        with page.expect_response(lambda r: r.request.method == 'POST' and '/image-workspace/assistant/' in r.url) as applied:
            page.locator('[data-image-batch-save]').click()
        self.assertEqual(applied.value.status, 409, applied.value.text())
        self.assertEqual(applied.value.json()['detail']['code'], 'DRAFT_REVISION_CONFLICT')
        print('Stale application:', applied.value.text())
        expect(page.locator('[data-image-batch-notice]')).not_to_be_empty()
        self.assertEqual(f.workspace.read_progress(f.token, **support.base.scope), before_apply)
        with page.expect_response(lambda r: r.request.method == 'POST' and '/cancel' in r.url) as cancelled:
            page.locator('[data-image-batch-cancel]').click()
        self.assertEqual(cancelled.value.status, 200, cancelled.value.text())
        expect(page.locator('[data-image-batch]')).to_contain_text('已取消')
        self.assertEqual(f.workspace.read_progress(f.token, **support.base.scope), before_apply)
        page.reload()
        expect(page.locator('[data-image-shot]')).to_have_count(len(current['shots']) - 1)
        expect(page.locator('[data-image-batch-cancel]')).to_have_count(0)
        persisted = script.read_current(f.token, **support.base.scope)
        self.assertNotIn(target, {shot['shot_id'] for shot in persisted['shots']})
        self.assertEqual(persisted['shots'][1]['shot_id'], current['shots'][2]['shot_id'])
        self.assertEqual(persisted['base_version_id'], current['base_version_id'])
        self.assertEqual(f.workspace.read_progress(f.token, **support.base.scope), before_apply)
