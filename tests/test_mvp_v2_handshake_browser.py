"""Handshake regressions against the real UI and anonymous HTTP boundary data.

These tests assert durable user choices, not model quality or real-service acceptance.
"""
from copy import deepcopy
import json
from urllib.parse import urlparse
import unittest

from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as browser_tests
from tests import test_mvp_v2_creative_draft_browser as draft_tests
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class HandshakeBrowserTests(unittest.TestCase):
    setUp = browser_tests.CreativeWorkspaceBrowserTests.setUp

    def install_confirmation_fixture(self, rejected=()):
        fixture = self.fixture
        fixture.image_selections = ["candidate-1", "candidate-2", "candidate-3"]
        original = fixture.response
        self.confirmations = []
        receipts = {}

        def response(method, path):
            endpoint = urlparse(path).path
            if endpoint.endswith('/confirmation-preview'):
                items = [{"shot_id": item['shot_id'], "shot_no": item['shot_id'].rsplit('-', 1)[-1],
                          "processable": item['shot_id'] not in rejected,
                          "reason": "来源需核对" if item['shot_id'] in rejected else "可确认",
                          "selection": item} for item in fixture.body['selections']]
                return 200, {"preview_digest": "b" * 64, "items": items,
                             "processable_count": sum(item['processable'] for item in items),
                             "pending_count": sum(not item['processable'] for item in items)}
            if endpoint.endswith('/confirm-progress'):
                body = deepcopy(fixture.body)
                if body['idempotency_key'] in receipts:
                    return 200, deepcopy(receipts[body['idempotency_key']])
                self.confirmations.append(body)
                for item in body['selections']:
                    if item['shot_id'] in body['accepted_shot_ids']:
                        fixture.progress_selections[item['shot_id']] = {**item, 'checked': True}
                fixture.progress_revision += 1
                _, result = original('GET', f'/api/formal/projects/{PROJECT_ID}/image-workspace/progress')
                result = {**result, 'confirmed_count': len(body['accepted_shot_ids']),
                          'pending_count': len(body['selections']) - len(body['accepted_shot_ids'])}
                receipts[body['idempotency_key']] = deepcopy(result)
                return 200, result
            return original(method, path)
        fixture.response = response

    def assert_confirmation_survives(self, selected=None, rejected=()):
        self.install_confirmation_fixture(rejected)
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        expect(page.locator('[data-image-shot-confirm="0"]')).not_to_be_checked()
        if selected:
            page.locator('[data-creative-batch-toggle]').click()
            for number in selected:
                page.locator(f'[data-creative-select-shot="shot-stable-{number}"]').check()
        page.locator(f'[data-preview-image-confirmation="{"selected" if selected else "all"}"]').click()
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('已确认')
        # Force an additional save and switch shot: neither may enqueue the old false values.
        page.locator('#creative-save-progress').click()
        expect(page.locator('#creative-progress-status')).to_contain_text('已保存')
        page.locator('[data-creative-shot="shot-stable-2"]').click()
        page.reload()
        expected = {f'shot-stable-{n}' for n in (selected or [1, 2, 3])} - set(rejected)
        for number in (1, 2, 3):
            page.locator(f'[data-creative-shot="shot-stable-{number}"]').click()
            control = page.locator(f'[data-image-shot-confirm="{number - 1}"]')
            if f'shot-stable-{number}' in expected:
                expect(control).to_be_checked()
            else:
                expect(control).not_to_be_checked()
        self.assertEqual({key for key, value in self.fixture.progress_selections.items() if value['checked']}, expected)
        self.assertEqual(len(self.confirmations), 1)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertEqual(self.errors, [])

    def test_all_three_existing_unconfirmed_images_remain_confirmed_after_save_reload(self):
        self.assert_confirmation_survives()

    def test_subset_confirmation_preserves_other_unconfirmed_shot(self):
        self.assert_confirmation_survives(selected=[1, 3])

    def test_partial_confirmation_leaves_unprocessable_shot_pending(self):
        self.assert_confirmation_survives(rejected=['shot-stable-2'])

    def test_new_choice_while_confirmation_in_flight_survives_response(self):
        self.install_confirmation_fixture()
        page = self.page
        pending = []
        page.route('**/image-workspace/confirm-progress?*', lambda route: pending.append(route))
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        page.locator('[data-preview-image-confirmation="all"]').click()
        with page.expect_request(lambda request: '/confirm-progress?' in request.url):
            page.locator('[data-confirm-image-progress]').click()
        page.locator('[data-image-shot-select="0"]').select_option('candidate-4')
        pending.pop().continue_()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('已确认')
        expect(page.locator('[data-image-shot-select="0"]')).to_have_value('candidate-4')
        page.locator('#creative-save-progress').click()
        expect(page.locator('#creative-progress-status')).to_contain_text('已保存')
        page.reload()
        expect(page.locator('[data-image-shot-select="0"]')).to_have_value('candidate-4')
        expect(page.locator('[data-image-shot-confirm="0"]')).not_to_be_checked()
        self.assertEqual(self.fixture.progress_selections['shot-stable-1']['image_key'], 'candidate-4')
        self.assertEqual(self.errors, [])

    def test_unknown_confirmation_response_recovers_original_receipt_once(self):
        self.install_confirmation_fixture()
        page = self.page
        bodies = []
        def lose_response(route):
            bodies.append(route.request.post_data_json)
            response = route.fetch()
            if len(bodies) == 1:
                route.abort('failed')
            else:
                route.fulfill(response=response)
        page.route('**/image-workspace/confirm-progress?*', lose_response)
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        page.locator('[data-preview-image-confirmation="all"]').click()
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('确认结果待核实')
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('已确认 3 镜')
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(len(self.confirmations), 1)
        page.reload()
        expect(page.locator('[data-image-shot-confirm="0"]')).to_be_checked()
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertEqual(self.errors, [])

    def assert_unknown_confirmation_after_reload_keeps_new_choice(self, newer_server=False):
        self.install_confirmation_fixture()
        page = self.page
        bodies = []
        def lose_response(route):
            bodies.append(route.request.post_data_json)
            response = route.fetch()
            if len(bodies) == 1:
                route.abort('failed')
            else:
                route.fulfill(response=response)
        page.route('**/image-workspace/confirm-progress?*', lose_response)
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        page.locator('[data-preview-image-confirmation="all"]').click()
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('确认结果待核实')
        # Simulate another window's newer saved progress, after the original receipt.
        if newer_server:
            self.fixture.progress_selections['shot-stable-2'].update(image_key='candidate-4', checked=False)
            self.fixture.progress_revision += 1
        revision_before_recovery = self.fixture.progress_revision
        page.reload()
        expect(page.locator('[data-image-shot-select="0"]')).to_have_value('candidate-1')
        page.locator('[data-image-shot-select="0"]').select_option('candidate-4')
        # Preview must recover the retained request even though a new local choice exists.
        page.locator('[data-preview-image-confirmation="all"]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('上次确认结果待核实')
        page.locator('[data-confirm-image-progress]').click()
        expect(page.locator('#creative-image-confirmation')).to_contain_text('已确认 3 镜')
        expect(page.locator('[data-image-shot-select="0"]')).to_have_value('candidate-4')
        page.locator('#creative-save-progress').click()
        expect(page.locator('#creative-progress-status')).to_contain_text('已保存')
        self.assertEqual(bodies[0], bodies[1])
        self.assertEqual(len(self.confirmations), 1)
        self.assertGreater(self.fixture.progress_revision, revision_before_recovery)
        page.reload()
        expect(page.locator('[data-image-shot-select="0"]')).to_have_value('candidate-4')
        expect(page.locator('[data-image-shot-confirm="0"]')).not_to_be_checked()
        page.locator('[data-creative-shot="shot-stable-2"]').click()
        expect(page.locator('[data-image-shot-select="1"]')).to_have_value('candidate-4' if newer_server else 'candidate-2')
        if newer_server:
            expect(page.locator('[data-image-shot-confirm="1"]')).not_to_be_checked()
        else:
            expect(page.locator('[data-image-shot-confirm="1"]')).to_be_checked()
        page.locator('[data-creative-shot="shot-stable-3"]').click()
        expect(page.locator('[data-image-shot-confirm="2"]')).to_be_checked()
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertEqual(self.errors, [])

    def test_unknown_confirmation_reload_then_new_choice_recovers_original_receipt(self):
        self.assert_unknown_confirmation_after_reload_keeps_new_choice()

    def test_stale_confirmation_receipt_does_not_overwrite_newer_saved_progress(self):
        self.assert_unknown_confirmation_after_reload_keeps_new_choice(newer_server=True)

    def install_brief_fixture(self):
        original = self.fixture.brief
        def brief():
            result = original()
            result['brief']['facts'] = [
                {'label': '目标时长', 'value': '60秒', 'sources': ['匿名需求.txt · 第1行']},
                {'label': '项目名称', 'value': '匿名项目', 'sources': ['匿名需求.txt · 第2行']}]
            return result
        self.fixture.brief = brief

    def test_field_roundtrip_refresh_and_different_project_keep_separate_drafts(self):
        self.install_brief_fixture()
        page = self.page
        other = 'project-browser-other'
        page.route(f'**/api/formal/projects/{other}**', lambda route: route.fulfill(
            response=route.fetch(url=route.request.url.replace(other, PROJECT_ID))))
        page.goto(f'{self.base}/projects/{PROJECT_ID}/brief')
        page.locator('[data-edit-formal-fact="目标时长"]').click()
        page.locator('#formal-brief-text').fill('目标时长: 90秒，保留现场同期声')
        page.locator('[data-edit-formal-fact="项目名称"]').click()
        page.locator('#formal-brief-text').fill('项目名称: 匿名年会')
        page.locator('[data-edit-formal-fact="目标时长"]').click()
        expect(page.locator('#formal-brief-text')).to_have_value('目标时长: 90秒，保留现场同期声')
        page.reload()
        expect(page.locator('#formal-brief-text')).to_have_value('目标时长: 90秒，保留现场同期声')
        page.locator('[data-edit-formal-fact="项目名称"]').click()
        expect(page.locator('#formal-brief-text')).to_have_value('项目名称: 匿名年会')
        page.goto(f'{self.base}/projects/{other}/brief')
        page.locator('[data-edit-formal-fact="目标时长"]').click()
        expect(page.locator('#formal-brief-text')).to_have_value('目标时长: 60秒')
        page.goto(f'{self.base}/projects/{PROJECT_ID}/brief')
        page.locator('[data-edit-formal-fact="目标时长"]').click()
        expect(page.locator('#formal-brief-text')).to_have_value('目标时长: 90秒，保留现场同期声')
        self.assertFalse(any(route.endswith('/brief/files') for route, _ in self.fixture.writes))
        self.assertEqual(self.errors, [])

    def test_failed_field_save_retries_only_active_field_and_keeps_other_draft(self):
        self.install_brief_fixture()
        page = self.page
        requests = []
        def submit(route):
            requests.append(route.request.post_data_buffer.decode('utf-8'))
            if len(requests) == 1:
                route.fulfill(status=503, content_type='application/json', body=json.dumps(
                    {'detail': {'code': 'UNAVAILABLE', 'message': '匿名需求保存中断'}}))
            else:
                route.continue_()
        page.route('**/brief/files?*', submit)
        page.goto(f'{self.base}/projects/{PROJECT_ID}/brief')
        page.locator('[data-edit-formal-fact="项目名称"]').click()
        page.locator('#formal-brief-text').fill('项目名称: 留待保存的名称')
        page.locator('[data-edit-formal-fact="目标时长"]').click()
        page.locator('#formal-brief-text').fill('目标时长: 90秒')
        page.locator('#formal-brief-upload-button').click()
        expect(page.locator('#brief-intake-status')).to_contain_text('本次录入未完成，输入和文件已保留')
        self.assertEqual(len(requests), 1)
        expect(page.locator('#formal-brief-text')).to_have_value('目标时长: 90秒')
        page.locator('#formal-brief-upload-button').click()
        expect(page.locator('#formal-brief-text')).to_have_value('')
        page.locator('[data-edit-formal-fact="项目名称"]').click()
        expect(page.locator('#formal-brief-text')).to_have_value('项目名称: 留待保存的名称')
        self.assertEqual(len(requests), 2)
        for body in requests:
            self.assertIn('目标时长: 90秒', body)
            self.assertNotIn('留待保存的名称', body)
        self.assertEqual(self.errors, [])

    def test_server_field_change_warns_while_retaining_local_text(self):
        self.install_brief_fixture()
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/brief')
        page.locator('[data-edit-formal-fact="目标时长"]').click()
        page.locator('#formal-brief-text').fill('目标时长: 本地尚未保存的90秒')
        original = self.fixture.brief
        def updated():
            result = original()
            result['brief']['facts'][0]['value'] = '120秒'
            return result
        self.fixture.brief = updated
        page.reload()
        page.locator('[data-edit-formal-fact="目标时长"]').click()
        expect(page.locator('#formal-brief-text')).to_have_value('目标时长: 本地尚未保存的90秒')
        expect(page.locator('#toast')).to_contain_text('服务端内容已变化')
        expect(page.locator('#toast')).to_contain_text('120秒')
        self.assertEqual(self.errors, [])

    def test_optional_fields_preserve_edits_and_expand_state_between_views(self):
        page = self.page
        self.fixture.shots[1]['notes'] = '保留已有同期声备注'
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script')
        expect(page.locator('#page-script > .ui00-script-toolbar')).to_be_visible()
        empty = page.locator('[data-script-shot-key="shot-stable-1"][data-script-field="voiceover"]')
        expect(empty).to_be_hidden()
        detail = empty.locator('..')
        expect(detail.locator('summary')).to_contain_text('按需填写')
        detail.locator('summary').click()
        empty.fill('新增旁白不可因切换视图丢失')
        page.locator('[data-creative-edit-mode="table"]').click()
        expect(empty).to_be_visible()
        expect(empty).to_have_value('新增旁白不可因切换视图丢失')
        empty.fill('完整表格里的旁白修改')
        page.locator('[data-creative-edit-mode="shot"]').click()
        expect(empty).to_be_visible()
        expect(empty).to_have_value('完整表格里的旁白修改')
        page.locator('[data-creative-shot="shot-stable-2"]').click()
        notes = page.locator('[data-script-shot-key="shot-stable-2"][data-script-field="notes"]')
        expect(notes).to_have_value('保留已有同期声备注')
        expect(notes.locator('..').locator('summary')).to_contain_text('保留已有同期声备注')
        expect(page.locator('#creative-current-image img').first).to_be_visible()
        for width in (1440, 1024, 390):
            page.set_viewport_size({'width': width, 'height': 844})
            self.assertLessEqual(page.locator('html').evaluate('el => el.scrollWidth'), width + 1)
        self.assertEqual(self.errors, [])

    def test_complete_table_exposes_initially_collapsed_optional_fields(self):
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script')
        voiceover = page.locator('[data-script-shot-key="shot-stable-1"][data-script-field="voiceover"]')
        expect(voiceover).to_be_hidden()
        page.locator('[data-creative-edit-mode="table"]').click()
        expect(voiceover).to_be_visible()
        with page.expect_response(lambda response: response.request.method == 'POST' and '/script/table' in response.url):
            voiceover.fill('在完整表格首次填写旁白')
            voiceover.press('Tab')
        page.locator('[data-creative-edit-mode="shot"]').click()
        expect(voiceover).to_be_hidden()
        voiceover.locator('..').locator('summary').click()
        expect(voiceover).to_have_value('在完整表格首次填写旁白')
        page.locator('[data-creative-edit-mode="table"]').click()
        page.reload()
        page.locator('[data-creative-edit-mode="table"]').click()
        expect(voiceover).to_be_visible()
        expect(voiceover).to_have_value('在完整表格首次填写旁白')
        self.assertEqual(self.errors, [])

    def test_plan_with_zero_decisions_shows_actual_missing_fact_and_return_link(self):
        original = self.fixture.response
        def response(method, path):
            status, value = original(method, path)
            if urlparse(path).path.endswith('/plans/plan-for-browser'):
                value['decision_counts'] = {'event_inclusion': 0, 'event_missing': 0,
                                            'capacity_conflict': 0, 'template_conflict': 0}
                value['audit'] = {'status': 'needs_clarification', 'findings': [{
                    'code': 'fact_missing', 'severity': 'blocking',
                    'message': '已要求展示的获奖名单尚未提供', 'fields': ['获奖名单'],
                    'risk': '无法核对出镜人员与奖项对应'}]}
            return status, value
        self.fixture.response = response
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=structure')
        audit = page.locator('#formal-plan-audit')
        expect(audit).to_contain_text('已要求展示的获奖名单尚未提供')
        expect(audit).to_contain_text('位置：获奖名单')
        expect(audit).to_contain_text('无法核对出镜人员与奖项对应')
        expect(page.locator('#formal-plan-confirm-check')).to_be_disabled()
        link = audit.get_by_role('link', name='补充资料后返回此规划', exact=True)
        expect(link).to_be_visible()
        href = link.get_attribute('href')
        self.assertIn('/brief?', href)
        self.assertIn('return', href)
        link.click()
        expect(page.locator('#page-brief')).to_be_visible()
        self.assertEqual(self.errors, [])




class HandshakeDraftLayoutTests(unittest.TestCase):
    setUp = draft_tests.CreativeDraftBrowserTests.setUp
    complete = draft_tests.CreativeDraftBrowserTests.complete
    open = draft_tests.CreativeDraftBrowserTests.open

    def capture(self, name):
        from pathlib import Path
        directory = Path('output/handshake-v1/visual')
        directory.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(directory / f'{name}.png'), full_page=True)

    def test_generation_feedback_failure_recovery_and_toolbar_at_three_widths(self):
        page = self.page
        self.open()
        for width in (1440, 1024, 390):
            with self.subTest(width=width):
                page.set_viewport_size({'width': width, 'height': 844})
                button = page.locator('#creative-first-confirmed-start')
                button.click()
                status = page.locator('#creative-first-status')
                expect(status).to_contain_text('正在按确认的需求创作')
                box = status.bounding_box()
                button_box = button.bounding_box()
                self.assertGreaterEqual(box['y'], 0)
                self.assertLessEqual(box['y'] + box['height'], 844)
                self.assertLess(abs(button_box['y'] - box['y']), 450)
                self.assertLessEqual(page.locator('html').evaluate('el => el.scrollWidth'), width + 1)
                expect(page.locator('#page-script > .ui00-script-toolbar')).to_be_hidden()
                self.capture(f'first-draft-running-{width}')
                expect(page.locator('#creative-first-blocks [data-first-field]')).to_have_count(0)
                self.value['task'].update(status='failed', is_running=False)
                self.value['revision'] += 1
                expect(status).to_contain_text('本次创作尚未完成')
                expect(button).to_be_enabled()
                self.assertEqual(self.value['understanding']['confirmation_status'], 'confirmed')
                button.scroll_into_view_if_needed()
                self.capture(f'first-draft-failed-{width}')
        page.locator('#creative-first-confirmed-start').click()
        expect(page.locator('#creative-first-status')).to_contain_text('正在按确认的需求创作')
        revision = self.value['revision'] + 1
        self.complete()
        self.value['revision'] = revision
        expect(page.locator('#creative-first-status')).to_contain_text('初稿已保存')
        expect(page.locator('#page-script > .ui00-script-toolbar')).to_be_hidden()
        self.capture('first-draft-saved-390')
        self.assertEqual(self.generated, 4)
        self.assertEqual(self.helper.errors, [])

    def test_unimplemented_answers_refresh_retry_merge_and_decline_keep_original_draft(self):
        self.complete()
        original_blocks = deepcopy(self.value['blocks'])
        self.value.update(unimplemented_answers=[
            {'field': '脚本类型', 'value': '年会'},
            {'field': '目标时长', 'value': '60秒'},
            {'field': '风格', 'value': '高级'}], answers_need_preview=True)
        original = self.helper.fixture.response
        attempts = []
        def response(method, path):
            endpoint = urlparse(path).path
            if endpoint.endswith('/creative-draft/answers/continue'):
                attempts.append(deepcopy(self.helper.fixture.body))
                if len(attempts) == 1:
                    return 503, {'detail': {'message': '合并预览暂时失败'}}
                status, result = original(method, path.replace('/answers/continue', '/revisions'))
                self.value['answers_need_preview'] = False
                return status, {**result, 'answers_need_preview': False}
            if endpoint.endswith('/creative-draft/revisions/confirm'):
                status, result = original(method, path)
                self.value.update(unimplemented_answers=[], answers_need_preview=False)
                return status, {**result, 'unimplemented_answers': [], 'answers_need_preview': False}
            return original(method, path)
        self.helper.fixture.response = response
        page = self.page
        self.open()
        notice = page.locator('#creative-first-requirement-update')
        expect(notice).to_contain_text('需求变更尚未应用到当前稿')
        expect(notice.locator('input, select, textarea')).to_have_count(0)
        page.reload()
        expect(notice).to_be_visible()
        page.locator('[data-first-answer-continue]').click()
        expect(page.locator('#creative-first-status')).to_contain_text('操作没有完成')
        expect(notice).to_be_visible()
        expect(page.locator('[data-first-answer-continue]')).to_be_visible()
        page.locator('[data-first-answer-continue]').click()
        expect(page.locator('#creative-first-proposal')).to_contain_text('现场掌声渐入')
        page.locator('#creative-first-decline').click()
        expect(page.locator('#creative-first-proposal')).to_be_hidden()
        self.assertEqual(self.confirmations[-1]['retained_block_ids'], [])
        self.assertEqual(self.value['blocks'], original_blocks)
        page.reload()
        expect(notice).to_be_hidden()
        expect(page.locator('[data-first-block="block-2"][data-first-field="声音"]')).to_have_value('环境声')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(self.helper.errors, [])
