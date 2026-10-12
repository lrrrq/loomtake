"""AC-G: real UI upload/analysis/agent selection/save/review/Excel/PDF.

Text and vision providers are explicitly deterministic. The DB, authority,
matching, confirmation, materialization, browser and delivered files are real.
"""
from __future__ import annotations

from contextlib import nullcontext
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import quote

from openpyxl import load_workbook
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader

from tests import test_mvp_v2_formal_browser_journey as browser_fixture
from tests import test_mvp_v2_formal_zero_image_browser_journeys as zero_fixture
from tests import test_mvp_v2_single_shot_library_journey as library_fixture


REPO = Path(__file__).resolve().parents[1]
SERVER = REPO / "tests/support/formal_uploaded_image_fixture_server.py"
SOURCE = REPO / "tests/fixtures/v10_image_delivery/cc0-participant-group.jpg"


class _UploadServer(browser_fixture._FixtureServer):
    def start(self):
        with patch.object(browser_fixture, "SERVER_SCRIPT", SERVER):
            return super().start()


def _counts(root, project_id):
    with sqlite3.connect(f"{(root / 'workspace.sqlite3').as_uri()}?mode=ro", uri=True) as db:
        return {
            "versions": db.execute("SELECT COUNT(*) FROM project_versions WHERE project_id=?", (project_id,)).fetchone()[0],
            "image_proposals": db.execute("SELECT COUNT(*) FROM tasks WHERE project_id=? AND task_type='formal_image_version'", (project_id,)).fetchone()[0],
        }


