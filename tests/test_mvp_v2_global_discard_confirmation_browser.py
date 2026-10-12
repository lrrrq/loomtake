"""UI interactions with anonymous transactional receipts for full draft actions."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from urllib.parse import urlparse
from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as fixture_module
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID

class _FullActionFixture(fixture_module._CreativeFixture):
    def __init__(self):
        super().__init__()
        self.original_shots = deepcopy(self.shots)
        self.image_discard_failure = False
        self.image_discard_conflict = False
        self.image_discards = []
        self.text_discards = []
        self.previews = []
        self.confirmations = []
        self.receipts = {}
        self.confirmation_failure = False
        self.confirmation_error_code = 'ACTION_RESULT_UNKNOWN'
        self.confirmed_effects = 0

    def response(self, method, path):
        route = urlparse(path).path
        if route.endswith('/script/discard'):
            self.text_discards.append(deepcopy(self.body))
            self.shots = deepcopy(self.original_shots)
            self.script_revision = 0
            return 200, self.script()
        if route.endswith('/image-workspace/progress/discard'):
            self.image_discards.append(deepcopy(self.body))
            if self.image_discard_conflict:
                self.image_discard_conflict = False
                return 409, {'detail':{'code':'IMAGE_PROGRESS_CONFLICT','message':'匿名进度冲突'}}
            if self.image_discard_failure:
                self.image_discard_failure = False
                return 503, {'detail':{'code':'UNAVAILABLE','message':'匿名图片阶段失败'}}
            self.progress_selections.clear()
            self.progress_revision += 1
            return super().response('GET', path.replace('/discard',''))
        if route.endswith('/image-workspace/confirmation-preview'):
            self.previews.append(deepcopy(self.body))
            items = [{'shot_id':s['shot_id'],'shot_no':s['shot_id'].rsplit('-',1)[1], 'selection':s,
                      'processable':s['shot_id'] != 'shot-stable-2','reason':'缺少复用说明' if s['shot_id']=='shot-stable-2' else ''} for s in self.body['selections']]
            return 200, {'items':items,'preview_digest':'anonymous-preview', 'processable_count':sum(i['processable'] for i in items),'pending_count':sum(not i['processable'] for i in items)}
        if route.endswith('/image-workspace/confirm-progress'):
            self.confirmations.append(deepcopy(self.body))
            key = self.body['idempotency_key']
            if key not in self.receipts:
                for s in self.body['selections']:
                    if s['shot_id'] in self.body['accepted_shot_ids']:
                        self.progress_selections[s['shot_id']] = {**s,'checked':True}
                self.progress_revision += 1
                _, result = super().response('GET', path.replace('/confirm-progress','/progress'))
                self.receipts[key] = {**result,'confirmed_count':len(self.body['accepted_shot_ids']),'pending_count':len(self.body['selections'])-len(self.body['accepted_shot_ids'])}
                self.confirmed_effects += 1
            if self.confirmation_failure:
                self.confirmation_failure = False
                return 503, {'detail':{'code':self.confirmation_error_code,'message':'匿名结果未知'}}
            return 200, self.receipts[key]
        return super().response(method,path)

class GlobalDiscardConfirmationBrowserTests(unittest.TestCase):
    def setUp(self):
        with patch.object(fixture_module,'_CreativeFixture',_FullActionFixture):
            fixture_module.CreativeWorkspaceBrowserTests.setUp(self)
        self.page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        expect(self.page.locator('[data-image-shot="0"]')).to_be_visible()

    def seed_image_changes(self):
        self.page.evaluate('''async () => {
            for (const shot of state.formalImageWorkspace.shots) {shot.explicitly_changed=true;shot.confirmed=false;creativeState().imageChanges.add(shot.shot_id);}
            renderFormalImageWorkspace(state.formalImageWorkspace);captureFormalImageInputs();await flushCreativeProgress({manual:true});
        }''')

    def test_global_discard_retries_only_image_phase_same_key_and_preserves_history_materials(self):
        self.seed_image_changes()
        self.fixture.script_revision = 1
        self.fixture.shots[0]['sound'] = '待放弃文字'
        self.page.evaluate('async () => {await loadFormalScript(formalRoute().projectId);}')
        self.fixture.image_discard_failure = True
        dialogs=[]
        self.page.on('dialog',lambda dialog:(dialogs.append(dialog.message),dialog.accept()))
        self.page.locator('#formal-script-discard').click()
        expect(self.page.locator('#formal-script-save-state')).to_contain_text('放弃操作尚未完成')
        self.page.locator('#formal-script-discard').click()
        expect(self.page.locator('#toast')).to_contain_text('全部草稿修改已放弃')
        self.assertEqual(len(dialogs),1)
        self.assertIn('全稿配图草稿',dialogs[0])
        self.assertEqual(len(self.fixture.text_discards),1)
        self.assertEqual(self.fixture.image_discards[0],self.fixture.image_discards[1])
        self.assertEqual(self.fixture.progress_selections,{})
        self.assertEqual(self.fixture.shots,self.fixture.original_shots)
        self.assertFalse(any('/assets/' in route or '/versions/' in route or '/images/' in route for route,_ in self.fixture.writes))
        self.assertFalse(self.errors)

    def test_discard_scope_read_failure_keeps_unsubmitted_text_without_confirmation(self):
        page = self.page
        page.locator('[data-creative-panel="script"]').click()
        page.locator('[data-creative-edit-mode="table"]').click()
        dialogs = []
        page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
        page.route('**/image-workspace/progress?**', lambda route: route.fulfill(
            status=409, json={'detail': {'code': 'IMAGE_PROGRESS_CONFLICT', 'message': '匿名范围读取冲突'}}))
        page.evaluate('''() => {
            const field = document.querySelector('[data-script-shot="1"][data-script-field="visual"]');
            field.value = '范围尚未确认，必须保留输入';
            field.dispatchEvent(new Event('input', {bubbles:true}));
            document.querySelector('#formal-script-discard').click();
        }''')
        expect(page.locator('#formal-script-save-state')).to_contain_text('当前修改已保留')
        expect(page.locator('[data-script-shot="1"][data-script-field="visual"]')).to_have_value('范围尚未确认，必须保留输入')
        self.assertEqual(page.evaluate('state.formalScriptDirtyCells.size'), 1)
        self.assertEqual(dialogs, [])
        self.assertEqual(self.fixture.text_discards, [])
        self.assertEqual(self.fixture.image_discards, [])

    def test_batch_selected_includes_filter_hidden_shots_and_reports_partial_counts(self):
        self.seed_image_changes()
        page=self.page
        page.locator('[data-creative-batch-toggle]').click()
        page.locator('[data-creative-select-visible]').click()
        page.locator('#creative-shot-search').fill('画面 3')
        page.locator('[data-preview-image-confirmation="selected"]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('本次 3 镜 · 可确认 2 镜 · 待处理 1 镜')
        self.assertEqual(len(self.fixture.previews[-1]['selections']),3)
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('已确认 2 镜')
        self.assertEqual(self.fixture.confirmations[-1]['accepted_shot_ids'],['shot-stable-1','shot-stable-3'])
        self.assertFalse(self.fixture.progress_selections['shot-stable-2']['checked'])

    def test_unknown_confirmation_survives_reload_and_replays_identical_payload_once(self):
        self.seed_image_changes()
        self.fixture.confirmation_failure=True
        page=self.page
        page.locator('[data-preview-image-confirmation="all"]').click()
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('结果待核实')
        page.reload()
        page.locator('[data-preview-image-confirmation="all"]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('上次确认结果待核实')
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('已确认 2 镜')
        self.assertEqual(self.fixture.confirmations[0],self.fixture.confirmations[1])
        self.assertEqual(self.fixture.confirmed_effects,1)

    def test_server_error_with_code_still_keeps_original_confirmation_request(self):
        self.seed_image_changes()
        self.fixture.confirmation_failure = True
        self.fixture.confirmation_error_code = 'UNAVAILABLE'
        page = self.page
        page.locator('[data-preview-image-confirmation="all"]').click()
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('结果待核实')
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('已确认 2 镜')
        self.assertEqual(self.fixture.confirmations[0], self.fixture.confirmations[1])
        self.assertEqual(self.fixture.confirmed_effects, 1)

    def test_new_image_choice_after_preview_cannot_be_overwritten_by_old_confirmation(self):
        self.seed_image_changes()
        page = self.page
        page.locator('[data-preview-image-confirmation="all"]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('可确认 2 镜')
        # Select through the actual shot control; confirmation must flush this
        # still-pending edit before evaluating the earlier preview revision.
        page.locator('[data-image-shot-select="0"]').select_option('candidate-2')
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('请重新预览确认范围')
        self.assertEqual(self.fixture.confirmations, [])
        self.assertEqual(self.fixture.progress_selections['shot-stable-1']['image_key'], 'candidate-2')
        expect(page.locator('[data-image-shot-select="0"]')).to_have_value('candidate-2')

    def test_known_discard_conflict_releases_request_and_requires_fresh_scope_confirmation(self):
        self.seed_image_changes()
        self.fixture.image_discard_conflict = True
        page = self.page
        dialogs = []
        page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.accept()))
        page.locator('#formal-script-discard').click()
        expect(page.locator('#formal-script-save-state')).to_contain_text('请重新核对放弃范围')
        self.assertIsNone(page.evaluate("sessionStorage.getItem(`mplus-discard-all:${state.agentWorkspaceId}:${formalRoute().projectId}`)"))
        self.assertEqual(len(self.fixture.progress_selections), 3)
        page.locator('#formal-script-discard').click()
        expect(page.locator('#toast')).to_contain_text('全部草稿修改已放弃')
        self.assertEqual(len(dialogs), 2)
        self.assertNotEqual(self.fixture.image_discards[0]['idempotency_key'], self.fixture.image_discards[1]['idempotency_key'])

    def test_image_only_change_enables_save_and_discard_without_reload(self):
        page = self.page
        expect(page.locator('#formal-script-save')).to_be_disabled()
        expect(page.locator('#formal-script-discard')).to_be_disabled()
        page.locator('[data-image-shot-select="0"]').select_option('candidate-2')
        expect(page.locator('#formal-script-save')).to_be_enabled()
        expect(page.locator('#formal-script-discard')).to_be_enabled()
        expect(page.locator('#formal-script-revision')).to_contain_text('待保存版本')
        page.evaluate('async () => { await flushCreativeProgress({manual:true}); }')
        self.assertEqual(self.fixture.progress_selections['shot-stable-1']['image_key'], 'candidate-2')
        page.on('dialog', lambda dialog: dialog.accept())
        page.locator('#formal-script-discard').click()
        expect(page.locator('#toast')).to_contain_text('全部草稿修改已放弃')
        expect(page.locator('#formal-script-discard')).to_be_disabled()
        self.assertFalse(self.errors)
