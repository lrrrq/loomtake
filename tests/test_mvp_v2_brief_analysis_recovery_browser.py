"""Anonymous real-HTTP regression for saved Brief analysis after reload.

Only the first analysis response is replaced with a transport-layer 403. The
retry reaches the real agent/API and SQLite with the existing deterministic
advisor fixture; this is not evidence of a real external Provider call.
"""
from __future__ import annotations

import json
import unittest

from playwright.sync_api import expect

from tests import test_mvp_v2_rounds_repair_browser as fixture_module

BRIEF = fixture_module.BRIEF


class BriefAnalysisRecoveryBrowserTests(unittest.TestCase):
    open_page = fixture_module.RoundsRepairBrowserTests.open_page
    setUp = fixture_module.RoundsRepairBrowserTests.setUp
    tearDown = fixture_module.RoundsRepairBrowserTests.tearDown

    def test_saved_documents_allow_analysis_retry_after_403_and_reload(self):
        page = self.page
        project_id = self.project['project_id']
        analysis_requests = []

        def fail_first_analysis(route):
            analysis_requests.append(json.loads(route.request.post_data or '{}'))
            if len(analysis_requests) == 1:
                route.fulfill(status=403, content_type='application/json', body=json.dumps({
                    'detail': {'code': 'UI_SESSION_INVALID',
                               'message': '本地会话已失效，请刷新页面后重试。'}}, ensure_ascii=False))
            else:
                route.continue_()

        page.route('**/brief-analysis', fail_first_analysis)
        page.locator('#brief-document-upload > summary').click()
        page.locator('#formal-brief-files').set_input_files({
            'name': 'anonymous-saved-requirements.txt', 'mimeType': 'text/plain',
            'buffer': BRIEF.encode('utf-8'),
        })
        with page.expect_response(lambda response: '/brief-analysis' in response.url
                                  and response.status == 403):
            page.locator('#formal-brief-upload-button').click()
        expect(page.locator('#brief-intake-status')).to_contain_text('资料已保存')
        expect(page.locator('#formal-brief-upload-button')).to_be_enabled()

        def frozen_input():
            version = self.adapter.service.latest_import_base(self.fixture.token,
                workspace_id=self.fixture.workspace_id, project_id=project_id)
            self.assertIsNotNone(version)
            return {'brief_version_id': version.brief_version_id,
                    'source_documents': version.payload_data['source_documents'],
                    'extractions': version.payload_data['extractions']}

        before = frozen_input()
        before_project = self.fixture.projects.get_project(self.fixture.token, project_id)
        self.assertEqual(len(before['source_documents']), 1)
        retained = '保留这段尚未提交的补充，重新分析原有资料时不要一并上传。'
        page.locator('#formal-brief-text').fill(retained)
        # Reload is deliberate. The existing session-scoped input draft must
        # survive; no browser storage manipulation is used to manufacture it.
        page.on('dialog', lambda dialog: dialog.accept())
        page.reload()
        expect(page.locator('#formal-brief-upload')).to_be_visible()
        expect(page.locator('#formal-brief-text')).to_have_value(retained)
        expect(page.locator('#formal-brief-files-state')).to_contain_text('anonymous-saved-requirements.txt')
        retry = page.locator('#brief-analysis-retry')
        expect(retry).to_be_visible()
        expect(retry).to_be_enabled()
        post_count = len([r for r in self.requests if r['method'] == 'POST'
                          and r['path'].endswith('/brief/files')])
        self.assertEqual(post_count, 1)
        with page.expect_response(lambda response: '/brief-analysis' in response.url
                                  and response.request.method == 'POST') as retried:
            retry.click()
        self.assertEqual(retried.value.status, 200, retried.value.text())
        expect(page.locator('#formal-brief-upload-button')).to_be_enabled()
        expect(page.locator('#formal-brief-upload-button')).to_have_text('发送')
        expect(retry).to_be_visible()
        expect(page.locator('#formal-brief-text')).to_have_value(retained)
        self.assertEqual(len(analysis_requests), 2)
        self.assertNotIn(retained, json.dumps(analysis_requests[1], ensure_ascii=False))
        self.assertEqual(len([r for r in self.requests if r['method'] == 'POST'
                             and r['path'].endswith('/brief/files')]), 1)
        self.assertEqual(frozen_input(), before)
        after_project = self.fixture.projects.get_project(self.fixture.token, project_id)
        self.assertEqual(after_project['project_version'], before_project['project_version'])
        self.assertEqual(self.errors, [])
        page.reload()
        expect(page.locator('#brief-analysis-retry')).to_be_visible()
        expect(page.locator('#formal-brief-text')).to_have_value(retained)
        self.assertEqual(frozen_input(), before)
