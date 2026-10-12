from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlparse
import unittest

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
UI_ROOT = ROOT / "mvp_v2" / "ui"
PROJECT_ID = "anonymous-agent-project"


class AgentUiFixture:
    def __init__(self):
        self.session_id = "session-anonymous-agent"
        self.stage = 0
        self.session_creates = 0
        self.ui_token = "ui-token"
        self.rejected_before_dispatch = 0
        self.requests: list[tuple[str, str, dict | None]] = []

    @property
    def project(self):
        return {
            "project_id": PROJECT_ID,
            "name": "匿名助手项目",
            "category_id": "launch",
            "status": "active",
            "project_version": 3,
            "has_brief": True,
        }

    def session(self):
        proposal = {
            "type": "action_proposal",
            "title": "确认应用草稿",
            "summary": "仅应用已解析的草稿修改。",
            "action": "apply_formal_script_revision",
            "proposal_digest": "a" * 64,
            "confirmation_stage": "apply_draft",
            "will_change": "第 2 镜声音",
            "will_not_change": "父版本、其他镜头和发布状态",
            "impact_scope": "当前项目／第 2 镜",
            "calls_models": False,
        }
        timeline = [{
            "task_id": "task-apply-draft",
            "task_type": "formal_script_revision",
            "sequence_no": 1,
            "status": "proposed" if self.stage == 0 else "completed",
            "created_at": "2026-08-31T10:00:00Z",
            "response": proposal,
        }]
        tasks = [{
            "task_id": "task-apply-draft",
            "task_type": "formal_script_revision",
            "status": "proposed" if self.stage == 0 else "completed",
        }]
        if self.stage >= 1:
            save_response = {
                "type": "formal_script_draft_updated",
                "title": "草稿已应用",
                "summary": "草稿修改已应用，等待确认保存版本。",
                "next_confirmation": {
                    "task_id": "task-save-version",
                    "proposal_digest": "b" * 64,
                },
                "will_change": "创建不可变脚本子版本",
                "will_not_change": "父版本、Brief 和发布状态",
                "impact_scope": "当前项目／草稿",
            }
            timeline.append({
                "task_id": "task-save-version",
                "task_type": "formal_script_revision",
                "sequence_no": 2,
                "status": "waiting_confirmation" if self.stage == 1 else "completed",
                "created_at": "2026-08-31T10:00:01Z",
                "response": save_response if self.stage == 1 else {
                    "type": "formal_script_revision_completed",
                    "title": "版本已保存",
                    "summary": "已按确认差异创建不可变子版本。",
                    "version_label": "版本 2",
                    "next_href": f"/projects/{PROJECT_ID}/script",
                },
            })
            tasks.append({
                "task_id": "task-save-version",
                "task_type": "formal_script_revision",
                "status": "waiting_confirmation" if self.stage == 1 else "completed",
            })
        return {
            "session": {"agent_session_id": self.session_id, "project_id": PROJECT_ID},
            "tasks": tasks,
            "timeline": timeline,
            "current_task": tasks[-1] if tasks else None,
        }

    def response(self, method: str, path: str, payload: dict | None = None):
        route = urlparse(path).path
        self.requests.append((method, route, payload))
        if method == "GET" and route == "/api/ui/session":
            return 200, {"token": self.ui_token}
        if method == "GET" and route == "/api/formal/setup":
            return 200, {"brief_adapter": "ready", "script_availability": "available", "admin_diagnostics_availability": "blocked"}
        if method == "GET" and route == f"/api/formal/workspaces/browser-workspace":
            return 200, {"workspace_version": 1}
        if method == "GET" and route == f"/api/formal/workspaces/browser-workspace/projects":
            return 200, {"items": [self.project]}
        if method == "GET" and route == f"/api/agent/projects/{PROJECT_ID}/state":
            return 200, {"project": self.project, "versions": {"count": 1, "latest_version_id": "version-1"}, "blocker": {"title": "可继续", "detail": "项目已准备好接受指令。", "next_step": "提交需要处理的事项"}}
        if method == "GET" and route == f"/api/formal/projects/{PROJECT_ID}/overview":
            return 200, {"stages": [], "blockers": [], "next_action": {}}
        if method == "POST" and route == "/api/agent/sessions":
            self.session_creates += 1
            return 200, {"session": {"agent_session_id": self.session_id, "project_id": PROJECT_ID}, "tasks": [], "timeline": [], "current_task": None}
        if method == "GET" and route == f"/api/agent/sessions/{self.session_id}":
            return 200, self.session()
        if method == "GET" and route.startswith("/api/agent/tasks/") and route.endswith("/events"):
            return 200, {"items": []}
        if method == "POST" and route.endswith("/messages"):
            return 200, {"task": {"task_id": "task-apply-draft", "status": "proposed"}, "response": self.session()["timeline"][0]["response"], "events": []}
        if method == "POST" and route.endswith("/confirm"):
            self.stage += 1
            return 200, {"task": {"task_id": "task-save-version" if self.stage > 1 else "task-apply-draft", "status": "completed"}, "response": {"type": "formal_script_revision_completed", "summary": "已保存版本 2", "version_label": "版本 2", "next_href": f"/projects/{PROJECT_ID}/script"}, "events": []}
        if method == "POST" and route.endswith("/dismiss"):
            return 200, {"task": {"task_id": "task-apply-draft", "status": "cancelled"}, "response": {"type": "proposal_cancelled", "summary": "提案已取消"}, "events": []}
        return 404, {"detail": "not found"}


