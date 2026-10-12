"""RV06-07 real upload/append UI preserves prior extraction and source evidence."""
import json
import unittest
from unittest.mock import patch

from playwright.sync_api import expect
from mvp_v2 import local_ui, formal_brief_input_adapter as adapter_module
from mvp_v2.formal_runtime import uploaded_document
from tests import test_mvp_v2_formal_brief_ocr as ocr_fixture
from tests.test_mvp_v2_formal_brief_ocr import _complete_brief, _image_bytes
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests.test_mvp_v2_library_browse import library_confirmation_agent


class BriefAppendBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_append_upload_preserves_sources_without_reextracting_old_documents(self):
        f = ocr_fixture.FormalBriefOcrLifecycleTests('runTest')
        f.setUp(); self.addCleanup(f.tearDown)
        calls = []
        adapter = f.counting_adapter(calls)
        agent = library_confirmation_agent(root=f.root, store=f.store, projects=f.projects, service=None)
        app = local_ui.create_app(agent_api=agent, formal_brief_adapter=adapter,
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + '/projects/' + f.first['project_id'] + '/brief')
        page.locator('#brief-document-upload summary').click()
        page.locator('#formal-brief-files').set_input_files([
            {'name': 'existing-brief.txt', 'mimeType': 'text/plain', 'buffer': _complete_brief().encode()},
            {'name': 'existing-photo.png', 'mimeType': 'image/png', 'buffer': _image_bytes('PNG')}])
        with page.expect_response(lambda r: '/brief/files?' in r.url and r.request.method == 'POST') as imported:
            page.locator('#formal-brief-upload-button').click()
        self.assertEqual(imported.value.status, 200, imported.value.text())
        old = f.raw(imported.value.json())
        self.assertEqual(len(calls), 1)
        old_rows = {row['document_id']: row for row in old['extractions']}
        page.reload()
        expect(page.locator('#formal-standard-card-state')).to_have_text('待确认')
        page.locator('#brief-document-upload summary').click()
        page.locator('#formal-brief-files').set_input_files({
            'name': 'additional.txt', 'mimeType': 'text/plain', 'buffer': '交付要求：横版高清'.encode()})
        with patch.object(adapter_module, '_extract_bytes', wraps=adapter_module._extract_bytes) as extracted:
            with page.expect_response(lambda r: '/brief/files?' in r.url and r.request.method == 'POST') as appended:
                page.locator('#formal-brief-upload-button').click()
            self.assertEqual(appended.value.status, 200, appended.value.text())
            self.assertEqual([call.args[0]['filename'] for call in extracted.call_args_list], ['additional.txt'])
        self.assertEqual(len(calls), 1)
        current = f.raw(appended.value.json())
        self.assertEqual(len(current['extractions']), 3)
        delivery_facts = [row for row in current['facts'] if row['field_path'] == '交付要求']
        self.assertEqual([json.loads(row['value_json']) for row in delivery_facts], ['横版高清'])
        self.assertTrue(delivery_facts[0]['evidence_refs'])
        for row in current['extractions']:
            if row['document_id'] in old_rows:
                self.assertEqual(row, old_rows[row['document_id']])
        self.assertEqual(current['images'], old['images'])
        page.reload()
        expect(page.locator('#formal-standard-card-state')).to_have_text('待确认')
        page.locator('.brief-source-evidence > summary').click()
        expect(page.locator('#formal-brief-files-state')).to_contain_text('additional.txt')
        expect(page.locator('#formal-brief-files-state')).to_contain_text('existing-photo.png')
        latest = adapter.service.latest_import_base(f.token, workspace_id=f.workspace_id,
            project_id=f.first['project_id'])
        self.assertEqual(len(latest.payload_data['source_documents']), 3)
        self.assertTrue(all(row.get('extraction_reuse_key') for row in latest.payload_data['source_documents']))
        self.assertEqual(f.raw(imported.value.json()), old)
        self.assertEqual(len(calls), 1)
