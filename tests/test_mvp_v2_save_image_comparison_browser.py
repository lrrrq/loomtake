"""Browser proof for real authorised image previews in complete-version proposals."""
import base64
import unittest
from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as fixture_module
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID, IMAGE_BYTES


class SaveImageComparisonBrowserTests(unittest.TestCase):
    setUp = fixture_module.CreativeWorkspaceBrowserTests.setUp

    def test_saved_images_reference_and_missing_before_render_without_losing_text_diff(self):
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        expect(page.locator('[data-image-shot="0"]')).to_be_visible()
        seen = []
        page.on('request', lambda request: seen.append(request) if '/images/candidate-' in request.url and '/preview?' in request.url else None)
        page.evaluate('''inline => {
            state.formalImageWorkspace.shots[1].reference_image = {inline_preview:inline};
            delete state.formalImageWorkspace.shots[2].reference_image;
            const response = {type:'action_proposal', proposal_digest:'anonymous-save', before:{version_id:state.formalImageWorkspace.base_version_id},
                preview:{changes:[{shot_no:'1',field:'sound',before:'旧声音',after:'新声音'}], image_changes:[
                    {shot_id:'shot-stable-1',shot_no:'1',before_image_key:'candidate-1',after_image_key:'candidate-2',before_caption:'原确认画面',after_caption:'更换后画面',reuse_reason:'同场景不同作用',review_impact:'重新核对本镜配图'},
                    {shot_id:'shot-stable-2',shot_no:'2',before_image_key:'',after_image_key:'candidate-2',review_impact:'新版整片确认需重做'},
                    {shot_id:'shot-stable-3',shot_no:'3',before_image_key:'',after_image_key:'candidate-3',review_impact:'核对新增配图'}]}};
            renderFormalScriptRevisionProposal({task_id:'anonymous-save',response});
            document.getElementById('creative-save').open = true;
        }''', 'data:image/png;base64,' + base64.b64encode(IMAGE_BYTES).decode())
        panel = page.locator('#formal-script-save-proposal')
        for index, side in [(0,'before'), (0,'after'), (1,'before'), (1,'after'), (2,'after')]:
            expect(panel.locator(f'[data-save-image-index="{index}"][data-save-image-side="{side}"] img')).to_have_count(1)
        expect(panel.locator('[data-save-image-index="2"][data-save-image-side="before"]')).to_have_text('无图')
        expect(panel).to_contain_text('原版本参考图，未确认')
        expect(panel).to_contain_text('同场景不同作用')
        expect(panel).to_contain_text('重新核对本镜配图')
        expect(panel).to_contain_text('旧声音')
        expect(panel).to_contain_text('新声音')
        page.wait_for_function("[...document.querySelectorAll('#formal-script-save-proposal [data-save-image-index] img')].every(image => image.complete && image.naturalWidth > 0)")
        self.assertTrue(all(request.headers.get('x-mplus-agent-auth') for request in seen))
        self.assertFalse(self.errors)
