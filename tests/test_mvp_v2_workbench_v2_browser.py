"""Anonymous DTO browser tests; service writes and providers are tested separately."""
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse
import unittest

from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_creative_workspace_browser import _CreativeFixture
from tests.test_mvp_v2_ui00_journey3 import UI_ROOT, _handler


class _WorkbenchFixture(_CreativeFixture):
    def __init__(self):
        super().__init__()
        self.release = Event()
        self.started = Event()
        self.slow_path = ""
        self.reads = []
        self.failure = None
        self.long_tasks = False
        self.projects = [{"project_id": f"p-{v}", "name": f"项目{v.upper()}", "status": "active",
                          "project_version": 1, "category_id": "launch"} for v in ("a", "b")]

    def response(self, method, path):
        route = urlparse(path).path
        query = parse_qs(urlparse(path).query)
        self.reads.append((method, path))
        if self.failure and route.endswith(self.failure[0]):
            return self.failure[1], {"detail": {"code": "PERMISSION_DENIED" if self.failure[1] == 403 else "READ_FAILED", "message": "本次读取失败"}}
        if self.slow_path and self.slow_path in path:
            self.started.set()
            self.release.wait(8)
        if route == "/api/formal/setup":
            status, value = super().response(method, path)
            return status, {**value, "admin_diagnostics_availability": "available"}
        if route.endswith("/projects") and method == "GET":
            return 200, {"items": self.projects}
        if route.startswith("/api/formal/projects/p-") and route.count("/") == 4:
            return 200, next(p for p in self.projects if p["project_id"] == route.rsplit("/", 1)[1])
        if route.endswith("/access"):
            return 200, {"admin_console": "available"}
        if route.startswith("/api/formal/admin/") and "/tasks/" in route:
            tid = route.rsplit("/", 1)[1]
            return 200, {"task": {"task_id": tid, "project_name": f"任务{tid.upper()}", "project_version": 1,
                "title": "脚本修改", "status": "failed", "state_label": "未完成", "issue": "本次操作未完成"},
                "attempts": [], "checkpoints": [], "provider_calls": [], "recovery_actions": [],
                "recovery_unavailable_reason": "当前没有可用处理器"}
        if route.startswith("/api/formal/admin/") and route.endswith("/overview"):
            return 200, {"tasks": [{"task_id": v, "project_name": ("跨部门联合发布项目与长事实核对" * 5) if self.long_tasks else f"任务{v.upper()}", "title": "脚本修改",
                "status": "failed", "state_label": "未完成"} for v in ([str(i) for i in range(50)] if self.long_tasks else ("a", "b"))], "count": 50 if self.long_tasks else 2,
                "matched_total": 2, "workspace_status_counts": {"failed": 2}, "next_cursor": None,
                "task_status_counts": {"failed": 2}, "services": {}, "operations": []}
        if route.endswith("/tasks") and method == "GET":
            return 200, {"items": [], "count": 0, "matched_total": 0, "next_cursor": None,
                         "workspace_status_counts": {}}
        if route == "/api/agent/sessions" and method == "POST":
            project_id = self.body["project_id"]
            return 200, {"session": {"agent_session_id": f"session-{project_id}", "project_id": project_id}}
        if route.startswith("/api/agent/sessions/"):
            project_id = route.rsplit("session-", 1)[1]
            return 200, {"session": {"agent_session_id": f"session-{project_id}", "project_id": project_id}}
        if route == "/api/agent/workbench/assets":
            pid = query["agent_session_id"][0].replace("session-", "")
            return 200, {"project_id": pid, "category_id": "launch", "package_id": f"资源{pid.upper()}",
                "package_version": 1, "imports": [{"import_id": f"import-{pid}", "revision": 1,
                    "state": "published", "scope": "project", "items": [{"filename": f"{pid}素材.xlsx"}]}]}
        return super().response(method, path)


