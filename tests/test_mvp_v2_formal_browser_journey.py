from __future__ import annotations

from contextlib import contextmanager
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, expect, sync_playwright

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.review_package import export_review_workbook
from tests.support.formal_control_evidence import FormalControlEvidenceRecorder


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SERVER_SCRIPT = REPOSITORY_ROOT / "tests" / "support" / "formal_console_fixture_server.py"


def _enable_local_workspace(page, root: Path):
    """Preserve the actual activation failure instead of hiding it with retries."""
    responses, failures = [], []

    def on_response(response):
        if urlparse(response.url).path.startswith("/api/formal/setup"):
            responses.append({"path": urlparse(response.url).path, "status": response.status})

    def on_failure(request):
        failures.append({"path": urlparse(request.url).path, "failure": request.failure})

    page.on("response", on_response)
    page.on("requestfailed", on_failure)
    try:
        page.locator("#formal-owner-confirm").check()
        page.locator("#formal-owner-enable").click()
    except PlaywrightTimeoutError:
        evidence = root / "onboarding-failure"
        evidence.mkdir(exist_ok=True)
        state = page.locator("#formal-owner-confirm").evaluate_all("""items => items.map(el => ({
          checked: el.checked, disabled: el.disabled, connected: el.isConnected,
          rect: el.getBoundingClientRect().toJSON(), visibility: getComputedStyle(el).visibility
        }))""")
        (evidence / "state.json").write_text(json.dumps({
            "path": urlparse(page.url).path, "checkboxes": state,
            "responses": responses, "request_failures": failures,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        page.screenshot(path=evidence / "page.png", full_page=True)
        raise
    finally:
        page.remove_listener("response", on_response)
        page.remove_listener("requestfailed", on_failure)


def _open_new_project_brief(page):
    """Start the existing Formal journey through the current visible navigation."""
    page.wait_for_url("**/projects/*/brief")
    page.locator("#formal-brief-upload").wait_for()


@contextmanager
def _buffered_image_confirm_response(page):
    """Retain the actual large JSON response in this page's bounded CDP session."""
    session = page.context.new_cdp_session(page)
    requests = {}
    responses = {}
    finished = set()

    def on_request(event):
        request = event["request"]
        path = urlparse(request["url"]).path
        if (request["method"] == "POST" and "/api/agent/tasks/" in path
                and path.endswith("/confirm")):
            requests[event["requestId"]] = {"method": request["method"], "url": request["url"]}

    def on_response(event):
        if event["requestId"] in requests:
            responses[event["requestId"]] = event["response"]

    session.on("Network.requestWillBeSent", on_request)
    session.on("Network.responseReceived", on_response)
    session.on("Network.loadingFinished", lambda event: finished.add(event["requestId"]))
    session.send("Network.enable", {
        "maxTotalBufferSize": 256 * 1024 * 1024,
        "maxResourceBufferSize": 64 * 1024 * 1024,
    })
    result = {}
    try:
        yield result
        assert len(requests) == len(responses) == 1, "必须捕获唯一的实际图片确认 POST"
        request_id, response = next(iter(responses.items()))
        deadline = time.monotonic() + 30
        while request_id not in finished and time.monotonic() < deadline:
            page.wait_for_timeout(50)
        assert request_id in finished, "实际图片确认响应尚未完成"
        body = session.send("Network.getResponseBody", {"requestId": request_id})
        raw = base64.b64decode(body["body"]) if body["base64Encoded"] else body["body"].encode("utf-8")
        result.update(request_id=request_id, **requests[request_id], status=response["status"],
                      raw_body=raw, payload=json.loads(raw), sha256=hashlib.sha256(raw).hexdigest())
    finally:
        session.detach()


@contextmanager
def _browser_journey_root():
    configured = os.environ.get("MPLUS_FORMAL_MAIN_BROWSER_ROOT", "").strip()
    if not configured:
        evidence_root = os.environ.get("MPLUS_V3_EVIDENCE_ROOT", "").strip()
        if evidence_root:
            parent = Path(evidence_root).expanduser().resolve()
            parent.mkdir(parents=True, exist_ok=True)
            yield Path(tempfile.mkdtemp(prefix="formal-browser-", dir=parent))
            return
        with tempfile.TemporaryDirectory(prefix="mplus-formal-browser-e2e-") as temporary:
            yield Path(temporary)
        return
    root = Path(configured).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise AssertionError(f"浏览器证据根目录必须为空，拒绝覆盖：{root}")
    yield root


def _expand_image_controls(page, index: int, kind: str = "temporary") -> None:
    details = page.locator(
        f"[data-image-shot='{index}'] details.ui00-image-{'temporary-options' if kind == 'temporary' else 'search'}"
    )
    if details.get_attribute("open") is None:
        details.locator("summary").click()


def _open_details(page, selector: str) -> None:
    details = page.locator(selector)
    if details.get_attribute("open") is None:
        details.locator("summary").first.click()


def _show_script_table(page) -> None:
    page.locator('[data-creative-edit-mode="table"]').click()
    expect(page.locator('[data-creative-edit-mode="table"]')).to_have_attribute("aria-pressed", "true")


def _save_unified_images(page):
    with page.expect_response(lambda response: response.request.method == "POST" and "/script/save?" in response.url, timeout=30_000) as preview:
        page.locator("#formal-image-version-save").click()
    assert preview.value.status == 200, preview.value.text()
    proposal = preview.value.json()
    with page.expect_response(lambda response: response.request.method == "POST" and f"/api/agent/tasks/{proposal['task_id']}/confirm" in response.url, timeout=90_000) as saved:
        page.locator("[data-confirm-script-revision]").click()
    assert saved.value.status == 200, saved.value.text()
    assert saved.value.json()["task"]["status"] == "completed", saved.value.text()
    page.locator("#formal-script-save-proposal", has_text="文字与配图已保存为一个新版本").wait_for(timeout=30_000)
    page.locator('[data-creative-panel="images"]').click()
    _show_all_image_shots(page)
    page.locator("#formal-image-version-state", has_text="已确认").wait_for(timeout=30_000)
    return proposal


def _show_all_image_shots(page) -> None:
    page.locator("#creative-image-scope").select_option("all")
    expect(page.locator("#creative-image-scope")).to_have_value("all")


def _open_project_materials(page) -> None:
    page.locator('#formal-project-navigation a[data-route="materials"]').click()
    expect(page.locator("#page-materials")).to_be_visible()


def _wait_for_creative_surface(page, project_id: str, panel: str, *, aux: str = "", focus_shot: str = "") -> None:
    def matches(url: str) -> bool:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        return (parsed.path == f"/projects/{quote(project_id)}/script"
                and params.get("panel") == [panel]
                and params.get("aux", [""]) == [aux]
                and (not focus_shot or params.get("focus_shot") == [focus_shot]))
    page.wait_for_url(matches)


def _open_project_images(page) -> None:
    page.locator('nav[aria-label="项目功能"] a[data-route="script"]').click()
    page.locator('[data-creative-panel="images"]').click()
    _show_all_image_shots(page)


def _saved_version_workbook(root: Path, project_id: str) -> Path:
    with sqlite3.connect((root / "workspace.sqlite3").as_uri() + "?mode=ro", uri=True) as database:
        run_id = database.execute(
            "SELECT v.run_id FROM projects p JOIN project_versions v ON v.version_id=p.current_version_id WHERE p.project_id=?",
            (project_id,),
        ).fetchone()[0]
    return RunArtifactResolver(root / "runs").resolve_artifact(run_id, "workbook").path


def _assert_review_workbook_matches_saved_version(path, source: Path, shot_count: int, version_no: int) -> None:
    original, reviewed = load_workbook(source), load_workbook(path)
    try:
        assert reviewed.sheetnames == original.sheetnames
        before, after = original["脚本"], reviewed["脚本"]
        assert before["A1"].value in after["A1"].value
        assert f"复核稿，非正式交付 · 版本 {version_no}" in after["A1"].value
        assert before.max_column >= 11
        assert (after.max_row, after.max_column) == (before.max_row, before.max_column)
        assert sorted(map(str, after.merged_cells.ranges)) == sorted(map(str, before.merged_cells.ranges))
        assert [str(after.cell(row, 2).value) for row in range(7, after.max_row + 1)] == [str(number) for number in range(1, shot_count + 1)]
        for row in before:
            for cell in row:
                if cell.coordinate != "A1":
                    value = cell.value
                    if isinstance(value, str) and (value.startswith(("/", "~/", "file://"))
                                                  or "artifact://" in value.casefold() or "sha256=" in value.casefold()):
                        value = None
                    assert after[cell.coordinate].value == value, cell.coordinate

        def images(sheet):
            records = []
            for image in sheet._images:
                marker, extent = image.anchor._from, image.anchor.ext
                with Image.open(BytesIO(image._data())) as pixels:
                    records.append((marker.row, marker.col, marker.rowOff, marker.colOff,
                                    extent.cx, extent.cy, pixels.size,
                                    hashlib.sha256(pixels.convert("RGBA").tobytes()).hexdigest()))
            return sorted(records)

        before_images, after_images = images(before), images(after)
        assert after_images == before_images
        shot_rows = set(range(6, 6 + shot_count))
        assert shot_rows - {item[0] for item in after_images} == shot_rows - {item[0] for item in before_images}
    finally:
        original.close()
        reviewed.close()


def _capture_low_frequency_keyboard_journeys(page, base_url: str, project_id: str, root: Path, shot_count: int) -> None:
    """Real Formal service evidence; every menu item is entered by keyboard."""
    directory = root / "low-frequency-keyboard"
    directory.mkdir(exist_ok=True)
    evidence = []
    script_url = f"{base_url}/projects/{quote(project_id)}/script"

    def activate(selector: str, *, tab_from_current: bool = False) -> None:
        control = page.locator(selector)
        expect(control).to_be_visible()
        expect(control).to_be_enabled()
        if tab_from_current:
            for _ in range(12):
                page.keyboard.press("Tab")
                if control.evaluate("element => element === document.activeElement"):
                    break
        else:
            control.focus()
        expect(control).to_be_focused()
        page.keyboard.press("Enter")

    def enter_more(selector: str) -> None:
        page.goto(script_url)
        expect(page.locator("#formal-script-version-label")).to_contain_text("版本 3")
        if "data-route=" in selector or 'a[href="/tasks"]' in selector:
            suffix = selector.removeprefix(".creative-more ")
            if page.locator("#formal-mobile-menu").is_visible():
                activate("#formal-mobile-menu > summary")
                selector = f"#formal-mobile-menu {suffix}"
            else:
                selector = f".sidebar {suffix}"
            activate(selector)
        else:
            activate(".creative-more > summary")
            activate(selector, tab_from_current=True)

    def capture(name: str, width: int, selector: str, result: str) -> None:
        expect(page.locator(selector)).to_be_visible()
        page.screenshot(path=str(directory / f"{name}-{width}.png"), full_page=True)
        evidence.append({
            "entry": name, "width": width, "keyboard": "focus + Enter",
            "visible_result": result,
            "surface": urlparse(page.url).path.rsplit("/", 1)[-1],
            "service": "real Formal fixture server",
        })

    for width in (375, 1024, 1440):
        page.set_viewport_size({"width": width, "height": 1000})
        enter_more('.creative-more a[href="/tasks"]')
        expect(page.locator("#formal-task-count")).not_to_have_text("读取中")
        expect(page.locator("#formal-task-list")).to_contain_text("规划")
        capture("tasks", width, "#formal-task-list", "真实任务列表与处理入口")

        enter_more('.creative-more a[data-route="overview"]')
        expect(page.locator("#formal-overview-state")).to_contain_text("匿名纵向闭环项目", timeout=30_000)
        expect(page.locator("#formal-overview-state .ui00-overview-stages")).to_contain_text("脚本")
        capture("overview", width, "#formal-overview-state", "当前真实项目、阶段状态与下一步")

        enter_more('.creative-more a[data-route="assistant"]')
        expect(page.locator("#agent-session-status")).to_have_text("会话已连接")
        expect(page.locator("#agent-project-name")).to_have_text("匿名纵向闭环项目")
        expect(page.locator("#agent-version-state")).to_have_text("已有版本")
        expect(page.locator("#agent-message-submit")).to_be_disabled()
        capture("assistant", width, "#agent-layout", "原项目助手会话、当前项目及版本上下文")

        enter_more('.creative-more [data-creative-open-tool="records"]')
        expect(page.locator(".ui00-script-versions")).to_have_attribute("open", "")
        expect(page.locator("#formal-script-versions .ui00-script-version-item")).to_have_count(3)
        expect(page.locator("#formal-script-diff")).to_contain_text("版本 2 → 版本 3")
        capture("records", width, "#formal-script-diff", "三版记录及实际字段差异依据")

        enter_more('.creative-more a[data-route="materials"]')
        expect(page.locator("#formal-image-total")).not_to_have_text("—")
        expect(page.locator("[data-formal-image-key]").first).to_be_visible()
        capture("materials", width, "#formal-image-grid", "项目素材与上传分析入口")

        enter_more('.creative-more a[data-route="brief"]')
        expect(page.locator("#formal-template-current")).not_to_have_text("正在读取模板…")
        activate("#formal-template-picker > summary")
        expect(page.locator("#formal-template-select")).to_be_enabled()
        capture("templates", width, "#formal-template-select", "实际项目模板及可选模板")

        enter_more("#formal-script-version-governance")
        expect(page.locator("#version-count")).to_contain_text("3")
        expect(page.locator("#version-tree button").first).to_be_visible()
        expect(page.locator("#version-preview tbody tr")).to_have_count(shot_count)
        activate("#version-tree button[data-version-read] >> nth=0")
        expect(page.locator("#version-preview tbody tr")).to_have_count(shot_count)
        expect(page.locator("#version-preview")).to_contain_text("历史版本，只读查看")
        page.locator("#version-left").select_option(index=1)
        page.locator("#version-right").select_option(index=2)
        activate("#version-compare")
        expect(page.locator("#version-diff .diff-metrics")).to_be_visible()
        expect(page.locator("#version-diff")).to_contain_text("现场掌声后衔接克制的节奏音乐")
        capture("versions", width, "#version-preview", "历史版本完整镜头、两版实际字段差异及有权限的治理入口")

        page.goto(script_url)
        activate(".creative-more > summary")
        with page.expect_download() as download:
            activate("#formal-script-review-workbook", tab_from_current=True)
        output = directory / f"review-{width}.xlsx"
        download.value.save_as(output)
        _assert_review_workbook_matches_saved_version(output, _saved_version_workbook(root, project_id), shot_count, 3)
        capture("review-workbook", width, "#formal-script-version-label", "真实 XLSX 复核稿且镜数与当前版本一致")

        activate("#formal-mobile-exit" if width == 375 else ".project-exit")
        expect(page).to_have_url(f"{base_url}/projects")
        expect(page.locator("#formal-project-list")).to_be_visible()
        expect(page.locator("#formal-project-list")).to_contain_text("匿名纵向闭环项目")
        activate("#formal-admin-link")
        expect(page.locator("#formal-admin-console")).to_be_visible()
        expect(page.locator("#formal-admin-schema")).not_to_have_text("—")
        capture("admin", width, "#formal-admin-console", "当前所有者权限下的真实诊断与恢复入口")

    (directory / "entries.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(script_url)
    _show_script_table(page)


def _use_image_and_continue(page, index: int) -> None:
    reason = page.locator(f"[data-image-shot-reuse-reason='{index}']")
    if reason.count() and reason.is_visible() and not reason.input_value().strip():
        reason.fill("已核对本镜内容、图片来源及必要复用")
    button = page.locator(f"[data-image-shot-use='{index}']")
    expect(button).to_be_enabled()
    button.click()
    expect(page.locator(f"[data-image-shot-confirm='{index}']")).to_be_checked()


def _expand_other_review_items(page) -> None:
    details = page.locator(".ui00-review-others")
    if details.count() and details.get_attribute("open") is None:
        details.locator("summary").first.click()


def _review_note(page, index: int):
    _expand_other_review_items(page)
    details = page.locator(f"[data-review-note-panel='{index}']")
    if details.get_attribute("open") is None:
        details.locator("summary").click()
    return page.locator(f"[data-review-note='{index}']")


def _final_review_note(page):
    details = page.locator("#formal-review-final-form details")
    if details.get_attribute("open") is None:
        details.locator("summary").click()
    return page.locator("#formal-review-final-note")


def _capture_script_flow_views(page, base_url: str, project_id: str, root: Path) -> None:
    measurements = []
    for route, selector in (
        ("script", "#formal-script-table tbody tr"),
        ("materials", "[data-formal-image-key]"),
        ("shot-images", "[data-image-shot]"),
        ("review", "#formal-review-issues .ui00-review-item"),
    ):
        page.goto(f"{base_url}/projects/{quote(project_id)}/{route}")
        page.wait_for_load_state("networkidle")
        page.locator(selector).first.wait_for()
        for width in (375, 1024, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            page.wait_for_timeout(350)
            measurements.append({
                "page": route, "width": width,
                **page.locator("html").evaluate("element => ({scrollWidth: element.scrollWidth, clientWidth: element.clientWidth})"),
            })
            page.screenshot(path=str(root / f"qingyu-{route}-{width}.png"))
    (root / "layout-measurements.json").write_text(
        json.dumps(measurements, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    page.goto(f"{base_url}/projects/{quote(project_id)}/script?panel=images")
    _show_all_image_shots(page)
    page.locator("#formal-image-version-state", has_text="已确认").wait_for()


def _approve_remaining_review(page, database: Path, *, verify_unsaved_notes: bool = False) -> None:
    _expand_other_review_items(page)
    if verify_unsaved_notes:
        indexes = [int(item.get_attribute("data-review-select")) for item in page.locator("[data-review-select]").all()]
        assert len(indexes) >= 4, "完整旅程必须覆盖多个独立审核项"
        notes = {
            indexes[1]: "已核对人物与画面关系，保留这条尚未提交的意见。",
            indexes[2]: "已核对产品信息与来源，保留另一条尚未提交的意见。",
        }
        for index, value in notes.items():
            _review_note(page, index).fill(value)
        with page.expect_response(lambda response: response.request.method == "POST" and "/review/decisions" in response.url) as response:
            page.locator(f"[data-review-decision='approve'][data-review-index='{indexes[0]}']").click()
        assert response.value.status == 200, response.value.text()
        expect(page.locator(f"[data-review-item-index='{indexes[0]}']")).to_have_attribute("data-status", "approve")
        for index, value in notes.items():
            expect(_review_note(page, index)).to_have_value(value)
        # Submit the two retained notes individually, so their text also becomes
        # audit evidence instead of being silently replaced by a batch summary.
        for index, value in notes.items():
            with page.expect_response(lambda response: response.request.method == "POST" and "/review/decisions" in response.url) as response:
                page.locator(f"[data-review-decision='approve'][data-review-index='{index}']").click()
            assert response.value.status == 200, response.value.text()
            assert response.value.request.post_data_json["note"] == value
            expect(page.locator(f"[data-review-item-index='{index}']")).to_have_attribute("data-status", "approve")
            _expand_other_review_items(page)

    _expand_other_review_items(page)
    controls = page.locator("[data-review-select]")
    if not controls.count():
        return
    for control in controls.all():
        control.check()
    selected_count = controls.count()
    assert selected_count >= 2, "批量审核必须真实覆盖多个镜头"
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        before = {row[0] for row in connection.execute("SELECT decision_id FROM version_review_human_decisions")}
    expect(page.locator("#formal-review-batch-count")).to_have_text(f"已选 {selected_count} 项")
    with page.expect_response(lambda response: response.request.method == "POST" and "/review/decisions" in response.url) as response:
        page.locator("#formal-review-batch-approve").click()
    assert response.value.status == 200, response.value.text()
    submitted = response.value.request.post_data_json
    assert submitted["decision"] == "approve"
    assert len(submitted["issue_keys"]) == selected_count
    assert len(set(submitted["issue_keys"])) == selected_count
    expect(page.locator("[data-review-select]")).to_have_count(0)
    with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        created = [dict(row) for row in connection.execute("SELECT * FROM version_review_human_decisions") if row["decision_id"] not in before]
        evidence = {row["evidence_ref"]: dict(row) for row in connection.execute("SELECT * FROM version_review_evidence")}
    assert len(created) == selected_count
    assert len({row["review_item_id"] for row in created}) == selected_count
    assert len({row["version_id"] for row in created}) == 1
    assert all(row["decision"] == "approve" and row["reviewer_actor_id"] and row["evidence_ref"].startswith("artifact://") and len(row["evidence_sha256"]) == 64 for row in created)
    for row in created:
        registered = evidence[row["evidence_ref"]]
        assert registered["evidence_sha256"] == row["evidence_sha256"]
        assert registered["version_id"] == row["version_id"]
        assert registered["created_by_actor_id"] == row["reviewer_actor_id"]


def _complete_review_workbook(path: Path) -> None:
    workbook = load_workbook(path, read_only=False, data_only=False)
    try:
        shot = workbook["剪辑复核"]
        shot_headers = {cell.value: cell.column for cell in shot[1]}
        for row in range(2, shot.max_row + 1):
            for column in (
                "结构审核", "叙事审核", "节奏审核", "创意审核", "配图审核", "总体审核",
            ):
                shot.cell(row, shot_headers[column], "通过")
            shot.cell(row, shot_headers["重大修改"], "否")
            shot.cell(row, shot_headers["审核人"], "匿名审核员")
            shot.cell(row, shot_headers["审核日期"], "2026-08-26")
        for sheet_name, columns in (
            ("相邻镜头复核", ("叙事承接", "动作衔接", "运镜衔接", "色彩衔接", "声音衔接", "总体审核")),
            ("整片复核", ("叙事弧", "节奏曲线", "音乐曲线", "高潮与收束", "总体审核")),
        ):
            sheet = workbook[sheet_name]
            headers = {cell.value: cell.column for cell in sheet[1]}
            for row in range(2, sheet.max_row + 1):
                for column in columns:
                    sheet.cell(row, headers[column], "通过")
                sheet.cell(row, headers["审核人"], "匿名审核员")
                sheet.cell(row, headers["审核日期"], "2026-08-26")
        workbook.save(path)
    finally:
        workbook.close()


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _FixtureServer:
    def __init__(self, root: Path, *, scenario: str = "", delay_script_table_ms: int = 0):
        self.root = root
        self.scenario = scenario
        self.delay_script_table_ms = delay_script_table_ms
        self.port = _free_port()
        self.process: subprocess.Popen | None = None
        self.log_path = root / "server.log"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        environment = os.environ.copy()
        environment.update({
            "MPLUS_FORMAL_FIXTURE_ROOT": str(self.root),
            "MPLUS_FORMAL_TEST_PORT": str(self.port),
            "PYTHONPATH": str(REPOSITORY_ROOT),
        })
        if self.scenario:
            environment["MPLUS_FORMAL_FIXTURE_SCENARIO"] = self.scenario
        if self.delay_script_table_ms:
            environment["MPLUS_FORMAL_DELAY_SCRIPT_TABLE_MS"] = str(self.delay_script_table_ms)
        log = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [sys.executable, "-u", str(SERVER_SCRIPT)],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        timeout_seconds = 90 if os.environ.get("MPLUS_FORMAL_REAL_DEEPSEEK") == "1" else 60
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(self.log_path.read_text(encoding="utf-8", errors="replace"))
            try:
                with urlopen(f"{self.base_url}/api/formal/setup", timeout=0.5) as response:
                    if response.status == 200:
                        return
            except (OSError, URLError):
                time.sleep(0.1)
        server_log = self.log_path.read_text(encoding="utf-8", errors="replace")
        raise AssertionError(
            f"正式浏览器 fixture 服务未在 {timeout_seconds} 秒内启动\n{server_log}"
        )

    def stop(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)

    def restart(self) -> None:
        self.stop()
        self.start()


class FormalBrowserJourneyTests(unittest.TestCase):
    def test_qingyu_star_activity_fixture_covers_eight_shots_and_image_matrix(self):
        with _browser_journey_root() as root:
            server = _FixtureServer(root, scenario="qingyu_star_activity")
            server.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000},
                        accept_downloads=True,
                    )
                    page = context.new_page()
                    page.goto(f"{server.base_url}/projects")
                    page.wait_for_load_state("networkidle")
                    _enable_local_workspace(page, root)
                    page.locator("#formal-project-new").click()
                    page.wait_for_url("**/projects/new")
                    page.locator("#formal-project-name").fill("青屿明星见面日")
                    expect(page.locator("#formal-project-category")).not_to_be_visible()
                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/projects")) as created:
                        page.locator("#formal-project-create button[type='submit']").click()
                    self.assertNotIn("category_id", created.value.request.post_data_json)
                    _open_new_project_brief(page)
                    project_id = urlparse(page.url).path.split("/")[2]
                    fixture_dir = root / "fixtures"
                    inputs = [
                        fixture_dir / name for name in (
                            "01-brief.pdf", "02-brief.docx", "03-brief.xlsx", "04-brief.pptx",
                            *(f"qingyu-project-{index:02d}.png" for index in range(1, 7)),
                        )
                    ]
                    page.locator("#formal-brief-files").set_input_files([str(path) for path in inputs])
                    page.locator("#formal-brief-upload-button").click()
                    page.locator("#formal-standard-card-state", has_text="待确认").wait_for()
                    page.locator("#formal-brief-confirm").click()
                    page.locator("#formal-standard-card-state", has_text="已确认").wait_for()
                    with sqlite3.connect(root / "workspace.sqlite3") as database:
                        self.assertEqual(database.execute("SELECT category_id FROM projects WHERE project_id=?", (project_id,)).fetchone()[0], "star_activity")

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images&aux=materials")
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#formal-image-total")).to_have_text("0")
                    with page.expect_response(lambda response: response.request.method == "POST"
                            and "/images/synchronize?" in response.url) as connected:
                        page.locator("#formal-image-synchronize").click()
                    self.assertEqual(connected.value.status, 200, connected.value.text())
                    expect(page.locator("#formal-image-total")).to_have_text("6")
                    self.assertEqual(page.locator("#formal-image-pending").inner_text(), "6")
                    page.locator("#formal-image-understand").click()
                    page.wait_for_function("state.formalImageAutoBusy === false")
                    self.assertEqual(page.locator("#formal-image-passed").inner_text(), "2")
                    self.assertEqual(page.locator("#formal-image-pending").inner_text(), "2")
                    self.assertEqual(page.locator("#formal-image-attention").inner_text(), "4")

                    pending_index = page.evaluate("""() => (state.formalImages.images || []).findIndex(image => image.status === 'pending')""")
                    self.assertGreaterEqual(pending_index, 0)
                    page.locator(".ui00-image-review summary").nth(pending_index).click()
                    failed_form = page.locator(f"[data-formal-image-form='{pending_index}']")
                    failed_form.locator("[name='status']").select_option("failed")
                    failed_form.locator("[name='caption']").fill("该图片文件损坏，无法用于正式交付")
                    failed_form.locator("[name='tags']").fill("坏图")
                    failed_form.locator("[name='subjects']").fill("不可辨认")
                    failed_form.locator("[name='scene']").fill("未知")
                    failed_form.locator("[name='purpose']").fill("不采用")
                    failed_form.locator("[name='quality']").fill("文件损坏")
                    failed_form.locator("[name='brand']").fill("青屿品牌")
                    failed_form.locator("[name='evidence']").fill("人工核对失败")
                    with page.expect_response(
                        lambda response: response.request.method == "POST" and "/analysis" in response.url,
                    ) as failed_response:
                        failed_form.locator("button[type='submit']").click()
                    self.assertEqual(failed_response.value.status, 200)
                    page.wait_for_function("state.formalImageBusy === false")
                    self.assertEqual(page.locator("#formal-image-pending").inner_text(), "1")
                    self.assertEqual(page.locator("#formal-image-attention").inner_text(), "4")

                    for _ in range(4):
                        index = page.evaluate("""() => (state.formalImages.images || []).findIndex(image => image.status !== 'pass')""")
                        self.assertGreaterEqual(index, 0)
                        details = page.locator(".ui00-image-review").nth(index)
                        if details.get_attribute("open") is None:
                            details.locator("summary").click()
                        form = page.locator(f"[data-formal-image-form='{index}']")
                        form.wait_for(state="visible")
                        form.locator("[name='status']").select_option("pass")
                        form.locator("[name='caption']").fill("青屿明星见面日现场图片")
                        form.locator("[name='tags']").fill("明星活动、粉丝互动、产品展示")
                        form.locator("[name='subjects']").fill("明星、活动参与者")
                        form.locator("[name='scene']").fill("青屿城市艺术中心")
                        form.locator("[name='purpose']").fill("支持明星活动镜头")
                        form.locator("[name='quality']").fill("画面清晰")
                        form.locator("[name='brand']").fill("青屿品牌")
                        form.locator("[name='evidence']").fill("匿名 fixture 已人工核对")
                        with page.expect_response(
                            lambda response: response.request.method == "POST" and "/analysis" in response.url,
                        ) as analysis_response:
                            form.locator("button[type='submit']").click()
                        self.assertEqual(analysis_response.value.status, 200)
                        page.wait_for_function("state.formalImageBusy === false")
                    self.assertEqual(page.locator("#formal-image-passed").inner_text(), "6")
                    self.assertEqual(page.locator("#formal-image-pending").inner_text(), "0")

                    page.locator("#formal-image-state", has_text="全部可用").wait_for()
                    page.locator("#formal-image-to-plan").click()
                    page.locator("#formal-plan-create").click()
                    page.locator("#formal-plan-confirmation").wait_for()
                    self.assertEqual(
                        page.locator("#formal-plan-shots .ui00-plan-shot").count(), 8,
                        json.dumps(page.evaluate("state.formalPlan"), ensure_ascii=False),
                    )
                    page.locator("#formal-plan-confirm-check").check()
                    page.locator("#formal-plan-confirm").click()
                    page.locator("#formal-plan-script-rows tbody tr").first.wait_for()
                    self.assertEqual(page.locator("#formal-plan-script-rows tbody tr").count(), 8)
                    page.locator("#formal-plan-to-script").click()
                    page.wait_for_url(f"**/projects/{quote(project_id)}/script")
                    page.locator("#formal-script-table tbody tr").first.wait_for()
                    _show_script_table(page)
                    self.assertEqual(page.locator("#formal-script-table tbody tr").count(), 8)

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator("#formal-image-shot-grid [data-image-shot]").first.wait_for()
                    self.assertEqual(page.locator("#formal-image-shot-grid [data-image-shot]").count(), 8)
                    candidate_count = page.locator("[data-image-shot-select] option:not([value=''])").count()
                    self.assertGreaterEqual(candidate_count, 3)
                    context.close()
                    browser.close()
            finally:
                server.stop()

    def test_qingyu_star_activity_complete_review_delivery_and_restart(self):
        self._complete_dynamic_review_delivery(8)

    def test_ten_shots_complete_review_delivery_and_restart(self):
        self._complete_dynamic_review_delivery(10)

    def _complete_dynamic_review_delivery(self, shot_count):
        with _browser_journey_root() as root:
            server = _FixtureServer(root, scenario="qingyu_star_activity")
            server.start()
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000},
                        accept_downloads=True,
                    )
                    page = context.new_page()
                    page.goto(f"{server.base_url}/projects")
                    page.wait_for_load_state("networkidle")
                    _enable_local_workspace(page, root)
                    page.locator("#formal-project-new").click()
                    page.wait_for_url("**/projects/new")
                    page.locator("#formal-project-name").fill("青屿明星见面日")
                    expect(page.locator("#formal-project-category")).not_to_be_visible()
                    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/projects")) as created:
                        page.locator("#formal-project-create button[type='submit']").click()
                    self.assertNotIn("category_id", created.value.request.post_data_json)
                    _open_new_project_brief(page)
                    project_id = urlparse(page.url).path.split("/")[2]
                    fixture_dir = root / "fixtures"
                    if shot_count != 8:
                        brief = load_workbook(fixture_dir / "03-brief.xlsx")
                        for row in brief["Brief"]:
                            for cell in row:
                                if isinstance(cell.value, str):
                                    cell.value = cell.value.replace("输出 8 镜", f"输出 {shot_count} 镜")
                        brief.save(fixture_dir / "03-brief.xlsx")
                        brief.close()
                    inputs = [
                        fixture_dir / name for name in (
                            "01-brief.pdf", "02-brief.docx", "03-brief.xlsx", "04-brief.pptx",
                            *(f"qingyu-project-{index:02d}.png" for index in range(1, 7)),
                        )
                    ]
                    page.locator("#formal-brief-files").set_input_files([str(path) for path in inputs])
                    with page.expect_response(lambda response: response.request.method == "POST"
                            and "/brief/files?" in response.url, timeout=60000) as imported:
                        page.locator("#formal-brief-upload-button").click()
                    self.assertEqual(imported.value.status, 200, imported.value.text())
                    for path in inputs:
                        expect(page.locator("#formal-brief-files-state")).to_contain_text(path.name)
                    page.locator("#formal-standard-card-state", has_text="待确认").wait_for()
                    page.locator("#formal-brief-confirm").click()
                    page.locator("#formal-standard-card-state", has_text="已确认").wait_for()
                    with sqlite3.connect(root / "workspace.sqlite3") as database:
                        self.assertEqual(database.execute("SELECT category_id FROM projects WHERE project_id=?", (project_id,)).fetchone()[0], "star_activity")

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images&aux=materials")
                    page.wait_for_load_state("networkidle")
                    expect(page.locator("#formal-image-total")).to_have_text("0")
                    with page.expect_response(lambda response: response.request.method == "POST"
                            and "/images/synchronize?" in response.url) as connected:
                        page.locator("#formal-image-synchronize").click()
                    self.assertEqual(connected.value.status, 200, connected.value.text())
                    expect(page.locator("#formal-image-total")).to_have_text("6")
                    page.locator("#formal-image-understand").click()
                    page.wait_for_function("state.formalImageAutoBusy === false")
                    self.assertEqual(page.locator("#formal-image-passed").inner_text(), "2")
                    self.assertEqual(page.locator("#formal-image-pending").inner_text(), "2")
                    self.assertEqual(page.locator("#formal-image-attention").inner_text(), "4")
                    self.assertEqual(
                        sorted(page.evaluate("""() => (state.formalImages.images || []).map(image => image.status)""")),
                        ["blocked", "low_confidence", "pass", "pass", "pending", "pending"],
                    )
                    self.assertIn(
                        "青屿明星见面日现场图片",
                        page.evaluate("state.formalImages.images.find(image => image.sources.some(source => source.label === 'qingyu-project-06.png')).summary.caption"),
                    )
                    page.locator("#formal-image-to-plan").click()
                    page.locator("#formal-plan-create").click()
                    page.locator("#formal-plan-confirmation").wait_for()
                    self.assertEqual(page.locator("#formal-plan-shots .ui00-plan-shot").count(), shot_count)
                    self.assertEqual(page.evaluate("state.formalPlan.capacity_report.requested_shot_count"), shot_count)
                    page.locator("#formal-plan-confirm-check").check()
                    page.locator("#formal-plan-confirm").click()
                    page.locator("#formal-plan-script-rows tbody tr").first.wait_for()
                    self.assertEqual(page.locator("#formal-plan-script-rows tbody tr").count(), shot_count)

                    page.locator("#formal-plan-to-script").click()
                    page.wait_for_url(f"**/projects/{quote(project_id)}/script")
                    page.locator("#formal-script-table tbody tr").first.wait_for()
                    _show_script_table(page)
                    self.assertEqual(page.locator("#formal-script-table tbody tr").count(), shot_count)
                    script_target = page.locator('[data-script-shot="1"][data-script-field="sound"]')
                    script_target.fill("青屿脚本修改后的现场声音")
                    page.wait_for_function(
                        "() => state.formalScript?.revision === 1 && state.formalScript?.shots?.[0]?.sound === '青屿脚本修改后的现场声音'"
                    )
                    self.assertEqual(page.locator("#formal-script-version-label").inner_text(), "版本 1")

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator("#formal-image-shot-grid [data-image-shot]").first.wait_for()
                    self.assertEqual(page.locator("#formal-image-shot-grid [data-image-shot]").count(), shot_count)
                    temporary_index = shot_count - 1
                    for index in range(shot_count):
                        if index == temporary_index:
                            _expand_image_controls(page, index)
                            page.locator(f"[data-image-shot-temporary='{index}']").check()
                            page.locator(f"[data-image-shot-reason='{index}']").fill(
                                "当前项目图片矩阵仍有不可用项，先以已确认 Brief 的文字依据暂准本镜。"
                            )
                            page.locator(f"[data-image-shot-reason='{index}']").press("Tab")
                            continue
                        select = page.locator(f"[data-image-shot-select='{index}']")
                        options = select.locator("option").evaluate_all("options => options.map(option => option.value).filter(Boolean)")
                        self.assertTrue(options, json.dumps(page.evaluate("state.formalImageWorkspace"), ensure_ascii=False))
                        select.select_option(options[0])
                        _use_image_and_continue(page, index)
                        reuse_reason = page.locator(f"[data-image-shot-reuse-reason='{index}']")
                        if reuse_reason.count() and reuse_reason.is_visible() and not reuse_reason.input_value().strip():
                            reuse_reason.fill("已核对候选差异并确认必要复用")
                    with page.expect_response(lambda response: response.request.method == "POST" and "/script/save?" in response.url) as incomplete:
                        page.locator("#formal-image-version-save").click()
                    self.assertEqual(incomplete.value.status, 409, incomplete.value.text())
                    self.assertEqual(incomplete.value.json()["detail"]["code"], "IMAGE_CONFIRMATION_REQUIRED")
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator(f"[data-image-shot-temporary='{temporary_index}']:checked").wait_for()
                    self.assertEqual(page.locator("#formal-script-version-label").inner_text(), "版本 1")
                    # Restore delivery by supplementing the gap shot with a
                    # real fixture image, then perform the same human analysis
                    # flow as any other project image.
                    _expand_image_controls(page, temporary_index)
                    page.locator(f"[data-image-supplement='{temporary_index}']").click()
                    page.locator("#formal-image-supplement-file").set_input_files(
                        str(fixture_dir / "qingyu-creative-distinct.png")
                    )
                    _wait_for_creative_surface(page, project_id, "images", aux="materials")
                    page.locator("#formal-image-total", has_text="7").wait_for()
                    supplement_index = page.evaluate(
                        """() => (state.formalImages.images || []).findIndex(image =>
                            (image.sources || []).some(source => source.label === 'qingyu-creative-distinct.png'))"""
                    )
                    self.assertGreaterEqual(supplement_index, 0)
                    supplement_form = page.locator(f"[data-formal-image-form='{supplement_index}']")
                    supplement_details = page.locator(f"details.ui00-image-review:has([data-formal-image-form='{supplement_index}'])")
                    if supplement_details.get_attribute("open") is None:
                        supplement_details.locator("summary").click()
                    supplement_form.locator("[name='status']").select_option("pass")
                    supplement_form.locator("[name='caption']").fill("青屿明星见面日主视觉与品牌信息收束落版")
                    supplement_form.locator("[name='tags']").fill("主视觉、KV、END、品牌信息、落版、明星活动")
                    supplement_form.locator("[name='subjects']").fill("活动主视觉、品牌标识")
                    supplement_form.locator("[name='scene']").fill("青屿城市艺术中心")
                    supplement_form.locator("[name='purpose']").fill("支持活动收束落版镜头")
                    supplement_form.locator("[name='quality']").fill("画面清晰")
                    supplement_form.locator("[name='brand']").fill("青屿品牌")
                    supplement_form.locator("[name='evidence']").fill("补充图片已人工核对像素、主体、用途与来源")
                    with page.expect_response(lambda response: response.request.method == "POST" and "/analysis" in response.url):
                        supplement_form.locator("button[type='submit']").click()
                    page.wait_for_function("state.formalImageBusy === false")
                    self.assertEqual(page.locator("#formal-image-passed").inner_text(), "3")

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator("#formal-image-shot-grid [data-image-shot]").first.wait_for()
                    page.locator(f"[data-image-shot-temporary='{temporary_index}']").uncheck()
                    replacement_select = page.locator(f"[data-image-shot-select='{temporary_index}']")
                    replacement_options = replacement_select.locator("option").evaluate_all("options => options.map(option => option.value).filter(Boolean)")
                    self.assertTrue(replacement_options, json.dumps(page.evaluate("state.formalImageWorkspace"), ensure_ascii=False))
                    replacement_select.select_option(replacement_options[0])
                    _use_image_and_continue(page, temporary_index)
                    for index in range(shot_count):
                        select = page.locator(f"[data-image-shot-select='{index}']")
                        options = select.locator("option").evaluate_all("options => options.map(option => option.value).filter(Boolean)")
                        if options:
                            select.select_option(options[0])
                        page.wait_for_function(
                            "index => !document.querySelector(`[data-image-shot-confirm=\"${index}\"]`)?.disabled",
                            arg=index,
                        )
                        _use_image_and_continue(page, index)
                        reuse_reason = page.locator(f"[data-image-shot-reuse-reason='{index}']")
                        if reuse_reason.count() and reuse_reason.is_visible() and not reuse_reason.input_value().strip():
                            reuse_reason.fill("补图后已核对候选差异并确认必要复用")
                    self.assertFalse(page.locator("#formal-image-version-save").is_disabled())
                    _save_unified_images(page)
                    page.reload()
                    if page.locator("#creative-script-panel").is_visible():
                        _show_script_table(page)
                    elif page.locator("#page-shot-images").is_visible():
                        _show_all_image_shots(page)
                    page.locator("#formal-image-version-state", has_text="已确认").wait_for()
                    self.assertEqual(page.locator("[data-image-shot-confirm]:checked").count(), shot_count)
                    _capture_script_flow_views(page, server.base_url, project_id, root)

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/review")
                    page.locator("#formal-review-state", has_text="需审核").wait_for()
                    page.locator("#formal-review-start").click()
                    page.locator("#formal-review-state", has_text="审核中").wait_for()
                    review_issue_count = page.locator("[data-review-note]").count()
                    self.assertGreaterEqual(review_issue_count, shot_count)
                    page.screenshot(path=str(root / "qingyu-review-collapsed.png"))
                    _expand_other_review_items(page)
                    page.screenshot(path=str(root / "qingyu-review-expanded.png"))
                    _review_note(page, int(page.locator("[data-review-note]").first.get_attribute("data-review-note"))).fill("第一轮发现第1镜图片需要调整，退回后重新核对。")
                    page.locator("[data-review-decision='return']").first.click()
                    page.locator("#formal-review-state", has_text="需修改").wait_for()

                    # A returned review must create a script child through the
                    # canonical script revision service before image repair.
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script")
                    _show_script_table(page)
                    page.locator("#formal-script-table tbody tr").first.wait_for()
                    returned_script = page.locator('[data-script-shot="1"][data-script-field="voiceover"]')
                    returned_script.fill("审核退回后补充的明星互动旁白")
                    returned_subtitle = page.locator('[data-script-shot="1"][data-script-field="subtitle"]')
                    returned_subtitle.fill("青屿明星见面日活动名称字幕")
                    page.wait_for_function(
                        "() => state.formalScript?.revision >= 1 && state.formalScript?.shots?.[0]?.voiceover === '审核退回后补充的明星互动旁白' && state.formalScript?.shots?.[0]?.subtitle === '青屿明星见面日活动名称字幕'"
                    )
                    self.assertEqual(page.locator("#formal-script-version-label").inner_text(), "版本 2")

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator("#formal-image-shot-grid [data-image-shot]").first.wait_for()
                    page.wait_for_function("state.formalImageWorkspace !== null")
                    # Text edits are durable working input, not yet a frozen
                    # child version. Keep saved images while requiring the
                    # unified save before this draft is reviewable again.
                    workspace = page.evaluate("state.formalImageWorkspace")
                    self.assertTrue(workspace["requires_exact_snapshot_save"], workspace)
                    self.assertFalse(workspace["review_eligible"], workspace)
                    self.assertEqual(len(workspace["shots"]), shot_count)
                    self.assertTrue(all(shot["confirmed"] for shot in workspace["shots"]), workspace)
                    self.assertTrue(all(shot["current_image"] for shot in workspace["shots"]), workspace)
                    target_index = next(
                        index for index in range(shot_count)
                        if page.locator(f"[data-image-shot-select='{index}'] option").count() > 1
                    )
                    target_select = page.locator(f"[data-image-shot-select='{target_index}']")
                    available = target_select.locator("option").evaluate_all("options => options.map(option => option.value).filter(Boolean)")
                    current = target_select.input_value()
                    replacement = next((value for value in available if value != current), None)
                    self.assertIsNotNone(replacement)
                    for index in range(shot_count):
                        select = page.locator(f"[data-image-shot-select='{index}']")
                        options = select.locator("option").evaluate_all("options => options.map(option => option.value).filter(Boolean)")
                        self.assertTrue(options, json.dumps(page.evaluate("state.formalImageWorkspace"), ensure_ascii=False))
                        select.select_option(replacement if index == target_index else options[0])
                        page.wait_for_function(
                            "index => !document.querySelector(`[data-image-shot-confirm=\"${index}\"]`)?.disabled",
                            arg=index,
                        )
                        _use_image_and_continue(page, index)
                        reuse_reason = page.locator(f"[data-image-shot-reuse-reason='{index}']")
                        if reuse_reason.count() and reuse_reason.is_visible() and not reuse_reason.input_value().strip():
                            reuse_reason.fill("审核退回后已重新核对候选与复用依据")
                    _save_unified_images(page)

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/review")
                    page.locator("#formal-review-state", has_text="需审核").wait_for()
                    page.locator("#formal-review-start").click()
                    page.locator("#formal-review-state", has_text="审核中").wait_for()
                    _approve_remaining_review(page, root / "workspace.sqlite3", verify_unsaved_notes=True)
                    page.locator("#formal-review-state", has_text="待整片确认").wait_for()
                    self.assertEqual(page.locator("#formal-review-final-check").count(), 0)
                    _final_review_note(page).fill("青屿整片脚本、图片、来源和审核退回链均已确认。")
                    page.locator("#formal-review-final-submit").click()
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()

                    excel_path = root / "qingyu-complete.xlsx"
                    with page.expect_download() as excel_download:
                        page.locator("#formal-delivery-excel").click()
                    excel_download.value.save_as(excel_path)
                    exported = load_workbook(excel_path, read_only=False, data_only=False)
                    try:
                        script_sheet = exported["脚本"]
                        script_headers = {cell.value: cell.column for cell in script_sheet[6]}
                        self.assertIn("旁白", script_headers)
                        self.assertIn("字幕", script_headers)
                        script_rows = [
                            row for row in script_sheet.iter_rows(min_row=7, max_row=script_sheet.max_row)
                            if row[1].value not in (None, "")
                        ]
                        self.assertEqual(len(script_rows), shot_count)
                        first_script = {
                            name: script_rows[0][column - 1].value
                            for name, column in script_headers.items()
                        }
                        self.assertEqual(first_script["旁白"], "审核退回后补充的明星互动旁白")
                        self.assertEqual(first_script["字幕"], "青屿明星见面日活动名称字幕")
                        self.assertEqual(len(script_sheet._images), shot_count)
                        self.assertEqual(exported["相邻镜头复核"].max_row - 1, shot_count - 1)
                        script_shot_numbers = [
                            script_sheet.cell(row, 2).value
                            for row in range(7, script_sheet.max_row + 1)
                            if script_sheet.cell(row, 2).value not in (None, "")
                        ]
                        self.assertEqual(
                            [str(value) for value in script_shot_numbers],
                            [str(index) for index in range(1, shot_count + 1)],
                        )
                        visual_sheet = exported["视觉资产"]
                        visual_headers = {cell.value: cell.column for cell in visual_sheet[1]}
                        for required in ("image_id", "media_id", "content_sha256", "来源文件", "来源 sheet", "来源行"):
                            self.assertIn(required, visual_headers)
                        connection = sqlite3.connect(root / "workspace.sqlite3")
                        connection.row_factory = sqlite3.Row
                        try:
                            snapshot = connection.execute(
                                "SELECT * FROM version_image_decision_snapshots_v3 WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
                                (project_id,),
                            ).fetchone()
                            self.assertIsNotNone(snapshot)
                            decisions = connection.execute(
                                "SELECT * FROM version_image_decisions_v3 WHERE snapshot_id=? ORDER BY CAST(shot_no AS INTEGER)",
                                (snapshot["snapshot_id"],),
                            ).fetchall()
                            self.assertEqual(len(decisions), shot_count)
                            expected_by_shot = {}
                            for decision in decisions:
                                package = connection.execute(
                                    "SELECT media_id, source_file, source_sheet, source_row FROM project_image_package_source_links WHERE project_image_id=? ORDER BY media_id LIMIT 1",
                                    (decision["project_image_id"],),
                                ).fetchone()
                                brief = connection.execute(
                                    """SELECT f.filename FROM project_image_source_links l
                                    JOIN document_images d ON d.document_image_id=l.document_image_id
                                    JOIN project_files f ON f.file_id=d.file_id
                                    WHERE l.project_image_id=? ORDER BY f.filename LIMIT 1""",
                                    (decision["project_image_id"],),
                                ).fetchone()
                                expected_by_shot[str(decision["shot_no"])] = {
                                    "image_id": str(decision["project_image_id"]),
                                    "media_id": str(package["media_id"] if package else decision["project_image_id"]),
                                    "content_sha256": str(decision["image_sha256"]),
                                    "source_file": str(package["source_file"] if package else brief["filename"]),
                                    "source_sheet": str(package["source_sheet"] if package else "图片"),
                                    "source_row": int(package["source_row"] if package else 1),
                                }
                        finally:
                            connection.close()
                        visual_rows = list(visual_sheet.iter_rows(min_row=2, max_row=visual_sheet.max_row))
                        self.assertEqual(len(visual_rows), shot_count)
                        for row in visual_rows:
                            shot_no = str(row[visual_headers["镜号"] - 1].value)
                            expected = expected_by_shot[shot_no]
                            self.assertEqual(row[visual_headers["image_id"] - 1].value, expected["image_id"])
                            self.assertEqual(row[visual_headers["media_id"] - 1].value, expected["media_id"])
                            self.assertEqual(row[visual_headers["content_sha256"] - 1].value, expected["content_sha256"])
                            self.assertEqual(row[visual_headers["来源文件"] - 1].value, expected["source_file"])
                            self.assertEqual(row[visual_headers["来源 sheet"] - 1].value, expected["source_sheet"])
                            self.assertEqual(int(row[visual_headers["来源行"] - 1].value), expected["source_row"])
                        embedded_by_row = {
                            image.anchor._from.row + 1: hashlib.sha256(image._data()).hexdigest()
                            for image in script_sheet._images
                        }
                        self.assertEqual(
                            embedded_by_row,
                            {int(shot_no) + 6: value["content_sha256"] for shot_no, value in expected_by_shot.items()},
                        )
                    finally:
                        exported.close()
                    pdf_path = root / "qingyu-complete.pdf"
                    with page.expect_download() as pdf_download:
                        page.locator("#formal-delivery-pdf").click()
                    pdf_download.value.save_as(pdf_path)
                    pdf_pages = PdfReader(str(pdf_path)).pages
                    self.assertEqual(len(pdf_pages), shot_count)
                    for page_number, pdf_page in enumerate(pdf_pages, start=1):
                        pdf_text = pdf_page.extract_text()
                        self.assertIn("旁白：", pdf_text)
                        self.assertIn("字幕：", pdf_text)
                        self.assertIn("来源：", pdf_text)
                        self.assertIn(f"shot={page_number}", pdf_text)
                        self.assertIn("image_sha256=", pdf_text)
                    excel_sha = hashlib.sha256(excel_path.read_bytes()).hexdigest()

                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/review")
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    self.assertIn("完成确认", page.locator("#formal-review-final-copy").inner_text())
                    restart_excel = root / "qingyu-complete-restart.xlsx"
                    with page.expect_download() as restart_download:
                        page.locator("#formal-delivery-excel").click()
                    restart_download.value.save_as(restart_excel)
                    self.assertEqual(hashlib.sha256(restart_excel.read_bytes()).hexdigest(), excel_sha)
                    context.close()
                    browser.close()
            finally:
                server.stop()

    def test_five_formats_images_plan_and_nonempty_script_survive_restart(self):
        if (
            os.environ.get("MPLUS_FORMAL_REAL_DEEPSEEK") == "1"
            or os.environ.get("MPLUS_FORMAL_REAL_MINIMAX") == "1"
        ):
            from mvp_v2.runtime_config import load_local_secrets

            load_local_secrets()
        if os.environ.get("MPLUS_FORMAL_REAL_DEEPSEEK") == "1":
            self.assertTrue(
                os.environ.get("DEEPSEEK_API_KEY"),
                "真实 Provider 验收已启用，但标准本地 Secret 中没有 DEEPSEEK_API_KEY",
            )
        if os.environ.get("MPLUS_FORMAL_REAL_MINIMAX") == "1":
            from mvp_v2.image_understanding import minimax_credentials_available

            self.assertTrue(
                minimax_credentials_available(os.environ.get("MINIMAX_API_KEY", ""), "mmx"),
                "真实视觉 Provider 验收已启用，但 MiniMax 凭据不可用",
            )
        with _browser_journey_root() as root:
            server = _FixtureServer(root, delay_script_table_ms=1200)
            server.start()
            recorder = FormalControlEvidenceRecorder(
                journey_id="formal-main-browser",
                database=root / "workspace.sqlite3",
            )
            try:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(
                        viewport={"width": 1440, "height": 1000},
                        accept_downloads=True,
                    )
                    page = context.new_page()
                    errors: list[str] = []
                    script_table_requests: list[dict[str, object]] = []
                    script_discard_requests: list[dict[str, object]] = []
                    def record_http_failure(response) -> None:
                        if response.status < 400:
                            return
                        try:
                            body = response.text()
                        except Exception as exc:
                            body = f"response body unavailable: {exc}"
                        with (root / "http-failures.jsonl").open("a", encoding="utf-8") as handle:
                            handle.write(json.dumps({
                                "url": response.url, "method": response.request.method,
                                "status": response.status, "body": body,
                                "page": page.url,
                            }, ensure_ascii=False) + "\n")

                    context.on("response", record_http_failure)
                    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on(
                        "request",
                        lambda request: script_table_requests.append(json.loads(request.post_data))
                        if request.method == "POST" and "/script/table" in request.url and request.post_data
                        else None,
                    )
                    page.on(
                        "request",
                        lambda request: script_discard_requests.append(json.loads(request.post_data))
                        if request.method == "POST" and "/script/discard" in request.url and request.post_data
                        else None,
                    )
                    page.goto(f"{server.base_url}/projects")
                    page.wait_for_load_state("networkidle")
                    page.locator(".skip-link").focus()
                    recorder.capture(
                        page, "M01", keyboard_selector=".skip-link",
                        double_click_selector="#main-content",
                        recovery_selector="#main-content",
                        terminal_result="跳到主要内容链接可由键盘聚焦，目标为唯一 main 工作区",
                    )
                    recorder.capture(
                        page, "M02", keyboard_selector='.workspace-nav [data-route="projects"]',
                        double_click_selector=".workspace-nav",
                        recovery_selector=".workspace-nav",
                        terminal_result="全局路由只指向统一工作台页面，未暴露迁移保留区",
                    )
                    recorder.capture(
                        page, "M05", keyboard_selector="#formal-owner-confirm",
                        double_click_selector="#formal-owner-onboarding",
                        recovery_selector="#formal-owner-onboarding",
                        terminal_result="本机工作区启用前必须先作显式所有者确认",
                    )
                    _enable_local_workspace(page, root)
                    page.locator("#formal-project-new").wait_for()
                    recorder.capture(
                        page, "M06", keyboard_selector="#formal-project-new",
                        recovery_selector="#formal-project-new",
                        terminal_result="唯一新建入口进入 /projects/new，不创建平行项目实现",
                    )
                    page.locator("#formal-project-new").click()
                    page.wait_for_url("**/projects/new")
                    page.locator("#formal-project-name").fill("匿名纵向闭环项目")
                    recorder.capture(
                        page, "M07", keyboard_selector="#formal-project-name",
                        double_click_selector="#formal-project-create",
                        recovery_selector="#formal-project-create",
                        terminal_result="新建项目只填写名称，类型在 Brief 中识别确认",
                    )
                    recorder.capture(
                        page, "M08",
                        keyboard_selector="#formal-project-create button[type='submit']",
                        double_click_selector="#formal-project-create",
                        recovery_selector="#formal-project-create",
                        terminal_result="创建和取消共用唯一新建表单，未产生重复项目写入",
                    )
                    page.locator("#formal-project-create button[type='submit']").click()
                    _open_new_project_brief(page)
                    project_id = urlparse(page.url).path.split("/")[2]
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/overview")
                    page.wait_for_load_state("networkidle")

                    recorder.capture(
                        page, "M03",
                        keyboard_selector='nav[aria-label="项目功能"] a[data-route="script"]',
                        double_click_selector='nav[aria-label="项目功能"]',
                        recovery_selector='nav[aria-label="项目功能"]',
                        terminal_result="桌面项目导航和移动菜单映射到同一项目路由集合",
                    )
                    recorder.capture(
                        page, "M11",
                        keyboard_selector='nav[aria-label="项目功能"] a[data-route="brief"]',
                        double_click_selector="#formal-overview-state",
                        recovery_selector="#formal-overview-state",
                        terminal_result="项目概览读取真实项目状态并给出唯一下一步",
                    )
                    _open_details(page, "#formal-project-lifecycle")
                    lifecycle_form = page.locator("#formal-lifecycle-form")
                    lifecycle_form.locator("textarea").fill("浏览器验收：验证可逆归档")
                    lifecycle_form.locator("button[type='submit']").click()
                    page.locator("#formal-lifecycle-confirm").wait_for()
                    page.locator("#formal-lifecycle-confirm-check").check()
                    page.locator("#formal-lifecycle-execute").click()
                    page.locator("#formal-archive-banner").wait_for()
                    recorder.capture(
                        page, "M04", keyboard_selector="#formal-archive-manage",
                        double_click_selector="#formal-archive-banner",
                        recovery_selector="#formal-archive-banner",
                        terminal_result="归档项目保持资料可读并明确阻断新增、修改、生成与审核",
                    )
                    recorder.capture(
                        page, "M12", keyboard_selector="#formal-lifecycle-prepare",
                        double_click_selector="#formal-project-lifecycle",
                        recovery_selector="#formal-project-lifecycle",
                        terminal_result="归档 preview-confirm 已完成，项目资料与版本未被删除",
                    )
                    lifecycle_form.locator("textarea").fill("浏览器验收：恢复后继续原主路径")
                    lifecycle_form.locator("button[type='submit']").click()
                    page.locator("#formal-lifecycle-confirm").wait_for()
                    page.locator("#formal-lifecycle-confirm-check").check()
                    page.locator("#formal-lifecycle-execute").click()
                    page.wait_for_function(
                        "document.querySelector('#formal-archive-banner')?.hidden === true"
                    )
                    page.goto(f"{server.base_url}/projects")
                    page.wait_for_load_state("networkidle")
                    page.locator("#formal-project-list", has_text="匿名纵向闭环项目").wait_for()
                    recorder.capture(
                        page, "M09", keyboard_selector="#formal-project-search",
                        double_click_selector="#formal-project-search",
                        recovery_selector="#formal-project-search",
                        terminal_result="项目搜索在真实项目集合上过滤，不修改项目权威状态",
                    )
                    recorder.capture(
                        page, "M10", keyboard_selector="#formal-project-list a",
                        double_click_selector="#formal-project-list",
                        recovery_selector="#formal-project-list",
                        terminal_result="动态项目卡显示恢复后的真实项目并保留进入主路径入口",
                    )

                    assistant_session_before = page.evaluate(
                        "id => localStorage.getItem('mplus.agent.session.' + id)", project_id,
                    )
                    with sqlite3.connect(root / "workspace.sqlite3") as database:
                        assistant_sessions_before = database.execute(
                            "SELECT COUNT(*) FROM agent_sessions WHERE project_id=?", (project_id,),
                        ).fetchone()[0]
                        if assistant_session_before:
                            self.assertEqual(database.execute(
                                "SELECT project_id FROM agent_sessions WHERE agent_session_id=?",
                                (assistant_session_before,),
                            ).fetchone(), (project_id,))
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/assistant")
                    page.locator("#agent-layout").wait_for()
                    page.locator("#agent-feed", has_text="进入 Brief 与资料").wait_for()
                    page.locator("#agent-session-status", has_text="会话已连接").wait_for()
                    page.locator("#agent-feed .agent-overview > summary").click()
                    self.assertEqual(page.locator("#agent-message-form").count(), 1)
                    self.assertIn("进入 Brief 与资料", page.locator("#agent-feed").inner_text())
                    next_link = page.locator("#agent-feed .agent-overview").get_by_role(
                        "link", name="进入 Brief 与资料", exact=True,
                    )
                    self.assertTrue(next_link.is_visible())
                    self.assertEqual(next_link.get_attribute("href"), f"/projects/{quote(project_id)}/brief")
                    assistant_session_after = page.evaluate("state.agentSessionId")
                    self.assertTrue(assistant_session_after)
                    if assistant_session_before:
                        self.assertEqual(assistant_session_after, assistant_session_before)
                    with sqlite3.connect(root / "workspace.sqlite3") as database:
                        self.assertEqual(database.execute(
                            "SELECT COUNT(*) FROM agent_sessions WHERE project_id=?", (project_id,),
                        ).fetchone()[0], assistant_sessions_before + (0 if assistant_session_before else 1))
                        self.assertEqual(database.execute(
                            "SELECT project_id FROM agent_sessions WHERE agent_session_id=?",
                            (assistant_session_after,),
                        ).fetchone(), (project_id,))
                    page.locator("#agent-message").fill("请开始生成一版脚本")
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and response.url.endswith("/messages"),
                    ) as agent_response:
                        page.locator("#agent-message-submit").click()
                    self.assertEqual(agent_response.value.status, 200, agent_response.value.text() if agent_response.value.status != 200 else "")
                    page.locator("#agent-message-hint", has_text="已返回结果").wait_for()
                    page.locator("#agent-feed", has_text="请开始生成一版脚本").wait_for()
                    self.assertIn("请开始生成一版脚本", page.locator("#agent-feed").inner_text())
                    page.goto(f"{server.base_url}/admin")
                    page.locator("#formal-admin-console").wait_for()
                    self.assertFalse(page.locator("#formal-admin-link").is_hidden())
                    self.assertIn("处理异常任务、核对服务情况和审批待办", page.locator("#page-admin").inner_text())
                    page.locator("[data-admin-task]").first.click()
                    page.locator("#formal-admin-task-detail", has_text="没有可用的恢复处理器").wait_for()
                    self.assertEqual(page.locator("[data-admin-recovery-form]").count(), 0)
                    self.assertIn("原任务记录保留", page.locator("#formal-admin-task-detail").inner_text())
                    recorder.capture(
                        page, "M59", keyboard_selector="#formal-admin-refresh",
                        double_click_selector="#formal-admin-console",
                        recovery_selector="#formal-admin-console",
                        terminal_result="管理员诊断只显示脱敏 attempt/checkpoint/Provider 证据",
                    )
                    page.locator('[data-admin-section="operations"]').click()
                    page.locator("#formal-admin-operations").wait_for()
                    recorder.capture(
                        page, "M60", keyboard_selector="#formal-admin-refresh",
                        double_click_selector="#formal-admin-operations",
                        recovery_selector="#formal-admin-operations",
                        terminal_result="无恢复处理器时明确原因且不展示执行表单；旧任务保持只读",
                    )
                    page.locator('.workspace-nav a[data-route="tools"]').click()
                    expect(page.locator("#page-tools")).to_be_visible()
                    page.locator('#page-tools a[data-route="system"]').click()
                    page.locator("#system-grid").wait_for()
                    recorder.capture(
                        page, "M58",
                        keyboard_selector='.workspace-nav a[data-route="tools"]',
                        double_click_selector="#system-grid",
                        recovery_selector="#system-grid",
                        terminal_result="系统页显示当前依赖检查结果，不把开发门禁当成产品状态",
                    )

                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/brief")
                    page.wait_for_url(f"**/projects/{quote(project_id)}/brief")
                    fixture_dir = root / "fixtures"
                    inputs = [fixture_dir / name for name in (
                        "01-brief.pdf", "02-brief.docx", "03-brief.xlsx",
                        "04-brief.pptx",
                        *(f"{number:02d}-reference.png" for number in range(5, 20)),
                        "22-reference.png",
                    )]
                    page.wait_for_load_state("networkidle")
                    page.locator("#formal-brief-files").set_input_files([str(path) for path in inputs])
                    with page.expect_response(lambda response: response.request.method == "POST"
                            and "/brief/files?" in response.url, timeout=60000) as imported_files:
                        page.locator("#formal-brief-upload-button").click()
                    self.assertEqual(imported_files.value.status, 200, imported_files.value.text())
                    page.locator("#formal-standard-card-state", has_text="待确认").wait_for()
                    for path in inputs:
                        expect(page.locator("#formal-brief-files-state")).to_contain_text(path.name)
                    recorder.capture(
                        page, "M13", keyboard_selector="#formal-brief-upload-button",
                        double_click_selector="#formal-brief-upload",
                        recovery_selector="#formal-brief-upload",
                        terminal_result="五类资料累积导入后保留全部来源文件，未覆盖先前 Brief",
                    )
                    recorder.capture(
                        page, "M14",
                        keyboard_selector="#formal-conflict-list button",
                        double_click_selector="#formal-fact-list",
                        recovery_selector="#formal-fact-list",
                        terminal_result="识别事实逐条显示来源文件、页或 Sheet 与行号",
                    )
                    recorder.capture(
                        page, "M15", keyboard_selector="#formal-conflict-list button",
                        double_click_selector="#formal-conflict-list",
                        recovery_selector="#formal-conflict-list",
                        terminal_result="Brief 冲突保持待人工选择，未静默选择任一来源",
                    )
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=structure")
                    page.locator("#formal-plan-brief-block").wait_for()
                    recorder.capture(
                        page, "M17",
                        keyboard_selector="#formal-plan-brief-block a",
                        double_click_selector="#formal-plan-brief-block",
                        recovery_selector="#formal-plan-brief-block",
                        terminal_result="未确认 Brief 时规划 fail-closed，并提供返回 Brief 的恢复入口",
                    )
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/brief")
                    page.locator("#formal-standard-card-state", has_text="待确认").wait_for()

                    for _ in range(4):
                        button = page.locator("#formal-conflict-list button", has_text="确认选择").first
                        if button.count() == 0:
                            break
                        button.click()
                        button.wait_for(state="detached")
                    self.assertEqual(page.locator("#formal-conflict-list button", has_text="确认选择").count(), 0)
                    page.wait_for_function("document.querySelector('#formal-brief-confirm')?.disabled === false")
                    page.locator("#formal-brief-confirm").click()
                    page.locator("#formal-standard-card-state", has_text="已确认").wait_for()
                    self.assertIn("已确认", page.locator("#formal-brief-readiness").inner_text())
                    recorder.capture(
                        page, "M16",
                        keyboard_selector='#formal-project-navigation a[data-route="materials"]',
                        double_click_selector="#formal-brief-confirm",
                        recovery_selector="#formal-brief-confirm",
                        terminal_result="Brief 与创作标准卡已共同确认，重复确认不产生第二份权威记录",
                    )

                    _open_project_materials(page)
                    page.wait_for_url(f"**/projects/{quote(project_id)}/script?panel=images&aux=materials")
                    page.wait_for_function("document.querySelector('#formal-image-total')?.textContent !== '—'")
                    self.assertEqual(page.locator("#formal-image-total").inner_text(), "0")
                    expect(page.locator("#formal-image-synchronize")).to_be_visible()
                    with page.expect_response(lambda response: response.request.method == "POST"
                            and "/images/synchronize?" in response.url) as synchronized_images:
                        page.locator("#formal-image-synchronize").click()
                    self.assertEqual(synchronized_images.value.status, 200, synchronized_images.value.text())
                    expect(page.locator("#formal-image-total")).to_have_text("19")
                    page.wait_for_function("""() => {
                        const preview = document.querySelector('#formal-image-preview-0');
                        return preview?.querySelector('img') || preview?.textContent?.trim() !== '正在读取图片…';
                    }""")
                    self.assertEqual(
                        page.locator("#formal-image-preview-0 img").count(), 1,
                        page.locator("#formal-image-preview-0").inner_text(),
                    )
                    self.assertEqual(page.locator("#formal-image-pending").inner_text(), "19")
                    self.assertEqual(page.locator("#formal-image-to-plan").get_attribute("aria-disabled"), "true")
                    self.assertFalse((root / "provider-calls.jsonl").exists())

                    if os.environ.get("MPLUS_FORMAL_REAL_MINIMAX") == "1":
                        while int(page.locator("#formal-image-pending").inner_text()) > 0:
                            before_pending = int(page.locator("#formal-image-pending").inner_text())
                            page.locator("#formal-image-understand").click()
                            page.wait_for_function("state.formalImageAutoBusy === false", timeout=180_000)
                            after_pending = int(page.locator("#formal-image-pending").inner_text())
                            self.assertLess(
                                after_pending,
                                before_pending,
                                json.dumps({
                                    "image_state": page.evaluate("state.formalImages"),
                                    "server_log": server.log_path.read_text(
                                        encoding="utf-8", errors="replace",
                                    ),
                                }, ensure_ascii=False),
                            )
                        self.assertEqual(page.locator("#formal-image-pending").inner_text(), "0")
                        provider_generated = page.evaluate("""() =>
                          (state.formalImages?.images || []).filter(
                            image => image.summary?.evidence_status === 'provider_generated'
                          ).length
                        """)
                        self.assertEqual(provider_generated, 19)

                    visual_profiles = (
                        {
                            "caption": "无文字无品牌的蓝紫色抽象光线主视觉",
                            "tags": "抽象视觉、光线、主视觉、无品牌",
                            "subjects": "蓝紫色抽象光线",
                            "scene": "数字抽象背景",
                            "purpose": "支持匿名开场主视觉镜头",
                        },
                        {
                            "caption": "现代会议场馆外观与入口广场",
                            "tags": "场馆外观、建筑、入口、广场",
                            "subjects": "会议场馆、入口广场",
                            "scene": "现代场馆外部",
                            "purpose": "支持活动场地建立镜头",
                        },
                        {
                            "caption": "工作人员在搭建发布会舞台灯光和桁架",
                            "tags": "搭建、施工、舞台、灯光、桁架",
                            "subjects": "工作人员、舞台设备",
                            "scene": "活动搭建现场",
                            "purpose": "支持前期搭建过程镜头",
                        },
                        {
                            "caption": "完成搭建的空主舞台与大屏全景",
                            "tags": "主舞台、空场、舞台全景、大屏",
                            "subjects": "舞台、大屏、观众席",
                            "scene": "发布会主会场",
                            "purpose": "支持会场完成态和主舞台建立镜头",
                        },
                        {
                            "caption": "多位嘉宾在接待台签到并自然交流合影",
                            "tags": "签到、登记、接待台、嘉宾、人物、人群、互动、合影",
                            "subjects": "接待人员、签到嘉宾、交流人群",
                            "scene": "发布会签到区",
                            "purpose": "支持签到入场、开场造势和人物入画镜头",
                        },
                        {"caption": "嘉宾在会场内分组交流", "tags": "嘉宾、交流、社交、人群", "subjects": "交流嘉宾", "scene": "会场交流区", "purpose": "支持嘉宾交流和氛围镜头"},
                        {"caption": "参与者在桌面操作和体验产品装置", "tags": "产品体验、操作、互动、演示", "subjects": "参与者、产品装置", "scene": "产品体验区", "purpose": "支持产品演示和互动镜头"},
                        {"caption": "布置整齐的茶歇餐饮服务台", "tags": "茶歇、餐饮、服务台、饮品", "subjects": "餐饮服务台", "scene": "会场茶歇区", "purpose": "支持茶歇和现场服务镜头"},
                        {"caption": "嘉宾沿会场走廊进入活动区域", "tags": "嘉宾、人流、走廊、入场", "subjects": "行走嘉宾", "scene": "会场走廊", "purpose": "支持人流和转场镜头"},
                        {"caption": "无文字无品牌的蓝色抽象光线过渡画面", "tags": "抽象视觉、过渡、无品牌", "subjects": "蓝色抽象光线", "scene": "数字抽象背景", "purpose": "支持匿名视觉过渡镜头"},
                        {"caption": "蓝色灯光下的空主舞台与大屏全景", "tags": "主舞台、空场、舞台全景、大屏", "subjects": "舞台、大屏、空观众席", "scene": "发布会主会场", "purpose": "支持主舞台和活动空间建立镜头"},
                        {"caption": "演讲者在发布会舞台进行主题发言", "tags": "演讲者、发言、舞台、主题演讲", "subjects": "演讲者", "scene": "发布会舞台", "purpose": "支持主题宣讲镜头"},
                        {"caption": "观众在舞台前举起手机拍摄记录", "tags": "观众、手机、拍摄、记录", "subjects": "观众、手机", "scene": "舞台前观众区", "purpose": "支持观众传播和手机记录镜头"},
                        {"caption": "乐队和歌手在舞台进行现场表演", "tags": "乐队、歌手、舞台表演、演出", "subjects": "表演者、乐器", "scene": "现场演出舞台", "purpose": "支持节目表演镜头"},
                        {"caption": "嘉宾在舞台完成颁奖并交接奖杯", "tags": "颁奖、奖杯、仪式、嘉宾", "subjects": "颁奖嘉宾、获奖者、奖杯", "scene": "颁奖舞台", "purpose": "支持颁奖授牌镜头"},
                        {"caption": "无文字无品牌的紫色抽象光线片尾落版", "tags": "抽象视觉、片尾落版、收尾、无品牌", "subjects": "紫色抽象光线", "scene": "数字抽象背景", "purpose": "支持匿名片尾收束镜头"},
                        {"caption": "大型发布会场馆与观众席超广角全景", "tags": "会场全景、观众席、超广角、舞台", "subjects": "会场、舞台、观众", "scene": "大型发布会主会场", "purpose": "支持活动全貌和收束镜头"},
                        {"caption": "无文字无品牌的紫色抽象光线收尾画面", "tags": "抽象视觉、收尾、片尾、无品牌", "subjects": "紫色抽象光线", "scene": "数字抽象背景", "purpose": "支持匿名片尾收束镜头"},
                    )
                    profile_by_source = dict(zip(
                        [*(f"{number:02d}-reference.png" for number in range(5, 20)), "22-reference.png"],
                        visual_profiles,
                    ))
                    embedded_profile = {
                        "caption": "嘉宾在人群中自然交流并面向镜头合影",
                        "tags": "嘉宾、人物、人群、互动、交流、合影",
                        "subjects": "交流嘉宾、活动参与者",
                        "scene": "发布会交流区",
                        "purpose": "支持开场造势、人物入画和现场氛围镜头",
                    }
                    reviewed_images: set[str] = set()
                    while len(reviewed_images) < 19:
                        images = page.evaluate("state.formalImages?.images || []")
                        index = next((
                            row_index for row_index, image in enumerate(images)
                            if image["image_key"] not in reviewed_images
                        ), None)
                        self.assertIsNotNone(index)
                        source_label = images[index]["sources"][0]["label"]
                        profile = (
                            embedded_profile if source_label == "03-brief.xlsx"
                            else profile_by_source[source_label]
                        )
                        page.locator(".ui00-image-review summary").nth(index).click()
                        form = page.locator(f"[data-formal-image-form='{index}']")
                        form.locator("[name='status']").select_option("pass")
                        form.locator("[name='caption']").fill(profile["caption"])
                        form.locator("[name='tags']").fill(profile["tags"])
                        form.locator("[name='subjects']").fill(profile["subjects"])
                        form.locator("[name='scene']").fill(profile["scene"])
                        form.locator("[name='purpose']").fill(profile["purpose"])
                        form.locator("[name='quality']").fill("画面清晰，主体可辨认")
                        form.locator("[name='brand']").fill("无")
                        form.locator("[name='evidence']").fill("匿名 fixture 已人工核对，只用于闭环机制测试")
                        with page.expect_response(
                            lambda response: response.request.method == "POST" and "/analysis" in response.url,
                        ) as analysis_response:
                            form.locator("button[type='submit']").click()
                        self.assertEqual(
                            analysis_response.value.status,
                            200,
                            analysis_response.value.text() if analysis_response.value.status != 200 else "",
                        )
                        page.wait_for_function("state.formalImageBusy === false")
                        page.wait_for_function("""sourceLabel =>
                          (state.formalImages?.images || []).some(image =>
                            image.status === 'pass' && image.sources?.[0]?.label === sourceLabel
                          )
                        """, arg=source_label)
                        reviewed_images.add(images[index]["image_key"])
                    page.locator("#formal-image-state", has_text="全部可用").wait_for()
                    self.assertTrue(page.evaluate("document.activeElement?.classList.contains('ui00-image-card')"))
                    recorder.capture(
                        page, "M33", keyboard_selector="#formal-image-grid .ui00-image-card",
                        double_click_selector="#formal-image-grid",
                        recovery_selector="#formal-image-grid",
                        terminal_result="19 张图片均显示理解结论、内容身份及来源，不以路径猜测身份",
                    )

                    page.locator("#formal-image-to-plan").click()
                    page.locator("#formal-plan-create").wait_for()
                    recorder.capture(
                        page, "M18", keyboard_selector="#formal-plan-create",
                        double_click_selector="#formal-plan-action",
                        recovery_selector="#formal-plan-create",
                        terminal_result="图片已就绪时唯一规划入口生成正式规划；文字草稿需另行显式选择",
                    )
                    page.locator("#formal-plan-create").click()
                    page.locator("#formal-plan-confirmation").wait_for()
                    self.assertGreaterEqual(page.locator("#formal-plan-shots .ui00-plan-shot").count(), 4)
                    if page.locator("#formal-plan-capacity-details").get_attribute("open") is None:
                        page.locator("#formal-plan-capacity-details > summary").click()
                    recorder.capture(
                        page, "M19", keyboard_selector="#formal-plan-confirm-check",
                        double_click_selector="#formal-plan-decision-counts",
                        recovery_selector="#formal-plan-confirmation",
                        terminal_result="规划预览显示动态镜数、硬槽与模板推荐质量计数",
                    )
                    if page.locator("#formal-plan-shots-details").get_attribute("open") is None:
                        page.locator("#formal-plan-shots-details > summary").click()
                    page.locator("#formal-plan-shots details").first.locator("summary").click()
                    source_copy = page.locator("#formal-plan-shots details").first.inner_text()
                    self.assertIn("03-brief.xlsx", source_copy)
                    self.assertIn("资料", source_copy)
                    self.assertRegex(source_copy, r"第\s*\d+\s*行")
                    self.assertFalse((root / "provider-calls.jsonl").exists())

                    source_workbook = root / "source" / "M+发布会类通用脚本库.xlsx"
                    evidence_source_path = os.environ.get("MPLUS_FORMAL_SOURCE_WORKBOOK_PATH")
                    if evidence_source_path:
                        Path(evidence_source_path).write_bytes(source_workbook.read_bytes())
                    source_sha_before_restart = hashlib.sha256(source_workbook.read_bytes()).hexdigest()
                    page.goto("about:blank")
                    server.restart()
                    self.assertEqual(
                        hashlib.sha256(source_workbook.read_bytes()).hexdigest(),
                        source_sha_before_restart,
                    )
                    page.goto(f"{server.base_url}/projects")
                    page.wait_for_load_state("networkidle")
                    self.assertIn("匿名纵向闭环项目", page.locator("#formal-project-list").inner_text())
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=structure")
                    page.locator("#formal-plan-confirmation").wait_for()
                    self.assertIn("已恢复待确认的规划", page.locator("#formal-plan-status-copy").inner_text())
                    self.assertFalse((root / "provider-calls.jsonl").exists())
                    recorder.capture(
                        page, "M21", keyboard_selector="#formal-plan-confirm-check",
                        double_click_selector="#formal-plan-confirmation",
                        recovery_selector="#formal-plan-confirmation",
                        terminal_result="服务重启后恢复同一规划 preview，确认前不创建脚本版本",
                    )

                    page.locator("#formal-plan-confirm-check").check()
                    confirm_responses: list[dict[str, object]] = []

                    def capture_confirm(response) -> None:
                        if response.request.method != "POST" or "/plans/" not in response.url or "/confirm" not in response.url:
                            return
                        confirm_responses.append({
                            "status": response.status,
                            "body": response.text(),
                        })

                    page.on("response", capture_confirm)
                    page.locator("#formal-plan-confirm").click()
                    generation_timeout = (
                        360_000 if os.environ.get("MPLUS_FORMAL_REAL_DEEPSEEK") == "1"
                        else 30_000
                    )
                    try:
                        page.wait_for_function("""() => {
                            const result = document.querySelector('#formal-plan-result');
                            const state = document.querySelector('#formal-plan-state')?.textContent || '';
                            return !result?.hidden || state !== '正在创建脚本版本…';
                        }""", timeout=generation_timeout)
                    except PlaywrightTimeoutError:
                        self.fail(json.dumps({
                            "generation_timeout_ms": generation_timeout,
                            "state": page.locator("#formal-plan-state").inner_text(),
                            "provider_calls": (
                                (root / "provider-calls.jsonl").read_text(encoding="utf-8")
                                if (root / "provider-calls.jsonl").exists() else ""
                            ),
                            "server_log": server.log_path.read_text(
                                encoding="utf-8", errors="replace",
                            ),
                        }, ensure_ascii=False, indent=2))
                    if page.locator("#formal-plan-result").is_hidden():
                        self.fail(json.dumps({
                            "responses": confirm_responses,
                            "state": page.locator("#formal-plan-state").inner_text(),
                            "status_copy": page.locator("#formal-plan-status-copy").inner_text(),
                            "provider_calls": (root / "provider-calls.jsonl").read_text(encoding="utf-8")
                            if (root / "provider-calls.jsonl").exists() else "",
                            "server_log": server.log_path.read_text(encoding="utf-8", errors="replace"),
                        }, ensure_ascii=False, indent=2))
                    rows = page.locator("#formal-plan-script-rows tbody tr")
                    self.assertGreaterEqual(rows.count(), 4)
                    script_shot_count = rows.count()
                    self.assertIn("产品演示", page.locator("#formal-plan-script-rows").inner_text())
                    self.assertNotIn("未提供", page.locator("#formal-plan-script-rows").inner_text())
                    calls = [json.loads(line) for line in (root / "provider-calls.jsonl").read_text(encoding="utf-8").splitlines()]
                    self.assertEqual(len(calls), 2)
                    expected_model = (
                        "deepseek-v4-flash"
                        if (
                            os.environ.get("MPLUS_FORMAL_REAL_DEEPSEEK") == "1"
                            and os.environ.get("MPLUS_FORMAL_REAL_ADVISOR_ONLY") != "1"
                        )
                        else "anonymous-fixture-writer-model"
                    )
                    self.assertTrue(all(call["model"] == expected_model for call in calls))
                    self.assertTrue(all(call["shot_count"] == rows.count() for call in calls))
                    self.assertTrue(all(call["image_caption"] for call in calls))
                    if (
                        os.environ.get("MPLUS_FORMAL_REAL_DEEPSEEK") == "1"
                        and os.environ.get("MPLUS_FORMAL_REAL_ADVISOR_ONLY") != "1"
                    ):
                        print(f"REAL_PROVIDER model={expected_model} calls={len(calls)}", flush=True)

                    page.locator("#formal-plan-to-script").click()
                    page.wait_for_url(f"**/projects/{quote(project_id)}/script")
                    page.locator("#formal-script-table .ui00-script-table").wait_for()
                    _show_script_table(page)
                    self.assertEqual(page.locator("#formal-script-table tbody tr").count(), rows.count())
                    visual = page.locator('[data-script-shot="1"][data-script-field="visual"]')
                    # Q4: the server commits before the response is lost. Discard
                    # must re-read/clear the authoritative draft instead of only
                    # clearing the stale client retry state.
                    def commit_then_lose_response(route) -> None:
                        response = route.fetch()
                        route.fulfill(
                            status=503,
                            content_type="application/json",
                            body=json.dumps({
                                "detail": {
                                    "code": "RESPONSE_LOST_AFTER_COMMIT",
                                    "message": "fixture response lost after commit",
                                },
                            }),
                        )

                    page.route("**/script/table**", commit_then_lose_response)
                    lost_response_value = "服务端已提交但浏览器未收到响应"
                    visual.fill(lost_response_value)
                    page.locator("#formal-script-save-state[data-state='error']").wait_for()
                    page.once("dialog", lambda dialog: dialog.accept())
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and "/script/discard" in response.url,
                        timeout=5000,
                    ) as discard_after_commit:
                        page.locator("#formal-script-discard").click()
                    self.assertEqual(discard_after_commit.value.status, 200)
                    page.wait_for_function("() => state.formalScript?.can_save_version === false")
                    page.unroute("**/script/table**", commit_then_lose_response)
                    expect(visual).not_to_have_value(lost_response_value)
                    page.wait_for_function("() => state.formalScriptDiscarding === false")

                    # Q1: after two committed operations, returning to an old
                    # value is a new operation with a new idempotency key.
                    subtitle = page.locator('[data-script-shot="1"][data-script-field="subtitle"]')
                    subtitle_original = subtitle.input_value()
                    q1_start = len(script_table_requests)
                    q1_values = [
                        "Q1 第一次保存",
                        "Q1 第二次保存",
                        "Q1 第一次保存",
                    ]
                    q1_revision = page.evaluate("state.formalScript.revision")
                    for offset, value in enumerate(q1_values, start=1):
                        subtitle.fill(value)
                        try:
                            page.wait_for_function(
                                "expected => state.formalScript?.revision >= expected",
                                arg=q1_revision + offset,
                            )
                        except PlaywrightTimeoutError:
                            self.fail(json.dumps({
                                "q1_offset": offset,
                                "q1_revision": q1_revision,
                                "state": page.evaluate("state.formalScript"),
                                "save_state": page.locator("#formal-script-save-state").inner_text(),
                                "requests": script_table_requests[-5:],
                                "server_log": server.log_path.read_text(encoding="utf-8", errors="replace")[-8000:],
                            }, ensure_ascii=False))
                    q1_requests = script_table_requests[q1_start:]
                    self.assertEqual([item["value"] for item in q1_requests], q1_values)
                    self.assertEqual(
                        len({item["idempotency_key"] for item in q1_requests}),
                        3,
                    )
                    page.reload()
                    if page.locator("#creative-script-panel").is_visible():
                        _show_script_table(page)
                    elif page.locator("#page-shot-images").is_visible():
                        _show_all_image_shots(page)
                    page.locator("#formal-script-table .ui00-script-table").wait_for()
                    _show_script_table(page)
                    self.assertEqual(
                        page.locator('[data-script-shot="1"][data-script-field="subtitle"]').input_value(),
                        "Q1 第一次保存",
                    )

                    # Q2: an unsent edit that returns to the authoritative value
                    # is removed without blocking another cell's queued write.
                    second_subtitle = page.locator('[data-script-shot="2"][data-script-field="subtitle"]')
                    second_original = second_subtitle.input_value()
                    q2_start = len(script_table_requests)
                    second_subtitle.fill("Q2 第一格临时值")
                    page.locator('[data-script-shot="1"][data-script-field="subtitle"]').fill("Q2 第二格待保存")
                    second_subtitle.fill(second_original)
                    page.wait_for_function(
                        "expected => state.formalScript?.revision >= expected",
                        arg=q1_revision + 4,
                    )
                    q2_requests = script_table_requests[q2_start:]
                    self.assertEqual([item["shot_no"] for item in q2_requests], ["1"])
                    self.assertEqual(q2_requests[0]["value"], "Q2 第二格待保存")
                    page.reload()
                    if page.locator("#creative-script-panel").is_visible():
                        _show_script_table(page)
                    elif page.locator("#page-shot-images").is_visible():
                        _show_all_image_shots(page)
                    page.locator("#formal-script-table .ui00-script-table").wait_for()
                    _show_script_table(page)
                    self.assertEqual(
                        page.locator('[data-script-shot="2"][data-script-field="subtitle"]').input_value(),
                        second_original,
                    )

                    # Q3: an in-flight value followed by a newer value and then
                    # the original in-flight value must not save the stale newer value.
                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script")
                    _show_script_table(page)
                    page.locator("#formal-script-table .ui00-script-table").wait_for()
                    inflight = page.locator('[data-script-shot="1"][data-script-field="subtitle"]')
                    q3_start = len(script_table_requests)
                    q3_revision = page.evaluate("state.formalScript.revision")
                    inflight.fill("Q3 正在保存值")
                    page.locator("#formal-script-save-state[data-state='busy']").wait_for()
                    inflight.fill("Q3 过时排队值")
                    inflight.fill("Q3 正在保存值")
                    page.wait_for_function(
                        "expected => state.formalScript?.revision >= expected && !state.formalScriptSavePromise && !state.formalScriptSaveInFlight",
                        arg=q3_revision + 1,
                    )
                    q3_requests = script_table_requests[q3_start:]
                    self.assertEqual([item["value"] for item in q3_requests], ["Q3 正在保存值"])
                    page.reload()
                    if page.locator("#creative-script-panel").is_visible():
                        _show_script_table(page)
                    elif page.locator("#page-shot-images").is_visible():
                        _show_all_image_shots(page)
                    page.locator("#formal-script-table .ui00-script-table").wait_for()
                    _show_script_table(page)
                    self.assertEqual(inflight.input_value(), "Q3 正在保存值")

                    queue_revision_before_fast_edits = page.evaluate("state.formalScript.revision")
                    recorder.capture(
                        page, "M23", keyboard_selector='[data-script-shot="1"][data-script-field="visual"]',
                        double_click_selector="#formal-script-table",
                        recovery_selector="#formal-script-table",
                        terminal_result="逐镜表按动态 N 镜读取当前权威版本并保留每镜来源",
                    )
                    visual.fill("先写入的画面草稿")
                    page.locator("#formal-script-save-state[data-state='busy']").wait_for()
                    visual.fill("最后一次输入必须保留")
                    page.wait_for_function(
                        "revision => state.formalScript?.revision === revision + 2",
                        arg=queue_revision_before_fast_edits,
                    )
                    self.assertEqual(
                        page.locator('[data-script-shot="1"][data-script-field="visual"]').input_value(),
                        "最后一次输入必须保留",
                    )
                    self.assertEqual(
                        [item["value"] for item in script_table_requests[-2:]],
                        ["先写入的画面草稿", "最后一次输入必须保留"],
                    )
                    self.assertEqual(
                        [item["expected_revision"] for item in script_table_requests[-2:]],
                        [queue_revision_before_fast_edits, queue_revision_before_fast_edits + 1],
                    )
                    self.assertIn("版本 1", page.locator("#formal-script-version-label").inner_text())
                    page.reload()
                    if page.locator("#creative-script-panel").is_visible():
                        _show_script_table(page)
                    elif page.locator("#page-shot-images").is_visible():
                        _show_all_image_shots(page)
                    page.wait_for_function("revision => state.formalScript?.revision === revision", arg=queue_revision_before_fast_edits + 2)
                    expect(page.locator("#formal-script-revision")).to_have_text("已暂存 · 待保存版本")
                    self.assertEqual(
                        page.locator('[data-script-shot="1"][data-script-field="visual"]').input_value(),
                        "最后一次输入必须保留",
                    )

                    page.locator("#formal-script-undo").click()
                    page.wait_for_function(
                        "value => document.querySelector('[data-script-shot=\"1\"][data-script-field=\"visual\"]')?.value === value",
                        arg="先写入的画面草稿",
                    )
                    page.locator("#formal-script-redo").click()
                    page.wait_for_function(
                        "value => document.querySelector('[data-script-shot=\"1\"][data-script-field=\"visual\"]')?.value === value",
                        arg="最后一次输入必须保留",
                    )
                    recorder.capture(
                        page, "M24", keyboard_selector="#formal-script-undo",
                        double_click_selector="#formal-script-save-state",
                        recovery_selector="#formal-script-table",
                        terminal_result="撤销后恢复旧值、恢复后回到同一草稿，服务端 revision 可重载",
                    )

                    # Image decisions are progress until the single immutable save.
                    page.locator('[data-creative-panel="images"]').click()
                    _show_all_image_shots(page)
                    for index in range(script_shot_count):
                        select = page.locator(f"[data-image-shot-select='{index}']")
                        available = select.locator("option").evaluate_all(
                            "options => options.filter(option => option.value && !option.disabled).map(option => option.value)"
                        )
                        self.assertTrue(available, f"第 {index + 1} 镜须有真实候选才可保存版本")
                        select.select_option(available[0])
                    # Compute reuse across the final selection before confirming any shot.
                    for index in range(script_shot_count):
                        _use_image_and_continue(page, index)
                    page.locator('[data-creative-panel="script"]').click()
                    _show_script_table(page)
                    _open_details(page, "#creative-save")
                    page.locator("#formal-script-save-reason").fill(
                        "保存入口画面事实修订并保留原版本"
                    )
                    with page.expect_response(lambda response: response.request.method == "POST" and "/script/save?" in response.url) as first_save_preview:
                        page.locator("#formal-script-save-form button[type='submit']").click()
                    self.assertEqual(first_save_preview.value.status, 200, first_save_preview.value.text())
                    page.locator("[data-confirm-script-revision]").wait_for()
                    page.locator("[data-confirm-script-revision]").click()
                    page.locator("#formal-script-version-label", has_text="版本 2").wait_for()
                    page.locator("#formal-script-save-proposal", has_text="文字与配图已保存为一个新版本").wait_for()
                    _open_details(page, "details.ui00-script-versions")
                    page.locator(
                        "#formal-script-diff-title", has_text="版本 1 → 版本 2"
                    ).wait_for()
                    recorder.capture(
                        page, "M27", keyboard_selector="#formal-script-save-reason",
                        double_click_selector="#formal-script-save-form",
                        recovery_selector="#formal-script-save-form",
                        recovery_open_selector="#creative-save > summary",
                        artifact_not_applicable=False,
                        artifact_result="草稿物化为不可变版本 2；父版和来源 artifact 保持不变",
                        terminal_result="草稿保存经 proposal-confirm 创建版本 2，未直接覆盖版本 1",
                    )
                    self.assertIn(
                        "最后一次输入必须保留",
                        page.locator("#formal-script-diff").inner_text(),
                    )

                    _open_details(page, "#creative-assistant")
                    page.locator("#creative-assistant-scope").select_option("all")
                    page.locator("#formal-script-instruction").fill(
                        "把第2镜声音修改为现场掌声后衔接克制的节奏音乐"
                    )
                    recorder.capture(
                        page, "M25", keyboard_selector="#formal-script-instruction",
                        double_click_selector="#formal-script-instruction-form",
                        recovery_selector="#formal-script-instruction-form",
                        recovery_open_selector="#creative-assistant > summary",
                        terminal_result="按要求修改只接收明确镜号、字段和新值，不读取平行草稿核心",
                    )
                    page.locator("#formal-script-instruction-submit").click()
                    try:
                        page.locator("[data-confirm-script-revision]").wait_for()
                    except PlaywrightTimeoutError:
                        failure_screenshot = root / "ui-test-screenshots" / "formal-main-M25-proposal-timeout.png"
                        failure_screenshot.parent.mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(failure_screenshot), full_page=True)
                        print(
                            "M25 proposal timeout "
                            f"status={page.locator('#formal-script-save-state').inner_text()!r} "
                            f"proposal={page.locator('#formal-script-revision-proposal').inner_text()!r} "
                            f"submit_disabled={page.locator('#formal-script-instruction-submit').is_disabled()} "
                            f"server_log={server.log_path}",
                            flush=True,
                        )
                        raise
                    recorder.capture(
                        page, "M26", keyboard_selector="[data-confirm-script-revision]",
                        double_click_selector="#formal-script-revision-proposal",
                        recovery_selector="#formal-script-instruction-form",
                        recovery_open_selector="#creative-assistant > summary",
                        terminal_result="自然语言修订先预览，确认后只写同一草稿，不创建子版本",
                    )
                    page.locator("[data-confirm-script-revision]").click()
                    page.wait_for_function("state.formalScript?.revision === 1")
                    expect(page.locator("#formal-script-revision")).to_have_text("已暂存 · 待保存版本")
                    self.assertIn("版本 2", page.locator("#formal-script-version-label").inner_text())
                    self.assertEqual(
                        page.locator('[data-script-shot="2"][data-script-field="sound"]').input_value(),
                        "现场掌声后衔接克制的节奏音乐",
                    )
                    page.locator("#formal-script-undo").click()
                    page.wait_for_function(
                        "() => document.querySelector('[data-script-shot=\"2\"][data-script-field=\"sound\"]')?.value !== '现场掌声后衔接克制的节奏音乐'"
                    )
                    page.locator("#formal-script-redo").click()
                    page.wait_for_function(
                        "() => document.querySelector('[data-script-shot=\"2\"][data-script-field=\"sound\"]')?.value === '现场掌声后衔接克制的节奏音乐'"
                    )
                    _open_details(page, "#creative-save")
                    page.locator("#formal-script-save-reason").fill(
                        "保存声音修改并保留版本 2"
                    )
                    page.locator("#formal-script-save-form button[type='submit']").click()
                    page.locator("[data-confirm-script-revision]").wait_for()
                    page.locator("[data-confirm-script-revision]").click()
                    page.locator("#formal-script-version-label", has_text="版本 3").wait_for()

                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script")
                    _show_script_table(page)
                    page.locator("#formal-script-version-label", has_text="版本 3").wait_for()
                    self.assertEqual(
                        page.locator('[data-script-shot="2"][data-script-field="sound"]').input_value(),
                        "现场掌声后衔接克制的节奏音乐",
                    )
                    _open_details(page, "details.ui00-script-versions")
                    page.locator("#formal-script-diff-title", has_text="版本 2 → 版本 3").wait_for()
                    self.assertEqual(page.locator("#formal-script-versions .ui00-script-version-item").count(), 3)
                    self.assertEqual(page.locator("#formal-script-revision").inner_text(), "已保存版本")
                    diff_copy = page.locator("#formal-script-diff").inner_text()
                    self.assertIn("现场掌声后衔接克制的节奏音乐", diff_copy)
                    self.assertIn("其余", diff_copy)
                    self.assertIn("镜头数量、顺序、来源和图片绑定保持不变", diff_copy)
                    _capture_low_frequency_keyboard_journeys(page, server.base_url, project_id, root, script_shot_count)
                    _open_details(page, "details.ui00-script-versions")
                    failure_screenshot = root / "ui-test-screenshots" / "formal-script-adversarial-failure.png"
                    failure_screenshot.parent.mkdir(parents=True, exist_ok=True)
                    errors_before_adversarial = len(errors)
                    try:
                        # S2: a focused cell can be retracted before the debounce
                        # timer fires without creating a server-side draft.
                        retract_target = page.locator('[data-script-shot="1"][data-script-field="visual"]')
                        saved_visual = retract_target.input_value()
                        request_count_before_retract = len(script_table_requests)
                        page.once("dialog", lambda dialog: dialog.accept())
                        page.evaluate("""value => {
                          const input = document.querySelector('[data-script-shot="1"][data-script-field="visual"]');
                          input.value = value;
                          input.dispatchEvent(new Event('input', {bubbles: true}));
                          document.querySelector('#formal-script-discard').click();
                        }""", "快速撤回且不得写入服务端")
                        page.wait_for_function(
                            "saved => document.querySelector('[data-script-shot=\\\"1\\\"][data-script-field=\\\"visual\\\"]')?.value === saved",
                            arg=saved_visual,
                        )
                        self.assertEqual(len(script_table_requests), request_count_before_retract)

                        # S3: a failed save exposes a retry control and preserves
                        # the exact request body until the retry succeeds.
                        fail_once = {"done": False}

                        def fail_next_table(route) -> None:
                            if not fail_once["done"]:
                                fail_once["done"] = True
                                route.fulfill(
                                    status=503,
                                    content_type="application/json",
                                    body=json.dumps({"detail": {"code": "FIXTURE_SAVE_FAILED", "message": "fixture save failure"}}),
                                )
                            else:
                                route.continue_()

                        page.route("**/script/table**", fail_next_table)
                        retry_value = "保存失败后仍保留并可重试"
                        retract_target.fill(retry_value)
                        page.locator("#formal-script-save-state[data-state='error']").wait_for()
                        self.assertTrue(page.locator("#formal-script-retry").is_visible())
                        self.assertFalse(page.locator("#formal-script-retry").is_disabled())
                        self.assertEqual(retract_target.input_value(), retry_value)
                        page.locator("#formal-script-retry").click()
                        page.wait_for_function(
                            "value => document.querySelector('[data-script-shot=\\\"1\\\"][data-script-field=\\\"visual\\\"]')?.value === value && state.formalScript?.revision === 1",
                            arg=retry_value,
                        )
                        page.wait_for_function("() => !state.formalScriptSavePromise && !state.formalScriptSaveInFlight")
                        page.unroute("**/script/table**", fail_next_table)

                        # S4: after a failed request, explicit discard clears the
                        # unconfirmed local write without silently retrying it.
                        fail_once = {"done": False}
                        page.route("**/script/table**", fail_next_table)
                        discard_value = "失败后确认放弃的内容"
                        s4_request_start = len(script_table_requests)
                        s4_discard_start = len(script_discard_requests)
                        retract_target.fill(discard_value)
                        page.locator("#formal-script-save-state[data-state='error']").wait_for()
                        page.once("dialog", lambda dialog: dialog.accept())
                        page.wait_for_function("() => !document.querySelector('#formal-script-discard')?.disabled")
                        with page.expect_response(
                            lambda response: response.request.method == "POST"
                            and "/script/discard" in response.url
                        ) as discard_response:
                            page.evaluate("document.querySelector('#formal-script-discard')?.click()")
                        self.assertEqual(discard_response.value.status, 200, discard_response.value.text() if discard_response.value.status != 200 else "")
                        page.wait_for_function(
                            "value => document.querySelector('[data-script-shot=\\\"1\\\"][data-script-field=\\\"visual\\\"]')?.value !== value",
                            arg=discard_value,
                        )
                        page.wait_for_function("() => !state.formalScriptBusy && !state.formalScriptSaveInFlight")
                        page.wait_for_function("() => !state.formalScriptSavePromise && !state.formalScriptSaveRetryPending")
                        page.wait_for_function("() => state.formalScript?.can_save_version === false")
                        s4_requests = script_table_requests[s4_request_start:]
                        self.assertTrue(s4_requests)
                        self.assertEqual({item["idempotency_key"] for item in s4_requests}, {s4_requests[0]["idempotency_key"]})
                        self.assertEqual(len(script_discard_requests), s4_discard_start + 1)
                        self.assertFalse(page.locator("#formal-script-save-state[data-state='error']").is_visible())
                        page.unroute("**/script/table**", fail_next_table)
                    except Exception:
                        page.screenshot(path=str(failure_screenshot), full_page=True)
                        print(
                            "script adversarial failure "
                            f"state={page.evaluate('state.formalScript')} "
                            f"retry={page.locator('#formal-script-retry').is_visible()} "
                            f"requests={script_table_requests} discard={script_discard_requests}",
                            flush=True,
                        )
                        raise
                    recorder.capture(
                        page, "M22", keyboard_selector="#formal-script-versions button",
                        double_click_selector="#formal-script-versions",
                        recovery_selector="#formal-script-versions",
                        recovery_open_selector="details.ui00-script-versions > summary",
                        terminal_result="版本树保留版本 1、草稿物化版本 2 和统一草稿保存版本 3",
                    )
                    recorder.capture(
                        page, "M28", keyboard_selector="#formal-script-versions button",
                        double_click_selector="#formal-script-diff",
                        recovery_selector="#formal-script-diff",
                        recovery_open_selector="details.ui00-script-versions > summary",
                        terminal_result="版本 2 到版本 3 的 deterministic diff 只含第 2 镜声音变化",
                    )
                    self.assertEqual(
                        len((root / "provider-calls.jsonl").read_text(encoding="utf-8").splitlines()),
                        2,
                    )
                    _open_project_images(page)
                    _wait_for_creative_surface(page, project_id, "images")
                    page.locator("#formal-image-shot-grid [data-image-shot]").first.wait_for()
                    _show_all_image_shots(page)
                    image_shots = page.locator("#formal-image-shot-grid [data-image-shot]")
                    self.assertEqual(image_shots.count(), script_shot_count)
                    candidate_debug = page.evaluate("""async () => {
                      const workspace = state.formalImageWorkspace;
                      const shot = workspace?.shots?.[0];
                      const path = `/api/formal/projects/${formalRoute().projectId}/image-workspace/shots/${shot.shot_no}/candidates?workspace_id=${state.agentWorkspaceId}&base_version_id=${workspace.base_version_id}&expected_project_version=${workspace.project_version}&query=`;
                      const response = await fetch(path, {headers: {'X-MPlus-Agent-Auth': state.agentAuth}});
                      return await response.json();
                    }""")
                    self.assertGreaterEqual(
                        page.locator("[data-image-shot-select='0'] option").count(), 2,
                        json.dumps(candidate_debug, ensure_ascii=False),
                    )
                    used_clusters = set()
                    for index in range(image_shots.count()):
                        shot_select = page.locator(f"[data-image-shot-select='{index}']")
                        # References require an explicit choice. Prefer distinct
                        # available fixture images, without accepting a recommendation
                        # on the user's behalf or weakening the diversity assertion.
                        available = shot_select.locator("option").evaluate_all(
                            "options => options.filter(option => option.value && !option.disabled).map(option => option.value)"
                        )
                        candidates = page.evaluate("index => state.formalImageWorkspace.shots[index].candidates", index)
                        selectable = [candidate for candidate in candidates if candidate["image_key"] in available]
                        if selectable:
                            chosen = next((candidate for candidate in selectable if candidate.get("perceptual_cluster_id") not in used_clusters), selectable[0])
                            shot_select.select_option(chosen["image_key"])
                            used_clusters.add(chosen.get("perceptual_cluster_id"))
                        if not shot_select.input_value():
                            _expand_image_controls(page, index)
                            page.locator(f"[data-image-shot-temporary='{index}']").check()
                            page.locator(f"[data-image-shot-reason='{index}']").fill(
                                "当前无满足收束镜头要求的安全图片，暂以文字依据保留未完成交付状态"
                            )
                        reuse_reason = page.locator(f"[data-image-shot-reuse-reason='{index}']")
                        if reuse_reason.count() and reuse_reason.is_visible():
                            reuse_reason.fill("该镜头功能仅有当前安全素材，已检查重复并批准必要复用")
                            page.locator(f"[data-image-reuse-apply='{index}']").click()
                        _use_image_and_continue(page, index)
                    image_decision_debug = page.evaluate("""() => (state.formalImageWorkspace?.shots || []).map((shot, index) => ({
                      shot_no: shot.shot_no,
                      visual: shot.visual,
                      logical_slot: shot.logical_slot,
                      shot_function: shot.shot_function,
                      selected: document.querySelector(`[data-image-shot-select="${index}"]`)?.value || '',
                      candidate_count: (shot.candidates || []).length,
                      candidate_captions: (shot.candidates || []).map(candidate => candidate.caption),
                      reuse_required: Boolean(shot.reuse_required),
                      reuse_reason: document.querySelector(`[data-image-shot-reuse-reason="${index}"]`)?.value || '',
                      confirmed: Boolean(document.querySelector(`[data-image-shot-confirm="${index}"]`)?.checked),
                    }))""")
                    selected_clusters = page.evaluate("""() => (state.formalImageWorkspace?.shots || []).map((shot, index) => {
                      const selected = document.querySelector(`[data-image-shot-select="${index}"]`)?.value || '';
                      return (shot.candidates || []).find(candidate => candidate.image_key === selected)?.perceptual_cluster_id || '';
                    }).filter(Boolean)""")
                    self.assertGreaterEqual(
                        len(set(selected_clusters)),
                        max(8, script_shot_count // 2),
                        json.dumps(image_decision_debug, ensure_ascii=False),
                    )
                    self.assertFalse(
                        page.locator("#formal-image-version-save").is_disabled(),
                        json.dumps(image_decision_debug, ensure_ascii=False),
                    )
                    _save_unified_images(page)
                    self.assertTrue(page.evaluate("document.activeElement?.matches('[data-creative-panel=images]')"))
                    self.assertEqual(
                        len((root / "provider-calls.jsonl").read_text(encoding="utf-8").splitlines()),
                        2,
                    )
                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator("#formal-image-version-state", has_text="已确认").wait_for()
                    self.assertEqual(page.locator("[data-image-shot-confirm]:checked").count(), script_shot_count)
                    self.assertEqual(page.locator(".ui00-image-shot-preview img").count(), script_shot_count)
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script")
                    _show_script_table(page)
                    page.locator("#formal-script-table .ui00-script-table").wait_for()
                    script_before_image_change = page.evaluate("""() => [...document.querySelectorAll('#formal-script-table tbody tr')].map(row =>
                      [...row.querySelectorAll('input, textarea')].map(input => input.value)
                    )""")
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/assistant")
                    page.locator("#agent-layout").wait_for()
                    page.set_viewport_size({"width": 375, "height": 812})
                    recorder.capture(
                        page, "M52", keyboard_selector="#agent-refresh",
                        double_click_selector="#agent-layout",
                        recovery_selector="#agent-layout",
                        terminal_result="项目助手恢复同一会话、服务端进度、当前问题和下一步",
                    )
                    recorder.capture(
                        page, "M53", keyboard_selector="#agent-message",
                        double_click_selector="#agent-message-form",
                        recovery_selector="#agent-feed",
                        terminal_result="项目助手提供指令输入与受控确认，业务写入仍汇聚权威项目服务",
                    )
                    page.locator("#agent-session-status", has_text="会话已连接").wait_for()
                    self.assertEqual(page.locator("#agent-message-form").count(), 1)
                    self.assertEqual(page.locator("[data-agent-confirm]").count(), 0)
                    self.assertIn("请开始生成一版脚本", page.locator("#agent-feed").inner_text())
                    self.assertTrue(page.evaluate(
                        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                    ))
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator("#formal-image-version-state", has_text="已确认").wait_for()
                    shot_selects = page.locator("[data-image-shot-select]")
                    selected_values = [
                        shot_selects.nth(index).input_value()
                        for index in range(script_shot_count)
                    ]
                    available_values = shot_selects.first.locator("option").evaluate_all(
                        "options => options.map(option => option.value).filter(Boolean)",
                    )
                    alternatives = [
                        value for value in available_values if value != selected_values[0]
                    ]
                    self.assertTrue(alternatives)
                    replacement_key = next(
                        (value for value in alternatives if value not in selected_values),
                        alternatives[0],
                    )
                    shot_selects.first.select_option(replacement_key)
                    _use_image_and_continue(page, 0)
                    for index in range(script_shot_count):
                        reuse_reason = page.locator(f"[data-image-shot-reuse-reason='{index}']")
                        if reuse_reason.count() and reuse_reason.is_visible() and not reuse_reason.input_value().strip():
                            reuse_reason.fill("替换后无其他安全候选，已核对并批准必要复用")
                    self.assertFalse(page.locator("#formal-image-version-save").is_disabled())
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and "/script/save?" in response.url,
                    ) as replacement_response:
                        page.locator("#formal-image-version-save").click()
                    self.assertEqual(
                        replacement_response.value.status,
                        200,
                        replacement_response.value.text() if replacement_response.value.status != 200 else "",
                    )
                    replacement_base_version = page.evaluate("state.formalImageWorkspace.base_version_id")
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and "/api/agent/tasks/" in response.url
                        and response.url.endswith("/confirm"),
                    ) as replacement_confirm_response:
                        page.locator("[data-confirm-script-revision]").click()
                    self.assertEqual(
                        replacement_confirm_response.value.status,
                        200,
                        replacement_confirm_response.value.text() if replacement_confirm_response.value.status != 200 else "",
                    )
                    page.locator("#formal-script-save-proposal", has_text="文字与配图已保存为一个新版本").wait_for(timeout=30_000)
                    page.locator('[data-creative-panel="images"]').click()
                    _show_all_image_shots(page)
                    page.wait_for_function(
                        "base => state.formalImageWorkspace?.base_version_id !== base",
                        arg=replacement_base_version,
                    )
                    self.assertEqual(page.locator("[data-image-shot-select]").first.input_value(), replacement_key)
                    self.assertEqual(
                        page.locator("[data-image-shot-confirm]:checked").count(),
                        script_shot_count,
                    )
                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script?panel=images")
                    _show_all_image_shots(page)
                    page.locator("#formal-image-version-state", has_text="已确认").wait_for()
                    self.assertEqual(
                        page.locator("[data-image-shot-select]").first.input_value(),
                        replacement_key,
                    )
                    review_target = page.evaluate("""() => {
                      const shots = state.formalImageWorkspace?.shots || [];
                      const index = shots.findIndex(shot =>
                        (shot.candidates || []).some(candidate => candidate.image_key !== shot.selected_image_key)
                      );
                      return index < 0 ? null : {index, shot_no: String(shots[index].shot_no)};
                    }""")
                    self.assertIsNotNone(review_target)
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/script")
                    _show_script_table(page)
                    page.locator("#formal-script-table .ui00-script-table").wait_for()
                    self.assertEqual(page.evaluate("""() => [...document.querySelectorAll('#formal-script-table tbody tr')].map(row =>
                      [...row.querySelectorAll('input, textarea')].map(input => input.value)
                    )"""), script_before_image_change)
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/review")
                    page.wait_for_url(f"**/projects/{quote(project_id)}/review")
                    page.wait_for_function("() => state.formalReview !== null")
                    self.assertEqual(
                        page.locator("#formal-review-state").inner_text(),
                        "需审核",
                        page.evaluate("JSON.stringify(state.formalReview)"),
                    )
                    preview_items = page.locator("#formal-review-issues .ui00-review-item")
                    self.assertGreater(preview_items.count(), script_shot_count)
                    page.wait_for_function(
                        "expected => document.querySelectorAll('#formal-review-issues .ui00-review-image img').length === expected",
                        arg=preview_items.count(),
                    )
                    self.assertEqual(
                        page.locator("#formal-review-issues .ui00-review-image img").count(),
                        preview_items.count(),
                    )
                    recorder.capture(
                        page, "M39", keyboard_selector="#formal-review-start",
                        double_click_selector="#formal-review-issues",
                        recovery_selector="#formal-review-start",
                        terminal_result="审核开始前展示全部逐镜与相邻问题，按钮不绕过预览",
                    )
                    page.locator(".ui00-review-evidence summary").click()
                    recorder.capture(
                        page, "M43", keyboard_selector=".ui00-review-evidence summary",
                        double_click_selector="#formal-review-evidence",
                        recovery_selector="#formal-review-issues",
                        terminal_result="证据层展示来源、机器检查和人工记录，不将机器 PASS 冒充人工通过",
                    )
                    page.locator("#formal-review-start").click()
                    page.locator("#formal-review-state", has_text="审核中").wait_for()
                    review_issue_index = page.evaluate("""shotNo =>
                      (state.formalReview?.issues || []).findIndex(issue => String(issue.shot_no) === shotNo)
                    """, review_target["shot_no"])
                    self.assertGreaterEqual(review_issue_index, 0)
                    review_note = _review_note(page, review_issue_index)
                    review_note.fill(
                        f"第{review_target['shot_no']}镜图片与画面重点不一致，请更换后重新提交。"
                    )
                    recorder.capture(
                        page, "M40", keyboard_selector=f"[data-review-note='{review_issue_index}']",
                        double_click_selector="#formal-review-issues",
                        recovery_selector="#formal-review-issues",
                        terminal_result="审核卡逐项显示脚本、图片、来源和人工决定输入",
                    )
                    page.locator(
                        f"[data-review-decision='return'][data-review-index='{review_issue_index}']"
                    ).click()
                    page.locator("#formal-review-state", has_text="需修改").wait_for()
                    page.locator("#formal-review-revision-proposal").wait_for()
                    recorder.capture(
                        page, "M41", keyboard_selector='nav[aria-label="项目功能"] a[data-route="script"]',
                        double_click_selector="#formal-review-revision-proposal",
                        recovery_selector="#formal-review-next-action",
                        terminal_result="退回只记录决定并给出编辑去向，不在审核页自动创建子版本",
                    )
                    returned_project_version = page.evaluate("state.formalReview.project_version")
                    self.assertIn("保存新版本", page.locator("#formal-review-next-action").inner_text())
                    self.assertEqual(
                        len((root / "provider-calls.jsonl").read_text(encoding="utf-8").splitlines()),
                        2,
                    )

                    _open_project_images(page)
                    _wait_for_creative_surface(page, project_id, "images")
                    page.wait_for_function(
                        "expected => state.formalImageWorkspace?.project_version === expected",
                        arg=returned_project_version,
                    )
                    page.locator("#formal-image-version-state", has_text="已确认").wait_for()
                    review_image_selects = page.locator("[data-image-shot-select]")
                    review_selected_values = [
                        review_image_selects.nth(index).input_value()
                        for index in range(script_shot_count)
                    ]
                    review_target_select = review_image_selects.nth(review_target["index"])
                    review_available_values = review_target_select.locator("option").evaluate_all(
                        "options => options.map(option => option.value).filter(Boolean)",
                    )
                    review_alternatives = [
                        value for value in review_available_values
                        if value != review_selected_values[review_target["index"]]
                    ]
                    self.assertTrue(review_alternatives)
                    reviewed_replacement_key = next(
                        (value for value in review_alternatives if value not in review_selected_values),
                        review_alternatives[0],
                    )
                    review_target_select.select_option(reviewed_replacement_key)
                    _use_image_and_continue(page, review_target['index'])
                    for index in range(script_shot_count):
                        reuse_reason = page.locator(f"[data-image-shot-reuse-reason='{index}']")
                        if reuse_reason.count() and reuse_reason.is_visible() and not reuse_reason.input_value().strip():
                            reuse_reason.fill("审核退回后无其他安全候选，已核对并批准必要复用")
                    self.assertFalse(page.locator("#formal-image-version-save").is_disabled())
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and "/script/save?" in response.url,
                    ) as reviewed_replacement_response:
                        page.locator("#formal-image-version-save").click()
                    self.assertEqual(
                        reviewed_replacement_response.value.status,
                        200,
                        reviewed_replacement_response.value.text() if reviewed_replacement_response.value.status != 200 else "",
                    )
                    reviewed_base_version = page.evaluate("state.formalImageWorkspace.base_version_id")
                    with _buffered_image_confirm_response(page) as buffered_confirmation:
                        with page.expect_response(
                            lambda response: response.request.method == "POST"
                            and "/api/agent/tasks/" in response.url
                            and response.url.endswith("/confirm"),
                        ) as reviewed_confirm_response:
                            page.locator("[data-confirm-script-revision]").click()
                        reviewed_confirm_response.value.finished()
                    self.assertEqual(
                        reviewed_confirm_response.value.status,
                        200,
                        reviewed_confirm_response.value.text() if reviewed_confirm_response.value.status != 200 else "",
                    )
                    self.assertEqual(buffered_confirmation["url"], reviewed_confirm_response.value.url)
                    self.assertEqual(buffered_confirmation["status"], reviewed_confirm_response.value.status)
                    self.assertEqual(buffered_confirmation["method"], reviewed_confirm_response.value.request.method)
                    (root / "image-confirm-response.json").write_bytes(buffered_confirmation["raw_body"])
                    (root / "image-confirm-response-evidence.json").write_text(json.dumps({
                        key: buffered_confirmation[key] for key in ("request_id", "url", "method", "status", "sha256")
                    } | {"body_bytes": len(buffered_confirmation["raw_body"])}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    reviewed_confirm_payload = buffered_confirmation["payload"]
                    self.assertEqual(reviewed_confirm_payload["task"]["status"], "completed")
                    page.locator("#formal-script-save-proposal", has_text="文字与配图已保存为一个新版本").wait_for(timeout=30_000)
                    page.locator('[data-creative-panel="images"]').click()
                    _show_all_image_shots(page)
                    page.wait_for_function(
                        "base => state.formalImageWorkspace?.base_version_id !== base",
                        arg=reviewed_base_version,
                    )
                    page.locator("#formal-image-version-state", has_text="已确认").wait_for()

                    reviewed_project_version = page.evaluate("state.formalImageWorkspace.project_version")
                    page.locator('nav[aria-label="项目功能"] a[data-route="review"]').click()
                    page.wait_for_url(f"**/projects/{quote(project_id)}/review")
                    page.wait_for_function(
                        "expected => state.formalReview?.project_version === expected",
                        arg=reviewed_project_version,
                    )
                    self.assertEqual(
                        page.locator("#formal-review-state").inner_text(),
                        "需审核",
                        page.evaluate("JSON.stringify(state.formalReview)"),
                    )
                    with sqlite3.connect(root / "workspace.sqlite3") as database:
                        current_run_id = database.execute(
                            """SELECT v.run_id FROM projects p
                            JOIN project_versions v ON v.version_id=p.current_version_id
                            WHERE p.project_id=?""",
                            (project_id,),
                        ).fetchone()[0]
                    review_copy = export_review_workbook(
                        root / "runs" / project_id,
                        root / "anonymous-human-review.xlsx",
                        run_id=current_run_id,
                    )
                    _complete_review_workbook(review_copy)
                    page.goto(
                        f"{server.base_url}/tasks?view=runs&run_id={quote(current_run_id)}"
                    )
                    page.locator("[data-history-run-detail]").wait_for()
                    self.assertFalse(page.locator("[data-history-review-file]").is_disabled())
                    review_control = page.locator("[data-history-review-control]")
                    self.assertEqual(review_control.get_attribute("role"), "button")
                    self.assertEqual(review_control.get_attribute("aria-disabled"), "false")
                    self.assertEqual(review_control.get_attribute("tabindex"), "0")
                    recorder.capture(
                        page, "M50", keyboard_selector="[data-history-review-control]",
                        double_click_selector="[data-history-run-detail]",
                        recovery_selector="[data-history-review-control]",
                        terminal_result="历史人工复核 XLSX 入口可键盘到达，上传只创建待确认 proposal",
                    )
                    with page.expect_response(
                        lambda response: response.request.method == "POST"
                        and "/review-import-proposals" in response.url,
                    ) as review_import_probe:
                        with page.expect_file_chooser() as review_file_chooser:
                            review_control.press("Enter")
                        review_file_chooser.value.set_files(str(review_copy))
                    self.assertEqual(
                        review_import_probe.value.status,
                        200,
                        review_import_probe.value.text() if review_import_probe.value.status != 200 else "",
                    )
                    page.locator("[data-confirm-history-review]").wait_for()
                    self.assertIn(
                        "只影响：人工审核决定",
                        page.locator("[data-history-revision-preview]").inner_text(),
                    )
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/review")
                    page.locator("#formal-review-state", has_text="需审核").wait_for()
                    page.locator("#formal-review-start").click()
                    page.locator("#formal-review-state", has_text="审核中").wait_for()
                    _review_note(page, 0).fill(
                        "已核对第1镜新图片、来源和入口画面重点，修改符合退回意见。"
                    )
                    page.locator(
                        "[data-review-decision='approve'][data-review-index='0']"
                    ).click()
                    page.wait_for_function(
                        "document.querySelectorAll('#formal-review-issues [data-status=\"approve\"]').length >= 1"
                    )
                    _expand_other_review_items(page)
                    recorder.capture(
                        page, "M42", keyboard_selector="[data-review-decision='approve']",
                        double_click_selector="#formal-review-issues",
                        recovery_selector="[data-review-decision='approve']",
                        recovery_open_selector=".ui00-review-others > summary",
                        terminal_result="每个审核项都有明确人工决定；普通通过可无意见或明确批量提交",
                    )
                    _approve_remaining_review(page, root / "workspace.sqlite3")
                    page.locator("#formal-review-state", has_text="待整片确认").wait_for()
                    self.assertEqual(
                        page.locator("#formal-review-issues [data-status='approve']").count(),
                        page.locator("#formal-review-issues .ui00-review-item").count(),
                    )
                    recorder.capture(
                        page, "M44", keyboard_selector="#formal-review-final-submit",
                        double_click_selector="#formal-review-final-form",
                        recovery_selector="#formal-review-final-form",
                        terminal_result="N 个审核项全部具备人工决定后才开放整片确认门",
                    )
                    self.assertEqual(page.locator("#formal-review-final-check").count(), 0)
                    _final_review_note(page).fill(
                        "整份脚本、每镜图片、变化镜头和来源均已检查，确认进入交付准备。"
                    )
                    self.assertFalse(page.locator("#formal-review-final-submit").is_disabled())
                    page.locator("#formal-review-final-submit").click()
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    self.assertEqual(page.evaluate("document.activeElement?.id"), "formal-review-state")
                    self.assertIn("尚未正式发布", page.locator("#toast").inner_text())

                    page.goto("about:blank")
                    server.restart()
                    page.goto(f"{server.base_url}/projects/{quote(project_id)}/review")
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    self.assertIn("完成确认", page.locator("#formal-review-final-copy").inner_text())
                    recovered_review_items = page.locator("#formal-review-issues .ui00-review-item")
                    _expand_other_review_items(page)
                    page.wait_for_function(
                        "expected => document.querySelectorAll('#formal-review-issues .ui00-review-image img').length === expected",
                        arg=recovered_review_items.count(),
                    )
                    recovered_images = page.locator("#formal-review-issues .ui00-review-image img")
                    for index in range(recovered_images.count()):
                        recovered_images.nth(index).scroll_into_view_if_needed()
                    page.wait_for_function(
                        "() => [...document.querySelectorAll('#formal-review-issues .ui00-review-image img')].every(image => image.complete && image.naturalWidth > 0)"
                    )
                    self.assertFalse(page.locator("#formal-delivery-excel").is_disabled())
                    first_export = root / "first-browser-export.xlsx"
                    with page.expect_response(
                        lambda response: "/review/excel?" in response.url,
                    ) as excel_probe:
                        with page.expect_download() as first_download:
                            page.locator("#formal-delivery-excel").click()
                    self.assertEqual(
                        excel_probe.value.status, 200, excel_probe.value.text() if excel_probe.value.status != 200 else "",
                    )
                    first_download.value.save_as(first_export)
                    self.assertTrue(first_download.value.suggested_filename.endswith("-脚本交付.xlsx"))
                    evidence_export_path = os.environ.get("MPLUS_FORMAL_EXCEL_PATH")
                    if evidence_export_path:
                        Path(evidence_export_path).write_bytes(first_export.read_bytes())
                    first_export_sha = hashlib.sha256(first_export.read_bytes()).hexdigest()
                    exported = load_workbook(first_export, read_only=False, data_only=False)
                    try:
                        self.assertEqual(len(exported["脚本"]._images), script_shot_count)
                        self.assertEqual(
                            exported["相邻镜头复核"].max_row - 1,
                            script_shot_count - 1,
                        )
                        cells = "\n".join(
                            str(cell.value or "")
                            for sheet in exported.worksheets
                            for row in sheet.iter_rows()
                            for cell in row
                        )
                        self.assertIn("采用来源文件", cells)
                        self.assertIn("采用来源 sheet", cells)
                        self.assertIn("采用来源行", cells)
                    finally:
                        exported.close()
                    self.assertFalse(page.locator("#formal-delivery-pdf").is_disabled())
                    first_pdf = root / "first-browser-export.pdf"
                    with page.expect_download() as pdf_download:
                        page.locator("#formal-delivery-pdf").click()
                    pdf_download.value.save_as(first_pdf)
                    self.assertTrue(pdf_download.value.suggested_filename.endswith("-脚本交付.pdf"))
                    self.assertEqual(len(PdfReader(str(first_pdf)).pages), script_shot_count)
                    first_pdf_sha = hashlib.sha256(first_pdf.read_bytes()).hexdigest()
                    evidence_pdf_path = os.environ.get("MPLUS_FORMAL_PDF_PATH")
                    if evidence_pdf_path:
                        Path(evidence_pdf_path).write_bytes(first_pdf.read_bytes())
                    self.assertIn(
                        "相同的交付 snapshot",
                        page.locator("#formal-delivery-status").inner_text(),
                    )
                    recorder.capture(
                        page, "M45", keyboard_selector="#formal-delivery-excel",
                        double_click_selector="#formal-delivery-card",
                        recovery_selector="#formal-delivery-card",
                        artifact_not_applicable=False,
                        artifact_result="同一 delivery snapshot 生成可重开的 Excel 与 PDF，SHA 已登记",
                        terminal_result="整片人工确认后 Excel/PDF 同源交付均成功且重复导出 SHA 稳定",
                    )

                    page.reload()
                    if page.locator("#creative-script-panel").is_visible():
                        _show_script_table(page)
                    elif page.locator("#page-shot-images").is_visible():
                        _show_all_image_shots(page)
                    page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    second_export = root / "restart-browser-export.xlsx"
                    with page.expect_download() as second_download:
                        page.locator("#formal-delivery-excel").click()
                    second_download.value.save_as(second_export)
                    self.assertEqual(
                        hashlib.sha256(second_export.read_bytes()).hexdigest(),
                        first_export_sha,
                    )

                    transferred_auth = page.evaluate("localStorage.getItem('mplus.agent.auth')")
                    transferred_workspace = page.evaluate("localStorage.getItem('mplus.agent.workspace')")
                    second_context = browser.new_context(
                        viewport={"width": 1024, "height": 900},
                        accept_downloads=True,
                    )
                    second_context.add_init_script(
                        f"localStorage.setItem('mplus.agent.auth', {json.dumps(transferred_auth)});"
                        f"localStorage.setItem('mplus.agent.workspace', {json.dumps(transferred_workspace)});"
                    )
                    second_page = second_context.new_page()
                    second_page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                    second_page.on("pageerror", lambda error: errors.append(str(error)))
                    second_page.goto(f"{server.base_url}/projects")
                    second_page.wait_for_load_state("networkidle")
                    self.assertIn(
                        "匿名纵向闭环项目",
                        second_page.locator("#formal-project-list").inner_text(),
                    )
                    second_page.goto(f"{server.base_url}/projects/{quote(project_id)}/tasks")
                    second_page.wait_for_function(
                        "() => document.querySelectorAll('#formal-task-list .task-compact-row').length >= 5"
                    )
                    recorder.capture(
                        second_page, "M46", keyboard_selector="#formal-task-filters button",
                        double_click_selector="#formal-task-filters",
                        recovery_selector="#formal-task-filters",
                        terminal_result="任务筛选读取项目与状态参数，不修改任务权威状态",
                    )
                    recorder.capture(
                        second_page, "M47", keyboard_selector="#formal-task-list a",
                        double_click_selector="#formal-task-list",
                        recovery_selector="#formal-task-list",
                        terminal_result="任务卡显示受控中文类型、状态和唯一 next action",
                    )
                    task_copy = second_page.locator("#formal-task-list").inner_text()
                    self.assertIn("规划与脚本生成", task_copy)
                    self.assertIn("脚本修改", task_copy)
                    # Unified saves appear under the existing script task type.
                    # Each immutable child must correspond to one completed save.
                    with sqlite3.connect(root / "workspace.sqlite3") as database:
                        saved_tasks = [json.loads(row[0])["internal_formal_parameters"] for row in database.execute(
                            "SELECT e.details_json FROM tasks t JOIN task_events e ON e.task_id=t.task_id "
                            "WHERE t.project_id=? AND t.task_type='formal_script_revision' "
                            "AND t.status='completed' AND e.status='proposed'", (project_id,)
                        ) if json.loads(row[0]).get("internal_formal_parameters", {}).get("materialize_existing_draft")]
                        parents = [row[0] for row in database.execute(
                            "SELECT parent_version_id FROM project_versions WHERE project_id=? AND parent_version_id IS NOT NULL", (project_id,)
                        )]
                    self.assertEqual(len(saved_tasks), 5)
                    self.assertCountEqual([task["base_version_id"] for task in saved_tasks], parents)
                    self.assertTrue(all(task["image_snapshot_digest"] for task in saved_tasks))
                    self.assertIn("历史复核导入", task_copy)
                    self.assertNotIn("formal historical review import", task_copy.casefold())
                    self.assertIn("已完成", task_copy)
                    for forbidden in (
                        "task_id", "token", "workspace", "actor", "sha", "current",
                        "artifact", "provider", "model", "path", "cache", "node",
                    ):
                        self.assertNotIn(forbidden, task_copy.casefold())
                    tasks_screenshot_path = os.environ.get(
                        "MPLUS_FORMAL_TASKS_SCREENSHOT_PATH"
                    )
                    if tasks_screenshot_path:
                        second_page.wait_for_timeout(500)
                        second_page.screenshot(path=tasks_screenshot_path, full_page=True)
                        self.assertTrue(Path(tasks_screenshot_path).is_file())
                    second_page.goto(f"{server.base_url}/tasks?view=runs")
                    second_page.wait_for_function(
                        "() => document.querySelectorAll('#formal-task-list .task-compact-row').length >= 1"
                    )
                    self.assertIn(
                        "匿名纵向闭环项目", second_page.locator("#formal-task-list").inner_text(),
                    )
                    second_page.locator('#formal-task-list a[href*="run_id="]').first.click()
                    second_page.locator("[data-history-run-detail]").wait_for()
                    history_copy = second_page.locator("[data-history-run-detail]").inner_text()
                    self.assertIn("当次 Brief", history_copy)
                    self.assertIn("匿名纵向闭环项目", history_copy)
                    self.assertIn("允许访问的文件", history_copy)
                    self.assertIn("下载脚本 Excel", history_copy)
                    self.assertIn("下载脚本 PDF", history_copy)
                    recorder.capture(
                        second_page, "M48", keyboard_selector="[data-history-artifact]",
                        double_click_selector="[data-history-run-detail]",
                        recovery_selector="[data-history-run-detail]",
                        artifact_not_applicable=False,
                        artifact_result="历史详情提供当前 run 允许访问的 Excel/PDF 下载引用",
                        terminal_result="历史详情绑定当次 Brief、run 和允许下载产物，不泄露本机路径",
                    )
                    recorder.capture(
                        second_page, "M49",
                        keyboard_selector="[data-history-run-detail] a[href$='/script']",
                        double_click_selector="[data-history-run-detail]",
                        recovery_selector="[data-history-run-detail]",
                        terminal_result="历史详情保持只读，修改回到脚本或逐镜配图权威页面",
                    )
                    history_export = root / "history-browser-export.xlsx"
                    with second_page.expect_download() as history_download:
                        second_page.locator("[data-history-artifact]", has_text="Excel").click()
                    history_download.value.save_as(history_export)
                    self.assertEqual(
                        hashlib.sha256(history_export.read_bytes()).hexdigest(), first_export_sha,
                    )
                    history_pdf = root / "history-browser-export.pdf"
                    with second_page.expect_download() as history_pdf_download:
                        second_page.locator("[data-history-artifact]", has_text="PDF").click()
                    history_pdf_download.value.save_as(history_pdf)
                    self.assertEqual(
                        hashlib.sha256(history_pdf.read_bytes()).hexdigest(), first_pdf_sha,
                    )
                    self.assertEqual(len(PdfReader(str(history_pdf)).pages), script_shot_count)
                    self.assertTrue(second_page.locator("[data-history-review-file]").is_disabled())
                    self.assertIn(
                        "已完成整片确认，无需重复导入复核结果",
                        second_page.locator("[data-history-review-import-reason]").inner_text(),
                    )
                    final_history_run_id = second_page.locator(
                        "[data-history-run-detail]"
                    ).get_attribute("data-history-run-detail")
                    self.assertTrue(final_history_run_id)
                    blocked_review_import = second_context.request.post(
                        f"{server.base_url}/api/formal/workspaces/{quote(transferred_workspace)}/legacy-runs/{quote(final_history_run_id)}/review-import-proposals",
                        headers={"X-MPlus-Agent-Auth": transferred_auth},
                    )
                    self.assertEqual(blocked_review_import.status, 409)
                    self.assertEqual(
                        blocked_review_import.json()["detail"]["code"],
                        "FINAL_CONFIRMATION_EXISTS",
                    )
                    self.assertFalse(second_page.locator("[data-history-continue='current']").is_disabled())
                    self.assertFalse(second_page.locator("[data-history-continue='new']").is_disabled())
                    self.assertEqual(second_page.locator("[data-history-revision-form]").count(), 0)
                    recorder.capture(
                        second_page, "M51", keyboard_selector="[data-history-continue='current']",
                        double_click_selector="[data-history-run-detail]",
                        recovery_selector="[data-history-run-detail]",
                        terminal_result="历史 Brief 可预览当前项目 child 或新项目续作，均不自动确认或生成",
                    )
                    second_page.locator("[data-history-continue='current']").click()
                    second_page.locator("[data-confirm-history-continuation]").wait_for()
                    self.assertIn(
                        "当前项目的新 Brief 子版本",
                        second_page.locator("[data-history-revision-preview]").inner_text(),
                    )
                    second_page.locator("[data-history-continue='new']").click()
                    second_page.locator("[data-confirm-history-continuation]").wait_for()
                    second_page.locator("[data-confirm-history-continuation]").click()
                    second_page.locator(
                        "[data-history-revision-preview]",
                        has_text="已创建后续项目需求",
                    ).wait_for()
                    continuation_href = second_page.locator(
                        "[data-history-revision-preview] a"
                    ).get_attribute("href")
                    self.assertRegex(continuation_href or "", r"^/projects/[^/]+/brief$")
                    second_page.goto(f"{server.base_url}/projects/{quote(project_id)}/review")
                    second_page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    fresh_profile_export = root / "fresh-browser-profile-export.xlsx"
                    with second_page.expect_download() as fresh_download:
                        second_page.locator("#formal-delivery-excel").click()
                    fresh_download.value.save_as(fresh_profile_export)
                    self.assertEqual(
                        hashlib.sha256(fresh_profile_export.read_bytes()).hexdigest(),
                        first_export_sha,
                    )
                    second_page.go_back()
                    second_page.locator("[data-history-run-detail]").wait_for()
                    self.assertIn(
                        "当次 Brief", second_page.locator("[data-history-run-detail]").inner_text(),
                    )
                    second_page.go_forward()
                    second_page.locator("#formal-review-state", has_text="整片已确认").wait_for()
                    second_page.set_viewport_size({"width": 375, "height": 812})
                    self.assertTrue(second_page.locator("#formal-mobile-menu").is_visible())
                    self.assertTrue(second_page.locator("#formal-mobile-exit").is_visible())
                    mobile_summary = second_page.locator("#formal-mobile-menu summary")
                    mobile_summary.focus()
                    mobile_summary.press("Enter")
                    self.assertTrue(second_page.locator("#formal-mobile-menu").get_attribute("open") is not None)
                    self.assertTrue(second_page.locator("#formal-mobile-menu nav").is_visible())
                    self.assertTrue(second_page.evaluate(
                        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                    ))
                    second_page.go_back()
                    second_page.locator("[data-history-run-detail]").wait_for()
                    self.assertEqual(second_page.locator("[data-history-revision-form]").count(), 0)
                    self.assertIn("历史详情只读", second_page.locator("[data-history-run-detail]").inner_text())
                    self.assertTrue(second_page.evaluate(
                        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
                    ))
                    second_context.close()
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_function("window.scrollY === 0")
                    screenshot_path = os.environ.get("MPLUS_FORMAL_SCREENSHOT_PATH")
                    if screenshot_path:
                        page.screenshot(path=screenshot_path, full_page=True)
                        self.assertTrue(Path(screenshot_path).is_file())
                    self.assertEqual(
                        len((root / "provider-calls.jsonl").read_text(encoding="utf-8").splitlines()),
                        2,
                    )
                    visible_review = page.locator("#page-review").inner_text().casefold()
                    for forbidden in (
                        "token", "workspace", "actor", "sha", "current", "artifact",
                        "provider", "model", "path", "cache", "node", "raw json",
                    ):
                        self.assertNotIn(forbidden, visible_review)
                    visible_script = page.locator("#page-shot-images").inner_text().casefold()
                    for forbidden in ("token", "workspace", "actor", "sha", "current", "artifact", "provider", "model", "path", "cache", "node"):
                        self.assertNotIn(forbidden, visible_script)
                    page.set_viewport_size({"width": 1024, "height": 900})
                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"))
                    adversarial_errors = errors[errors_before_adversarial:]
                    self.assertTrue(all(
                        "status of 503 (Service Unavailable)" in error
                        or "status of 409 (Conflict)" in error
                        for error in adversarial_errors
                    ), msg=f"Unexpected adversarial browser errors: {adversarial_errors!r}")
                    self.assertTrue(all(
                        "status of 503 (Service Unavailable)" in error
                        for error in errors[:errors_before_adversarial]
                    ), msg=f"Unexpected main-journey browser errors: {errors[:errors_before_adversarial]!r}")
                    context.close()
                    browser.close()
            finally:
                server.stop()

            with sqlite3.connect(root / "workspace.sqlite3") as connection:
                current_version = connection.execute(
                    "SELECT current_version_id FROM projects WHERE project_id = ?", (project_id,)
                ).fetchone()[0]
                self.assertTrue(current_version)
                self.assertGreaterEqual(connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project_id,)
                ).fetchone()[0], 1)
                draft_submissions = connection.execute(
                    """SELECT base_version_id, materialized_version_id, reason,
                    revision_report_ref, revision_report_sha256
                    FROM version_review_draft_submissions
                    WHERE project_id=? ORDER BY submitted_at""",
                    (project_id,),
                ).fetchall()
                self.assertEqual(len(draft_submissions), 2)
                self.assertIn(
                    "保存入口画面事实修订并保留原版本",
                    [row[2] for row in draft_submissions],
                )
                self.assertEqual(len({row[1] for row in draft_submissions}), 2)
                self.assertTrue(all(row[0] != row[1] for row in draft_submissions))
                self.assertTrue(all(str(row[3]).startswith("artifact://") for row in draft_submissions))
                self.assertTrue(all(len(str(row[4])) == 64 for row in draft_submissions))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM project_versions WHERE project_id = ?", (project_id,)
                ).fetchone()[0], 6)
                # Every unified save freezes the complete image selection,
                # including the two saves initiated from text editing.
                # Match each snapshot to its immutable child, not only the
                # three operations initiated from the image panel.
                image_snapshots = connection.execute(
                    """SELECT base_version_id, materialized_version_id, run_id,
                    shot_count, image_confirmed_count, temporary_text_basis_count
                    FROM version_image_decision_snapshots_v3 WHERE project_id=?""",
                    (project_id,),
                ).fetchall()
                children = connection.execute(
                    """SELECT parent_version_id, version_id, run_id FROM project_versions
                    WHERE project_id=? AND parent_version_id IS NOT NULL""", (project_id,),
                ).fetchall()
                self.assertCountEqual([row[:3] for row in image_snapshots], children)
                self.assertTrue(all(row[3:] == (script_shot_count, script_shot_count, 0)
                                    for row in image_snapshots))
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM version_image_decision_snapshots WHERE project_id = ?", (project_id,)
                ).fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM version_image_decision_snapshots_v2 WHERE project_id = ?", (project_id,)
                ).fetchone()[0], 0)
                delivered_version = connection.execute(
                    """SELECT version_id FROM delivery_snapshots
                    WHERE project_id=? ORDER BY created_at DESC LIMIT 1""",
                    (project_id,),
                ).fetchone()[0]
                self.assertEqual(current_version, delivered_version)
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM formal_version_feedback_records
                    WHERE project_id=? AND child_version_id=? AND feedback_kind='copy'""",
                    (project_id, current_version),
                ).fetchone()[0], 0)
                current_review_items = connection.execute(
                    "SELECT COUNT(*) FROM version_review_items WHERE version_id = ?", (delivered_version,)
                ).fetchone()[0]
                self.assertGreater(current_review_items, script_shot_count)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM version_review_human_decisions WHERE version_id = ?",
                    (delivered_version,),
                ).fetchone()[0], current_review_items)
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM version_quality_final_confirmations WHERE version_id = ?",
                    (delivered_version,),
                ).fetchone()[0], 1)
                self.assertEqual(connection.execute(
                    """SELECT COUNT(*) FROM tasks
                    WHERE project_id=? AND task_type='formal_historical_review_import'
                      AND status='proposed'""",
                    (project_id,),
                ).fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 2
                )
                continued_project = connection.execute(
                    "SELECT project_id, current_version_id FROM projects WHERE project_id <> ?",
                    (project_id,),
                ).fetchone()
                self.assertIsNotNone(continued_project)
                self.assertIsNone(continued_project[1])
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM brief_confirmations WHERE project_id = ?",
                        (continued_project[0],),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0], 0)
            self.assertFalse((root / "runs" / "CURRENT").exists())


if __name__ == "__main__":
    unittest.main()
