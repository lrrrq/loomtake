"""RV05-01: deletion retains confirmed image identity and reopens review."""
import unittest
from hashlib import sha256

from openpyxl import load_workbook
from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.task_service import TaskService
from tests.test_mvp_v2_library_browse import library_confirmation_agent
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests import test_mvp_v2_formal_review_workspace as review_fixture


class DeleteImageInheritanceBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_middle_delete_keeps_confirmed_images_and_reopens_new_adjacency(self):
        f = review_fixture.FormalReviewWorkspaceTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f._confirm_current()  # Anonymous historical approved parent, not a customer approval.
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        script, images = f.base.base.workspace, f.base.workspace
        original = script.read_current(f.token, **scope)
        before_images = images.read_current(f.token, **scope)
        parent = f.review.services.delivery.resolve(f.token, f.project_id, original['base_version_id'])
        old_paths = [f.base.resolver.resolve_stage_artifact(parent.run_id, stage).path
            for stage in ('script_plan', 'script_draft', 'visual_binding')]
        old_paths.append(f.base.resolver.resolve_artifact(parent.run_id, 'workbook').path)
        old_bytes = [p.read_bytes() for p in old_paths]
        agent = library_confirmation_agent(root=f.root, store=f.store, projects=f.base.projects, service=None)
        agent.script_revision = FormalRevisionService(projects=f.base.projects,
            tasks=TaskService(f.store), workspace=script, image_workspace=images, writes_enabled=True)
        app = local_ui.create_app(agent_api=agent, formal_script_service=script,
            formal_image_service=images, formal_review_service=f.review,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.root/'runtime'), data_root=f.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{f.project_id}/script')
        page.wait_for_load_state('networkidle')
        page.locator('[data-creative-edit-mode="table"]').click()
        page.locator('#creative-assistant > summary').click()
        page.locator('#creative-assistant-scope').select_option('all')
        page.locator('#formal-script-instruction').fill('删除第5镜')
        expect(page.locator('#formal-script-instruction')).to_have_value('删除第5镜')
        try:
            with page.expect_response(lambda r: r.request.method == 'POST' and '/api/' in r.url) as instruction:
                page.locator('#formal-script-instruction-submit').click()
            print('Delete proposal:', instruction.value.status, instruction.value.text())
        finally:
            print('Delete notice:', page.locator('body').inner_text()[-9000:])
        with page.expect_response(lambda r: r.request.method == 'POST' and '/confirm' in r.url) as removed:
            page.locator('[data-confirm-script-revision]').click()
        self.assertEqual(removed.value.status, 200, removed.value.text())
        expect(page.locator('#creative-shot-count')).to_have_text(f"{len(original['shots']) - 1} 镜")
        for action, count in [('undo', len(original['shots'])), ('redo', len(original['shots']) - 1)]:
            with page.expect_response(lambda r: r.request.method == 'POST' and f'/script/{action}?' in r.url) as history:
                page.locator('#formal-script-' + action).click()
            self.assertEqual(history.value.status, 200, history.value.text())
            expect(page.locator('#creative-shot-count')).to_have_text(f'{count} 镜')
        page.locator('#creative-save > summary').click()
        page.locator('#formal-script-save-reason').fill('删除中间镜头，保留其他镜头的确认图片')
        with page.expect_response(lambda r: r.request.method == 'POST' and '/script/save?' in r.url) as proposed:
            page.locator('#formal-script-save').click()
        self.assertEqual(proposed.value.status, 200, proposed.value.text())
        task_id = proposed.value.json()['task_id']
        with page.expect_response(lambda r: r.request.method == 'POST' and f'/tasks/{task_id}/confirm' in r.url, timeout=60000) as saved:
            page.locator('[data-confirm-script-revision]').click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        self.assertEqual(saved.value.json()['task']['status'], 'completed', saved.value.text())
        expect(page.locator('#formal-script-save-proposal')).to_contain_text('已保存为一个新版本', timeout=60000)
        page.reload()
        page.locator('[data-creative-panel="images"]').click()
        expected = [s for index, s in enumerate(original['shots']) if index != 4]
        expect(page.locator('[data-image-shot]')).to_have_count(len(expected))
        current = script.read_current(f.token, **scope)
        after_images = images.read_current(f.token, **scope)
        self.assertEqual([s['shot_id'] for s in current['shots']], [s['shot_id'] for s in expected])
        original_keys = {s['shot_id']: s['selected_image_key'] for s in before_images['shots']}
        self.assertTrue(all(original_keys.values()))
        self.assertEqual({s['shot_id']: s['selected_image_key'] for s in after_images['shots']},
            {s['shot_id']: original_keys[s['shot_id']] for s in expected})
        page.goto(origin + f'/projects/{f.project_id}/review')
        expect(page.locator('#formal-delivery-excel')).to_be_disabled(timeout=60000)
        with page.expect_response(lambda r: r.request.method == 'POST' and '/review/start' in r.url) as started:
            page.locator('#formal-review-start').click()
        self.assertEqual(started.value.status, 200, started.value.text())
        review = started.value.json()
        affected = [issue for issue in review['issues'] if '4->5' in issue.get('affected_adjacent_edges', [])]
        self.assertTrue(affected, review['issues'])
        self.assertTrue(any(issue['status'] == 'open' for issue in affected), affected)
        page.reload()
        expect(page.locator('#formal-delivery-excel')).to_be_disabled(timeout=60000)
        child = f.review.services.delivery.resolve(f.token, f.project_id, current['base_version_id'])
        workbook = load_workbook(f.base.resolver.resolve_artifact(child.run_id, 'workbook').path)
        self.assertEqual(len(workbook['脚本']._images), len(expected))
        parent_book = load_workbook(old_paths[-1])
        parent_pixels = {str(image.anchor._from.row - 5): sha256(image._data()).hexdigest()
            for image in parent_book['脚本']._images}
        child_pixels = {str(image.anchor._from.row - 5): sha256(image._data()).hexdigest()
            for image in workbook['脚本']._images}
        self.assertEqual(child_pixels, {str(index): parent_pixels[shot['shot_no']]
            for index, shot in enumerate(expected, 1)})
        parent_book.close()

        self.assertEqual(workbook['相邻镜头复核'].max_row - 1, len(expected) - 1)
        workbook.close()
        self.assertEqual([p.read_bytes() for p in old_paths], old_bytes)
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_quality_final_confirmations WHERE version_id=?',
                (current['base_version_id'],)).fetchone()[0], 0)
