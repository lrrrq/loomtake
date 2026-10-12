"""Original-task recovery through browser UI, without touching real projects."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as fixture_module
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID

class _ScriptRecoveryFixture(fixture_module._CreativeFixture):
    def __init__(self):
        super().__init__()
        self.script_revision = 1
        self.confirm_payloads=[]
        self.receipt_reads=0
        self.complete_before_failure=True
        self.completed=False
        self.effects=0
    def response(self,method,path):
        route=path.split('?')[0]
        if route == '/api/agent/tasks/unknown-save':
            self.receipt_reads+=1
            return 200,{'task':{'task_id':'unknown-save','status':'completed' if self.completed else 'waiting_confirmation'},'response':{}}
        if route == '/api/agent/tasks/unknown-save/confirm':
            self.confirm_payloads.append(deepcopy(self.body))
            if len(self.confirm_payloads)>1 or self.complete_before_failure:
                if not self.completed:self.effects+=1
                self.completed=True;self.script_revision=0
            if len(self.confirm_payloads)==1:return 500,{'detail':{'code':'INTERNAL_ERROR','message':'匿名结果丢失'}}
            return 200,{'task':{'task_id':'unknown-save','status':'completed'},'response':{}}
        return super().response(method,path)

class ScriptConfirmationRecoveryBrowserTests(unittest.TestCase):
    def setUp(self):
        with patch.object(fixture_module,'_CreativeFixture',_ScriptRecoveryFixture):
            fixture_module.CreativeWorkspaceBrowserTests.setUp(self)
        self.page.goto(f'{self.base}/projects/{PROJECT_ID}/script')
        expect(self.page.locator('#creative-open-save')).to_be_enabled()
        self.page.locator('#creative-open-save').click()
        self.page.evaluate('''() => renderFormalScriptRevisionProposal({task_id:'unknown-save',response:{
            type:'action_proposal',proposal_digest:'original-digest',summary:'匿名保存预览',preview:{changes:[]}}})''')

    def first_unknown(self):
        self.page.locator('[data-confirm-script-revision]').click()
        expect(self.page.locator('#formal-script-save-proposal')).to_contain_text('保存结果待核实')
        pending=self.page.evaluate("JSON.parse(sessionStorage.getItem(`mplus-version-confirm:${state.agentWorkspaceId}:${formalRoute().projectId}`))")
        self.assertEqual(pending['response']['proposal_digest'],'original-digest')
        self.assertTrue(pending['result_unknown'])

    def resume_after_reload(self):
        self.page.reload()
        self.page.locator('#creative-open-save').click()
        self.page.locator('#formal-script-save').click()
        expect(self.page.locator('#formal-script-save-proposal')).to_contain_text('上次保存结果待核实')
        self.page.locator('[data-confirm-script-revision]').click()
        expect(self.page.locator('#formal-script-save-proposal')).to_contain_text('文字与配图已保存为一个新版本')

    def test_completed_receipt_after_500_and_reload_never_posts_confirmation_again(self):
        self.first_unknown();self.resume_after_reload()
        self.assertEqual(len(self.fixture.confirm_payloads),1)
        self.assertEqual(self.fixture.receipt_reads,1)
        self.assertEqual(self.fixture.effects,1)
        self.assertFalse(self.errors)

    def test_unfinished_receipt_replays_original_confirmation_digest(self):
        self.fixture.complete_before_failure=False
        self.first_unknown();self.resume_after_reload()
        self.assertEqual(self.fixture.confirm_payloads,[{'proposal_digest':'original-digest'}]*2)
        self.assertEqual(self.fixture.receipt_reads,1)
        self.assertEqual(self.fixture.effects,1)
        self.assertFalse(self.errors)

    def test_late_confirmation_does_not_replace_other_page(self):
        held=[]
        self.page.route('**/api/agent/tasks/unknown-save/confirm',lambda route:held.append(route))
        with self.page.expect_request('**/api/agent/tasks/unknown-save/confirm'):
            self.page.locator('[data-confirm-script-revision]').click()
        self.page.evaluate(f"navigateFormal('/projects/{PROJECT_ID}/materials')")
        expect(self.page.locator('#page-materials')).to_be_visible()
        with self.page.expect_response('**/api/agent/tasks/unknown-save/confirm'):
            held[0].fulfill(status=200,content_type='application/json',body=json.dumps({'task':{'status':'completed'},'response':{}}))
        expect(self.page.locator('#page-materials')).to_be_visible()
        self.assertNotIn('文字与配图已保存为一个新版本',self.page.locator('#formal-script-save-proposal').inner_text())
        self.assertFalse(self.errors)
