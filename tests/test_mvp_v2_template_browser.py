from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
import tempfile
from urllib.parse import unquote, urlparse
from unittest import TestCase, mock

from playwright.sync_api import expect, sync_playwright

from tests import test_mvp_v2_formal_browser_journey as journey
from mvp_v2.script_type_adapter import SCRIPT_TYPE_METADATA


class TemplateBrowserTests(TestCase):
    def test_six_categories_import_bind_trial_and_restart(self):
        with tempfile.TemporaryDirectory(prefix="mplus-template-browser-") as temporary:
            root = Path(os.environ.get("MPLUS_TEMPLATE_BROWSER_ROOT", temporary))
            root.mkdir(parents=True, exist_ok=True)
            server = journey._FixtureServer(root)
            helper = Path(__file__).parent / "support/template_console_fixture_server.py"
            with mock.patch.object(journey, "SERVER_SCRIPT", helper):
                server.start()
                try:
                    with sync_playwright() as playwright:
                        browser = playwright.chromium.launch(headless=True)
                        context = browser.new_context(viewport={"width": 1440, "height": 1000})
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(server.base_url + "/projects")
                        page.wait_for_load_state("networkidle")
                        page.locator("#formal-owner-confirm").check()
                        page.locator("#formal-owner-enable").click()
                        expect(page.locator("#formal-owner-onboarding")).to_be_hidden()
                        projects = []
                        for metadata in SCRIPT_TYPE_METADATA:
                            page.goto(server.base_url + "/projects/new")
                            expect(page.locator("#formal-project-category")).to_be_hidden()
                            page.locator("#formal-project-name").fill("匿名" + metadata.display_name)
                            with page.expect_response(lambda response: response.url.endswith("/projects") and response.request.method == "POST") as created:
                                page.locator("#formal-project-create-submit").click()
                            self.assertEqual(created.value.status, 200, created.value.text())
                            self.assertNotIn("category_id", created.value.request.post_data_json)
                            self.assertIsNone(created.value.json()["category_id"])
                            journey._open_new_project_brief(page)
                            project_id = unquote(urlparse(page.url).path.split("/")[2])
                            expect(page.locator("#formal-template-current")).to_have_text("确认项目类型后选择模板")
                            expect(page.locator("#formal-template-picker")).to_be_hidden()
                            expect(page.locator("#formal-template-trial")).to_be_disabled()
                            # Type selection now supplements the Brief; creation has no category default.
                            page.locator("#formal-brief-category-edit").click()
                            expect(page.locator("#formal-project-category")).to_be_enabled()
                            expect(page.locator("#formal-project-category option")).to_have_text(
                                ["请选择类型", *[m.display_name for m in SCRIPT_TYPE_METADATA]],
                            )
                            if metadata.category_id == "launch":
                                # Preserve the original four-format Brief and real conflict confirmation.
                                files = [root / "fixtures" / name for name in ("01-brief.pdf", "02-brief.docx", "03-brief-no-images.xlsx", "04-brief.pptx")]
                                page.locator("#formal-brief-files").set_input_files([str(path) for path in files])
                            else:
                                page.locator("#formal-brief-text").fill("\n".join((
                                    "项目名称：匿名" + metadata.display_name,
                                    "目标时长：60s", "风格调性：克制真实", "视频目标：记录活动流程",
                                    "必须出现：品牌标识与活动主要环节", "禁止：无依据夸张",
                                )))
                            page.locator("#formal-project-category").select_option(metadata.category_id)
                            page.locator("#formal-brief-category-apply").click()
                            expect(page.locator("#formal-brief-text")).to_have_value(re.compile("脚本类型: " + re.escape(metadata.display_name)))
                            page.locator("#formal-brief-upload-button").click()
                            expect(page.locator("#formal-standard-card-state")).to_have_text("待确认", timeout=30000)
                            for _ in range(5):
                                button = page.locator("#formal-conflict-list button", has_text="确认选择").first
                                if not button.count():
                                    break
                                button.click()
                                button.wait_for(state="detached")
                            expect(page.locator("#formal-brief-confirm")).to_be_enabled()
                            page.locator("#formal-brief-confirm").click()
                            expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                            expect(page.locator("#formal-template-current")).to_contain_text(metadata.display_name + "基础模板")
                            expect(page.locator("#formal-template-preview")).to_contain_text("8 镜")
                            page.locator("#formal-template-picker > summary").click()
                            expect(page.locator("#formal-template-trial")).to_be_enabled()
                            # Fix the displayed default explicitly after its category is confirmed.
                            expect(page.locator("#formal-template-binding")).to_be_visible()
                            page.locator("#formal-template-confirm").check()
                            with page.expect_response(lambda response: response.url.endswith("/templates/select")) as fixed_default:
                                page.locator("#formal-template-apply").click()
                            fixed = fixed_default.value.json()
                            self.assertEqual(fixed_default.value.status, 200, fixed)
                            self.assertTrue(fixed["is_bound"])
                            expect(page.locator("#formal-template-binding")).to_be_hidden()
                            projects.append({"id": project_id, "category": metadata.category_id, "bound": fixed["selected_ref"]})
                        project_id = projects[0]["id"]
                        page.goto(f"{server.base_url}/projects/{project_id}/brief?panel=templates")
                        expect(page.locator("#formal-template-picker")).to_have_attribute("open", "")
                        page.locator(".template-import > summary").click()
                        invalid = root / "invalid.xlsx"
                        invalid.write_bytes(b"not-an-excel")
                        page.locator("#formal-template-file").set_input_files(str(invalid))
                        page.locator("#formal-template-import").click()
                        expect(page.locator("#formal-template-notice")).to_contain_text("导入失败")
                        expect(page.locator("#formal-template-import")).to_be_enabled()
                        template = root / "fixtures/templates/new/匿名结构模板.xlsx"
                        page.locator("#formal-template-file").set_input_files(str(template))
                        with page.expect_response(lambda response: response.url.endswith("/templates/import") and response.request.method == "POST") as imported:
                            page.locator("#formal-template-import").click()
                        payload = imported.value.json()
                        self.assertEqual(imported.value.status, 200, payload)
                        before_version = payload["project_version"]
                        expect(page.locator("#formal-template-select")).to_have_value(payload["preview"]["ref"])
                        expect(page.locator("#formal-template-current")).to_contain_text("发布会基础模板")
                        expect(page.locator("#formal-template-preview")).to_contain_text("10 镜")
                        expect(page.locator("#formal-template-apply")).to_be_disabled()
                        page.locator("#formal-template-confirm").check()
                        with page.expect_response(lambda response: response.url.endswith("/templates/select")) as selected:
                            page.locator("#formal-template-apply").click()
                        selected_payload = selected.value.json()
                        self.assertEqual(selected.value.status, 200, selected_payload)
                        self.assertEqual(selected_payload["project_version"], before_version + 1)
                        self.assertEqual(selected_payload["selected_ref"], payload["preview"]["ref"])
                        expect(page.locator("#formal-template-current")).to_contain_text("匿名结构模板 · v1")
                        page.reload()
                        expect(page.locator("#formal-template-current")).to_have_text("匿名结构模板 · v1")
                        # The original multi-document Brief was confirmed before binding its category.
                        expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                        expect(page.locator("#formal-template-trial")).to_be_enabled()
                        with sqlite3.connect(root / "workspace.sqlite3") as connection:
                            version_before_trial = connection.execute("SELECT lock_version FROM projects WHERE project_id = ?", (project_id,)).fetchone()[0]
                            runs_before_trial = connection.execute("SELECT COUNT(*) FROM project_runs").fetchone()[0]
                        with page.expect_response(lambda response: response.url.endswith("/templates/trial"), timeout=60000) as trial:
                            page.locator("#formal-template-trial").click()
                        result = trial.value.json()
                        self.assertEqual(trial.value.status, 200, result)
                        self.assertEqual(result["plan"]["status"], "trial_completed")
                        expect(page.locator("#formal-template-trial-result")).to_be_visible()
                        self.assertEqual(page.locator("#formal-template-trial-result tbody tr").count(), len(result["plan"]["shots"]))
                        page.screenshot(path=str(root / "template-desktop.png"), full_page=True)
                        server.restart()
                        page.goto(f"{server.base_url}/projects/{project_id}/plan")
                        expect(page.locator("#formal-plan-template")).to_have_text("匿名结构模板 · v1")
                        page.locator("#formal-plan-template-link").click()
                        page.wait_for_url("**/brief?panel=templates")
                        expect(page.locator("#formal-template-picker")).to_have_attribute("open", "")
                        expect(page.locator("#formal-template-current")).to_have_text("匿名结构模板 · v1")
                        for width in (375, 1024):
                            page.set_viewport_size({"width": width, "height": 900})
                            page.screenshot(path=str(root / f"template-{width}.png"), full_page=True)
                            page.locator(".ui00-route-card").screenshot(path=str(root / f"template-panel-{width}.png"))
                        self.assertEqual(errors, [])
                        with sqlite3.connect(root / "workspace.sqlite3") as connection:
                            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_versions").fetchone()[0], 0)
                            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_runs").fetchone()[0], runs_before_trial)
                            self.assertEqual(connection.execute("SELECT lock_version FROM projects WHERE project_id = ?", (project_id,)).fetchone()[0], version_before_trial)
                        self.assertFalse((root / "provider-calls.jsonl").exists())
                        (root / "summary.json").write_text(json.dumps({"projects": projects, "binding": selected_payload["selected_ref"], "trial_status": result["plan"]["status"], "trial_shots": len(result["plan"]["shots"]), "page_errors": errors, "script_versions": 0, "provider_calls": 0}, ensure_ascii=False, indent=2))
                        browser.close()
                finally:
                    server.stop()