def handler_for(fixture: AgentUiFixture):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            return

        def _send(self, status: int, body: dict | bytes, content_type: str):
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            route = urlparse(self.path).path
            if route == "/assets/app.css":
                self._send(200, (UI_ROOT / "app.css").read_bytes(), "text/css; charset=utf-8")
                return
            if route == "/assets/app.js":
                self._send(200, (UI_ROOT / "app.js").read_bytes(), "text/javascript; charset=utf-8")
                return
            if route.startswith("/api/"):
                status, body = fixture.response("GET", self.path)
                self._send(status, body, "application/json; charset=utf-8")
                return
            self._send(200, (UI_ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8")) if raw else None
            if self.headers.get("X-MPlus-UI-Token") != fixture.ui_token:
                fixture.rejected_before_dispatch += 1
                self._send(403, {"detail": "本地会话已失效，请刷新页面"}, "application/json; charset=utf-8")
                return
            status, body = fixture.response("POST", self.path, payload)
            self._send(status, body, "application/json; charset=utf-8")

    return Handler


class AgentUiBrowserTests(unittest.TestCase):
    def test_project_assistant_uses_real_controls_and_recovers_session(self):
        fixture = AgentUiFixture()
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(fixture))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1280, "height": 900})
                context.add_init_script("localStorage.setItem('mplus.agent.auth', 'browser-agent-auth'); localStorage.setItem('mplus.agent.workspace', 'browser-workspace');")
                page = context.new_page()
                page.goto(f"{base}/projects/{PROJECT_ID}/assistant")
                page.locator("#agent-session-status", has_text="会话已连接").wait_for()
                fixture.ui_token = "rotated-after-server-restart"
                page.locator("#agent-message").fill("请把第 2 镜声音改为现场掌声")
                page.locator("#agent-message-submit").click()
                page.get_by_role("button", name="应用到草稿", exact=True).wait_for()
                self.assertEqual(fixture.session_creates, 1)
                self.assertEqual(fixture.rejected_before_dispatch, 1)
                message_posts = [payload for method, route, payload in fixture.requests if method == "POST" and route.endswith("/messages")]
                self.assertEqual(message_posts[-1], {"content": "请把第 2 镜声音改为现场掌声"})
                self.assertEqual(len(message_posts), 1)
                page.get_by_role("button", name="应用到草稿", exact=True).click()
                page.get_by_role("button", name="确认保存版本").wait_for()
                page.get_by_role("button", name="确认保存版本").click()
                page.locator(".agent-version-result", has_text="版本 2").wait_for()
                next_link = page.locator(".agent-next").get_by_role("link", name="查看脚本", exact=True)
                self.assertEqual(next_link.get_attribute("href"), f"/projects/{PROJECT_ID}/script")
                self.assertNotIn("proposal_digest", page.locator("#page-agent").inner_text())
                self.assertNotIn("/api/", page.locator("#page-agent").inner_text())

                storage = context.storage_state()
                context.close()
                recovered = browser.new_context(storage_state=storage, viewport={"width": 1280, "height": 900})
                page = recovered.new_page()
                page.goto(f"{base}/projects/{PROJECT_ID}/assistant")
                page.locator("#agent-session-status", has_text="会话已连接").wait_for()
                self.assertEqual(fixture.session_creates, 1)
                self.assertIn("请把第 2 镜声音改为现场掌声", page.locator("#page-agent").inner_text())
                self.assertIn("版本 2", page.locator("#page-agent").inner_text())
                recovered.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
