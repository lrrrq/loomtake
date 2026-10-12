"""Browser acceptance for two review returns becoming one immutable child."""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from urllib.parse import quote

from openpyxl import load_workbook
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfReader

from tests import test_mvp_v2_formal_browser_journey as browser_helpers
from tests import test_mvp_v2_formal_zero_image_browser_journeys as brief_helpers
from tests import test_mvp_v2_single_shot_library_journey as gallery_helpers


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(root: Path, query: str, parameters=()) -> list[dict]:
    with sqlite3.connect(f"{(root / 'workspace.sqlite3').as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(query, parameters)]


class CombinedReturnBrowserTests(unittest.TestCase):
    maxDiff = None

    def test_two_shots_return_together_then_one_proposal_child_review_and_delivery(self):
        parent = os.environ.get("MPLUS_COMBINED_RETURN_EVIDENCE_ROOT", "").strip()
        if parent:
            Path(parent).mkdir(parents=True, exist_ok=True)
        directory = (nullcontext(tempfile.mkdtemp(prefix="combined-return-", dir=parent))
                     if parent else tempfile.TemporaryDirectory(prefix="combined-return-"))
        with directory as temporary:
            root = Path(temporary)
            server = gallery_helpers._GalleryServer(root)
            evidence = {"completed_stages": [], "external_provider_calls": 0}
            page = None
            try:
                server.start()
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    evidence["page_errors"] = errors
                    gallery = gallery_helpers.SingleShotLibraryBrowserJourneyTests("runTest")
                    project_id = gallery._create_project(page, server.base_url)
                    brief = brief_helpers.FormalZeroImageBrowserJourneyTests("runTest")
                    brief._confirm_zero_image_brief(page, server.base_url, root, project_id)
                    route = f"{server.base_url}/projects/{quote(project_id)}"
                    evidence["project_id"] = project_id
                    page.goto(f"{route}/materials")
                    gallery._adopt_starting_library(page)
                    page.goto(f"{route}/plan")
                    page.locator("#formal-plan-create").click()
                    page.locator("#formal-plan-confirmation").wait_for()
                    page.locator("#formal-plan-confirm-check").check()
                    with page.expect_response(lambda r: r.request.method == "POST" and "/plans/" in r.url and "/confirm" in r.url, timeout=90_000) as generated:
                        page.locator("#formal-plan-confirm").click()
                    self.assertEqual(generated.value.status, 200, generated.value.text())
                    page.locator("#formal-plan-result").wait_for(timeout=90_000)
                    with page.expect_response(lambda r: "/image-workspace?" in r.url and r.request.method == "GET") as workspace:
                        page.goto(f"{route}/shot-images")
                    self.assertEqual(workspace.value.status, 200, workspace.value.text())
                    count = len(workspace.value.json()["shots"])
                    self.assertGreaterEqual(count, 8)
                    evidence["shot_count"] = count
                    page.locator("[data-image-shot='0']").wait_for()
                    browser_helpers._show_all_image_shots(page)
                    for index in range(count):
                        selector = page.locator(f"[data-image-shot-select='{index}']")
                        if not selector.input_value():
                            candidate = selector.locator('option:not([value=""]):not([disabled])').first
                            selector.select_option(candidate.get_attribute("value"))
                    for index in range(count):
                        browser_helpers._use_image_and_continue(page, index)
                    with page.expect_response(lambda r: r.request.method == "POST" and "/script/save?" in r.url) as image_proposal:
                        page.locator("#formal-image-version-save").click()
                    self.assertEqual(image_proposal.value.status, 200, image_proposal.value.text())
                    with page.expect_response(lambda r: r.request.method == "POST" and "/api/agent/tasks/" in r.url and "/confirm" in r.url, timeout=90_000) as image_saved:
                        page.locator("[data-confirm-script-revision]").click()
                    self.assertEqual(image_saved.value.status, 200, image_saved.value.text())
                    self.assertEqual(image_saved.value.json()["task"]["status"], "completed")
                    page.locator("#formal-script-save-proposal", has_text="文字与配图已保存为一个新版本").wait_for()
                    baseline = gallery_helpers._saved_version(root, project_id)
                    evidence["parent_version"] = baseline["version"]
                    evidence["completed_stages"].append("generated_and_image_version_saved")

                    page.goto(f"{route}/review")
                    with page.expect_response(lambda r: r.request.method == "POST" and "/review/start" in r.url) as started:
                        page.locator("#formal-review-start").click()
                    self.assertEqual(started.value.status, 200, started.value.text())
                    review = started.value.json()
                    targets = {"4", "7"}
                    # Image-save children have both a change item and a quality
                    # item per shot. Return the two content-change items while
                    # separately retaining the actual image-quality approvals.
                    indices = [index for index, issue in enumerate(review["issues"])
                               if issue["shot_no"] in targets and issue["kind"] == "change"]
                    self.assertEqual(len(indices), 2, review["issues"])
                    browser_helpers._expand_other_review_items(page)
                    for control in page.locator("[data-review-select]").all():
                        if int(control.get_attribute("data-review-select")) not in indices:
                            control.check()
                    with page.expect_response(lambda r: r.request.method == "POST" and "/review/decisions" in r.url) as approved:
                        page.locator("#formal-review-batch-approve").click()
                    self.assertEqual(approved.value.status, 200, approved.value.text())
                    browser_helpers._expand_other_review_items(page)
                    for index in indices:
                        page.locator(f"[data-review-select='{index}']").check()
                    note = "第4镜补充现场掌声承接，第7镜降低音乐并保留现场声；两镜合并修订后一起复核。"
                    browser_helpers._open_details(page, "details.creative-review-return")
                    page.locator("#creative-review-return-note").fill(note)
                    expect(page.locator("#formal-review-batch-count")).to_have_text("已选 2 项")
                    with page.expect_response(lambda r: r.request.method == "POST" and "/review/decisions" in r.url) as returned:
                        page.locator("#creative-review-batch-return").click()
                    self.assertEqual(returned.value.status, 200, returned.value.text())
                    self.assertEqual(returned.value.request.post_data_json["decision"], "return")
                    self.assertEqual(len(returned.value.request.post_data_json["issue_keys"]), 2)
                    return_model = returned.value.json()
                    self.assertEqual({issue["shot_no"] for issue in return_model["issues"] if issue["status"] == "return"}, targets)
                    self.assertEqual(gallery_helpers._saved_version(root, project_id)["version"], baseline["version"])
                    evidence["combined_return"] = {"request": returned.value.request.post_data_json, "review": return_model}
                    page.screenshot(path=str(root / "01-two-shots-returned.png"), full_page=True)
                    evidence["completed_stages"].append("two_shots_returned_in_one_visible_batch")

                    page.locator("#formal-review-revision-proposal a", has_text="前往脚本与版本").click()
                    browser_helpers._show_script_table(page)
                    values = {"4": "现场掌声自然接入轻节奏音乐", "7": "音乐降低，保留现场交流声与短暂停顿"}
                    for shot_no, value in values.items():
                        with page.expect_response(lambda r: r.request.method == "POST" and "/script/table" in r.url) as changed:
                            page.locator(f"[data-script-shot='{shot_no}'][data-script-field='sound']").fill(value)
                            page.locator(f"[data-script-shot='{shot_no}'][data-script-field='sound']").press("Tab")
                        self.assertEqual(changed.value.status, 200, changed.value.text())
                    before_proposal = _rows(root, "SELECT version_id FROM project_versions WHERE project_id=?", (project_id,))
                    self.assertEqual(gallery_helpers._saved_version(root, project_id)["version"], baseline["version"])
                    browser_helpers._open_details(page, "#creative-save")
                    page.locator("#formal-script-save-reason").fill(note)
                    with page.expect_response(lambda r: r.request.method == "POST" and "/script/save?" in r.url) as proposed:
                        page.locator("#formal-script-save-form button[type='submit']").click()
                    self.assertEqual(proposed.value.status, 200, proposed.value.text())
                    proposal = proposed.value.json()
                    self.assertEqual(set(proposal["response"]["affected_shots"]), targets)
                    self.assertEqual(proposal["response"]["action"], "apply_formal_script_revision")
                    self.assertFalse(proposal["response"].get("draft_only", False))
                    self.assertEqual(_rows(root, "SELECT version_id FROM project_versions WHERE project_id=?", (project_id,)), before_proposal)
                    page.screenshot(path=str(root / "02-one-combined-proposal.png"), full_page=True)
                    with page.expect_response(lambda r: r.request.method == "POST" and f"/api/agent/tasks/{proposal['task_id']}/confirm" in r.url, timeout=90_000) as saved:
                        page.locator("[data-confirm-script-revision]").click()
                    self.assertEqual(saved.value.status, 200, saved.value.text())
                    self.assertEqual(saved.value.json()["task"]["status"], "completed", saved.value.text())
                    page.locator("#formal-script-save-proposal", has_text="文字与配图已保存为一个新版本").wait_for()
                    child = gallery_helpers._saved_version(root, project_id)
                    self.assertEqual(child["version"]["parent_version_id"], baseline["version"]["version_id"])
                    self.assertEqual(len(_rows(root, "SELECT version_id FROM project_versions WHERE project_id=?", (project_id,))), len(before_proposal) + 1)
                    children = _rows(root, "SELECT * FROM project_versions WHERE parent_version_id=?", (baseline["version"]["version_id"],))
                    self.assertEqual(len(children), 1)
                    self.assertEqual(_sha(Path(baseline["draft_path"])), baseline["draft_sha256"])
                    self.assertEqual(len(child["draft"]["script_rows"]), count)
                    for before, after in zip(baseline["draft"]["script_rows"], child["draft"]["script_rows"]):
                        shot_no = str(before["镜号"])
                        expected = dict(before)
                        if shot_no in values:
                            expected["声音"] = values[shot_no]
                        self.assertEqual(after, expected, f"镜头 {shot_no} 出现非预期内容变化")
                    evidence.update(proposal=proposal, confirmed=saved.value.json(), child_version=child["version"])
                    evidence["completed_stages"].append("one_proposal_confirmation_created_exactly_one_child")

                    with page.expect_response(lambda r: r.request.method == "GET" and "/review?" in r.url) as previewed:
                        page.goto(f"{route}/review")
                    self.assertEqual(previewed.value.status, 200, previewed.value.text())
                    preview = previewed.value.json()
                    changes = [issue for issue in preview["issues"] if issue["kind"] == "change"]
                    self.assertEqual({issue["shot_no"] for issue in changes}, targets, preview)
                    self.assertTrue(all(issue["focus_required"] for issue in changes), changes)
                    self.assertEqual({edge for issue in changes for edge in issue["affected_adjacent_edges"]}, {"3->4", "4->5", "6->7", "7->8"})
                    evidence["child_review_preview"] = preview
                    page.screenshot(path=str(root / "03-focused-two-shot-review.png"), full_page=True)
                    page.locator("#formal-review-start").click()
                    page.locator("#formal-review-state", has_text="审核中").wait_for()
                    browser_helpers._approve_remaining_review(page, root / "workspace.sqlite3")
                    page.locator("#formal-review-state", has_text="待整片确认").wait_for()
                    browser_helpers._final_review_note(page).fill("第4/7镜和3→4、4→5、6→7、7→8声音衔接已复核，整片确认。")
                    page.locator("#formal-review-final-submit").click()
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    excel, pdf = gallery._download(page, root, "combined-return-delivery")
                    self._assert_deliverables(excel, pdf, count, values)
                    evidence.update(excel_sha256=_sha(excel), pdf_sha256=_sha(pdf))
                    evidence["completed_stages"].append("focused_review_and_actual_excel_pdf_delivery")
                    page.screenshot(path=str(root / "04-confirmed-delivery.png"), full_page=True)
                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{route}/review")
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    again_excel, again_pdf = gallery._download(page, root, "combined-return-after-restart")
                    self.assertEqual((_sha(excel), _sha(pdf)), (_sha(again_excel), _sha(again_pdf)))
                    self.assertEqual(gallery_helpers._saved_version(root, project_id)["version"], child["version"])
                    self.assertEqual(errors, [])
                    evidence["completed_stages"].append("restart_preserved_one_child_and_identical_deliverables")
                    evidence["status"] = "passed"
                    context.close()
                    browser.close()
            except Exception as exc:
                evidence.update(status="failed", error_type=type(exc).__name__, error=str(exc))
                if page and not page.is_closed():
                    try:
                        page.screenshot(path=str(root / "failure.png"), full_page=True)
                    except Exception:
                        pass
                raise
            finally:
                server.stop()
                (root / "combined-return-result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _assert_deliverables(self, excel: Path, pdf: Path, count: int, sounds: dict[str, str]) -> None:
        book = load_workbook(excel)
        try:
            script = book["脚本"]
            header = next(row for row in script.iter_rows() if "画面描述" in [cell.value for cell in row] and "声音" in [cell.value for cell in row])
            columns = {cell.value: cell.column for cell in header if cell.value}
            # Parent layout commonly merges 镜号 over rows 5 and 6.
            number_column = columns.get("镜号") or next(cell.column for row in script.iter_rows(min_row=1, max_row=header[0].row) for cell in row if cell.value == "镜号")
            data = {str(script.cell(row, number_column).value): row for row in range(header[0].row + 1, script.max_row + 1) if script.cell(row, number_column).value is not None}
            self.assertEqual(len(data), count)
            for shot_no, sound in sounds.items():
                self.assertEqual(script.cell(data[shot_no], columns["声音"]).value, sound)
            self.assertEqual(len(script._images), count)
            self.assertEqual(book["相邻镜头复核"].max_row - 1, count - 1)
        finally:
            book.close()
        reader = PdfReader(str(pdf))
        self.assertEqual(len(reader.pages), count)
        for shot_no, sound in sounds.items():
            self.assertIn(sound, reader.pages[int(shot_no) - 1].extract_text())


if __name__ == "__main__":
    unittest.main()
