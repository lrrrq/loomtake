"""RV01 real browser -> HTTP -> canonical draft service -> isolated SQLite.

Provider output is an anonymous fixture used only to seed an existing version and
draft. No business API is mocked; response loss/delay is injected at transport.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
import uvicorn

from mvp_v2.formal_image_workspace import FormalImageWorkspaceService, FormalImageWorkspaceServices
from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_image_authority import ProjectImagePoolService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from tests import test_mvp_v2_formal_script_workspace as fixture
from tests import test_mvp_v2_formal_generation_service as generation_fixture


class ScriptSaveBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.base = fixture.FormalScriptWorkspaceTests('runTest')
        evidence = os.environ.get('MPLUS_RV01_EVIDENCE_ROOT')
        if evidence:
            Path(evidence).mkdir(parents=True, exist_ok=True)
            root = tempfile.mkdtemp(prefix=self._testMethodName + '-', dir=evidence)
            with patch.object(generation_fixture.tempfile, 'TemporaryDirectory',
                              return_value=SimpleNamespace(name=root, cleanup=lambda: None)):
                self.base.setUp()
        else:
            self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.root = self.base.generated.root
        self.script, self.store = self.base.workspace, self.base.store
        self.token, self.workspace_id = self.base.generated.token, self.base.generated.workspace_id
        self.project_id = self.base.generated.project['project_id']
        self.scope = dict(workspace_id=self.workspace_id, project_id=self.project_id)
        initial = self.current()
        self.script.apply_table_change(self.token, **self.scope,
            expected_project_version=initial['project_version'], expected_revision=initial['revision'],
            shot_no='1', shot_id=initial['shots'][0]['shot_id'], field='subtitle',
            value='原有项目的待保存字幕', idempotency_key='existing-draft')
        self.before = self.current()
        self.images = FormalImageWorkspaceService(FormalImageWorkspaceServices(
            store=self.store, projects=self.base.projects, delivery=self.base.delivery,
            artifacts=self.base.resolver, images=ProjectImagePoolService(self.store,
                data_root=self.root / 'runtime')), runs_root=self.root / 'runs',
            source_root=self.base.generated.source_root)
        agent = ReadonlyAgentService(ReadonlyAgentServices(store=self.store,
            identity=LocalIdentityAdapter(self.store), projects=self.base.projects,
            tasks=TaskService(self.store), versions=VersionService(self.store), artifacts=self.base.resolver))
        agent.script_revision = FormalScriptRevisionService(projects=self.base.projects,
            tasks=TaskService(self.store), workspace=self.script, image_workspace=self.images, writes_enabled=True)
        self.app = create_app(agent_api=agent, formal_script_service=self.script, formal_image_service=self.images)
        self.start_server()
        self.errors = []
        self.page = self.new_page()
        self.requests = []
        self.page.on('request', lambda request: self.requests.append(deepcopy(request.post_data_json))
            if request.method == 'POST' and '/script/table?' in request.url else None)
        self.page.goto(self.url)
        self.page.locator('[data-creative-edit-mode="table"]').click()
        expect(self.cell('subtitle')).to_have_value('原有项目的待保存字幕')

    def start_server(self):
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        self.port = sock.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(self.app, log_level='error', lifespan='off'))
        thread = threading.Thread(target=self.server.run, kwargs={'sockets': [sock]}, daemon=True)
        thread.start()
        def stop():
            self.server.should_exit = True
            thread.join(timeout=10)
            sock.close()
        self.addCleanup(stop)
        deadline = time.monotonic() + 10
        while not self.server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(self.server.started)
        self.url = f'http://127.0.0.1:{self.port}/projects/{self.project_id}/script'

    def new_page(self):
        page = self.browser.new_page(viewport={'width': 1440, 'height': 1000})
        self.addCleanup(page.context.close)
        page.on('pageerror', lambda error: self.errors.append(str(error)))
        page.set_default_timeout(10000)
        page.add_init_script('localStorage.setItem("mplus.agent.auth", ' + json.dumps(self.token) + ');'
            'localStorage.setItem("mplus.agent.workspace", ' + json.dumps(self.workspace_id) + ');')
        return page

    def cell(self, field, shot='1', page=None):
        return (page or self.page).locator(f'[data-script-shot="{shot}"][data-script-field="{field}"]')

    def current(self):
        return self.script.read_current(self.token, **self.scope)

    def submit(self, field, value, page=None):
        page = page or self.page
        with page.expect_response(lambda r: r.request.method == 'POST' and '/script/table?' in r.url) as response:
            self.cell(field, page=page).fill(value)
            self.cell(field, page=page).press('Tab')
        return response.value

    def assert_persisted(self, values):
        self.page.reload()
        self.page.locator('[data-creative-edit-mode="table"]').click()
        for field, value in values.items():
            expect(self.cell(field)).to_have_value(str(value))
            self.assertEqual(self.current()['shots'][0][field], value)
        current = self.current()
        self.assertEqual(current['shots'][1:], self.before['shots'][1:])
        self.assertEqual(current['base_version_id'], self.before['base_version_id'])
        self.assertEqual(len(self.base.projects.list_project_versions(self.token, self.project_id)), 1)
        self.assertEqual(self.errors, [])
        if os.environ.get('MPLUS_RV01_EVIDENCE_ROOT'):
            self.page.screenshot(path=str(self.root / 'saved.png'), full_page=True)
            (self.root / 'result.json').write_text(json.dumps({
                'business_api_mocked': False, 'provider_fixture': True,
                'existing_draft_revision': self.before['revision'], 'final_revision': current['revision'],
                'shot_count': len(current['shots']), 'values': values,
                'parent_version_unchanged': True, 'non_target_shots_unchanged': True,
                'requests': self.requests}, ensure_ascii=False, indent=2))

    def test_confirmed_camera_conflict_revises_only_target_and_exports_saved_child(self):
        from mvp_v2 import local_ui
        origin = f'http://127.0.0.1:{self.port}'
        allowed = patch.object(local_ui, 'ALLOWED_ORIGINS', {*local_ui.ALLOWED_ORIGINS, origin})
        allowed.start(); self.addCleanup(allowed.stop)
        from mvp_v2.formal_script_revision import FormalRevisionService
        from tests.test_mvp_v2_library_browse import library_confirmation_agent
        from openpyxl import load_workbook
        # Anonymous existing dirty draft: fixed aerial camera contradicts lateral
        # motion in the prose. No real customer's compound movement is changed.
        initial = self.current()
        self.assertEqual(initial['shots'][0]['camera_move'], '固定')
        self.assertEqual(initial['shots'][0]['camera_type'], '航拍')
        conflicting = self.script.apply_table_change(self.token, **self.scope,
            expected_project_version=initial['project_version'], expected_revision=initial['revision'],
            shot_no='1', shot_id=initial['shots'][0]['shot_id'], field='visual',
            value='航拍镜头持续横移，嘉宾沿入口走向舞台。', idempotency_key='confirmed-camera-conflict-fixture')
        versions = self.base.projects.list_project_versions(self.token, self.project_id)
        agent = library_confirmation_agent(root=self.root, store=self.store, projects=self.base.projects, service=None)
        agent.script_revision = FormalRevisionService(projects=self.base.projects, tasks=TaskService(self.store),
            workspace=self.script, image_workspace=self.images, writes_enabled=True)
        self.app.state.agent_api = agent
        self.page.reload(); self.page.locator('[data-creative-edit-mode="table"]').click()
        expect(self.cell('visual')).to_have_value(conflicting['shots'][0]['visual'])
        corrected = '航拍机位悬停固定，嘉宾沿入口走向舞台。'
        self.assertEqual(self.submit('visual', corrected).status, 200)
        changed = self.current()
        self.assertEqual(changed['shots'][0]['camera_move'], '固定')
        self.assertEqual(changed['shots'][1:], conflicting['shots'][1:])
        self.assertEqual(changed['shots'][0]['source'], conflicting['shots'][0]['source'])
        self.page.locator('#creative-save > summary').click()
        self.page.locator('#formal-script-save-reason').fill('已确认保留固定机位，纠正正文横移矛盾')
        with self.page.expect_response(lambda r:'/script/save?' in r.url and r.request.method=='POST', timeout=60000) as proposed:
            self.page.locator('#formal-script-save').click()
        self.assertEqual(proposed.value.status, 200, proposed.value.text())
        task = proposed.value.json()['task_id']
        with self.page.expect_response(lambda r:f'/tasks/{task}/confirm' in r.url, timeout=60000) as saved:
            self.page.locator('[data-confirm-script-revision]').click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        self.page.reload(); self.page.locator('[data-creative-edit-mode="table"]').click()
        expect(self.cell('visual')).to_have_value(corrected)
        final = self.current(); new_versions = self.base.projects.list_project_versions(self.token, self.project_id)
        self.assertEqual(new_versions[:-1], versions)
        self.assertEqual(new_versions[-1]['parent_version_id'], versions[-1]['version_id'])
        self.assertEqual(final['shots'][1:], conflicting['shots'][1:])
        self.assertEqual(final['shots'][0]['camera_move'], '固定')
        self.assertEqual(final['shots'][0]['source'], conflicting['shots'][0]['source'])
        self.page.locator('.creative-more > summary').click()
        with self.page.expect_download() as downloaded:
            self.page.locator('#formal-script-review-workbook').click()
        review_path = self.root / 'confirmed-camera-review.xlsx'
        downloaded.value.save_as(review_path)
        workbook = load_workbook(review_path, data_only=True); self.addCleanup(workbook.close)
        rows = list(workbook['脚本'].values)
        containing = [row for row in rows if corrected in row]
        self.assertEqual(len(containing), 1)
        self.assertIn('固定', containing[0])
        self.assertIn('航拍', containing[0])
        self.assertEqual(self.errors, [])

    def test_composition_keeps_focus_and_saves_only_committed_chinese_text(self):
        cell = self.cell('subtitle')
        cell.focus()
        cell.evaluate("""element => {
            element.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
            element.value = 'zhong';
            element.dispatchEvent(new InputEvent('input', {bubbles:true, isComposing:true,
                inputType:'insertCompositionText', data:'zhong'}));
        }""")
        self.page.wait_for_timeout(1000)
        self.assertEqual(self.requests, [], 'Uncommitted IME text must not be sent')
        expect(cell).to_be_focused()
        expect(cell).to_have_value('zhong')
        self.assertEqual(self.current()['shots'][0]['subtitle'], self.before['shots'][0]['subtitle'])
        with self.page.expect_response(lambda r: r.request.method == 'POST' and '/script/table?' in r.url) as saved:
            cell.evaluate("""element => {
                element.value = '中文输入完成';
                element.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true, data:'中文输入完成'}));
                element.dispatchEvent(new InputEvent('input', {bubbles:true, isComposing:false,
                    inputType:'insertText', data:'中文输入完成'}));
            }""")
        self.assertEqual(saved.value.status, 200, saved.value.text())
        expect(cell).to_be_focused()
        self.assertEqual([request['value'] for request in self.requests], ['中文输入完成'])
        self.assert_persisted({'subtitle': '中文输入完成'})

    def test_composition_survives_prior_save_receipt_without_sending_candidate(self):
        held = []
        def hold_first(route):
            if not held:
                held.append(route)
            else:
                route.continue_()
        self.page.route('**/script/table?*', hold_first)
        with self.page.expect_request('**/script/table?*'):
            self.cell('subtitle').fill('先前已确认的输入')
        cell = self.cell('subtitle')
        cell.focus()
        cell.evaluate("""element => {
            element.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}));
            element.value = 'hou';
            element.dispatchEvent(new InputEvent('input', {bubbles:true, isComposing:true,
                inputType:'insertCompositionText', data:'hou'}));
        }""")
        held[0].fulfill(response=held[0].fetch())
        self.page.wait_for_timeout(1000)
        self.assertEqual([r['value'] for r in self.requests], ['先前已确认的输入'])
        expect(cell).to_be_focused()
        expect(cell).to_have_value('hou')
        self.assertEqual(self.current()['shots'][0]['subtitle'], '先前已确认的输入')
        with self.page.expect_response(lambda r: r.request.method == 'POST' and '/script/table?' in r.url):
            cell.evaluate("""element => {
                element.value = '后续中文';
                element.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true, data:'后续中文'}));
                element.dispatchEvent(new InputEvent('input', {bubbles:true, isComposing:false,
                    inputType:'insertText', data:'后续中文'}));
            }""")
        self.assertEqual([r['value'] for r in self.requests], ['先前已确认的输入', '后续中文'])
        expect(cell).to_be_focused()
        self.assert_persisted({'subtitle':'后续中文'})

    def test_rejected_value_does_not_block_other_cell_or_correction(self):
        response = self.submit('duration_seconds', '0')
        self.assertEqual(response.status, 409)
        self.assertEqual(response.json()['detail']['code'], 'INVALID_INPUT')
        expect(self.page.locator('#formal-script-save-state')).to_contain_text('时长')
        # Independent correct input can save while the invalid field stays visible.
        self.assertEqual(self.current()['shots'][0]['duration_seconds'], self.before['shots'][0]['duration_seconds'])
        response = self.submit('sound', '保留现场真实环境声')
        self.assertEqual(response.status, 200)
        expect(self.cell('duration_seconds')).to_have_value('0')
        self.assertEqual(self.submit('duration_seconds', '8').status, 200)
        self.assertEqual(self.submit('subtitle', '').status, 200)
        self.page.locator('[data-creative-edit-mode="shot"]').click()
        second = self.before['shots'][1]['shot_id']
        self.page.locator(f'[data-creative-shot="{second}"]').click()
        self.page.locator('[data-creative-edit-mode="table"]').click()
        self.assert_persisted({'duration_seconds': 8, 'subtitle': '', 'sound': '保留现场真实环境声'})

    def test_optional_fields_clear_and_restore_in_both_editor_layouts(self):
        values = {'voiceover': '匿名旁白', 'subtitle': '匿名字幕', 'notes': '匿名备注'}
        def submit_optional(field, value):
            cell = self.cell(field)
            if not cell.is_visible():
                cell.locator('xpath=ancestor::details/summary').click()
            return self.submit(field, value)
        for mode, width in [('shot', 390), ('table', 1440)]:
            with self.subTest(mode=mode, width=width):
                self.page.set_viewport_size({'width': width, 'height': 1000})
                self.page.locator(f'[data-creative-edit-mode="{mode}"]').click()
                for field, value in values.items():
                    self.assertEqual(submit_optional(field, value + mode).status, 200)
                    self.assertEqual(submit_optional(field, '').status, 200)
                self.assert_persisted(dict.fromkeys(values, ''))
                self.page.locator(f'[data-creative-edit-mode="{mode}"]').click()
                for field, value in values.items():
                    self.assertEqual(submit_optional(field, value).status, 200)
                self.assert_persisted(values)

    def test_new_input_during_inflight_save_survives_old_receipt(self):
        held = []
        def hold_first(route):
            if not held:
                held.append(route)
            else:
                route.continue_()
        self.page.route('**/script/table?*', hold_first)
        with self.page.expect_request('**/script/table?*'):
            self.cell('subtitle').fill('保存中的 A')
        self.page.wait_for_function('state.formalScriptSaveInFlight')
        self.cell('subtitle').fill('最终 B')
        route = held[0]
        route.fulfill(response=route.fetch())
        expect(self.page.locator('#formal-script-save-state')).to_contain_text('修改已保存')
        self.assert_persisted({'subtitle': '最终 B'})

    def test_unknown_response_replays_exact_request_once(self):
        attempts = []
        def lose_first(route):
            attempts.append(deepcopy(route.request.post_data_json))
            response = route.fetch()
            if len(attempts) == 1:
                route.abort('failed')
            else:
                route.fulfill(response=response)
        self.page.route('**/script/table?*', lose_first)
        self.cell('subtitle').fill('响应丢失但服务端已保存')
        expect(self.page.locator('#formal-script-retry')).to_be_visible()
        revision = self.current()['revision']
        self.page.locator('#formal-script-retry').click()
        expect(self.page.locator('#formal-script-retry')).to_be_hidden()
        self.assertEqual(attempts[0], attempts[1])
        self.assertEqual(self.current()['revision'], revision)
        self.assert_persisted({'subtitle': '响应丢失但服务端已保存'})

    def test_two_windows_require_explicit_conflict_resolution(self):
        other = self.new_page()
        other.goto(self.url)
        other.locator('[data-creative-edit-mode="table"]').click()
        self.assertEqual(self.submit('subtitle', '另一窗口的已保存内容', page=other).status, 200)
        self.assertEqual(self.submit('subtitle', '此处保留的输入').status, 409)
        expect(self.cell('subtitle')).to_have_value('此处保留的输入')
        self.page.locator('[data-script-conflict-read]').click()
        expect(self.page.locator('#formal-script-conflict')).to_contain_text('另一窗口的已保存内容')
        for width in (1440, 390):
            self.page.set_viewport_size({'width': width, 'height': 1000})
            self.assertLessEqual(self.page.locator('html').evaluate('el => el.scrollWidth'), width + 1)
            if os.environ.get('MPLUS_RV01_EVIDENCE_ROOT'):
                self.page.screenshot(path=str(self.root / f'conflict-{width}.png'), full_page=True)
        self.assertEqual(self.current()['shots'][0]['subtitle'], '另一窗口的已保存内容')
        self.page.locator('[data-script-conflict-local]').click()
        expect(self.page.locator('#formal-script-conflict')).to_be_hidden()
        expect(self.page.locator('#formal-script-save-state')).to_contain_text('修改已保存')
        self.assert_persisted({'subtitle': '此处保留的输入'})

    def test_conflict_can_use_server_content_without_another_write(self):
        other = self.new_page()
        other.goto(self.url)
        other.locator('[data-creative-edit-mode="table"]').click()
        self.assertEqual(self.submit('subtitle', '保留另一窗口', page=other).status, 200)
        self.assertEqual(self.submit('subtitle', '明确放弃此处修改').status, 409)
        revision = self.current()['revision']
        self.page.locator('[data-script-conflict-read]').click()
        self.page.locator('[data-script-conflict-server]').click()
        expect(self.cell('subtitle')).to_have_value('保留另一窗口')
        self.assertEqual(self.current()['revision'], revision)
        self.assert_persisted({'subtitle': '保留另一窗口'})

    def test_late_response_cannot_replace_new_project_or_release_its_save(self):
        workspace = self.base.projects.get_workspace(self.token, self.workspace_id)
        other = self.base.projects.create_project(self.token, workspace_id=self.workspace_id,
            name='独立的第二项目', category_id='launch', expected_workspace_version=workspace['workspace_version'])
        held = []
        self.page.route('**/script/table?*', lambda route: held.append(route))
        with self.page.expect_request('**/script/table?*'):
            self.cell('subtitle').fill('仅属于原项目的保存')
        # Force a new loader generation while transport is outstanding. Normal
        # navigation awaits flush; this also covers remounts and stale callbacks.
        self.page.evaluate('''id => {
            history.pushState({}, '', `/projects/${id}/script`);
            return loadFormalScript(id);
        }''', other['project_id'])
        before = self.page.evaluate('JSON.stringify(state.formalScript)')
        route = held[0]
        with self.page.expect_response('**/script/table?*'):
            route.fulfill(response=route.fetch())
        self.page.wait_for_function('state.formalScriptSaveInFlight === false')
        self.assertEqual(self.page.evaluate('state.formalScriptProjectId'), other['project_id'])
        self.assertEqual(self.page.evaluate('JSON.stringify(state.formalScript)'), before)
        self.assertIsNone(self.page.evaluate('state.formalScriptSavePromise'))
        self.assertEqual(self.current()['shots'][0]['subtitle'], '仅属于原项目的保存')