class UploadedImageBrowserJourneyTests(unittest.TestCase):
    maxDiff = None

    def test_new_upload_natural_third_shot_has_one_proposal_child_and_real_delivery(self):
        evidence_parent = os.environ.get("MPLUS_V3_EVIDENCE_ROOT", "").strip()
        if evidence_parent:
            Path(evidence_parent).mkdir(parents=True, exist_ok=True)
        directory = (nullcontext(tempfile.mkdtemp(prefix="mplus-upload-agent-", dir=evidence_parent)) if evidence_parent
                     else tempfile.TemporaryDirectory(prefix="mplus-upload-agent-"))
        with directory as temporary:
            root = Path(temporary)
            server = _UploadServer(root)
            evidence = {"provider_mode": "deterministic", "external_provider_calls": 0,
                        "manual_image_analysis_requests": 0, "completed_stages": []}
            page = None
            try:
                server.start()
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                    page = context.new_page()
                    page.set_default_timeout(20_000)
                    errors, writes = [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    def observe(request):
                        if request.method == "POST":
                            writes.append(request.url)
                            if "/images/" in request.url and "/analysis?" in request.url:
                                evidence["manual_image_analysis_requests"] += 1
                    page.on("request", observe)
                    helper = library_fixture.SingleShotLibraryBrowserJourneyTests("runTest")
                    project_id = helper._create_project(page, server.base_url)
                    zero_fixture.FormalZeroImageBrowserJourneyTests("runTest")._confirm_zero_image_brief(page, server.base_url, root, project_id)
                    route = f"{server.base_url}/projects/{quote(project_id)}"
                    evidence["project_id"] = project_id
                    page.goto(f"{route}/materials")
                    helper._adopt_starting_library(page)
                    page.goto(f"{route}/plan")
                    page.locator("#formal-plan-create").click()
                    page.locator("#formal-plan-confirmation").wait_for()
                    page.locator("#formal-plan-confirm-check").check()
                    with page.expect_response(lambda r: r.request.method == "POST" and "/plans/" in r.url and "/confirm" in r.url, timeout=90_000) as generated:
                        page.locator("#formal-plan-confirm").click()
                    self.assertEqual(generated.value.status, 200, generated.value.text())
                    page.locator("#formal-plan-result").wait_for(timeout=90_000)
                    with page.expect_response(lambda r: "/image-workspace?" in r.url and r.request.method == "GET") as loaded:
                        page.goto(f"{route}/shot-images")
                    self.assertEqual(loaded.value.status, 200, loaded.value.text())
                    initial = loaded.value.json()
                    count = len(initial["shots"])
                    self.assertGreaterEqual(count, 8)
                    page.locator("[data-image-shot='0']").wait_for()
                    page.locator("#creative-image-scope").select_option("all")
                    for index in range(count):
                        selector = page.locator(f"[data-image-shot-select='{index}']")
                        if not selector.input_value():
                            selector.select_option(selector.locator('option:not([value=""]):not([disabled])').first.get_attribute("value"))
                    for index in range(count):
                        browser_fixture._use_image_and_continue(page, index)
                    helper._save_images(page)
                    baseline = library_fixture._saved_version(root, project_id)
                    self.assertGreaterEqual(len({x["image_sha256"] for x in baseline["decisions"]["decisions"]}), 3)
                    baseline_counts = _counts(root, project_id)
                    text_provider_calls = (root / "provider-calls.jsonl").read_text()
                    self.assertTrue(all(json.loads(line)["model"].startswith("anonymous-fixture-")
                        for line in text_provider_calls.splitlines()))
                    self.assertEqual(json.loads((root / "fixture-mode.json").read_text())["mode"], "deterministic")
                    writes.clear()
                    evidence.update(before=baseline_counts, shot_count=count)
                    evidence["completed_stages"].append("baseline_all_images_confirmed")

                    # Upload actual image bytes through the third-shot UI.
                    page.locator("[data-image-supplement='2']").click()
                    with page.expect_response(lambda r: r.request.method == "POST" and "/image-workspace/supplements?" in r.url) as uploaded:
                        page.locator("#formal-image-supplement-file").set_input_files(str(SOURCE))
                    self.assertEqual(uploaded.value.status, 200, uploaded.value.text())
                    upload = uploaded.value.json()
                    evidence["uploaded_image_key"] = upload["image_key"]
                    with page.expect_response(lambda r: r.request.method == "POST" and "/images/understand?" in r.url) as understood:
                        page.locator("#formal-image-understand").click()
                    self.assertEqual(understood.value.status, 200, understood.value.text())
                    analysis = next(x for x in understood.value.json()["images"] if x["image_key"] == upload["image_key"])
                    self.assertEqual(analysis["status"], "pass")
                    self.assertEqual(analysis["summary"]["evidence_status"], "provider_generated")
                    provider_calls = [json.loads(x) for x in (root / "uploaded-image-provider.jsonl").read_text().splitlines()]
                    self.assertEqual(len(provider_calls), 1)
                    self.assertEqual(provider_calls[0]["sha256"], library_fixture._sha(SOURCE))
                    self.assertEqual(_counts(root, project_id), baseline_counts)
                    evidence["completed_stages"].append("upload_and_provider_analysis_without_manual_pass")

                    page.goto(f"{route}/assistant")
                    page.locator("#agent-session-status", has_text="会话已连接").wait_for()
                    page.locator("#agent-message").fill("把第 3 镜换成刚上传的图片，文案、时长和其他镜头都不要改。")
                    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/messages")) as candidates:
                        page.locator("#agent-message-submit").click()
                    self.assertEqual(candidates.value.status, 200, candidates.value.text())
                    candidate_result = candidates.value.json()
                    self.assertEqual(candidate_result["response"]["type"], "formal_image_candidates")
                    evidence["agent_candidates"] = candidate_result["response"]
                    chosen = next(x for x in candidate_result["response"]["candidates"] if x["image_key"] == upload["image_key"])
                    card = page.locator("[data-agent-candidate-card]").filter(has=page.locator(f'[data-agent-image-select$="{chosen["candidate_key"]}"]'))
                    # A stable server-provided key selects the actual displayed image.
                    button = page.locator(f'[data-agent-image-select$="{chosen["candidate_key"]}"]')
                    expect(button).to_be_enabled()
                    if card.locator("[data-agent-reuse-reason]").count():
                        card.locator("[data-agent-reuse-reason]").fill("第3镜单独使用新上传的嘉宾现场参考图。")
                    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/image-selection")) as selected:
                        button.click()
                    self.assertEqual(selected.value.status, 200, selected.value.text())
                    proposal = selected.value.json()
                    self.assertEqual(proposal["response"]["affected_shots"], ["3"])
                    self.assertEqual(_counts(root, project_id), {**baseline_counts, "image_proposals": baseline_counts["image_proposals"] + 1})
                    page.screenshot(path=str(root / "one-third-shot-proposal.png"), full_page=True)
                    with page.expect_response(lambda r: r.request.method == "POST" and "/api/agent/tasks/" in r.url and r.url.endswith("/confirm"), timeout=90_000) as confirmed:
                        page.locator("[data-agent-confirm]").click()
                    self.assertEqual(confirmed.value.status, 200, confirmed.value.text())
                    self.assertEqual(confirmed.value.json()["task"]["status"], "completed", confirmed.value.text())
                    page.locator("#agent-message-hint", has_text="已更新").wait_for()
                    expect(page.locator("[data-agent-confirm]")).to_have_count(0)
                    expect(page.locator("#agent-feed")).to_contain_text("确认单镜图片替换")
                    expect(page.locator("#agent-feed")).to_contain_text("图片决定已完成")
                    child = library_fixture._saved_version(root, project_id)
                    helper._assert_only_third_changed(baseline, child, library_fixture._sha(SOURCE))
                    before_decisions = {x["shot_no"]: x for x in baseline["decisions"]["decisions"]}
                    for item in child["decisions"]["decisions"]:
                        if item["shot_no"] != "3":
                            self.assertEqual({k: v for k, v in item.items() if k != "decision_id"},
                                {k: v for k, v in before_decisions[item["shot_no"]].items() if k != "decision_id"})
                    page.reload()
                    page.locator("#agent-session-status", has_text="会话已连接").wait_for()
                    expect(page.locator("[data-agent-confirm]")).to_have_count(0)
                    self.assertEqual(_counts(root, project_id), {"versions": baseline_counts["versions"] + 1,
                        "image_proposals": baseline_counts["image_proposals"] + 1})
                    self.assertEqual(sum(url.endswith("/messages") for url in writes), 1)
                    self.assertEqual(sum(url.endswith("/image-selection") for url in writes), 1)
                    self.assertEqual(sum("/api/agent/tasks/" in url and url.endswith("/confirm") for url in writes), 1)
                    self.assertEqual(evidence["manual_image_analysis_requests"], 0)
                    self.assertEqual((root / "provider-calls.jsonl").read_text(), text_provider_calls)
                    evidence["completed_stages"].append("one_natural_request_one_proposal_one_confirmation_one_child")

                    page.goto(f"{route}/review")
                    page.locator("#formal-review-start").click()
                    page.locator("#formal-review-state", has_text="审核中").wait_for()
                    browser_fixture._approve_remaining_review(page, root / "workspace.sqlite3")
                    page.locator("#formal-review-state", has_text="待整片确认").wait_for()
                    browser_fixture._final_review_note(page).fill("已看图并核对第3镜新图、相邻承接和全部镜头，确认整片。")
                    page.locator("#formal-review-final-submit").click()
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    excel, pdf = helper._download(page, root, "uploaded-third-shot")
                    self._assert_exports(excel, pdf, count)
                    self.assertEqual(_counts(root, project_id)["versions"], baseline_counts["versions"] + 1)
                    self.assertEqual(errors, [])
                    evidence.update(status="passed", after=_counts(root, project_id), non_target_shots_compared=count - 1,
                        excel_sha256=library_fixture._sha(excel), pdf_sha256=library_fixture._sha(pdf))
                    evidence["completed_stages"].append("real_browser_review_and_pixel_verified_excel_pdf")
                    page.screenshot(path=str(root / "uploaded-image-review-delivery.png"), full_page=True)
                    context.close()
                    browser.close()
            except Exception as exc:
                evidence.update(status="failed", error_type=type(exc).__name__, error=str(exc))
                raise
            finally:
                server.stop()
                (root / "uploaded-image-journey.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _assert_exports(self, excel, pdf, count):
        with Image.open(SOURCE) as source:
            pixels = source.convert("RGB").tobytes()
        workbook = load_workbook(excel)
        try:
            images = sorted(workbook["脚本"]._images, key=lambda image: image.anchor._from.row)
            self.assertEqual(len(images), count)
            self.assertEqual(workbook["相邻镜头复核"].max_row - 1, count - 1)
            with Image.open(BytesIO(images[2]._data())) as embedded:
                self.assertEqual(embedded.convert("RGB").tobytes(), pixels)
        finally:
            workbook.close()
        reader = PdfReader(str(pdf))
        self.assertEqual(len(reader.pages), count)
        # pypdf's image convenience object re-encodes JPEG; inspect the actual
        # embedded DCT stream to verify the delivered bytes without that step.
        embedded_bytes = reader.pages[2].images[0].indirect_reference.get_object().get_data()
        self.assertEqual(embedded_bytes, SOURCE.read_bytes())
        with Image.open(BytesIO(embedded_bytes)) as embedded:
            self.assertEqual(embedded.convert("RGB").tobytes(), pixels)
        self.assertIn(library_fixture._sha(SOURCE), reader.pages[2].extract_text())
