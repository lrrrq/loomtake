"""Browser actions against real admin HTTP routes and SQLite; shell DTO is anonymous."""
import json
import os
from pathlib import Path
import socket
from threading import Thread
import time
import unittest

import uvicorn
from playwright.sync_api import expect, sync_playwright
from mvp_v2.local_ui import create_app
from tests import test_mvp_v2_formal_admin_service as service_fixture
from tests.test_mvp_v2_workbench_v2_browser import _WorkbenchFixture


class AdminRecoveryBrowserTests(unittest.TestCase):
    setUp = service_fixture.FormalAdminServiceTests.setUp
    tearDown = service_fixture.FormalAdminServiceTests.tearDown

    def test_cancel_retry_terminal_and_refresh_use_one_actual_attempt(self):
        calls = []
        self.admin.build_recovery_executor({'formal_generate': lambda ctx: calls.append(ctx.attempt_id) or {
            'status': 'succeeded', 'output_ref': 'artifact://anonymous/verified-output', 'output_sha256': 'a' * 64}})
        shell = _WorkbenchFixture()
        shell.projects = [self.project]
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_app(formal_admin_service=self.admin), log_level='error'))
        worker = Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        worker.start()
        try:
            for _ in range(100):
                if server.started:
                    break
                time.sleep(.02)
            self.assertTrue(server.started)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={'width': 1440, 'height': 900})
                page.add_init_script("localStorage.setItem('mplus.agent.auth', %s);localStorage.setItem('mplus.agent.workspace', %s);" % (json.dumps(self.owner_token), json.dumps(self.workspace_id)))
                errors = []
                page.on('pageerror', lambda e: errors.append(str(e)))
                def shell_reads(route):
                    from urllib.parse import urlparse
                    path = urlparse(route.request.url).path
                    if path.startswith('/api/formal/admin/') or path == '/api/ui/session':
                        route.continue_()
                    else:
                        status, dto = shell.response(route.request.method, path)
                        route.fulfill(status=status, json=dto)
                page.route('**/api/**', shell_reads)
                page.goto(f'http://127.0.0.1:{port}/admin')
                page.locator(f'[data-admin-task="{self.task["task_id"]}"]').focus()
                page.keyboard.press('Enter')
                form = page.locator('[data-admin-recovery-form]')
                form.locator('textarea').fill('已核对匿名失败原因，检查恢复影响')
                form.locator('button[type="submit"]').press('Enter')
                page.locator('[data-admin-recovery-cancel]').press('Enter')
                self.assertEqual(calls, [])
                with self.store.connection() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM task_attempts WHERE task_id=?', (self.task['task_id'],)).fetchone()[0], 1)
                expect(form.locator('textarea')).to_be_focused()
                form.locator('button[type="submit"]').press('Enter')
                page.locator('[data-admin-recovery-check]').check()
                with page.expect_response(lambda r: r.request.method == 'POST' and r.url.endswith('/confirm')) as confirmed:
                    page.locator('[data-admin-recovery-confirm]').press('Enter')
                self.assertEqual(confirmed.value.status, 200)
                expect(page.locator('.attempt-result').last).to_contain_text('本次阶段执行成功')
                # Replay the exact HTTP confirmation as a lost-response retry.
                request = confirmed.value.request
                replay = page.request.post(request.url, headers=request.all_headers(), data=request.post_data)
                self.assertEqual(replay.status, 200)
                self.assertEqual(len(calls), 1)
                page.reload()
                expect(page.locator('.attempt-result').last).to_contain_text('本次阶段执行成功')
                expect(page.locator('.attempt-result').first).to_contain_text('执行失败')
                self.assertEqual(len(calls), 1)
                with self.store.connection() as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM task_attempts WHERE task_id=?', (self.task['task_id'],)).fetchone()[0], 2)
                    self.assertEqual(db.execute('SELECT status FROM tasks WHERE task_id=?', (self.task['task_id'],)).fetchone()[0], 'failed')
                target = os.environ.get('MPLUS_WORKBENCH_V2_ARTIFACTS')
                if target:
                    Path(target).mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(Path(target) / 'real-admin-recovery.png'), full_page=True)
                self.assertEqual(errors, [])
                browser.close()
        finally:
            server.should_exit = True
            worker.join(10)
            listener.close()
