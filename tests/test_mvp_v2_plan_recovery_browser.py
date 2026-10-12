"""Actual UI/API/persistent-service recovery; only model boundaries are substituted."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright
from tests.test_mvp_v2_formal_browser_journey import _FixtureServer, _open_new_project_brief

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "tests/support/stabilization_fixture_server.py"


class _StabilizationServer(_FixtureServer):
    def start(self):
        with patch("tests.test_mvp_v2_formal_browser_journey.SERVER_SCRIPT", SERVER):
            super().start()


def read_rows(root, sql, args=()):
    with sqlite3.connect((root / "data/workspace.sqlite3").as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, args)]


class PlanRecoveryBrowserTests(unittest.TestCase):
    def test_existing_waiting_plan_reconfirms_same_template_and_changed_type(self):
        configured = os.environ.get("MPLUS_PLAN_RECOVERY_BROWSER_ROOT")
        if configured:
            root = Path(configured).resolve()
        else:
            temporary = tempfile.TemporaryDirectory(prefix="mplus-plan-recovery-browser-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name).resolve()
        root.mkdir(parents=True, exist_ok=True)
        external = os.environ.get("MPLUS_PLAN_RECOVERY_BROWSER_URL")
        server = None if external else _StabilizationServer(root)
        if server:
            server.start()
            self.addCleanup(server.stop)
        base = external or server.base_url
        results, requests, errors = [], [], []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.set_default_timeout(20_000)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def record(response):
                path = urlparse(response.url).path
                if path.startswith("/api/formal/"):
                    requests.append({"method": response.request.method, "path": path,
                                     "status": response.status})
            page.on("response", record)
            try:
                page.goto(base + "/projects")
                page.locator("#formal-owner-confirm").check()
                page.locator("#formal-owner-enable").click()
                expect(page.locator("#formal-owner-onboarding")).to_be_hidden()
                for branch, expected_count in (("same-template", None), ("changed-type", 25)):
                    page.goto(base + "/projects")
                    page.locator("#formal-project-new").click()
                    page.locator("#formal-project-name").fill("匿名规划恢复-" + branch)
                    page.locator("#formal-project-create-submit").click()
                    _open_new_project_brief(page)
                    project_id = urlparse(page.url).path.split("/")[2]
                    brief_url = page.url
                    page.locator("#formal-brief-text").fill("\n".join((
                        "项目名称: 匿名规划恢复-" + branch, "脚本类型: 发布会", "品牌或客户: 匿名团队",
                        "目标时长: 60秒", "风格调性: 克制", "视频目标: 记录现场互动",
                        "关键事件与流程: 来宾入场、互动体验、主舞台活动、合影收束",
                        "使用场景: 现场活动", "禁止或不能出现的内容: 不得虚构事实")))
                    page.locator("#formal-brief-upload-button").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("待确认")
                    page.locator("#formal-brief-confirm").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                    plan_url = f"{base}/projects/{project_id}/script?panel=structure&mode=text-only"
                    page.goto(plan_url)
                    with page.expect_response(lambda response: response.request.method == "POST" and urlparse(response.url).path.endswith("/plans")) as prepared:
                        page.locator("#formal-plan-create").click()
                    self.assertEqual(prepared.value.status, 200, prepared.value.text())
                    old = prepared.value.json()
                    self.assertGreater(old["shot_count"], 3)
                    # Only the style changes in this branch; retain the original
                    # valid plan's N, including the planner's capacity rules.
                    if branch == "same-template":
                        expected_count = old["shot_count"]
                    expect(page.locator("#formal-plan-confirmation")).to_be_visible()
                    page.reload()
                    expect(page.locator("#formal-plan-status-copy")).to_contain_text("已恢复待确认")
                    before = read_rows(root, "SELECT current_brief_snapshot_id,template_package_ref FROM projects WHERE project_id=?", (project_id,))[0]
                    page.goto(brief_url)
                    field, value = ("风格调性", "高级") if branch == "same-template" else ("脚本类型", "年会")
                    page.locator(f'[data-edit-formal-fact="{field}"]').click()
                    page.locator("#formal-brief-text").fill(field + ": " + value)
                    page.locator("#formal-brief-upload-button").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("待确认")
                    conflict = page.locator("#formal-conflict-list .ui00-conflict-item").filter(has=page.locator("select"))
                    expect(conflict).to_have_count(1)
                    conflict.locator("select").select_option(label=value)
                    conflict.get_by_role("button", name="确认选择", exact=True).click()
                    expect(page.locator("#formal-conflict-list select")).to_have_count(0)
                    page.locator("#formal-brief-confirm").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                    if branch == "changed-type":
                        page.locator("#formal-template-picker > summary").click()
                        select = page.locator("#formal-template-select")
                        select.select_option(index=0)
                        page.locator("#formal-template-confirm").check()
                        page.locator("#formal-template-apply").click()
                        expect(page.locator("#formal-template-current")).to_contain_text("匿名年会25镜")
                    page.goto(plan_url)
                    expect(page.locator("#formal-plan-create")).to_be_visible()
                    expect(page.locator("#formal-plan-status-copy")).to_contain_text("已更新")
                    expect(page.locator("#formal-plan-confirmation")).to_be_hidden()
                    page.screenshot(path=str(root / f"{branch}-stale.png"), full_page=True)
                    with page.expect_response(lambda response: response.request.method == "POST" and urlparse(response.url).path.endswith("/plans")) as rebuilt:
                        page.locator("#formal-plan-create").click()
                    self.assertEqual(rebuilt.value.status, 200, rebuilt.value.text())
                    new = rebuilt.value.json()
                    self.assertNotEqual(new["plan_id"], old["plan_id"])
                    self.assertEqual(new["shot_count"], expected_count)
                    page.locator("#formal-plan-confirm-check").check()
                    with page.expect_response(lambda response: response.request.method == "POST" and "/plans/" in response.url and "/confirm" in response.url, timeout=60_000) as confirmed:
                        page.locator("#formal-plan-confirm").click()
                    self.assertEqual(confirmed.value.status, 200, confirmed.value.text())
                    result = confirmed.value.json()
                    self.assertEqual(len(result["script_rows"]), expected_count)
                    page.reload()
                    expect(page.locator("#formal-script-version-label")).to_have_text("版本 1")
                    project = read_rows(root, "SELECT current_brief_snapshot_id,current_version_id,category_id,template_package_ref FROM projects WHERE project_id=?", (project_id,))[0]
                    self.assertNotEqual(before["current_brief_snapshot_id"], project["current_brief_snapshot_id"])
                    self.assertEqual(project["current_version_id"], result["version_id"])
                    self.assertEqual(read_rows(root, "SELECT count(*) n FROM project_versions WHERE project_id=?", (project_id,))[0]["n"], 1)
                    old_task = read_rows(root, "SELECT status FROM tasks WHERE task_id=?", (old["plan_id"],))[0]
                    self.assertEqual(old_task["status"], "waiting_user")
                    event = json.loads(read_rows(root, "SELECT details_json FROM task_events WHERE task_id=? AND status='waiting_user' ORDER BY sequence_no DESC LIMIT 1", (new["plan_id"],))[0]["details_json"])
                    self.assertEqual(event["brief_snapshot_id"], project["current_brief_snapshot_id"])
                    results.append({"branch": branch, "project_id": project_id, "old_plan_id": old["plan_id"],
                        "new_plan_id": new["plan_id"], "version_id": result["version_id"], "shot_count": expected_count,
                        "current_brief_bound": True, "old_task_preserved": True, "version_count": 1})
                if server:
                    server.restart()
                    page.reload()
                    expect(page.locator("#formal-script-version-label")).to_have_text("版本 1")
                self.assertEqual(errors, [])
            except Exception:
                page.screenshot(path=str(root / "failure.png"), full_page=True)
                (root / "failure.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
                raise
            finally:
                (root / "recovery-evidence.json").write_text(json.dumps({"results": results,
                    "requests": requests, "page_errors": errors, "external_models": "deterministic substitutes",
                    "actual_identity_api_services_database": True}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                context.tracing.stop(path=str(root / "recovery-trace.zip"))
                context.close()
                browser.close()
