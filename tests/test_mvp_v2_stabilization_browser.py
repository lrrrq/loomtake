"""Continuous creative input through the production runtime to reviewed output."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse

from playwright.sync_api import expect, sync_playwright
from tests.test_mvp_v2_plan_recovery_browser import _StabilizationServer, read_rows
from tests import test_mvp_v2_formal_retry_browser as delivery


class StabilizationBrowserTests(unittest.TestCase):
    def test_creative_supplements_protection_images_versions_review_and_delivery(self):
        configured = os.environ.get("MPLUS_STABILIZATION_BROWSER_ROOT")
        if configured:
            root = Path(configured).resolve()
        else:
            temporary = tempfile.TemporaryDirectory(prefix="mplus-stabilization-browser-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
        root.mkdir(parents=True, exist_ok=True)
        server = _StabilizationServer(root)
        server.start()
        self.addCleanup(server.stop)
        evidence, errors = [], []
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            page.set_default_timeout(20000)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def capture(page, name, details=None):
                page.screenshot(path=str(root / (name + ".png")), full_page=True)
                evidence.append({"step": name, "path": urlparse(page.url).path, "details": details or {}})
                (root / "journey-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

            try:
                page.goto(server.base_url + "/projects")
                page.locator("#formal-owner-confirm").check()
                page.locator("#formal-owner-enable").click()
                expect(page.locator("#formal-owner-onboarding")).to_be_hidden()
                page.locator("#formal-project-new").click()
                page.locator("#formal-project-name").fill("匿名连续创作交付")
                page.locator("#formal-project-create-submit").click()
                page.wait_for_url("**/projects/*/brief")
                project_id = urlparse(page.url).path.split("/")[2]
                brief_url = page.url
                page.locator("#formal-brief-text").fill("\n".join((
                    "项目名称: 匿名连续创作交付", "品牌或客户: 匿名团队",
                    "脚本类型: 发布会", "目标时长: 45秒", "风格调性: 克制",
                    "视频目标: 记录现场互动", "使用场景: 现场活动", "禁止或不能出现的内容: 不得虚构事实",
                    "关键事件与流程: 来宾入场、互动体验、主舞台活动、合影收束")))
                page.locator("#formal-brief-upload-button").click()
                expect(page.locator("#formal-standard-card-state")).to_have_text("待确认")
                page.locator("#formal-brief-confirm").click()
                expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                page.locator('#formal-project-navigation a[data-route="script"]').click()
                page.locator("#creative-first-confirmed-start").click()
                expect(page.locator("[data-first-block][data-first-field='画面描述']")).to_have_count(5)
                expect(page.locator("#creative-first-status")).to_contain_text("初稿已保存")
                original = page.locator("[data-first-block][data-first-field='画面描述']").evaluate_all("els => els.map(e => e.value)")
                first_sound = page.locator("[data-first-block][data-first-field='声音']").first
                first_sound.fill("用户保留的现场声")
                first_sound.press("Tab")
                expect(page.locator("#creative-first-status")).to_contain_text("初稿已保存")
                page.locator("[data-first-protect]").first.check()
                for field, value in (("风格调性", "高级"), ("目标时长", "60秒")):
                    page.goto(brief_url)
                    page.locator(f'[data-edit-formal-fact="{field}"]').click()
                    page.locator("#formal-brief-text").fill(field + ": " + value)
                    page.locator("#formal-brief-upload-button").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("待确认")
                    conflict = page.locator("#formal-conflict-list .ui00-conflict-item").filter(has=page.locator("select"))
                    expected_choices = [value] if field == "风格调性" else [value, "高级"]
                    expect(conflict).to_have_count(len(expected_choices))
                    for index, choice in enumerate(expected_choices):
                        target = conflict.filter(has=page.locator("option").filter(has_text=choice))
                        target.locator("select").select_option(label=choice)
                        target.get_by_role("button", name="确认选择", exact=True).click()
                        expect(conflict).to_have_count(len(expected_choices) - index - 1)
                    expect(page.locator("#formal-conflict-list select")).to_have_count(0)
                    page.locator("#formal-brief-confirm").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                    page.locator('#formal-project-navigation a[data-route="script"]').click()
                    page.locator('[data-first-answer-continue]').click()
                    expect(page.locator("#creative-first-apply")).to_be_enabled()
                    self.assertEqual(page.locator("[data-first-block][data-first-field='画面描述']").evaluate_all("els => els.map(e => e.value)"), original)
                expect(page.locator("#creative-first-proposal")).to_contain_text("克制高级的音乐")
                # Explicitly unselected groups are declined, so later previews
                # must preserve them just like protected manual fields.
                page.locator("[data-first-retain]").last.uncheck()
                page.locator("#creative-first-apply").click()
                expect(page.locator("#creative-first-proposal")).to_be_hidden()
                expect(first_sound).to_have_value("用户保留的现场声")
                expect(page.locator("[data-first-block][data-first-field='声音']").last).to_have_value("现场氛围与音乐推进")
                page.reload()
                expect(first_sound).to_have_value("用户保留的现场声")
                expect(page.locator("[data-first-block][data-first-field='声音']").last).to_have_value("现场氛围与音乐推进")
                capture(page, "01-continuous-creative", {"project_id": project_id, "blocks": 5, "partial_adoption": True, "manual_protection": True})
                page.locator("#creative-first-intake-link").click()
                expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                page.goto(f"{server.base_url}/projects/{project_id}/script?panel=images&aux=materials")
                page.locator("[data-load-default-images]").click()
                panel = page.locator("[data-default-image-panel]")
                candidates = panel.locator("[data-default-library-candidate]")
                candidates.first.wait_for()
                for candidate in candidates.all():
                    candidate.check()
                self.assertGreater(candidates.count(), 3)
                panel.locator("[data-propose-default-images]").click()
                panel.locator("[data-confirm-default-images]").click()
                expect(page.locator("#formal-image-state")).to_have_text("全部可用")
                page.goto(f"{server.base_url}/projects/{project_id}/script")
                expect(page.locator("[data-first-block][data-first-field='画面描述']")).to_have_count(5)
                page.locator("#creative-first-plan-link").click()
                page.wait_for_url("**/script?panel=structure&creative=1")
                page.locator("#formal-plan-create").click()
                expect(page.locator("#formal-plan-confirmation")).to_be_visible()
                shot_count = page.locator("#formal-plan-shots .ui00-plan-shot").count()
                self.assertGreater(shot_count, 5)
                page.locator("#formal-plan-confirm-check").check()
                with page.expect_response(lambda r: r.request.method == "POST" and "/plans/" in r.url and "/confirm" in r.url, timeout=60000) as generated:
                    page.locator("#formal-plan-confirm").click()
                self.assertEqual(generated.value.status, 200, generated.value.text())
                result = generated.value.json()
                capture(page, "02-creative-formal-version", {"version_id": result["version_id"], "shot_count": shot_count})
                helper = delivery.FormalSafeRetryBrowserTests("runTest")
                helper._complete_delivery(page, root, root, server, project_id, shot_count, capture)
                original_excel = hashlib.sha256((root / "安全重试交付.xlsx").read_bytes()).hexdigest()
                server.restart()
                page.reload()
                expect(page.locator("#formal-review-state")).to_have_text("整片已确认")
                with page.expect_download() as downloaded:
                    page.locator("#formal-delivery-excel").click()
                restored = root / "重启回读交付.xlsx"
                downloaded.value.save_as(restored)
                self.assertEqual(hashlib.sha256(restored.read_bytes()).hexdigest(), original_excel)
                self.assertGreaterEqual(len(read_rows(root, "SELECT version_id FROM project_versions WHERE project_id=?", (project_id,))), 2)
                capture(page, "05-restart-readback", {"same_workbook_sha256": original_excel})
                self.assertEqual(errors, [])
            except Exception:
                capture(page, "failure", {"page_errors": errors})
                (root / "failure.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
                raise
            finally:
                context.tracing.stop(path=str(root / "journey-trace.zip"))
                context.close()
                browser.close()
