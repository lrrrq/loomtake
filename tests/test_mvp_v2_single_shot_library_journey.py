"""Browser acceptance: one later-page image changes only the selected third shot."""
from __future__ import annotations

from contextlib import nullcontext
from copy import deepcopy
from io import BytesIO
import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, quote, urlparse

from openpyxl import load_workbook
from PIL import Image
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader

from mvp_v2.artifact_service import RunArtifactResolver
from tests import test_mvp_v2_formal_browser_journey as browser_fixture
from tests.test_mvp_v2_formal_browser_journey import _approve_remaining_review, _final_review_note, _use_image_and_continue
from tests import test_mvp_v2_formal_zero_image_browser_journeys as zero_image_fixture


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = REPOSITORY_ROOT / "tests" / "support" / "formal_single_shot_library_fixture_server.py"


class _GalleryServer(browser_fixture._FixtureServer):
    def start(self):
        with patch.object(browser_fixture, "SERVER_SCRIPT", SERVER_SCRIPT):
            return super().start()


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _saved_version(root: Path, project_id: str) -> dict:
    with sqlite3.connect(f"{(root / 'workspace.sqlite3').as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("""SELECT v.* FROM projects p JOIN project_versions v
            ON p.current_version_id=v.version_id WHERE p.project_id=?""", (project_id,)).fetchone()
        version = dict(row)
    resolver = RunArtifactResolver(root / "runs")
    draft = resolver.resolve_stage_artifact(version["run_id"], "script_draft").path
    binding = resolver.resolve_stage_artifact(version["run_id"], "visual_binding").path
    decisions = resolver.resolve_artifact(version["run_id"], "image_decisions").path
    return {"version": version, "draft_path": str(draft), "draft_sha256": _sha(draft),
            "draft": _json(draft), "binding": _json(binding), "decisions": _json(decisions)}


def _input_snapshot(page, count: int) -> list[dict]:
    # DOM form values are observed, never assigned through application state.
    result = []
    for index in range(count):
        def value(name):
            return page.locator(f"[data-image-shot-{name}='{index}']").input_value()
        result.append({"image_key": value("select"),
            "confirmed": page.locator(f"[data-image-shot-confirm='{index}']").is_checked(),
            "temporary": page.locator(f"[data-image-shot-temporary='{index}']").is_checked(),
            "temporary_reason": value("reason"), "reuse_reason": value("reuse-reason"),
            "query": value("query")})
    return result


class SingleShotLibraryBrowserJourneyTests(unittest.TestCase):
    maxDiff = None

    def test_third_shot_later_page_selection_preserves_others_through_delivery_and_restart(self):
        evidence_root = os.environ.get("MPLUS_V3_EVIDENCE_ROOT", "").strip()
        if evidence_root:
            Path(evidence_root).mkdir(parents=True, exist_ok=True)
        directory = (nullcontext(tempfile.mkdtemp(prefix="mplus-single-shot-library-", dir=evidence_root))
                     if evidence_root else tempfile.TemporaryDirectory(prefix="mplus-single-shot-library-"))
        with directory as temporary:
            root = Path(temporary)
            server = _GalleryServer(root)
            evidence = {"external_provider_calls": 0, "completed_stages": []}
            try:
                server.start()
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("framenavigated", lambda frame: evidence.update(last_browser_url=frame.url))
                    evidence["page_errors"] = errors
                    helper = zero_image_fixture.FormalZeroImageBrowserJourneyTests("runTest")
                    project_id = self._create_project(page, server.base_url)
                    helper._confirm_zero_image_brief(page, server.base_url, root, project_id)
                    evidence["project_id"] = project_id
                    route = f"{server.base_url}/projects/{quote(project_id)}"
                    page.goto(f"{route}/materials")
                    self._adopt_starting_library(page)
                    evidence["completed_stages"].append("anonymous_recommendations_adopted_before_generation")

                    page.goto(f"{route}/plan")
                    page.locator("#formal-plan-create").click()
                    page.locator("#formal-plan-confirmation").wait_for()
                    page.locator("#formal-plan-confirm-check").check()
                    with page.expect_response(lambda r: r.request.method == "POST" and "/plans/" in r.url and "/confirm" in r.url, timeout=90_000) as generated:
                        page.locator("#formal-plan-confirm").click()
                    self.assertEqual(generated.value.status, 200, generated.value.text())
                    page.locator("#formal-plan-result").wait_for(timeout=90_000)
                    with page.expect_response(lambda r: "/image-workspace?" in r.url and r.request.method == "GET") as workspace_response:
                        page.goto(f"{route}/shot-images")
                    self.assertEqual(workspace_response.value.status, 200, workspace_response.value.text())
                    workspace = workspace_response.value.json()
                    count = len(workspace["shots"])
                    self.assertEqual(count, 12)
                    target = workspace["shots"][2]
                    self.assertEqual(target["shot_no"], "3")
                    evidence.update(shot_count=count, target_shot_id=target["shot_id"], target_shot_no="3")
                    page.locator("[data-image-shot='0']").wait_for()
                    with sqlite3.connect(root / "workspace.sqlite3") as connection:
                        initial_run = connection.execute("""SELECT v.run_id FROM projects p
                            JOIN project_versions v ON p.current_version_id=v.version_id WHERE p.project_id=?""", (project_id,)).fetchone()[0]
                    initial_workbook = RunArtifactResolver(root / "runs").resolve_artifact(initial_run, "workbook").path
                    initial_book = load_workbook(initial_workbook)
                    try:
                        originals = {str(image.anchor._from.row - 5): image._data() for image in initial_book["脚本"]._images}
                    finally:
                        initial_book.close()
                    for index, shot in enumerate(workspace["shots"]):
                        if shot["shot_no"] not in originals:
                            self.assertIsNone(shot["reference_image"])
                            expect(page.locator(f"#formal-image-shot-preview-{index} img")).to_have_count(0)
                            continue
                        reference = shot["reference_image"]["inline_preview"]
                        self.assertEqual(base64.b64decode(reference.split(",", 1)[1]), originals[shot["shot_no"]])
                        expect(page.locator(f"#formal-image-shot-preview-{index} img")).to_have_attribute("src", reference)
                        expect(page.locator(f"[data-image-shot='{index}'] .ui00-image-shot-source")).to_contain_text("版本参考图，待确认")
                    evidence["initial_embedded_image_count"] = len(originals)
                    if page.locator("#creative-image-scope").count():
                        page.locator("#creative-image-scope").select_option("all")
                    for index in range(count):
                        selector = page.locator(f"[data-image-shot-select='{index}']")
                        if not selector.input_value():
                            candidate = selector.locator('option:not([value=""]):not([disabled])').first
                            selector.select_option(candidate.get_attribute("value"))
                    # Resolve the whole initial selection before confirmations,
                    # so later choices cannot introduce unreviewed cross-shot reuse.
                    for index in range(count):
                        _use_image_and_continue(page, index)
                    self._save_images(page)
                    baseline = _saved_version(root, project_id)
                    with page.expect_request(lambda r: "/image-workspace?" in r.url and r.method == "GET") as saved_request:
                        page.reload()
                    saved_response = saved_request.value.response()
                    self.assertIsNotNone(saved_response)
                    self.assertEqual(saved_response.status, 200)
                    print("Reading refreshed image workspace", flush=True)
                    saved_workspace = saved_response.json()
                    print("Read refreshed image workspace", flush=True)
                    saved_workbook = RunArtifactResolver(root / "runs").resolve_artifact(baseline["version"]["run_id"], "workbook").path
                    saved_book = load_workbook(saved_workbook)
                    try:
                        saved_images = {str(image.anchor._from.row - 5): image._data() for image in saved_book["脚本"]._images}
                    finally:
                        saved_book.close()
                    self.assertEqual(len(saved_images), count)
                    for index, shot in enumerate(saved_workspace["shots"]):
                        reference = shot["reference_image"]["inline_preview"]
                        self.assertEqual(base64.b64decode(reference.split(",", 1)[1]), saved_images[shot["shot_no"]])
                        expect(page.locator(f"[data-image-shot='{index}'] .ui00-image-shot-source")).to_contain_text("当前确认图")
                    evidence["saved_reference_images_match_workbook"] = True
                    page.locator("#creative-image-scope").select_option("all")
                    distinct_baseline_images = len({item["image_sha256"] for item in baseline["decisions"]["decisions"]})
                    self.assertGreaterEqual(distinct_baseline_images, 3)
                    evidence["distinct_baseline_images"] = distinct_baseline_images
                    browser_fixture._expand_image_controls(page, 0, "search")
                    page.locator("[data-image-shot-query='0']").fill("保留的查找条件：入口与来宾交流")
                    baseline_inputs = _input_snapshot(page, count)
                    evidence["before_image_inputs"] = baseline_inputs
                    evidence["completed_stages"].append("all_shots_explicitly_confirmed_and_saved")
                    baseline_provider_calls = (root / "provider-calls.jsonl").read_text(encoding="utf-8")
                    calls = [json.loads(line) for line in baseline_provider_calls.splitlines()]
                    self.assertTrue(calls)
                    self.assertTrue(all(call["model"].startswith("anonymous-fixture-") for call in calls))
                    evidence["anonymous_text_calls"] = len(calls)

                    # These actions are the public, unified workspace journey.
                    page.locator("[data-creative-panel='script']").click()
                    page.locator(f"button[data-creative-shot='{target['shot_id']}']").click()
                    page.locator("button[data-creative-change-image]").click()
                    panel = page.get_by_role("dialog", name="第 3 镜", exact=True)
                    expect(panel).to_be_visible()
                    with page.expect_response(lambda r: "/images/default-candidates?" in r.url and parse_qs(urlparse(r.url).query).get("view") == ["library"]) as first_page:
                        panel.locator("[data-picker-source='library']").click()
                    self.assertEqual(first_page.value.status, 200, first_page.value.text())
                    panel.locator("[data-picker-query]").fill("匿名图库")
                    with page.expect_response(lambda r: "/images/default-candidates?" in r.url and parse_qs(urlparse(r.url).query).get("query") == ["匿名图库"]) as searched:
                        panel.locator("form button[type='submit']").click()
                    self.assertEqual(searched.value.status, 200, searched.value.text())
                    first_page_keys = {item["candidate_key"] for item in searched.value.json()["library_candidates"]}
                    self.assertGreater(searched.value.json()["total_groups"], 24)
                    evidence["keyword"] = "匿名图库"
                    with page.expect_response(lambda r: "/images/default-candidates?" in r.url and parse_qs(urlparse(r.url).query).get("page") == ["2"]) as second_page:
                        panel.locator("[data-picker-page='2']").click()
                    self.assertEqual(second_page.value.status, 200, second_page.value.text())
                    candidates = second_page.value.json()
                    later = next(item for item in candidates["library_candidates"] if not item.get("duplicate_of") and item["content_sha256"] != baseline["decisions"]["decisions"][2]["image_sha256"])
                    self.assertNotIn(later["candidate_key"], first_page_keys)
                    selected_index = candidates["library_candidates"].index(later)
                    panel.locator(f"[data-picker-candidate='{selected_index}']").click()
                    expect(panel.locator(".creative-picker-selected img")).to_be_visible()
                    selected_preview = panel.locator(".creative-picker-selected img").get_attribute("src")
                    hidden_query = "匿名不存在筛选结果ZXQ987"
                    panel.locator("[data-picker-query]").fill(hidden_query)
                    with page.expect_response(lambda r: "/images/default-candidates?" in r.url
                            and parse_qs(urlparse(r.url).query).get("query") == [hidden_query]) as filtered:
                        panel.locator("form button[type='submit']").click()
                    self.assertEqual(filtered.value.status, 200, filtered.value.text())
                    self.assertEqual(filtered.value.json()["library_candidates"], [])
                    expect(panel.locator(".creative-picker-selected img")).to_be_visible()
                    expect(panel.locator(".creative-picker-selected img")).to_have_attribute("src", selected_preview)
                    expect(panel.locator("[data-picker-adopt]")).to_be_enabled()
                    expect(panel.locator(".creative-picker-selected .creative-preview-status")).to_contain_text("当前暂选不在本次筛选结果中")
                    evidence["selection_preserved_outside_search_results"] = True
                    with page.expect_response(lambda r: r.request.method == "POST" and "/images/default-proposals?" in r.url) as proposal:
                        panel.locator("[data-picker-adopt]").click()
                    self.assertEqual(proposal.value.status, 200, proposal.value.text())
                    proposal_input = proposal.value.request.post_data_json
                    evidence["adoption_proposal_input"] = proposal_input
                    self.assertEqual([str(item["shot_no"]) for item in proposal_input["selections"]], ["3"])
                    self.assertEqual(proposal_input["allowed_shot_ids"], [target["shot_id"]])
                    with page.expect_response(lambda r: r.request.method == "POST" and "/api/agent/tasks/" in r.url and r.url.endswith("/confirm")) as adopted_response:
                        panel.locator("[data-picker-adopt]").click()
                    self.assertEqual(adopted_response.value.status, 200, adopted_response.value.text())
                    self.assertEqual(adopted_response.value.json()["task"]["status"], "completed", adopted_response.value.text())
                    adopted = adopted_response.value.json()["response"]
                    self.assertEqual([item["shot_no"] for item in adopted["adopted_selections"]], ["3"])
                    mapping = adopted["adopted_selections"][0]
                    self.assertEqual(mapping["content_sha256"], later["content_sha256"])
                    evidence.update(total_groups=candidates["total_groups"], selected_page=2,
                                    selected_candidate=deepcopy(later), adopted_mapping=mapping)
                    expect(panel).not_to_be_visible()
                    page.locator("[data-creative-panel='images']").click()
                    page.locator(f"button[data-creative-shot='{target['shot_id']}']").click()
                    try:
                        expect(page.locator("[data-image-shot-select='2']")).to_have_value(mapping["image_key"])
                    except AssertionError:
                        evidence["after_adoption_image_inputs"] = _input_snapshot(page, count)
                        page.screenshot(path=str(root / "third-shot-adoption-failure.png"))
                        raise
                    after_adoption = _input_snapshot(page, count)
                    evidence["after_adoption_image_inputs"] = after_adoption
                    self.assertEqual(after_adoption[:2] + after_adoption[3:], baseline_inputs[:2] + baseline_inputs[3:])
                    self.assertFalse(after_adoption[2]["confirmed"])
                    expect(page.locator("[data-image-shot='2'] .ui00-image-shot-source")).to_contain_text("已选用，待保存配图版本")
                    self.assertEqual(_saved_version(root, project_id)["version"], baseline["version"])
                    page.screenshot(path=str(root / "third-shot-later-page-adopted.png"))
                    evidence["completed_stages"].append("third_shot_only_later_page_adoption")

                    _use_image_and_continue(page, 2)
                    save_input = self._save_images(page)
                    evidence["image_save_input"] = save_input
                    self.assertEqual([str(item["shot_no"]) for item in save_input["image_changes"]], ["3"])
                    self.assertEqual([item["shot_id"] for item in save_input["image_changes"]], [target["shot_id"]])
                    child = _saved_version(root, project_id)
                    self._assert_only_third_changed(baseline, child, later["content_sha256"])
                    expect(page.locator("[data-image-shot='2'] .ui00-image-shot-source")).to_contain_text("当前确认图")
                    saved_inputs = _input_snapshot(page, count)
                    evidence["after_save_image_inputs"] = saved_inputs
                    self.assertEqual(saved_inputs[:2] + saved_inputs[3:], baseline_inputs[:2] + baseline_inputs[3:])
                    self.assertEqual((root / "provider-calls.jsonl").read_text(encoding="utf-8"), baseline_provider_calls)
                    evidence["completed_stages"].append("single_shot_saved_and_other_business_fields_preserved")

                    page.goto(f"{route}/review")
                    page.locator("#formal-review-start").click()
                    page.locator("#formal-review-state", has_text="审核中").wait_for()
                    _approve_remaining_review(page, root / "workspace.sqlite3")
                    page.locator("#formal-review-state", has_text="待整片确认").wait_for()
                    _final_review_note(page).fill("已核对第 3 镜后页参考图与其余镜头，确认整片及来源。")
                    page.locator("#formal-review-final-submit").click()
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    excel, pdf = self._download(page, root, "before-restart")
                    self._assert_deliverables(root, excel, pdf, count, later["content_sha256"])
                    evidence["completed_stages"].append("review_confirmed_and_excel_pdf_downloaded")
                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{route}/review")
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    again_excel, again_pdf = self._download(page, root, "after-restart")
                    self.assertEqual((_sha(again_excel), _sha(again_pdf)), (_sha(excel), _sha(pdf)))
                    restored = _saved_version(root, project_id)
                    self.assertEqual(restored["decisions"], child["decisions"])
                    self.assertEqual(restored["binding"], child["binding"])
                    self.assertEqual(errors, [])
                    evidence.update(status="passed", before_version=baseline["version"], after_version=child["version"],
                        excel_sha256=_sha(excel), pdf_sha256=_sha(pdf), non_target_shots_compared=count - 1)
                    evidence["completed_stages"].append("restart_preserved_snapshot_and_download_hashes")
                    page.screenshot(path=str(root / "review-delivery-after-restart.png"), full_page=True)
                    context.close()
                    browser.close()
            except Exception as exc:
                evidence.update(status="failed", error_type=type(exc).__name__, error=str(exc))
                raise
            finally:
                server.stop()
                (root / "single-shot-library-result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _create_project(self, page, base_url):
        page.goto(f"{base_url}/projects")
        page.locator("#formal-owner-confirm").check()
        page.locator("#formal-owner-enable").click()
        expect(page.locator("#formal-owner-onboarding")).not_to_be_visible()
        page.locator("#formal-project-new").click()
        page.locator("#formal-project-name").fill("匿名单镜后页选图验收")
        with page.expect_response(lambda r: r.request.method == "POST" and "/projects" in r.url) as created:
            page.locator("#formal-project-create button[type='submit']").click()
        self.assertEqual(created.value.status, 200, created.value.text())
        project_id = created.value.json()["project_id"]
        browser_fixture._open_new_project_brief(page)
        return project_id

    def _adopt_starting_library(self, page):
        with page.expect_response(lambda r: "/images/default-candidates?" in r.url) as loaded:
            page.locator("[data-load-default-images]").click()
        self.assertEqual(loaded.value.status, 200, loaded.value.text())
        payload = loaded.value.json()
        panel = page.locator("[data-default-image-panel]")
        with page.expect_response(lambda r: "/images/default-candidates?" in r.url and parse_qs(urlparse(r.url).query).get("view") == ["recommended"]) as recommended:
            panel.locator("[data-library-view='recommended']").click()
        self.assertEqual(recommended.value.status, 200, recommended.value.text())
        payload = recommended.value.json()
        selected = set()
        while True:
            for candidate in payload["library_candidates"]:
                if candidate.get("duplicate_of"):
                    continue
                checkbox = panel.locator(f"[data-default-library-candidate='{candidate['candidate_key']}']")
                expect(checkbox).to_be_enabled()
                checkbox.check()
                selected.add(candidate["candidate_key"])
            if not payload.get("has_more", False):
                break
            next_page = payload["page"] + 1
            with page.expect_response(lambda r: "/images/default-candidates?" in r.url and parse_qs(urlparse(r.url).query).get("page") == [str(next_page)]) as response:
                panel.locator(f"[data-library-page='{next_page}']").click()
            self.assertEqual(response.value.status, 200, response.value.text())
            payload = response.value.json()
        self.assertGreaterEqual(len(selected), 8)
        with page.expect_response(lambda r: r.request.method == "POST" and "/images/default-proposals?" in r.url) as proposed:
            panel.locator("[data-propose-default-images]").click()
        self.assertEqual(proposed.value.status, 200, proposed.value.text())
        self.assertEqual({item["candidate_key"] for item in proposed.value.request.post_data_json["selections"]}, selected)
        with page.expect_response(lambda r: r.request.method == "POST" and "/api/agent/tasks/" in r.url and r.url.endswith("/confirm")) as adopted:
            panel.locator("[data-confirm-default-images]").click()
        self.assertEqual(adopted.value.status, 200, adopted.value.text())
        self.assertEqual(adopted.value.json()["task"]["status"], "completed", adopted.value.text())
        page.locator("#formal-image-state", has_text="全部可用").wait_for()

    def _save_images(self, page):
        try:
            expect(page.locator("#formal-image-version-save")).to_be_enabled()
        except AssertionError:
            self.fail(json.dumps({"selection_status": page.locator("#formal-image-selection-status").inner_text(),
                "scope": page.locator("#creative-image-scope").input_value(),
                "shots": _input_snapshot(page, page.locator("[data-image-shot]").count())}, ensure_ascii=False))
        with page.expect_response(lambda r: r.request.method == "POST" and "/script/save?" in r.url) as preview:
            page.locator("#formal-image-version-save").click()
        self.assertEqual(preview.value.status, 200, preview.value.text())
        proposed = preview.value.json()
        with page.expect_response(lambda r: r.request.method == "POST" and f"/api/agent/tasks/{proposed['task_id']}/confirm" in r.url, timeout=60_000) as confirmed:
            page.locator("[data-confirm-script-revision]").click()
        self.assertEqual(confirmed.value.status, 200, confirmed.value.text())
        self.assertEqual(confirmed.value.json()["task"]["status"], "completed")
        page.locator("#formal-script-save-proposal", has_text="文字与配图已保存为一个新版本").wait_for(timeout=60_000)
        page.locator('[data-creative-panel="images"]').click()
        page.locator("#formal-image-version-state", has_text="已确认").wait_for(timeout=60_000)
        expect(page.locator("[data-confirm-script-revision]")).to_have_count(0)
        return proposed["response"]["preview"]

    def _assert_only_third_changed(self, before, after, expected_sha):
        self.assertNotEqual(before["version"]["version_id"], after["version"]["version_id"])
        self.assertEqual(after["version"]["parent_version_id"], before["version"]["version_id"])
        self.assertEqual(_sha(Path(before["draft_path"])), before["draft_sha256"])
        self.assertEqual(before["draft"]["script_rows"], after["draft"]["script_rows"])
        def business_decisions(snapshot):
            return {item["shot_no"]: {key: value for key, value in item.items() if key not in {"decision_id", "decided_at"}}
                    for item in snapshot["decisions"]["decisions"]}
        first, second = business_decisions(before), business_decisions(after)
        self.assertEqual(first.keys(), second.keys())
        for decision in second.values():
            source = decision["source_location"]
            self.assertTrue(source["file"])
            self.assertTrue(source["sheet"])
            self.assertGreater(int(source["row"]), 0)
        for shot_no in first:
            if shot_no != "3":
                self.assertEqual(second[shot_no], first[shot_no], f"非目标镜头 {shot_no} 图片业务字段变化")
        self.assertEqual(second["3"]["image_sha256"], expected_sha)
        self.assertNotEqual(first["3"]["image_sha256"], second["3"]["image_sha256"])
        first_rows, second_rows = before["binding"]["script_rows"], after["binding"]["script_rows"]
        self.assertEqual(first_rows[:2] + first_rows[3:], second_rows[:2] + second_rows[3:])

    def _download(self, page, root, prefix):
        paths = []
        for kind, extension in (("excel", "xlsx"), ("pdf", "pdf")):
            path = root / f"{prefix}.{extension}"
            with page.expect_download() as download:
                page.locator(f"#formal-delivery-{kind}").click()
            download.value.save_as(path)
            paths.append(path)
        return paths

    def _assert_deliverables(self, root, excel, pdf, count, expected_sha):
        book = load_workbook(excel)
        try:
            images = sorted(book["脚本"]._images, key=lambda image: image.anchor._from.row)
            self.assertEqual(len(images), count)
            self.assertEqual(book["相邻镜头复核"].max_row - 1, count - 1)
            original = next(path for path in (root / "single-shot-gallery-v3").rglob("*") if path.is_file() and _sha(path) == expected_sha)
            with Image.open(BytesIO(images[2]._data())) as embedded, Image.open(original) as source:
                expected_pixels = source.convert("RGB").tobytes()
                self.assertEqual(embedded.convert("RGB").tobytes(), expected_pixels)
        finally:
            book.close()
        reader = PdfReader(str(pdf))
        self.assertEqual(len(reader.pages), count)
        self.assertEqual(len(reader.pages[2].images), 1)
        self.assertEqual(reader.pages[2].images[0].image.convert("RGB").tobytes(), expected_pixels)
        self.assertIn(expected_sha, reader.pages[2].extract_text())
