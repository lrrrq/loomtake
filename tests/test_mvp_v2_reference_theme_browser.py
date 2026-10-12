"""Reference caption is visible as image evidence, never adopted into saved copy."""
from copy import deepcopy
import unittest
from playwright.sync_api import expect
from mvp_v2.project_image_authority import ManualImageAnalysisInput, ManualProjectImageAnalysisCommandService
from tests import test_mvp_v2_image_scope_recovery_browser as scope_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class ReferenceThemeBrowserTests(unittest.TestCase):
    def test_reference_theme_preview_and_refresh_preserve_customer_script(self):
        helper = scope_fixture.ImageScopeRecoveryBrowserTests('runTest')
        self.addCleanup(helper.doCleanups)
        support, f, script, current, _, _, _, app = helper.setup_scope()
        scope = support.base.scope
        images = f.workspace.read_current(f.token, **scope)
        image_id = images['shots'][0]['selected_image_key']
        brief_id, snapshot_id = f.pool.current_confirmed_brief_binding(
            f.token, **scope, expected_project_lock_version=images['project_version'])
        pool = f.pool.resolve_for_delivery(f.token, **scope, brief_version_id=brief_id,
            brief_snapshot_id=snapshot_id, expected_project_lock_version=images['project_version'])
        image = next(a for a in pool.assets if a.image_id == image_id)
        summary = image.business_summary
        caption = summary.caption + '；原图主题：远山计划（来源记录）'
        ManualProjectImageAnalysisCommandService(f.pool).record_analysis(f.token, **scope,
            expected_project_lock_version=images['project_version'],
            submission=ManualImageAnalysisInput(image_id=image_id, image_sha256=image.image_sha256,
                brief_version_id=brief_id, brief_snapshot_id=snapshot_id, status='pass',
                caption=caption, tags=summary.tags, subjects=summary.subjects,
                scene=summary.scene, purpose=summary.purpose, quality=summary.quality,
                brand=summary.brand, evidence=('匿名参考图主题记录，非客户主题确认',)))
        before = deepcopy(script.read_current(f.token, **scope)['shots'])
        self.assertNotIn('远山计划', str(before))
        versions = deepcopy(f.projects.list_project_versions(f.token, f.project_id))
        progress = deepcopy(f.workspace.read_progress(f.token, **scope))
        page, origin = browser_fixture.ManualReceiptBrowserTests.open_page(self, app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{f.project_id}/script')
        for reload in (False, True):
            if reload: page.reload()
            page.locator('[data-creative-change-image]').click()
            picker = page.locator('#creative-image-picker')
            expect(picker.locator('.creative-picker-intent')).not_to_contain_text('远山计划')
            card = picker.locator('[data-picker-candidate]').filter(has_text='远山计划')
            expect(card).to_have_count(1)
            expect(card).to_be_visible()
            expect(card.locator('strong')).to_have_text(caption)
            expect(card.locator('.creative-picker-source')).not_to_be_empty()
            card.click()
            expect(picker.locator('.creative-picker-intent')).not_to_contain_text('远山计划')
            page.locator('[data-picker-close]').click()
        self.assertEqual(script.read_current(f.token, **scope)['shots'], before)
        self.assertEqual(f.workspace.read_progress(f.token, **scope), progress)
        self.assertEqual(f.projects.list_project_versions(f.token, f.project_id), versions)