class WorkbenchV2BrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = _WorkbenchFixture()

        class Handler(_handler(fixture)):
            def _api(handler, method):
                fixture.body = {}
                if method in {"POST", "PATCH"} and "application/json" in handler.headers.get("Content-Type", ""):
                    fixture.body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", "0"))) or "{}")
                return super()._api(method)

            def _file(handler):
                if urlparse(handler.path).path.startswith("/assets/"):
                    return super()._file()
                body = (UI_ROOT / "index.html").read_text().replace("__MPLUS_UI_BUILD__", "updated-anonymous-build").encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(fixture.release.set)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 900})
        self.page.set_default_timeout(5000)
        self.page.add_init_script("localStorage.setItem('mplus.agent.auth','browser-session');localStorage.setItem('mplus.agent.workspace','browser-workspace');")
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def capture(self, name):
        directory = os.environ.get("MPLUS_WORKBENCH_V2_ARTIFACTS")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target / f"{name}.png"), full_page=True)

    def test_admin_selection_is_immediate_and_late_detail_cannot_replace_new_task(self):
        self.fixture.slow_path = "/tasks/a"
        page = self.page
        page.goto(self.base + "/admin")
        page.locator('[data-admin-task="a"]').click()
        self.assertTrue(self.fixture.started.wait(2))
        expect(page.locator("#formal-admin-task-detail")).to_contain_text("正在读取")
        page.locator('[data-admin-task="b"]').click()
        expect(page.locator("#formal-admin-task-detail h2")).to_have_text("任务B")
        with page.expect_response(lambda r: r.url.endswith("/tasks/a")):
            self.fixture.release.set()
        expect(page.locator("#formal-admin-task-detail h2")).to_have_text("任务B")
        expect(page.locator('[data-admin-task="b"]')).to_have_attribute("aria-current", "true")
        self.capture("admin-selected-b-1440")
        page.set_viewport_size({"width": 375, "height": 812})
        self.capture("admin-detail-375")
        page.locator("#admin-task-back").click()
        expect(page.locator('[data-admin-task="b"]')).to_be_focused()
        self.assertEqual(self.errors, [])

    def test_explicit_invalid_project_stays_empty_and_assets_do_not_cross_projects(self):
        page = self.page
        page.goto(self.base + "/assets?project_id=missing")
        expect(page.locator("#asset-project-select")).to_have_value("")
        expect(page.locator("#workbench-context-status")).to_contain_text("不可访问")
        self.assertFalse(any("/api/agent/sessions" in path for _, path in self.fixture.reads))
        self.fixture.slow_path = "agent_session_id=session-p-a"
        page.locator("#asset-project-select").select_option("p-a")
        self.assertTrue(self.fixture.started.wait(2))
        page.locator("#asset-project-select").select_option("p-b")
        expect(page.locator("#asset-imports")).to_contain_text("p-b素材.xlsx")
        with page.expect_response(lambda r: "agent_session_id=session-p-a" in r.url):
            self.fixture.release.set()
        expect(page.locator("#asset-imports")).not_to_contain_text("p-a素材.xlsx")
        expect(page).to_have_url(self.base + "/assets?project_id=p-b")
        page.reload()
        expect(page.locator("#asset-project-select")).to_have_value("p-b")
        self.assertEqual(self.errors, [])

    def test_long_list_keyboard_and_responsive_detail_keep_local_scroll(self):
        self.fixture.long_tasks = True
        page = self.page
        page.goto(self.base + "/admin")
        expect(page.locator("[data-admin-task]")).to_have_count(50)
        for width in [1440, 1024, 390, 375]:
            page.set_viewport_size({"width": width, "height": 900})
            row = page.locator('[data-admin-task="3"]')
            row.focus()
            row.press("Enter")
            expect(page.locator("#formal-admin-task-detail h2")).to_have_text("任务3")
            expect(page.locator("#formal-admin-task-detail h2")).to_be_focused()
            self.capture(f"long-admin-detail-{width}")
            # Read layout dimensions only; all interactions use visible controls.
            dimensions = page.locator("html").evaluate("node => [node.scrollWidth, node.clientWidth]")
            self.assertLessEqual(dimensions[0], dimensions[1] + 1)
            if width <= 600:
                page.locator("#admin-task-back").press("Enter")
                expect(row).to_be_focused()
            self.capture(f"long-admin-list-{width}")
        self.assertEqual(self.errors, [])

    def test_task_failures_are_distinct_from_empty_and_query_survives_refresh(self):
        page = self.page
        self.fixture.failure = ("/tasks", 500)
        page.goto(self.base + "/tasks?query=核对&status=waiting_confirmation")
        expect(page.locator("#formal-task-list")).to_contain_text("任务状态暂不可用")
        expect(page.locator("#formal-task-query")).to_have_value("核对")
        self.fixture.failure = None
        page.locator("#formal-task-list [data-task-refresh]").click()
        expect(page.locator("#formal-task-list")).to_contain_text("暂无业务任务")
        page.reload()
        expect(page.locator("#formal-task-query")).to_have_value("核对")
        expect(page.locator("#formal-task-status-filter")).to_have_value("waiting_confirmation")
        self.fixture.failure = ("/overview", 403)
        page.goto(self.base + "/admin")
        expect(page.locator("#formal-admin-task-list")).to_contain_text("没有读取权限")
        self.assertEqual(self.errors, [])
