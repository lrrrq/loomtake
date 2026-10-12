"""Anonymous multi-image UI recovery tests; authority tests cover database writes."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as fixture_module
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class _RecoveryFixture(fixture_module._CreativeFixture):
    def __init__(self):
        super().__init__()
        self.analysis_enabled = True
        _, self.readiness = super().response('GET', f'/api/formal/projects/{PROJECT_ID}/images')
        self.readiness['images'][2]['status'] = 'pass'
        self.readiness['images'][2]['summary']['evidence_status'] = 'verified'
        self.receipts = {}
        self.confirm_payloads = []
        self.lose_response = False
        self.reject_once = False

    def response(self, method, path):
        route = urlparse(path).path
        if '/images/analysis-confirmations/' in route:
            key = route.rsplit('/', 1)[1]
            return 200, deepcopy(self.receipts.get(key, {'confirmation_status': 'not_found'}))
        if route.endswith('/images/analysis-confirmations'):
            self.confirm_payloads.append(deepcopy(self.body))
            if self.reject_once:
                self.reject_once = False
                return 503, {'detail': {'message': '匿名暂时中断'}}
            for item in self.body['selections']:
                next(image for image in self.readiness['images'] if image['image_key'] == item['image_key'])['summary']['evidence_status'] = 'verified'
            result = {**deepcopy(self.readiness), 'confirmation_status': 'completed',
                      'newly_confirmed_count': len(self.body['selections']), 'already_confirmed_count': 0}
            self.receipts[self.body['idempotency_key']] = result
            if self.lose_response:
                self.lose_response = False
                return 503, {'detail': {'message': '匿名响应中断'}}
            return 200, result
        if route.endswith('/analysis') and method == 'POST':
            image = next(image for image in self.readiness['images'] if image['image_key'] == route.split('/')[-2])
            image['summary']['caption'] = self.body['caption']
            image['analysis_key'] += '-saved'
            return 200, deepcopy(self.readiness)
        if route.endswith('/images'):
            return 200, deepcopy(self.readiness)
        return super().response(method, path)


class AnalysisRecoveryBrowserTests(unittest.TestCase):
    def setUp(self):
        with patch.object(fixture_module, '_CreativeFixture', _RecoveryFixture):
            fixture_module.CreativeWorkspaceBrowserTests.setUp(self)
        self.page.goto(f'{self.base}/projects/{PROJECT_ID}/materials')
        expect(self.page.locator('[data-formal-image-key="candidate-2"]')).to_be_visible()

    def form(self, number):
        return self.page.locator(f'[data-formal-image-key="candidate-{number}"] [data-formal-image-form]')

    def edit(self, number, value):
        form = self.form(number)
        form.locator('..').locator('summary').first.click()
        form.locator('[name="caption"]').fill(value)
        form.locator('[name="evidence"]').fill('匿名人工观察')
        return form

    def test_saving_one_card_preserves_other_card_input_and_clears_only_saved_draft(self):
        first = self.edit(1, '第一张修正')
        self.edit(2, '第二张尚未提交')
        first.get_by_role('button', name='保存人工理解').click()
        expect(self.form(1).locator('[name="caption"]')).to_have_value('第一张修正')
        expect(self.form(2).locator('[name="caption"]')).to_have_value('第二张尚未提交')
        expect(self.form(2).locator('[data-analysis-draft-notice]')).to_contain_text('已保留')
        expect(self.form(1).locator('[data-analysis-draft-notice]')).to_have_count(0)
        self.assertFalse(self.errors)

    def test_only_pending_analyses_selected_and_real_result_counts_shown(self):
        page = self.page
        expect(page.locator('[data-analysis-select="candidate-3"]')).to_be_disabled()
        page.locator('[data-analysis-select-visible]').click()
        expect(page.locator('#creative-analysis-count')).to_contain_text('已选 2 张')
        page.locator('#creative-analysis-confirm').click()
        expect(page.locator('#creative-analysis-result')).to_contain_text('本次新增确认 2 张 · 原已确认 0 张')
        self.assertEqual(len(self.fixture.confirm_payloads[0]['selections']), 2)
        expect(page.locator('#creative-analysis-confirm')).to_be_disabled()

    def test_lost_success_response_reads_receipt_without_second_post(self):
        self.fixture.lose_response = True
        page = self.page
        page.locator('[data-analysis-select="candidate-2"]').check()
        page.locator('#creative-analysis-confirm').click()
        expect(page.locator('#creative-analysis-result')).to_contain_text('结果暂时未知')
        page.reload()
        expect(page.locator('#creative-analysis-confirm')).to_have_text('读取原确认结果 / 重试')
        page.locator('#creative-analysis-confirm').click()
        expect(page.locator('#creative-analysis-result')).to_contain_text('本次新增确认 1 张')
        self.assertEqual(len(self.fixture.confirm_payloads), 1)

    def test_missing_receipt_retries_identical_original_payload_despite_selection_change(self):
        self.fixture.reject_once = True
        page = self.page
        page.locator('[data-analysis-select="candidate-2"]').check()
        page.locator('#creative-analysis-confirm').click()
        expect(page.locator('#creative-analysis-result')).to_contain_text('结果暂时未知')
        page.locator('[data-analysis-select="candidate-1"]').check()
        page.locator('#creative-analysis-confirm').click()
        expect(page.locator('#creative-analysis-result')).to_contain_text('本次新增确认 1 张')
        self.assertEqual(self.fixture.confirm_payloads[0], self.fixture.confirm_payloads[1])
        expect(page.locator('[data-analysis-select="candidate-1"]')).to_be_checked()

    def test_changed_analysis_removes_selection_with_notice_and_preserves_unsaved_input(self):
        first = self.edit(1, '保存第一张')
        self.edit(2, '第二张本地输入')
        self.page.locator('[data-analysis-select="candidate-2"]').check()
        self.fixture.readiness['images'][1]['analysis_key'] = 'changed-analysis-2'
        self.fixture.readiness['images'][1]['summary']['caption'] = '另一处已更新'
        first.get_by_role('button', name='保存人工理解').click()
        expect(self.page.locator('#creative-analysis-result')).to_contain_text('1 张原选择')
        expect(self.page.locator('[data-analysis-select="candidate-2"]')).not_to_be_checked()
        expect(self.form(2).locator('[name="caption"]')).to_have_value('第二张本地输入')
        expect(self.form(2).locator('[data-analysis-draft-notice]')).to_contain_text('分析结果已更新')

    def test_manual_conflict_preserves_inputs_and_loads_new_analysis(self):
        first = self.edit(1, '第一张本地输入')
        self.edit(2, '第二张本地输入')
        def conflict(route):
            self.fixture.readiness['images'][0]['analysis_key'] = 'new-current-analysis'
            route.fulfill(status=409, content_type='application/json', body=json.dumps({
                'detail': {'code': 'IMAGE_ANALYSIS_VERSION_CONFLICT', 'message': '分析已更新'}}))
        self.page.route('**/images/candidate-1/analysis?*', conflict)
        first.get_by_role('button', name='保存人工理解').click()
        expect(self.form(1).locator('[data-analysis-draft-notice]')).to_contain_text('分析结果已更新')
        expect(self.form(1).locator('[name="caption"]')).to_have_value('第一张本地输入')
        expect(self.form(2).locator('[name="caption"]')).to_have_value('第二张本地输入')

    def test_review_loading_clears_counts_and_disables_previous_final_action(self):
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/review')
        expect(page.locator('#formal-review-problem-count')).to_have_text('3')
        held = []
        page.route('**/review?*', lambda route: held.append(route))
        page.evaluate('void loadFormalReview(formalRoute().projectId)')
        expect(page.locator('#formal-review-problem-count')).to_have_text('—')
        expect(page.locator('#formal-review-approved-count')).to_have_text('—')
        expect(page.locator('#formal-review-final-submit')).to_be_disabled()
        page.wait_for_timeout(50)
        for route in held:
            route.continue_()
        expect(page.locator('#formal-review-problem-count')).to_have_text('3')

    def test_late_review_start_and_final_cannot_replace_new_project_model(self):
        page = self.page
        for action in ('start', 'final'):
            page.goto(f'{self.base}/projects/{PROJECT_ID}/review')
            expect(page.locator('#formal-review-problem-count')).to_have_text('3')
            # Anonymous UI model supplies the two action stages; the response is held
            # while another project's review is rendered, without any real service writes.
            page.evaluate('''action => {
                state.formalReview.review_started = action === 'final';
                state.formalReview.ready_for_final = action === 'final';
                renderFormalReview(state.formalReview);
            }''', action)
            held = []
            page.route(f'**/review/{action}?*', lambda route: held.append(route))
            button = '#formal-review-start' if action == 'start' else '#formal-review-final-submit'
            with page.expect_request(lambda request: f'/review/{action}?' in request.url):
                page.locator(button).click()
            page.evaluate('''() => {
                history.pushState({}, '', '/projects/another-anonymous-project/review');
                state.formalReviewEpoch += 1;
                state.formalReviewBusy = false;
                renderFormalReview({state:'ready', project_version:2, review_started:false,
                    version_label:'另一项目版本', issues:[], decision:{label:'另一项目审核', problem_count:7}});
            }''')
            with page.expect_response(lambda response: f'/review/{action}?' in response.url):
                held[0].fulfill(status=200, content_type='application/json', body=json.dumps({
                    'state':'confirmed', 'project_version':1, 'issues':[], 'decision':{'label':'旧项目结果', 'problem_count':0}}))
            expect(page.locator('#formal-review-problem-count')).to_have_text('7')
            expect(page.locator('#formal-review-decision-title')).to_have_text('另一项目审核')
        self.assertFalse(self.errors)
