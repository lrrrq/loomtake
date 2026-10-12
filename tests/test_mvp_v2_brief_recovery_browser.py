"""Field drafts survive real project navigation and a transport failure."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright
from tests.test_mvp_v2_plan_recovery_browser import _StabilizationServer


class BriefRecoveryBrowserTests(unittest.TestCase):
    def test_fields_refresh_retry_and_projects_keep_independent_drafts(self):
        configured = os.environ.get("MPLUS_BRIEF_RECOVERY_BROWSER_ROOT")
        if configured:
            root = Path(configured).resolve()
        else:
            temporary = tempfile.TemporaryDirectory(prefix="mplus-brief-recovery-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
        root.mkdir(parents=True, exist_ok=True)
        server = _StabilizationServer(root)
        server.start()
        self.addCleanup(server.stop)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.set_default_timeout(15000)
            errors, submissions = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            try:
                page.goto(server.base_url + "/projects")
                page.locator("#formal-owner-confirm").check()
                page.locator("#formal-owner-enable").click()
                expect(page.locator("#formal-owner-onboarding")).to_be_hidden()
                projects = []
                for name in ("匿名字段项目A", "匿名字段项目B"):
                    page.goto(server.base_url + "/projects")
                    page.locator("#formal-project-new").click()
                    page.locator("#formal-project-name").fill(name)
                    page.locator("#formal-project-create-submit").click()
                    page.wait_for_url("**/projects/*/brief")
                    project_id = urlparse(page.url).path.split("/")[2]
                    expect(page.locator("#formal-brief-upload")).to_be_visible()
                    page.locator("#formal-brief-text").fill("\n".join((
                        "项目名称: " + name, "脚本类型: 发布会", "品牌或客户: 匿名团队",
                        "目标时长: 60秒", "风格调性: 克制", "视频目标: 记录现场互动",
                        "关键事件与流程: 来宾入场、互动体验、主舞台活动、合影收束")))
                    page.locator("#formal-brief-upload-button").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("待确认")
                    projects.append(project_id)
                first, second = projects
                def enter(project_id):
                    page.goto(f"{server.base_url}/projects/{project_id}/brief")
                    expect(page.locator('[data-edit-formal-fact="风格调性"]').locator("..")).to_be_visible()
                def edit(field, value=None):
                    form = page.locator(f'[data-inline-fact="{field}"]')
                    if not form.count():
                        page.locator(f'[data-edit-formal-fact="{field}"]').click()
                    control = form.locator("[data-inline-fact-value]")
                    control.focus()
                    if value is not None:
                        control.fill(value.removeprefix(field + ": "))
                    return control
                enter(first)
                edit("风格调性", "风格调性: A项目尚未提交的高级表达")
                edit("目标时长", "目标时长: 90秒")
                edit("风格调性")
                expect(page.locator('[data-inline-fact="风格调性"] [data-inline-fact-value]')).to_have_value("A项目尚未提交的高级表达")
                enter(second)
                second_style = edit("风格调性")
                self.assertNotIn("A项目", second_style.input_value())
                second_style.fill("B项目独立草稿")
                enter(first)
                edit("目标时长")
                expect(page.locator('[data-inline-fact="目标时长"] [data-inline-fact-value]')).to_have_value("90秒")
                page.reload()
                edit("风格调性")
                expect(page.locator('[data-inline-fact="风格调性"] [data-inline-fact-value]')).to_have_value("A项目尚未提交的高级表达")
                edit("目标时长")
                def fail_once(route):
                    submissions.append(json.loads(route.request.post_data or "{}"))
                    if len(submissions) == 1:
                        route.abort("failed")
                    else:
                        route.continue_()
                page.route("**/brief/field?*", fail_once)
                duration_form = page.locator('[data-inline-fact="目标时长"]')
                duration_form.get_by_role("button", name="保存此字段", exact=True).click()
                expect(duration_form.locator('[role="status"]')).to_contain_text("操作没有完成")
                expect(duration_form.locator("[data-inline-fact-value]")).to_have_value("90秒")
                with page.expect_response(lambda response: response.request.method == "POST" and "/brief/field?" in response.url) as retried:
                    duration_form.get_by_role("button", name="保存此字段", exact=True).click()
                self.assertEqual(retried.value.status, 200)
                expect(page.locator('[data-inline-fact="目标时长"]')).to_have_count(0)
                current_duration = page.locator("#formal-fact-list .ui00-fact-item").filter(has=page.get_by_text("目标时长", exact=True))
                expect(current_duration).to_have_count(1)
                expect(current_duration).to_contain_text("90秒")
                expect(current_duration).not_to_contain_text("60秒")
                for selector in ("#brief-source-panel", ".brief-source-evidence", ".brief-fact-history"):
                    details = page.locator(selector)
                    if details.get_attribute("open") is None:
                        details.locator(":scope > summary").click()
                duration_history = page.locator(".brief-fact-history .ui00-fact-item").filter(has=page.get_by_text("目标时长", exact=True))
                expect(duration_history).to_have_count(1)
                expect(duration_history).to_contain_text("60秒")
                self.assertTrue(duration_history.locator(":scope > small").last.text_content().strip())
                current_sources = current_duration.locator("details li").all_text_contents()
                self.assertTrue(current_sources)
                self.assertTrue(all(source.strip() for source in current_sources))
                edit("风格调性")
                expect(page.locator('[data-inline-fact="风格调性"] [data-inline-fact-value]')).to_have_value("A项目尚未提交的高级表达")
                self.assertEqual(len(submissions), 2)
                for body in submissions:
                    self.assertEqual(body["field_label"], "目标时长")
                    self.assertEqual(body["value"], "90秒")
                    self.assertNotIn("A项目尚未提交", json.dumps(body, ensure_ascii=False))
                enter(second)
                edit("风格调性")
                expect(page.locator('[data-inline-fact="风格调性"] [data-inline-fact-value]')).to_have_value("B项目独立草稿")
                self.assertEqual(errors, [])
                (root / "recovery-evidence.json").write_text(json.dumps({
                    "projects": projects, "field_roundtrip": True, "reload": True,
                    "transport_failure_then_real_retry_status": retried.value.status,
                    "only_current_field_submitted": True, "other_project_draft_preserved": True,
                    "page_errors": errors}, ensure_ascii=False, indent=2))
            except Exception:
                page.screenshot(path=str(root / "failure.png"), full_page=True)
                (root / "failure.txt").write_text(page.locator("body").inner_text())
                raise
            finally:
                context.tracing.stop(path=str(root / "trace.zip"))
                context.close()
                browser.close()
