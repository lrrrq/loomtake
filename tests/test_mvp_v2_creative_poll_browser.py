"""RV06-08 actual HTTP generation, delta polling, editing and persisted reload."""
import json
import threading
import unittest

from playwright.sync_api import expect
from mvp_v2 import local_ui
from mvp_v2.formal_runtime import uploaded_document
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.version_delivery import VersionDeliveryService
from tests import test_mvp_v2_creative_draft_api as api_fixture
from tests import test_mvp_v2_manual_receipt_browser as browser_fixture
from tests.test_mvp_v2_creative_generation import BLOCKS


class CreativePollBrowserTests(unittest.TestCase):
    open_page = browser_fixture.ManualReceiptBrowserTests.open_page

    def test_stream_delta_terminal_edit_and_reload_use_real_services(self):
        support = api_fixture.CreativeDraftApiTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.fixture
        f._confirm_formal_brief(support.project['project_id'])
        protected_tables = ('project_versions', 'version_reviews', 'version_publications',
            'document_images', 'project_images', 'brief_confirmations')
        def protected_rows():
            with f.store.connection() as connection:
                return {table: [tuple(row) for row in connection.execute(f'SELECT * FROM {table} ORDER BY rowid')]
                    for table in protected_tables}
        before = protected_rows()
        release = threading.Event()
        provider_calls = []
        def stream(prompt, model, limit, emit):
            provider_calls.append(True)
            text = json.dumps({'blocks': BLOCKS}, ensure_ascii=False)
            emit(text)
            release.wait(25)
            return text
        f.generation.creative_stream_provider = stream
        artifacts = f.agent.services.artifacts
        scripts = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(store=f.store,
            projects=f.projects, delivery=VersionDeliveryService(f.store, artifacts), commands=None,
            artifacts=artifacts), runs_root=f.root/'runs', source_root=f.root/'source')
        app = local_ui.create_app(agent_api=f.agent, formal_script_service=scripts,
            formal_generation_service=f.generation, formal_brief_adapter=f.adapter,
            formal_brief_upload_factory=uploaded_document)
        page, origin = self.open_page(app, f.token, f.workspace_id)
        self.addCleanup(release.set)
        page.goto(origin + '/projects/' + support.project['project_id'] + '/script')
        with page.expect_response(lambda r: '/creative-draft?' in r.url
                and r.request.method == 'GET' and r.status == 200
                and r.json().get('response_kind') == 'delta') as delta:
            page.locator('#creative-first-confirmed-start').click()
        self.assertNotIn('blocks', delta.value.json())
        task_id = delta.value.json()['task']['task_id']
        with page.expect_response(lambda r: '/creative-draft?' in r.url
                and r.request.method == 'GET' and r.status == 200
                and r.json().get('response_kind') == 'delta') as resumed:
            page.reload()
        self.assertEqual(resumed.value.json()['task']['task_id'], task_id)
        self.assertEqual(len(provider_calls), 1)
        release.set()
        expect(page.locator('#creative-first-status')).to_contain_text('初稿已保存')
        subsequent_reads = []
        page.on('request', lambda request: subsequent_reads.append(request.url)
            if '/creative-draft?' in request.url and request.method == 'GET' else None)
        page.wait_for_timeout(1500)  # More than two normal 650 ms polling intervals.
        self.assertEqual(subsequent_reads, [])
        cell = page.locator('[data-first-field="画面描述"]').first
        cell.fill('匿名实际服务轮询后的修改')
        page.locator('#creative-first-save').click()
        expect(page.locator('#creative-first-status')).to_contain_text('初稿已保存')
        page.reload()
        expect(cell).to_have_value('匿名实际服务轮询后的修改')
        current = support.request().json()
        self.assertEqual(current['blocks'][0]['fields']['画面描述'], '匿名实际服务轮询后的修改')
        self.assertEqual(current['task']['status'], 'completed')
        self.assertEqual(protected_rows(), before)
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)

    def test_late_project_a_poll_cannot_replace_project_b_and_return_resumes_a(self):
        from starlette.concurrency import run_in_threadpool
        support = api_fixture.CreativeDraftApiTests('runTest')
        support.setUp(); self.addCleanup(support.doCleanups)
        f = support.fixture
        a = support.project['project_id']
        b = f._project('匿名切换项目乙')['project_id']
        for project_id in (a, b):
            f._confirm_formal_brief(project_id)
        finish = threading.Event()
        release_response = threading.Event()
        held = threading.Event()
        self.addCleanup(finish.set)
        self.addCleanup(release_response.set)
        calls = []
        def stream(prompt, model, limit, emit):
            calls.append(True)
            text = json.dumps({'blocks': BLOCKS}, ensure_ascii=False)
            emit(text)
            finish.wait(60)
            return text
        f.generation.creative_stream_provider = stream
        artifacts = f.agent.services.artifacts
        scripts = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(store=f.store,
            projects=f.projects, delivery=VersionDeliveryService(f.store, artifacts),
            commands=None, artifacts=artifacts), runs_root=f.root/'runs', source_root=f.root/'source')
        app = local_ui.create_app(agent_api=f.agent, formal_script_service=scripts,
            formal_generation_service=f.generation, formal_brief_adapter=f.adapter,
            formal_brief_upload_factory=uploaded_document)
        @app.middleware('http')
        async def delay_one_real_poll(request, call_next):
            response = await call_next(request)
            if (request.url.path == f'/api/formal/projects/{a}/creative-draft'
                    and request.method == 'GET' and 'known_revision' in request.query_params
                    and not held.is_set()):
                held.set()
                await run_in_threadpool(release_response.wait, 60)
            return response
        page, origin = self.open_page(app, f.token, f.workspace_id)
        page.goto(origin + f'/projects/{a}/script')
        page.locator('#creative-first-confirmed-start').click()
        self.assertTrue(held.wait(15), 'A delta poll was not held')
        # Use the actual SPA navigation so the pending request remains in this document.
        page.locator('.project-exit').click()
        page.locator(f'#formal-project-list a[href*="/projects/{b}/"]').click()
        page.locator('.project-nav [data-route="script"]').click()
        expect(page.locator('#creative-first-confirmed-start')).to_be_visible()
        expect(page.locator('[data-first-field="画面描述"]')).to_have_count(0)
        with page.expect_response(lambda r: f'/projects/{a}/creative-draft?' in r.url
                and r.request.method == 'GET'):
            release_response.set()
        page.wait_for_timeout(900)
        self.assertIn(f'/projects/{b}/script', page.url)
        expect(page.locator('[data-first-field="画面描述"]')).to_have_count(0)
        expect(page.locator('#creative-first-confirmed-start')).to_be_visible()
        finish.set()
        page.locator('.project-exit').click()
        page.locator(f'#formal-project-list a[href*="/projects/{a}/"]').click()
        page.locator('.project-nav [data-route="script"]').click()
        expect(page.locator('#creative-first-status')).to_contain_text('初稿已保存', timeout=30000)
        self.assertEqual(len(calls), 1)
        page.reload()
        expect(page.locator('#creative-first-status')).to_contain_text('初稿已保存')
        with f.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tasks WHERE project_id=?', (a,)).fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM tasks WHERE project_id=?', (b,)).fetchone()[0], 0)
