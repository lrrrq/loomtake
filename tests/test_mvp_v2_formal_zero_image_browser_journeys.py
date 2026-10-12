from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from urllib.parse import parse_qs, quote, urlparse

from openpyxl import load_workbook
from PIL import Image
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
from pypdf import PdfReader

from tests.test_mvp_v2_formal_browser_journey import (
    _FixtureServer, _save_unified_images, _expand_image_controls, _use_image_and_continue, _open_new_project_brief,
    _approve_remaining_review, _final_review_note, _open_details,
    _show_script_table, _show_all_image_shots, _wait_for_creative_surface,
    _assert_review_workbook_matches_saved_version, _saved_version_workbook,
)
from tests.support.formal_control_evidence import FormalControlEvidenceRecorder


class FormalZeroImageBrowserJourneyTests(unittest.TestCase):
    def test_zero_image_category_defaults_reach_review_and_delivery(self):
        self._run_journey("category-defaults")

    def test_zero_image_explicit_text_draft_recovers_then_adds_images_and_delivers(self):
        self._run_journey("text-draft-recovery")

    def _run_journey(self, strategy: str) -> None:
        with tempfile.TemporaryDirectory(prefix=f"mplus-formal-{strategy}-") as temporary:
            root = Path(temporary)
            server = _FixtureServer(root)
            server.start()
            recorder = FormalControlEvidenceRecorder(
                journey_id=f"zero-image-{strategy}",
                database=root / "workspace.sqlite3",
            )
            project_id = ""
            script_shot_count = 0
            errors: list[str] = []
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000},
                        accept_downloads=True,
                    )
                    page = context.new_page()
                    page.on(
                        "console",
                        lambda message: errors.append(message.text)
                        if message.type == "error" and not (
                            strategy == "text-draft-recovery" and "/script/save?" in message.location.get("url", "")
                            and "409" in message.text) else None,
                    )
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    project_id = self._create_project(page, server.base_url, strategy)
                    self._confirm_zero_image_brief(page, server.base_url, root, project_id)
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/materials")
                    page.wait_for_function(
                        "document.querySelector('#formal-image-total')?.textContent !== '—'"
                    )
                    self.assertEqual(page.locator("#formal-image-total").inner_text(), "0")
                    self.assertIn("当前项目没有图片", page.locator("#page-materials").inner_text())
                    upload_cta = page.locator("[data-upload-project-image]")
                    text_only_cta = page.locator("[data-text-only-draft]")
                    self.assertEqual(upload_cta.get_attribute("type"), "button")
                    self.assertEqual(
                        text_only_cta.get_attribute("href"),
                        f"/projects/{quote(project_id)}/script?mode=text-only&panel=structure",
                    )
                    with page.expect_file_chooser() as chooser:
                        upload_cta.click()
                    chooser.value.set_files([])
                    self.assertEqual(urlparse(page.url).path, f"/projects/{project_id}/script")
                    self.assertEqual(parse_qs(urlparse(page.url).query)["aux"], ["materials"])
                    self.assertIsNotNone(page.locator("#formal-image-upload-files").get_attribute("multiple"))
                    page.locator("[data-text-only-draft]").click()
                    page.wait_for_url(f"**/projects/{quote(project_id)}/script?mode=text-only&panel=structure")
                    page.locator("#formal-plan-create").wait_for()
                    self.assertTrue(page.evaluate("state.formalPlanTextOnly"))
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/materials")
                    page.wait_for_function(
                        "document.querySelector('#formal-image-total')?.textContent === '0'"
                    )
                    recorder.capture(
                        page, "M29", keyboard_selector="[data-load-default-images]",
                        recovery_selector="#formal-image-gate",
                        terminal_result="零图项目素材 gate 明确阻断规划并给出三条受控路径",
                    )
                    recorder.capture(
                        page, "M30", keyboard_selector="[data-load-default-images]",
                        recovery_selector="#formal-image-grid",
                        terminal_result="零图状态显示上传、类别默认图、显式文字草稿三选一，未自动生成",
                    )

                    if strategy == "category-defaults":
                        adopted_count = self._adopt_defaults(
                            page, preplan=True, recorder=recorder,
                        )
                        self.assertGreater(adopted_count, 0)
                        page.locator("#formal-image-to-plan").click()
                        page.wait_for_url(f"**/projects/{quote(project_id)}/script?panel=structure")
                        page.locator("#formal-plan-create").click()
                    else:
                        page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=structure")
                        page.locator("#formal-plan-create").wait_for()
                        self.assertFalse(page.evaluate("state.formalPlanTextOnly"))
                        self.assertFalse((root / "provider-calls.jsonl").exists())
                        page.locator("#formal-plan-create").click()
                        page.wait_for_url("**/script?mode=text-only&panel=structure")
                        self.assertTrue(page.evaluate("state.formalPlanTextOnly"))
                        self.assertFalse((root / "provider-calls.jsonl").exists())
                        page.locator("#formal-plan-create").click()

                    page.locator("#formal-plan-confirmation").wait_for()
                    self.assertGreater(
                        page.locator("#formal-plan-shots .ui00-plan-shot").count(), 0
                    )
                    if strategy == "text-draft-recovery":
                        page.goto("about:blank")
                        server.restart()
                        page.goto(
                            f"{server.base_url}/projects/{quote(project_id)}/script?mode=text-only&panel=structure"
                        )
                        page.locator("#formal-plan-confirmation").wait_for()
                        self.assertIn(
                            "已恢复待确认的规划",
                            page.locator("#formal-plan-status-copy").inner_text(),
                        )

                    page.locator("#formal-plan-confirm-check").check()
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and "/plans/" in response.url and "/confirm" in response.url,
                        timeout=60_000,
                    ) as generation_response:
                        page.locator("#formal-plan-confirm").click()
                    self.assertEqual(
                        generation_response.value.status,
                        200,
                        generation_response.value.text(),
                    )
                    try:
                        page.locator("#formal-plan-result").wait_for(timeout=60_000)
                    except PlaywrightTimeoutError:
                        self.fail(json.dumps({
                            "response": generation_response.value.text(),
                            "state": page.locator("#formal-plan-state").inner_text(),
                            "copy": page.locator("#formal-plan-status-copy").inner_text(),
                            "server_log": server.log_path.read_text(
                                encoding="utf-8", errors="replace",
                            ),
                        }, ensure_ascii=False, indent=2))
                    rows = page.locator("#formal-plan-script-rows tbody tr")
                    script_shot_count = rows.count()
                    self.assertGreater(script_shot_count, 0)
                    page.locator("#formal-plan-to-script").click()
                    page.wait_for_url(f"**/projects/{quote(project_id)}/script")
                    page.locator("#formal-script-table .ui00-script-table").wait_for()
                    _show_script_table(page)
                    self.assertEqual(
                        page.locator("#formal-script-table tbody tr").count(),
                        script_shot_count,
                    )
                    if strategy == "text-draft-recovery":
                        self.assertFalse(
                            page.locator("#formal-script-review-workbook").is_disabled()
                        )
                        review_path = root / "text-script-review.xlsx"
                        _open_details(page, ".creative-more")
                        with page.expect_download() as review_download:
                            page.locator("#formal-script-review-workbook").click()
                        review_download.value.save_as(review_path)
                        _assert_review_workbook_matches_saved_version(
                            review_path, _saved_version_workbook(root, project_id), script_shot_count, 1,
                        )

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/materials")
                    if strategy == "text-draft-recovery":
                        page.wait_for_function(
                            "document.querySelector('#formal-image-total')?.textContent === '0'"
                        )
                        adopted_count = self._adopt_defaults(
                            page, preplan=False, recorder=recorder,
                        )
                        self.assertGreater(adopted_count, 0)
                        page.locator("[data-image-return]").click()
                        _wait_for_creative_surface(page, project_id, "images")
                    else:
                        page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                        _show_all_image_shots(page)
                    page.locator("#formal-image-shot-grid [data-image-shot]").first.wait_for()
                    _show_all_image_shots(page)
                    image_shots = page.locator("#formal-image-shot-grid [data-image-shot]")
                    self.assertEqual(image_shots.count(), script_shot_count)
                    if strategy == "text-draft-recovery":
                        first_select = page.locator("[data-image-shot-select='0']")
                        self.assertGreaterEqual(first_select.locator("option").count(), 2)
                        returned = next((item for item in self.last_default_adoption["adopted_selections"]
                                         if str(item["shot_no"]) == "1"), None)
                        self.assertIsNotNone(returned, json.dumps(self.last_default_adoption, ensure_ascii=False))
                        self.assertEqual(first_select.input_value(), returned["image_key"], json.dumps({
                            "adoption": self.last_default_adoption,
                            "first_shot": page.evaluate("state.formalImageWorkspace?.shots?.[0]"),
                        }, ensure_ascii=False))
                        page.wait_for_selector("[data-image-shot='0'] img")
                        before_key = first_select.input_value()
                        candidate_keys = [
                            first_select.locator("option").nth(index).get_attribute("value")
                            for index in range(first_select.locator("option").count())
                        ]
                        before_pixels = page.locator("[data-image-shot='0'] img").evaluate(
                            """async image => {
                              await image.decode();
                              const canvas = document.createElement('canvas');
                              canvas.width = canvas.height = 2;
                              const context = canvas.getContext('2d');
                              context.drawImage(image, 0, 0, 2, 2);
                              return [...context.getImageData(0, 0, 2, 2).data];
                            }"""
                        )
                        after_pixels = before_pixels
                        selected_key = before_key
                        for candidate_key in candidate_keys[1:]:
                            first_select.select_option(candidate_key)
                            page.wait_for_function(
                                """expected => {
                                  const image = document.querySelector("[data-image-shot='0'] img");
                                  return state.formalImageWorkspace?.shots?.[0]?.selected_image_key === expected
                                    && image && image.complete && image.naturalWidth > 0;
                                }""",
                                arg=candidate_key,
                            )
                            after_pixels = page.locator("[data-image-shot='0'] img").evaluate(
                                """async image => {
                                  await image.decode();
                                  const canvas = document.createElement('canvas');
                                  canvas.width = canvas.height = 2;
                                  const context = canvas.getContext('2d');
                                  context.drawImage(image, 0, 0, 2, 2);
                                  return [...context.getImageData(0, 0, 2, 2).data];
                                }"""
                            )
                            selected_key = candidate_key
                            if after_pixels != before_pixels:
                                break
                        self.assertNotEqual(before_key, selected_key)
                        self.assertNotEqual(before_pixels, after_pixels)
                        _expand_image_controls(page, 0, "search")
                        search_button = page.locator("[data-image-shot-search='0']")
                        with page.expect_response(
                            lambda response: "/image-workspace/shots/" in response.url
                            and "/candidates" in response.url
                            and response.status == 200
                        ):
                            search_button.click()
                        page.locator("#creative-image-picker [data-picker-close]").click()
                        page.wait_for_function(
                            """() => {
                              const image = document.querySelector("[data-image-shot='0'] img");
                              return image && image.complete && image.naturalWidth > 0;
                            }"""
                        )
                        self.assertTrue(
                            page.locator("[data-image-shot='0'] img").evaluate(
                                "image => image.currentSrc.startsWith('blob:')"
                            )
                        )
                        local_path = root / "anonymous-local-supplement.png"
                        with Image.open(root / "fixtures" / "05-reference.png") as source:
                            local = source.convert("RGB")
                            local.putpixel((0, 0), (251, 17, 93))
                            local.save(local_path, format="PNG")
                        non_target_before = page.evaluate(
                            """() => (state.formalImageWorkspace?.shots || []).slice(1).map(shot => ({
                              shot_no: shot.shot_no,
                              visual: shot.visual,
                              image: shot.selected_image_key,
                              confirmed: shot.confirmed,
                            }))"""
                        )
                        _expand_image_controls(page, 0)
                        page.locator("[data-image-supplement='0']").click()
                        page.locator("#formal-image-supplement-file").set_input_files(str(local_path))
                        _wait_for_creative_surface(page, project_id, "images", aux="materials", focus_shot="1")
                        self.assertTrue(parse_qs(urlparse(page.url).query)["supplement_image_key"][0])
                        self.assertTrue(parse_qs(urlparse(page.url).query)["shot_id"][0])
                        page.locator("[data-formal-image-key]").first.wait_for()
                        pending_card = page.locator("[data-formal-image-key]").filter(
                            has_text="待理解"
                        ).first
                        pending_details = pending_card.locator("details.ui00-image-review")
                        if pending_details.get_attribute("open") is None:
                            pending_details.locator("summary").click()
                        pending_card.locator("[data-formal-image-form]").wait_for()
                        pending_card.locator("img").wait_for()
                        page.wait_for_function(
                            "image => image.complete && image.naturalWidth > 0",
                            arg=pending_card.locator("img").element_handle(),
                        )
                        self.assertTrue(
                            pending_card.locator("img").evaluate(
                                "image => image.currentSrc.startsWith('blob:')"
                            )
                        )
                        pending_card.locator("[name='caption']").fill("匿名本地补图")
                        pending_card.locator("[name='tags']").fill("现场,补图")
                        pending_card.locator("[name='subjects']").fill("匿名主体")
                        pending_card.locator("[name='scene']").fill("匿名现场")
                        pending_card.locator("[name='purpose']").fill("第 1 镜补图")
                        pending_card.locator("[name='quality']").fill("清晰")
                        pending_card.locator("[name='brand']").fill("无")
                        pending_card.locator("[name='evidence']").fill("已核对本地补图像素与来源")
                        pending_card.locator("[data-formal-image-form] button[type='submit']").click()
                        _wait_for_creative_surface(page, project_id, "images", focus_shot="1")
                        page.locator("[data-image-shot='0']").wait_for()
                        page.wait_for_function(
                            "document.activeElement?.closest('[data-image-shot=\\\"0\\\"]') !== null"
                        )
                        self.assertEqual(
                            non_target_before,
                            page.evaluate(
                                """() => (state.formalImageWorkspace?.shots || []).slice(1).map(shot => ({
                                  shot_no: shot.shot_no,
                                  visual: shot.visual,
                                  image: shot.selected_image_key,
                                  confirmed: shot.confirmed,
                                }))"""
                            ),
                        )
                        page.route(
                            "**/api/formal/projects/*/images/*/preview*",
                            lambda route: route.abort(),
                        )
                        page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                        _show_all_image_shots(page)
                        page.locator("[data-image-shot='0']").wait_for()
                        self.assertTrue(
                            page.locator("[data-image-shot-confirm='0']").is_disabled()
                        )
                        errors.clear()
                        page.unroute("**/api/formal/projects/*/images/*/preview*")
                        page.reload()
                        _show_all_image_shots(page)
                        page.locator("[data-image-shot='0'] img").wait_for()
                    recorder.capture(
                        page, "M34", keyboard_selector="[data-image-shot-select='0']",
                        recovery_selector="#formal-image-shot-grid",
                        terminal_result="N 个镜头均显示同源安全候选、来源和确认控件",
                    )
                    _expand_image_controls(page, 0)
                    recorder.capture(
                        page, "M35", keyboard_selector="[data-image-shot-temporary='0']",
                        recovery_selector="#formal-image-shot-grid",
                        terminal_result="暂准文字依据为逐镜显式选择，默认未选中",
                    )
                    recorder.capture(
                        page, "M36", keyboard_selector="[data-image-supplement='0']",
                        double_click_selector="#formal-image-shot-grid",
                        recovery_selector="#formal-image-shot-grid",
                        terminal_result="每镜保留可键盘到达的本地补图入口，不覆盖现有候选",
                    )
                    # Make pending user selections first so any cross-shot reuse
                    # is visible before each shot's reason and confirmation.
                    for index in range(script_shot_count):
                        if strategy == "text-draft-recovery" and index == script_shot_count - 1:
                            continue
                        select = page.locator(f"[data-image-shot-select='{index}']")
                        if not select.input_value():
                            options = select.locator('option:not([disabled]):not([value=""])')
                            self.assertGreater(options.count(), 0, f"镜头 {index + 1} 应有可手选参考图")
                            select.select_option(options.first.get_attribute("value"))
                            page.locator(f"[data-image-shot='{index}'] img").wait_for()
                    missing = []
                    for index in range(script_shot_count):
                        if strategy == "text-draft-recovery" and index == script_shot_count - 1:
                            _expand_image_controls(page, index)
                            page.locator(f"[data-image-shot-temporary='{index}']").check()
                            page.locator(f"[data-image-shot-reason='{index}']").fill(
                                "品牌落版图待补充，当前仅保留文字依据"
                            )
                            page.locator(f"[data-image-shot-reason='{index}']").press("Tab")
                            continue
                        select = page.locator(f"[data-image-shot-select='{index}']")
                        if not select.input_value():
                            missing.append(index + 1)
                            continue
                        reuse_reason = page.locator(
                            f"[data-image-shot-reuse-reason='{index}']"
                        )
                        if reuse_reason.count() and reuse_reason.is_visible():
                            reuse_reason.fill(
                                "同一参考图用于多镜，已逐镜核对内容身份和本镜用途"
                            )
                        page.wait_for_function(
                            "index => !document.querySelector(`[data-image-shot-confirm=\\\"${index}\\\"]`)?.disabled",
                            arg=index,
                        )
                        _use_image_and_continue(page, index)
                    self.assertEqual(
                        missing,
                        [],
                        json.dumps(
                            page.evaluate("state.formalImageWorkspace?.shots || []"),
                            ensure_ascii=False,
                        ),
                    )
                    self.assertEqual(
                        page.locator("[data-image-shot-temporary]:checked").count(),
                        1 if strategy == "text-draft-recovery" else 0,
                    )
                    if strategy == "text-draft-recovery":
                        with page.expect_response(lambda response: response.request.method == "POST" and "/script/save?" in response.url) as incomplete:
                            page.locator("#formal-image-version-save").click()
                        self.assertEqual(incomplete.value.status, 409, incomplete.value.text())
                        self.assertEqual(incomplete.value.json()["detail"]["code"], "IMAGE_CONFIRMATION_REQUIRED")
                        last_index = script_shot_count - 1
                        page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                        _show_all_image_shots(page)
                        page.locator(f"[data-image-shot-temporary='{last_index}']:checked").wait_for()
                        with sqlite3.connect(root / "workspace.sqlite3") as connection:
                            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_versions WHERE project_id=?", (project_id,)).fetchone()[0], 1)
                        _expand_image_controls(page, last_index)
                        page.locator(f"[data-image-shot-temporary='{last_index}']").uncheck()
                        select = page.locator(f"[data-image-shot-select='{last_index}']")
                        self.assertTrue(select.is_enabled())
                        candidates = select.locator('option:not([disabled]):not([value=""])')
                        self.assertGreater(candidates.count(), 0, "取消暂准后应有可明确选择的图片候选")
                        select.select_option(candidates.first.get_attribute("value"))
                        reuse_reason = page.locator(f"[data-image-shot-reuse-reason='{last_index}']")
                        if reuse_reason.count() and reuse_reason.is_visible():
                            reuse_reason.fill("同一参考图用于多镜，已逐镜核对内容身份和本镜用途")
                        _use_image_and_continue(page, last_index)
                    self.assertFalse(page.locator("#formal-image-version-save").is_disabled(),
                        page.locator("#formal-image-selection-status").inner_text())
                    recorder.capture(page, "M37", keyboard_selector="#formal-image-version-save",
                        double_click_selector="#formal-image-shot-grid", recovery_selector="#formal-image-shot-grid",
                        terminal_result="全稿 N 镜配图确认完成后开放统一保存预览")
                    _save_unified_images(page)
                    recorder.capture(page, "M38", keyboard_selector="#creative-next-action",
                        double_click_selector="#formal-image-shot-grid", recovery_selector="#formal-image-shot-grid",
                        terminal_result="统一提案明确确认后仅创建一个文字与图片完整的子版本")
                    self.assertEqual(page.locator("[data-image-shot-confirm]:checked").count(), script_shot_count)
                    with sqlite3.connect(root / "workspace.sqlite3") as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_versions WHERE project_id=?", (project_id,)).fetchone()[0], 2)

                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator("#formal-image-version-state", has_text="已确认").wait_for()
                    self.assertEqual(
                        page.locator("[data-image-shot-confirm]:checked").count(),
                        script_shot_count,
                    )
                    page.locator('nav[aria-label="项目功能"] a[data-route="review"]').click()
                    page.wait_for_url(f"**/projects/{quote(project_id)}/review")
                    page.wait_for_function("() => state.formalReview !== null")
                    self.assertEqual(page.locator("#formal-review-state").inner_text(), "需审核")
                    page.locator("#formal-review-start").click()
                    page.locator("#formal-review-state", has_text="审核中").wait_for()
                    _approve_remaining_review(page, root / "workspace.sqlite3")
                    page.locator("#formal-review-state", has_text="待整片确认").wait_for()
                    self.assertEqual(
                        page.locator("#formal-review-issues [data-status='approve']").count(),
                        page.locator("#formal-review-issues .ui00-review-item").count(),
                    )
                    self.assertEqual(page.locator("#formal-review-final-check").count(), 0)
                    _final_review_note(page).fill(
                        "完整检查 N 镜图片决定、N-1 相邻复核与交付来源后确认。"
                    )
                    page.locator("#formal-review-final-submit").click()
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()

                    excel_path = root / f"{strategy}-delivery.xlsx"
                    with page.expect_download() as excel_download:
                        page.locator("#formal-delivery-excel").click()
                    excel_download.value.save_as(excel_path)
                    pdf_path = root / f"{strategy}-delivery.pdf"
                    with page.expect_download() as pdf_download:
                        page.locator("#formal-delivery-pdf").click()
                    pdf_download.value.save_as(pdf_path)
                    self._assert_deliverables(excel_path, pdf_path, script_shot_count)

                    screenshot_paths = []
                    for width, height in ((375, 812), (1024, 900), (1440, 1000)):
                        page.set_viewport_size({"width": width, "height": height})
                        self.assertTrue(page.evaluate(
                            "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                        ))
                        screenshot = root / f"{strategy}-{width}.png"
                        page.screenshot(path=str(screenshot), full_page=True)
                        screenshot_paths.append(screenshot)
                    self.assertEqual(errors, [])
                    context.close()
                    browser.close()
            finally:
                server.stop()

            database = root / "workspace.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.row_factory = sqlite3.Row
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                snapshot = connection.execute(
                    """SELECT snapshot_id FROM version_image_decision_snapshots_v3
                       WHERE project_id=? ORDER BY created_at DESC LIMIT 1""",
                    (project_id,),
                ).fetchone()
                self.assertIsNotNone(snapshot)
                decisions = connection.execute(
                    """SELECT decision_kind, source_location_json
                       FROM version_image_decisions_v3 WHERE snapshot_id=?""",
                    (snapshot["snapshot_id"],),
                ).fetchall()
                self.assertEqual(len(decisions), script_shot_count)
                self.assertEqual(
                    {row["decision_kind"] for row in decisions}, {"image_confirmed"}
                )
                for row in decisions:
                    source = json.loads(row["source_location_json"])
                    self.assertTrue(source.get("file"))
                    self.assertTrue(source.get("sheet"))
                    self.assertGreater(int(source.get("row") or 0), 0)
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM delivery_snapshots WHERE project_id=?",
                        (project_id,),
                    ).fetchone()[0],
                    1,
                )
            self._preserve_evidence(root, strategy, script_shot_count)

    def _create_project(self, page, base_url: str, strategy: str) -> str:
        page.goto(f"{base_url}/projects")
        page.wait_for_load_state("networkidle")
        page.locator("#formal-owner-confirm").check()
        page.locator("#formal-owner-enable").click()
        page.locator("#formal-project-new").wait_for()
        page.locator("#formal-project-new").click()
        page.wait_for_url("**/projects/new")
        page.locator("#formal-project-name").fill(f"匿名零图验收-{strategy}")
        page.locator("#formal-project-create button[type='submit']").click()
        _open_new_project_brief(page)
        return urlparse(page.url).path.split("/")[2]

    def _confirm_zero_image_brief(
        self, page, base_url: str, root: Path, project_id: str,
    ) -> None:
        page.goto(f"{base_url}/projects/{quote(project_id)}/brief")
        fixture_dir = root / "fixtures"
        inputs = [fixture_dir / name for name in (
            "01-brief.pdf", "02-brief.docx", "03-brief-no-images.xlsx", "04-brief.pptx",
        )]
        page.locator("#formal-brief-files").set_input_files([str(path) for path in inputs])
        page.locator("#formal-brief-upload-button").click()
        page.locator("#formal-standard-card-state", has_text="待确认").wait_for()
        for _ in range(8):
            button = page.locator(
                "#formal-conflict-list button", has_text="确认选择"
            ).first
            if button.count() == 0:
                break
            button.click()
            button.wait_for(state="detached")
        self.assertEqual(
            page.locator("#formal-conflict-list button", has_text="确认选择").count(), 0
        )
        page.wait_for_function(
            "document.querySelector('#formal-brief-confirm')?.disabled === false"
        )
        page.locator("#formal-brief-confirm").click()
        page.locator("#formal-standard-card-state", has_text="已确认").wait_for()

    def _adopt_defaults(
        self, page, *, preplan: bool, recorder: FormalControlEvidenceRecorder,
    ) -> int:
        with page.expect_response(
            lambda response: "/images/default-candidates?" in response.url
        ) as candidate_response:
            page.locator("[data-load-default-images]").click()
        self.assertEqual(
            candidate_response.value.status,
            200,
            candidate_response.value.text(),
        )
        candidate_payload = candidate_response.value.json()
        panel = page.locator("[data-default-image-panel]")
        with page.expect_response(
            lambda response: "/images/default-candidates?" in response.url
            and "view=recommended" in response.url
        ) as recommended_response:
            panel.locator("[data-library-view='recommended']").click()
        self.assertEqual(recommended_response.value.status, 200, recommended_response.value.text())
        candidate_payload = recommended_response.value.json()
        expected_shot_selections = {}
        if preplan:
            self.assertTrue(
                candidate_payload.get("library_candidates"),
                json.dumps(candidate_payload, ensure_ascii=False),
            )
            candidates = panel.locator("[data-default-library-candidate]")
            try:
                candidates.first.wait_for()
            except PlaywrightTimeoutError:
                self.fail(json.dumps({
                    "candidate_payload": candidate_payload,
                    "panel": panel.inner_text(),
                }, ensure_ascii=False, indent=2))
            for index in range(candidates.count()):
                candidates.nth(index).check()
            selected_count = candidates.count()
            self.assertEqual(
                selected_count,
                candidate_payload.get("safe_candidate_count"),
                json.dumps(candidate_payload, ensure_ascii=False),
            )
        else:
            panel.locator("[data-library-scope]").select_option("all")
            selects = panel.locator("[data-default-image-select]")
            selects.first.wait_for()
            selected_count = selects.count()
            self.assertEqual(
                panel.locator("[data-default-image-gap]").count(),
                0,
                json.dumps(candidate_payload, ensure_ascii=False),
            )
            pending_previews = []
            preview_pattern = "**/images/default-candidates/*/preview?*"
            hold_preview = lambda route: pending_previews.append(route)
            page.route(preview_pattern, hold_preview)
            try:
                # Keep real preview requests pending while the user selects
                # several shots. Different shots must not cancel each other.
                for index in range(selected_count):
                    select = panel.locator("[data-default-image-select]").nth(index)
                    key = select.input_value()
                    if not key:
                        options = select.locator('option:not([disabled]):not([value=""])')
                        self.assertGreater(options.count(), 0, f"镜头 {index + 1} 应有可手选参考图")
                        key = options.first.get_attribute("value")
                        select.select_option(key)
                    shot_no = str(candidate_payload["shots"][index]["shot_no"])
                    expected_shot_selections[shot_no] = key
                self.assertGreaterEqual(
                    len(pending_previews), 2,
                    "此旅程必须覆盖多个镜头预览同时未返回的快速选择",
                )
            finally:
                for request in reversed(pending_previews):
                    request.continue_()
                page.unroute(preview_pattern, hold_preview)
            for shot_no, key in expected_shot_selections.items():
                preview = panel.locator(
                    f'[data-default-image-shot="{shot_no}"] '
                    f'[data-default-preview="{key}"] img'
                )
                preview.wait_for()
                page.wait_for_function(
                    "image => image.complete && image.naturalWidth > 0",
                    arg=preview.element_handle(),
                )
        recorder.capture(
            page, "M31", keyboard_selector="[data-propose-default-images]",
            double_click_selector="#formal-image-grid",
            recovery_selector="#formal-image-grid",
            terminal_result="类别默认图候选显示安全数量、来源和明确缺口，不静默采用",
        )
        with page.expect_response(
            lambda response: response.request.method == "POST"
            and "/images/default-proposals?" in response.url
        ) as proposal_response:
            panel.locator("[data-propose-default-images]").click()
        self.assertEqual(
            proposal_response.value.status, 200, proposal_response.value.text()
        )
        if not preplan:
            submitted = proposal_response.value.request.post_data_json["selections"]
            self.assertEqual(
                {str(item["shot_no"]): item["candidate_key"] for item in submitted},
                expected_shot_selections,
                "连续选择后提案必须包含每个明确选择的镜头",
            )
        panel.locator("[data-confirm-default-images]").wait_for()
        recorder.capture(
            page, "M32", double_click_selector="#formal-image-grid",
            recovery_selector="#formal-image-grid",
            terminal_result="默认图采用先形成受控 proposal，确认按钮不直接绕过 Task",
        )
        with page.expect_response(
            lambda response: response.request.method == "POST"
            and "/api/agent/tasks/" in response.url
            and response.url.endswith("/confirm")
        ) as confirm_response:
            panel.locator("[data-confirm-default-images]").click()
        self.assertEqual(
            confirm_response.value.status, 200, confirm_response.value.text()
        )
        self.assertEqual(
            confirm_response.value.json().get("task", {}).get("status"),
            "completed",
            confirm_response.value.text(),
        )
        self.last_default_adoption = confirm_response.value.json().get("response", {})
        if not preplan:
            adopted = self.last_default_adoption.get("adopted_selections", [])
            self.assertEqual(
                {str(item["shot_no"]) for item in adopted},
                set(expected_shot_selections),
                json.dumps(self.last_default_adoption, ensure_ascii=False),
            )
            self.assertTrue(all(item.get("image_key") and item.get("content_sha256") for item in adopted))
        page.wait_for_function(
            "Number(document.querySelector('#formal-image-total')?.textContent || 0) > 0"
        )
        self.assertEqual(page.locator("#formal-image-state").inner_text(), "全部可用")
        return selected_count

    def _assert_deliverables(
        self, excel_path: Path, pdf_path: Path, shot_count: int,
    ) -> None:
        workbook = load_workbook(excel_path, read_only=False, data_only=False)
        try:
            self.assertEqual(len(workbook["脚本"]._images), shot_count)
            self.assertEqual(workbook["相邻镜头复核"].max_row - 1, shot_count - 1)
            self.assertIn("审核记录", workbook.sheetnames)
            review_headers = {
                str(cell.value or ""): cell.column for cell in workbook["剪辑复核"][1]
            }
            self.assertEqual(
                {
                    workbook["剪辑复核"].cell(row, review_headers["总体审核"]).value
                    for row in range(2, workbook["剪辑复核"].max_row + 1)
                },
                {"通过"},
            )
            full_headers = {
                str(cell.value or ""): cell.column for cell in workbook["整片复核"][1]
            }
            self.assertEqual(
                workbook["整片复核"].cell(2, full_headers["总体审核"]).value,
                "通过",
            )
            cells = "\n".join(
                str(cell.value or "")
                for sheet in workbook.worksheets
                for row in sheet.iter_rows()
                for cell in row
            )
            for header in ("采用来源文件", "采用来源 sheet", "采用来源行"):
                self.assertIn(header, cells)
            for forbidden in ("artifact://", "image_sha256", "analysis_sha256"):
                self.assertNotIn(forbidden, cells.casefold())
        finally:
            workbook.close()
        reopened = load_workbook(excel_path, read_only=True, data_only=False)
        reopened.close()
        self.assertEqual(len(PdfReader(str(pdf_path)).pages), shot_count)

    def _preserve_evidence(self, root: Path, strategy: str, shot_count: int) -> None:
        configured = os.environ.get("MPLUS_ZERO_IMAGE_EVIDENCE_ROOT", "").strip()
        if not configured:
            return
        target = Path(configured) / strategy
        target.mkdir(parents=True, exist_ok=True)
        names = [
            ("workspace.sqlite3", "workspace.sqlite3"),
            (f"{strategy}-delivery.xlsx", "delivery.xlsx"),
            (f"{strategy}-delivery.pdf", "delivery.pdf"),
            (f"{strategy}-375.png", f"{strategy}-375.png"),
            (f"{strategy}-1024.png", f"{strategy}-1024.png"),
            (f"{strategy}-1440.png", f"{strategy}-1440.png"),
            ("server.log", "server.log"),
            ("provider-calls.jsonl", "provider-calls.jsonl"),
        ]
        records = []
        for source_name, destination_name in names:
            source = root / source_name
            if not source.is_file():
                continue
            destination = target / destination_name
            shutil.copy2(source, destination)
            records.append({
                "file": destination_name,
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                "bytes": destination.stat().st_size,
            })
        (target / "summary.json").write_text(
            json.dumps({
                "strategy": strategy,
                "shot_count": shot_count,
                "adjacent_review_count": shot_count - 1,
                "records": records,
            }, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
