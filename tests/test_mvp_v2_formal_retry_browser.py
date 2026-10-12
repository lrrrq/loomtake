"""A real browser must turn a safe retry into a readable reviewed deliverable."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote, urlparse

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_formal_browser_journey import (
    _FixtureServer, _approve_remaining_review, _final_review_note, _open_new_project_brief,
    _show_all_image_shots, _use_image_and_continue, _save_unified_images,
)


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "tests" / "support" / "formal_retry_fixture_server.py"


class _RetryServer(_FixtureServer):
    def start(self):
        with patch("tests.test_mvp_v2_formal_browser_journey.SERVER_SCRIPT", SERVER):
            super().start()


def _rows(root: Path, sql: str, parameters=()):
    database = root / "data" / "workspace.sqlite3"
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


def _analysis_inventory(root: Path):
    inventory = []
    for row in _rows(root, "SELECT artifact_sha256 FROM project_image_analysis_versions ORDER BY artifact_sha256"):
        digest = row["artifact_sha256"]
        path = root / "data" / "artifacts" / "project-image-analysis" / digest[:2] / digest
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise AssertionError("冻结图片理解 SHA 已变化")
        inventory.append({"expected_sha256": digest, "actual_sha256": actual})
    return inventory


class FormalSafeRetryBrowserTests(unittest.TestCase):
    @unittest.skipIf(os.environ.get("MPLUS_FORMAL_RETRY_RESUME"), "继续已有项目时不重复生成")
    def test_transient_generation_failure_safe_retry_finishes_and_delivers_real_excel(self):
        configured_root = os.environ.get("MPLUS_FORMAL_RETRY_BROWSER_ROOT")
        if configured_root:
            root = Path(configured_root).resolve()
        else:
            temporary = tempfile.TemporaryDirectory(prefix="mplus-safe-retry-browser-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name).resolve()
        root.mkdir(parents=True, exist_ok=True)
        self.assertFalse(any(root.iterdir()), "拒绝覆盖已有浏览器证据")
        server = _RetryServer(root)
        server.start()
        steps, page_errors = [], []
        project_id = ""

        def capture(page, name, details=None):
            page.screenshot(path=str(root / f"{name}.png"), full_page=True)
            (root / f"{name}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
            steps.append({"step": name, "url": page.url, "details": details or {}})
            (root / "browser-steps.json").write_text(json.dumps(steps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = context.new_page()
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                try:
                    page.goto(server.base_url + "/projects")
                    page.locator("#formal-owner-confirm").check()
                    page.locator("#formal-owner-enable").click()
                    page.locator("#formal-project-new").click()
                    page.locator("#formal-project-name").fill("匿名安全重试交付项目")
                    page.locator("#formal-project-create-submit").click()
                    _open_new_project_brief(page)
                    project_id = urlparse(page.url).path.split("/")[2]
                    page.locator("#formal-brief-text").fill("\n".join((
                        "项目名称: 匿名安全重试交付项目", "品牌或客户: 匿名品牌", "脚本类型: 发布会",
                        "目标时长: 60秒", "风格调性: 克制、真实、清晰", "视频目标: 嘉宾发布",
                        "必须出现的内容: 产品演示", "关键事件与流程: 嘉宾发布、说明发布重点、产品演示、品牌收束",
                        "禁止或不能出现的内容: 不得虚构事实", "使用场景: 发布会现场",
                    )))
                    page.locator("#formal-brief-upload-button").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("待确认")
                    page.locator("#formal-brief-confirm").click()
                    expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images&aux=materials")
                    page.locator("[data-load-default-images]").click()
                    panel = page.locator("[data-default-image-panel]")
                    candidates = panel.locator("[data-default-library-candidate]")
                    candidates.first.wait_for()
                    for candidate in candidates.all():
                        candidate.check()
                    self.assertGreater(candidates.count(), 3)
                    with page.expect_response(lambda response: response.request.method == "POST" and "/images/default-proposals?" in response.url) as proposed:
                        panel.locator("[data-propose-default-images]").click()
                    self.assertEqual(proposed.value.status, 200, proposed.value.text())
                    panel.locator("[data-confirm-default-images]").click()
                    expect(page.locator("#formal-image-state")).to_have_text("全部可用")
                    page.locator("#formal-image-to-plan").click()
                    page.locator("#formal-plan-create").click()
                    page.locator("#formal-plan-confirmation").wait_for()
                    shot_count = page.locator("#formal-plan-shots .ui00-plan-shot").count()
                    self.assertGreater(shot_count, 3)
                    page.locator("#formal-plan-confirm-check").check()
                    with page.expect_response(lambda response: response.request.method == "POST" and "/plans/" in response.url and "/confirm" in response.url, timeout=60_000) as initial:
                        page.locator("#formal-plan-confirm").click()
                    self.assertEqual(initial.value.status, 409, initial.value.text())
                    self.assertTrue((root / "transient-provider-failure.json").is_file())
                    self.assertEqual(_rows(root, "SELECT COUNT(*) AS count FROM project_versions WHERE project_id=?", (project_id,))[0]["count"], 0)
                    failed_task_id = initial.value.url.split("/plans/", 1)[1].split("/", 1)[0]
                    failed_task_before = _rows(root, "SELECT task_id,status,lock_version,error_code,updated_at FROM tasks WHERE task_id=?", (failed_task_id,))
                    capture(page, "01-technical-failure", initial.value.json())

                    # A business-language instruction is submitted through the
                    # visible assistant input, then the user confirms its proposal.
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/assistant")
                    expect(page.locator("#agent-session-status")).to_have_text("会话已连接")
                    page.locator("#agent-message").fill("重试刚才失败的生成")
                    page.locator("#agent-message-submit").click()
                    confirm = page.locator("[data-agent-confirm]").last
                    confirm.wait_for()
                    capture(page, "02-safe-retry-proposal")
                    with page.expect_response(lambda response: response.request.method == "POST" and "/api/agent/tasks/" in response.url and response.url.endswith("/confirm"), timeout=60_000) as retried:
                        confirm.click()
                    self.assertEqual(retried.value.status, 200, retried.value.text())
                    receipt = retried.value.json()
                    self.assertEqual(receipt["response"]["status"], "completed", retried.value.text())
                    expect(page.locator("#agent-feed")).to_contain_text("重试已完成")
                    capture(page, "03-safe-retry-receipt", receipt)
                    terminals = _rows(root, """SELECT a.attempt_no, a.origin, c.stage_id, c.status,
                        c.output_ref, c.output_sha256, c.error_code
                        FROM task_attempts a JOIN stage_checkpoints c ON c.attempt_id=a.attempt_id
                        WHERE a.project_id=? AND a.origin='retry' ORDER BY a.attempt_no, c.created_at""", (project_id,))
                    (root / "retry-terminal-evidence.json").write_text(json.dumps(terminals, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    self.assertTrue(terminals, "重试必须留真实执行终态")
                    self.assertEqual(terminals[-1]["status"], "completed", json.dumps({"terminals": terminals, "receipt": receipt}, ensure_ascii=False))
                    self.assertEqual(receipt["response"]["execution"]["terminal_status"], "succeeded")
                    self.assertTrue(terminals[-1]["output_ref"])
                    self.assertEqual(len(terminals[-1]["output_sha256"]), 64)
                    self.assertEqual(_rows(root, "SELECT task_id,status,lock_version,error_code,updated_at FROM tasks WHERE task_id=?", (failed_task_id,)), failed_task_before)
                    self.assertEqual(_rows(root, "SELECT COUNT(*) AS count FROM project_versions WHERE project_id=?", (project_id,))[0]["count"], 1)

                    self._complete_delivery(page, root, root, server, project_id, shot_count, capture)
                    self._verify_views(page, root, root, project_id, shot_count, capture)
                    (root / "analysis-inventory.json").write_text(json.dumps(_analysis_inventory(root), indent=2) + "\n", encoding="utf-8")
                    self.assertEqual(page_errors, [])
                except Exception:
                    capture(page, "failure-state", {"page_errors": page_errors})
                    raise
                finally:
                    context.tracing.stop(path=str(root / "browser-trace.zip"))
                    context.close()
                    browser.close()
        finally:
            server.stop()

    def _complete_delivery(self, page, root, evidence_root, server, project_id, shot_count, capture):
        page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
        _show_all_image_shots(page)
        shots = page.locator("#formal-image-shot-grid [data-image-shot]")
        expect(shots).to_have_count(shot_count)
        for index in range(shot_count):
            select = page.locator(f"[data-image-shot-select='{index}']")
            options = select.locator('option:not([disabled]):not([value=""])')
            self.assertGreater(options.count(), 0)
            selected = options.nth(min(index, options.count() - 1)).get_attribute("value")
            select.select_option(selected)
        for index in range(shot_count):
            _use_image_and_continue(page, index)
        _save_unified_images(page)
        page.locator('nav[aria-label="项目功能"] a[data-route="review"]').click()
        page.locator("#formal-review-start").click()
        expect(page.locator("#formal-review-state")).to_have_text("审核中")
        _approve_remaining_review(page, root / "data" / "workspace.sqlite3")
        expect(page.locator("#formal-review-state")).to_have_text("待整片确认")
        _final_review_note(page).fill("已核对重试后的真实脚本、逐镜图片和相邻复核。")
        page.locator("#formal-review-final-submit").click()
        expect(page.locator("#formal-review-state")).to_have_text("整片已确认")
        delivery = evidence_root / "安全重试交付.xlsx"
        with page.expect_download() as download:
            page.locator("#formal-delivery-excel").click()
        download.value.save_as(delivery)
        workbook = load_workbook(delivery)
        try:
            self.assertEqual(len(workbook["脚本"]._images), shot_count)
            self.assertEqual(workbook["相邻镜头复核"].max_row - 1, shot_count - 1)
            self.assertIn("审核记录", workbook.sheetnames)
            rows = [row for row in workbook["脚本"].iter_rows(min_row=7, values_only=True) if row[1] is not None]
            self.assertEqual(len(rows), shot_count)
            for sheet in workbook:
                for row in sheet:
                    for cell in row:
                        value = str(cell.value or "")
                        self.assertFalse(value.startswith("file://"), f"{sheet.title}!{cell.coordinate}")
                        self.assertNotIn(str(root), value, f"{sheet.title}!{cell.coordinate}")
                        if cell.hyperlink is not None:
                            target = str(cell.hyperlink.target or "")
                            self.assertFalse(target.startswith("file://"), f"{sheet.title}!{cell.coordinate}")
                            self.assertNotIn(str(root), target, f"{sheet.title}!{cell.coordinate}")
        finally:
            workbook.close()
        capture(page, "04-reviewed-delivery", {
            "file": delivery.name, "sha256": hashlib.sha256(delivery.read_bytes()).hexdigest(),
            "bytes": delivery.stat().st_size, "shot_count": shot_count,
            "actual_embedded_images": shot_count, "adjacent_reviews": shot_count - 1,
            "internal_path_or_file_uri_cells_and_links": 0,
        })

    def _verify_views(self, page, root, evidence_root, project_id, shot_count, capture):
        provider_log = root / "fixture-preparation" / "provider-calls.jsonl"
        provider_calls_before_views = provider_log.read_bytes()
        versions_before_views = _rows(root, "SELECT version_id,run_id FROM project_versions WHERE project_id=? ORDER BY created_at,version_id", (project_id,))
        measurements = []
        for width, height in ((375, 812), (1024, 900), (1440, 1000)):
            page.set_viewport_size({"width": width, "height": height})
            button = page.locator("#formal-delivery-excel")
            button.scroll_into_view_if_needed()
            expect(button).to_be_enabled()
            expect(button).to_be_in_viewport()
            # Read-only visible DOM geometry; no state injection,
            # JavaScript business action or HTTP shortcut is used.
            geometry = page.locator("html").evaluate("element => ({scrollWidth: element.scrollWidth, clientWidth: element.clientWidth})")
            self.assertLessEqual(geometry["scrollWidth"], geometry["clientWidth"])
            measurements.append({"width": width, **geometry, "download_visible": True})
            capture(page, f"05-delivery-{width}", measurements[-1])
        (evidence_root / "viewport-checks.json").write_text(json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator('nav[aria-label="项目功能"] a[data-route="script"]').click()
        page.wait_for_load_state("networkidle")
        outline = page.locator("[data-creative-shot]")
        expect(outline).to_have_count(shot_count)
        third = outline.nth(2)
        third_id = third.get_attribute("data-creative-shot")
        third.click()
        expect(page.locator(f'[data-creative-shot="{third_id}"]')).to_have_attribute("aria-current", "true")
        script_url = page.url
        page.locator('[data-creative-panel="images"]').click()
        expect(page.locator('[data-creative-panel="images"]')).to_have_attribute("aria-selected", "true")
        image_url = page.url
        page.go_back()
        page.wait_for_url(script_url)
        expect(page.locator(f'[data-creative-shot="{third_id}"]')).to_have_attribute("aria-current", "true")
        page.go_forward()
        page.wait_for_url(image_url)
        expect(page.locator('[data-creative-panel="images"]')).to_have_attribute("aria-selected", "true")
        expect(page.locator(f'[data-creative-shot="{third_id}"]')).to_have_attribute("aria-current", "true")
        self.assertIn(f"/projects/{quote(project_id)}/script", page.url)
        capture(page, "06-history-project-and-shot", {"project_id": project_id, "shot_id": third_id, "shot_number": 3, "back_forward_preserved": True})
        self.assertEqual(provider_log.read_bytes(), provider_calls_before_views)
        self.assertEqual(_rows(root, "SELECT version_id,run_id FROM project_versions WHERE project_id=? ORDER BY created_at,version_id", (project_id,)), versions_before_views)

    @unittest.skipUnless(os.environ.get("MPLUS_FORMAL_RETRY_RESUME"), "仅用于从已验收的重试版本继续交付")
    def test_resume_same_project_without_another_generation(self):
        """Retain the successful retry and finish only the requested browser tail."""
        root = Path(os.environ["MPLUS_FORMAL_RETRY_BROWSER_ROOT"]).resolve()
        evidence_root = Path(os.environ["MPLUS_FORMAL_RETRY_TAIL_OUT"]).resolve()
        evidence_root.mkdir(parents=True, exist_ok=True)
        self.assertFalse(any(evidence_root.iterdir()), "拒绝覆盖已有尾段证据")
        original_steps = json.loads((root / "browser-steps.json").read_text(encoding="utf-8"))
        receipt = next(step["details"] for step in original_steps if step["step"] == "03-safe-retry-receipt")
        self.assertEqual(receipt["response"]["status"], "completed")
        project_id = receipt["task"]["project_id"]
        original_task = _rows(root, "SELECT status FROM tasks WHERE task_id=?", (receipt["response"]["task_id"],))
        self.assertEqual(original_task[0]["status"], "blocked")
        provider_log = root / "fixture-preparation" / "provider-calls.jsonl"
        provider_before = provider_log.read_bytes()
        analysis_before = _analysis_inventory(root)
        self.assertGreater(len(analysis_before), 3)
        server = _RetryServer(root)
        server.start()
        steps, errors = [], []

        def capture(page, name, details=None):
            page.screenshot(path=str(evidence_root / f"{name}.png"), full_page=True)
            (evidence_root / f"{name}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
            steps.append({"step": name, "url": page.url, "details": details or {}})
            (evidence_root / "browser-steps.json").write_text(json.dumps(steps, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        try:
            self.assertEqual(_analysis_inventory(root), analysis_before)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                context.tracing.start(screenshots=True, snapshots=True, sources=True)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                try:
                    page.goto(server.base_url + "/projects")
                    page.locator("#formal-owner-confirm").check()
                    page.locator("#formal-owner-enable").click()
                    page.locator(f'a[href="/projects/{project_id}/overview"]').first.wait_for()
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script")
                    page.locator("[data-creative-shot]").first.wait_for()
                    shot_count = page.locator("[data-creative-shot]").count()
                    self.assertGreater(shot_count, 3)
                    if os.environ["MPLUS_FORMAL_RETRY_RESUME"] != "views":
                        self._complete_delivery(page, root, evidence_root, server, project_id, shot_count, capture)
                    else:
                        page.goto(f"{server.base_url}/projects/{quote(project_id)}/review")
                        expect(page.locator("#formal-review-state")).to_have_text("整片已确认")
                    self._verify_views(page, root, evidence_root, project_id, shot_count, capture)
                    self.assertEqual(provider_log.read_bytes(), provider_before)
                    self.assertEqual(errors, [])
                    (evidence_root / "resume-check.json").write_text(json.dumps({
                        "project_id": project_id, "shot_count": shot_count,
                        "original_successful_retry": str(root / "retry-terminal-evidence.json"),
                        "provider_log_unchanged": True,
                        "additional_generation_calls": 0,
                        "analysis_before_restart": analysis_before,
                        "analysis_after_restart": _analysis_inventory(root),
                    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                except Exception:
                    capture(page, "failure-state", {"page_errors": errors})
                    raise
                finally:
                    context.tracing.stop(path=str(evidence_root / "browser-trace.zip"))
                    context.close()
                    browser.close()
        finally:
            server.stop()
