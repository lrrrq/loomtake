"""RV05-03: normal owner, real HTTP and persisted ancestor delivery."""
import unittest
import hashlib
import json
from PIL import Image
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook
from pypdf import PdfReader
from mvp_v2.excel_io import read_managed_sheet_rows

from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from mvp_v2.version_delivery import resolve_version_shot_ids
from tests import test_mvp_v2_frozen_version_recovery as recovery_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture


class FrozenVersionBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_ancestor_read_download_refresh_and_return_to_unapproved_child(self):
        self._ancestor_download()

    def test_dirty_child_rejects_current_review_copy_but_exports_frozen_ancestor(self):
        self._ancestor_download(dirty_child=True)

    def _ancestor_download(self, *, dirty_child=False):
        support = recovery_fixture.FrozenVersionRecoveryTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.f
        f._confirm_current()
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        old = f.review.read_current(f.token, **scope)
        support._create_child()
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store,
            identity=LocalIdentityAdapter(f.store), projects=f.base.projects,
            tasks=TaskService(f.store), versions=VersionService(f.store), artifacts=f.base.resolver))
        if dirty_child:
            from mvp_v2.formal_script_revision import FormalRevisionService
            agent.script_revision = FormalRevisionService(projects=f.base.projects,
                tasks=TaskService(f.store), workspace=f.base.base.workspace,
                image_workspace=f.base.workspace, writes_enabled=True)
        app = local_ui.create_app(agent_api=agent, formal_review_service=f.review,
            formal_script_service=f.base.base.workspace, formal_image_service=f.base.workspace,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.root / "runtime"), data_root=f.root / "runtime"),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + '/projects/' + f.project_id + '/script')
        page.wait_for_load_state('networkidle')
        if dirty_child:
            page.locator('[data-creative-edit-mode="table"]').click()
            with page.expect_response(lambda r: '/script/table?' in r.url and r.request.method == 'POST') as edited:
                page.locator('[data-script-shot="1"][data-script-field="sound"]').fill('当前子版尚未保存的声音 REVIEW-DIRTY')
                page.locator('[data-script-shot="1"][data-script-field="sound"]').blur()
            self.assertEqual(edited.value.status, 200, edited.value.text())
            page.reload()
            page.wait_for_load_state('networkidle')
        page.locator('#formal-script-versions-title').click()
        if dirty_child:
            versions = f.base.projects.list_project_versions(f.token, f.project_id)
            with page.expect_response(lambda r: '/review-workbook?' in r.url) as rejected:
                page.locator(f'[data-script-version-workbook="{len(versions)}"]').click()
            self.assertEqual(rejected.value.status, 409, rejected.value.text())
            self.assertIn('DRAFT_NOT_SAVED', rejected.value.text())
            expect(page.locator('#toast')).to_contain_text('请先保存')
            with page.expect_download(timeout=60000) as review_download:
                page.locator(f'[data-script-version-workbook="{old["version_label"].split()[-1]}"]').click()
            self.assertIsNone(review_download.value.failure())
            self.assertIn('复核稿', review_download.value.suggested_filename)
            book = load_workbook(BytesIO(Path(review_download.value.path()).read_bytes()))
            try:
                ancestor = f.review.services.delivery.resolve(f.token, f.project_id, old['base_version_id'],
                    ancestor_of=versions[-1]['version_id'])
                original = load_workbook(f.base.resolver.resolve_artifact(ancestor.run_id, 'workbook').path)
                try:
                    self.assertEqual(read_managed_sheet_rows(book, '脚本'), read_managed_sheet_rows(original, '脚本'))
                    self.assertEqual([hashlib.sha256(image._data()).hexdigest() for image in book['脚本']._images],
                        [hashlib.sha256(image._data()).hexdigest() for image in original['脚本']._images])
                finally:
                    original.close()
            finally:
                book.close()
            with f.store.connection() as connection:
                self.assertEqual(connection.execute('SELECT COUNT(*) FROM delivery_snapshots').fetchone()[0], 0)

        target = page.locator('#formal-script-versions a[href*="' + 'version_no=' + old['version_label'].split()[-1] + '"]')
        expect(target).to_have_count(1)
        with page.expect_response(lambda response: '/api/formal/projects/' in response.url and '/review?' in response.url
                and 'version_no=' in response.url and response.status == 200, timeout=60000) as loaded:
            target.click()
        model = loaded.value.json()
        project = f.base.projects.get_project(f.token, f.project_id)
        context = f.review.services.delivery.resolve(f.token, f.project_id,
            old['base_version_id'], ancestor_of=project['current_version_id'])
        shot_ids = resolve_version_shot_ids(f.base.resolver, context)
        evidence_count = 0
        for row in model['traceability']:
            for evidence in row.get('output_evidence', []):
                evidence_count += 1
                self.assertEqual(evidence['content_evidence']['shot_id'], shot_ids[evidence['shot_no']])
                self.assertEqual(evidence['content_evidence']['requirement_id'], row['requirement_id'])
        self.assertGreater(evidence_count, 0)
        expect(page.locator('#creative-review-draft')).to_contain_text('只读', timeout=60000)
        expect(page.locator('#formal-review-start')).to_be_hidden()
        expect(page.locator('#formal-review-final-form')).to_be_hidden()
        with page.expect_download(timeout=60000) as download:
            page.locator('#formal-delivery-excel').click()
        self.assertIsNone(download.value.failure())
        page.reload()
        expect(page.locator('#formal-delivery-excel')).to_be_enabled(timeout=60000)
        page.get_by_role('link', name='返回当前版本审核').click()
        expect(page.locator('#formal-delivery-excel')).to_be_disabled(timeout=60000)
        with f.store.connection() as connection:
            snapshots = connection.execute('SELECT version_id FROM delivery_snapshots').fetchall()
            self.assertEqual([row['version_id'] for row in snapshots], [old['base_version_id']])
        if dirty_child:
            page.goto(origin + '/projects/' + f.project_id + '/script')
            page.wait_for_load_state('networkidle')
            page.locator('[data-creative-edit-mode="table"]').click()
            expect(page.locator('[data-script-shot="1"][data-script-field="sound"]')).to_have_value(
                '当前子版尚未保存的声音 REVIEW-DIRTY')
            self.assertTrue(f.base.base.workspace.read_current(f.token, **scope)['can_save_version'])


    def test_dirty_working_text_and_frozen_delivery_are_both_visible(self):
        self._dirty_working_and_frozen_delivery('subtitle')

    def test_dirty_sound_and_images_keep_frozen_excel_pdf_contents(self):
        self._dirty_working_and_frozen_delivery('sound', verify_contents=True)

    def _dirty_working_and_frozen_delivery(self, field, *, verify_contents=False):
        support = recovery_fixture.FrozenVersionRecoveryTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.f
        f._confirm_current()
        scope = dict(workspace_id=f.workspace_id, project_id=f.project_id)
        script = f.base.base.workspace
        old = f.review.read_current(f.token, **scope)
        frozen_shots = script.read_current(f.token, **scope)['shots']
        frozen_image_digests = []
        if verify_contents:
            context = f.review.services.delivery.resolve(f.token, f.project_id, old['base_version_id'])
            original_workbook = load_workbook(f.base.resolver.resolve_artifact(context.run_id, 'workbook').path)
            try:
                frozen_image_bytes = [image._data() for image in original_workbook['脚本']._images]
                frozen_image_digests = [hashlib.sha256(data).hexdigest() for data in frozen_image_bytes]
                frozen_rows = {name: read_managed_sheet_rows(original_workbook, name)
                    for name in ('脚本', '抽调明细')}
                frozen_pixels = []
                for data in frozen_image_bytes:
                    with Image.open(BytesIO(data)) as source_image:
                        rgb = source_image.convert('RGB')
                        frozen_pixels.append((rgb.size, rgb.tobytes()))
                with f.store.connection() as connection:
                    traces = connection.execute('''SELECT d.shot_no, d.source_location_json
                        FROM version_image_decisions_v3 d
                        JOIN version_image_decision_snapshots_v3 s ON s.snapshot_id=d.snapshot_id
                        WHERE s.materialized_version_id=?''', (old['base_version_id'],)).fetchall()
                frozen_sources = {str(row['shot_no']): json.loads(row['source_location_json']) for row in traces}
                self.assertEqual(len(frozen_sources), len(frozen_shots))
            finally:
                original_workbook.close()
        dirty_text = '未保存声音变更 ONLY-WORKING-SOUND' if field == 'sound' else '旧版可交付时的新稿字幕'
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=f.store,
            identity=LocalIdentityAdapter(f.store), projects=f.base.projects,
            tasks=TaskService(f.store), versions=VersionService(f.store), artifacts=f.base.resolver))
        from mvp_v2.formal_script_revision import FormalRevisionService
        agent.script_revision = FormalRevisionService(projects=f.base.projects,
            tasks=TaskService(f.store), workspace=script, image_workspace=f.base.workspace,
            writes_enabled=True)
        app = local_ui.create_app(agent_api=agent, formal_review_service=f.review,
            formal_script_service=script, formal_image_service=f.base.workspace,
            formal_brief_adapter=FormalBriefInputAdapter(FormalBriefWorkspaceService(f.store,
                artifact_root=f.root/'runtime'), data_root=f.root/'runtime'),
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + '/projects/' + f.project_id + '/script')
        page.locator('[data-creative-edit-mode="table"]').click()
        with page.expect_response(lambda r: '/script/table?' in r.url and r.request.method == 'POST') as saved:
            page.locator(f'[data-script-shot="1"][data-script-field="{field}"]').fill(dirty_text)
            page.locator(f'[data-script-shot="1"][data-script-field="{field}"]').blur()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        expect(page.locator('#creative-next-label')).to_contain_text('待形成新版本')
        delivery = page.locator('#creative-saved-delivery')
        expect(delivery).to_be_visible()
        expect(delivery).to_contain_text(old['version_label'])
        page.reload()
        expect(delivery).to_be_visible()
        expect(page.locator('#creative-next-label')).to_contain_text('待形成新版本')
        working = script.read_current(f.token, **scope)
        progress = f.base.workspace.read_progress(f.token, **scope)
        f.base.workspace.save_progress(f.token, **scope,
            base_version_id=working['base_version_id'], expected_project_version=working['project_version'],
            expected_revision=progress['revision'], expected_script_revision=working['revision'],
            idempotency_key='dual-state-unfinished-image',
            selections=[dict(shot_id=working['shots'][0]['shot_id'], image_key='', checked=False,
                decision_kind='temporary_text_basis', temporary_reason='新稿图片待补',
                reuse_reason='', query='')])
        page.reload()
        expect(delivery).to_be_visible(timeout=60000)
        expect(page.locator('#creative-next-label')).to_contain_text('配图进行中')
        expect(delivery).to_have_attribute('href', '/projects/' + f.project_id + '/review?version_id=' + old['base_version_id'])
        delivery.click()
        expect(page.locator('#formal-delivery-excel')).to_be_enabled(timeout=60000)
        with page.expect_download(timeout=60000) as download:
            page.locator('#formal-delivery-excel').click()
        self.assertIsNone(download.value.failure())
        if verify_contents:
            workbook = load_workbook(BytesIO(Path(download.value.path()).read_bytes()))
            try:
                for name, expected_rows in frozen_rows.items():
                    exported_rows = read_managed_sheet_rows(workbook, name)
                    self.assertEqual(len(exported_rows), len(expected_rows))
                    for exported, frozen in zip(exported_rows, expected_rows):
                        for key, value in exported.items():
                            expected = frozen.get(key)
                            private_reference = isinstance(expected, str) and (
                                'artifact://' in expected.casefold() or 'sha256=' in expected.casefold()
                                or expected.startswith(('/', '~/', 'file://')))
                            self.assertEqual(value, None if private_reference else expected, (name, key))
                rows = read_managed_sheet_rows(workbook, '脚本')
                self.assertEqual(len(rows), len(frozen_shots))
                for row, shot in zip(rows, frozen_shots):
                    self.assertEqual(str(row['声音'] or ''), str(shot['sound'] or ''))
                    self.assertNotIn(dirty_text, str(row))
                self.assertEqual(len(workbook['脚本']._images), len(frozen_shots))
                self.assertEqual([hashlib.sha256(image._data()).hexdigest()
                    for image in workbook['脚本']._images], frozen_image_digests)
            finally:
                workbook.close()
            with page.expect_download(timeout=60000) as pdf_download:
                page.locator('#formal-delivery-pdf').click()
            self.assertIsNone(pdf_download.value.failure())
            pdf = PdfReader(BytesIO(Path(pdf_download.value.path()).read_bytes()))
            self.assertEqual(len(pdf.pages), len(frozen_shots))
            pdf_text = ''.join(item.extract_text() or '' for item in pdf.pages)
            self.assertNotIn('ONLY-WORKING-SOUND', pdf_text)
            self.assertIn('version=' + old['base_version_id'], str(pdf.metadata.get('/Subject')))
            for index, (pdf_page, shot) in enumerate(zip(pdf.pages, frozen_shots)):
                page_text = ''.join((pdf_page.extract_text() or '').split())
                for text_field in ('visual', 'sound', 'voiceover', 'subtitle'):
                    if shot.get(text_field):
                        self.assertIn(''.join(shot[text_field].split()), page_text)
                source = frozen_sources[str(shot['shot_no'])]
                self.assertIn(''.join(f"{source['file']} / {source['sheet']} / {source['row']}".split()), page_text)
                objects = pdf_page['/Resources']['/XObject'].get_object()
                page_images = [ref.get_object() for ref in objects.values()
                    if ref.get_object().get('/Subtype') == '/Image']
                self.assertEqual(len(page_images), 1)
                embedded = page_images[0]
                raw = embedded.get_data()
                if '/DCTDecode' in str(embedded.get('/Filter')):
                    with Image.open(BytesIO(raw)) as decoded:
                        rgb = decoded.convert('RGB')
                else:
                    self.assertEqual(embedded['/ColorSpace'], '/DeviceRGB')
                    self.assertEqual(embedded['/BitsPerComponent'], 8)
                    rgb = Image.frombytes('RGB', (embedded['/Width'], embedded['/Height']), raw)
                expected_size, expected_pixels = frozen_pixels[index]
                self.assertEqual(rgb.size, expected_size)
                self.assertEqual(hashlib.sha256(rgb.tobytes()).hexdigest(),
                    hashlib.sha256(expected_pixels).hexdigest())
            with f.store.connection() as connection:
                bindings = connection.execute(
                    'SELECT delivery_snapshot_id, artifact_kind FROM delivery_artifact_bindings'
                ).fetchall()
                self.assertEqual(len({row['delivery_snapshot_id'] for row in bindings}), 1)
                self.assertEqual({row['artifact_kind'] for row in bindings}, {'excel', 'pdf'})
            page.goto(origin + '/projects/' + f.project_id + '/script')
            page.wait_for_load_state('networkidle')
            page.locator('[data-creative-edit-mode="table"]').click()
            expect(page.locator(f'[data-script-shot="1"][data-script-field="{field}"]')).to_have_value(dirty_text)
            expect(page.locator('#creative-next-label')).to_contain_text('配图进行中')
        self.assertEqual(script.read_current(f.token, **scope)['base_version_id'], old['base_version_id'])
        self.assertTrue(script.read_current(f.token, **scope)['can_save_version'])
        self.assertEqual(f.review.read_current(f.token, **scope)['final_confirmation'], old['final_confirmation'])
