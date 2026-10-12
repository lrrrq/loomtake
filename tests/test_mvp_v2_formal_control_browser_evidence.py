from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
import unittest
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_formal_browser_journey import _open_new_project_brief

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "tests" / "support" / "legacy_control_fixture_server.py"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _rows(database: Path, sql: str, parameters: tuple = ()) -> list[dict]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, parameters)]


@unittest.skipUnless(
    os.environ.get("MPLUS_FORMAL_CONTROL_EVIDENCE_ROOT"),
    "显式外部证据阶段才运行真实旧能力浏览器链路",
)
class FormalLegacyControlBrowserEvidenceTests(unittest.TestCase):
    def test_assets_remain_reachable_from_unified_routes(self):
        self._run_journey("assets")

    def test_governed_knowledge_remains_reachable_from_unified_routes(self):
        self._run_journey("knowledge")

    def _run_journey(self, section: str):
        fixture_root = Path(os.environ["MPLUS_FORMAL_CONTROL_LEGACY_ROOT"]).resolve() / section
        fixture_root.mkdir(parents=True, exist_ok=True)
        evidence = fixture_root / "browser"
        evidence.mkdir(exist_ok=True)
        database = fixture_root / "runtime" / "workspace.sqlite3"
        port = _free_port()
        log_path = fixture_root / "legacy-control-server.log"
        environment = os.environ.copy()
        environment.update({
            "MPLUS_UI_TEST_PORT": str(port),
            "MPLUS_PORT": str(port),
            "MPLUS_FORMAL_CONTROL_LEGACY_ROOT": str(fixture_root),
            "PYTHONPATH": str(ROOT),
        })
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", str(SERVER)], cwd=ROOT, env=environment,
                stdout=log, stderr=subprocess.STDOUT,
            )
        base_url = f"http://127.0.0.1:{port}"
        steps: list[dict] = []
        errors: list[str] = []
        failed_responses: list[dict] = []

        def capture(page, name: str, result: str) -> None:
            page.screenshot(path=str(evidence / f"{name}.png"), full_page=True)
            (evidence / f"{name}.txt").write_text(page.locator("body").inner_text(), encoding="utf-8")
            steps.append({"step": name, "result": result, "url": page.url})

        def execute_confirmation(page, selector: str, endpoint: str) -> None:
            with page.expect_response(lambda response: response.url.endswith(endpoint)) as executed:
                page.locator(selector).click()
            result = executed.value.json()
            self.assertEqual(executed.value.status, 200, result)
            self.assertEqual(result.get("status"), "completed", result.get("error"))

        try:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(log_path.read_text(encoding="utf-8", errors="replace"))
                try:
                    with urlopen(f"{base_url}/api/formal/setup", timeout=0.5) as response:
                        if response.status == 200:
                            break
                except (OSError, URLError):
                    time.sleep(0.1)
            else:
                self.fail("旧能力浏览器 fixture 未在 60 秒内启动")

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.on("pageerror", lambda error: errors.append(str(error)))
                def response_evidence(response):
                    if response.status >= 400 and "/api/" in response.url:
                        failed_responses.append({"path":urlparse(response.url).path,
                            "status":response.status,"body":response.text()})
                page.on("response", response_evidence)
                try:
                    page.goto(f"{base_url}/projects")
                    page.locator("#formal-owner-confirm").check()
                    page.locator("#formal-owner-enable").click()
                    page.locator("#formal-project-new").click()
                    page.locator("#formal-project-name").fill("匿名保留能力项目")
                    page.locator("#formal-project-create-submit").click()
                    _open_new_project_brief(page)
                    project_id = urlparse(page.url).path.split("/")[2]
                    page.locator("#formal-brief-text").fill("\n".join((
                        "项目名称: 匿名保留能力项目", "品牌或客户: 匿名品牌", "脚本类型: 发布会",
                        "目标时长: 60秒", "风格调性: 克制、真实、清晰", "视频目标: 清晰说明发布重点",
                        "必须出现的内容: 产品演示、品牌收束", "关键事件与流程: 嘉宾发布、产品演示、品牌收束",
                        "禁止或不能出现的内容: 不得虚构事实", "使用场景: 发布会现场",
                    )))
                    page.locator("#formal-brief-upload-button").click()
                    page.locator("#formal-standard-card-state", has_text="待确认").wait_for()
                    page.locator("#formal-brief-confirm").click()
                    page.locator("#formal-standard-card-state", has_text="已确认").wait_for()
                    capture(page, "01-project-and-brief", "可见表单创建项目、导入并确认真实 Brief")

                    # Read the authoritative source only to verify the picker
                    # submits the exact same frozen Brief after user selection.
                    brief_ref = _rows(database, "SELECT b.brief_ref FROM projects p JOIN project_brief_snapshots b ON b.brief_snapshot_id=p.current_brief_snapshot_id WHERE p.project_id=?", (project_id,))[0]["brief_ref"]
                    self.assertTrue(brief_ref.startswith("artifact://"))
                    if section == "assets":
                        original_package = {
                            str(path.relative_to(fixture_root / "package")): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (fixture_root / "package").rglob("*") if path.is_file()
                        }
                        page.goto(f"{base_url}/assets?project_id={project_id}")
                        page.locator("#asset-package-label", has_text="资源版本 1").wait_for()
                        page.locator(".asset-intake-panel > summary").click()
                        with page.expect_file_chooser() as chooser:
                            page.locator("#asset-choose").click()
                        chooser.value.set_files(fixture_root / "anonymous-reference.png")
                        expect(page.locator("#asset-local-path")).to_have_value("anonymous-reference.png")
                        with page.expect_response(lambda response: urlparse(response.url).path.endswith("/assets/upload")) as frozen_response:
                            page.locator("#asset-freeze").click()
                        frozen = frozen_response.value.json()
                        expected_sha = hashlib.sha256((fixture_root / "anonymous-reference.png").read_bytes()).hexdigest()
                        self.assertEqual(frozen["content_sha256"], expected_sha)
                        page.locator("#asset-frozen-list", has_text="anonymous-reference.png").wait_for()
                        page.locator("#asset-stage").click()
                        page.locator("#asset-confirmation").wait_for()
                        capture(page, "02-asset-before-stage", "真实文件冻结后，首次人工确认前未创建导入记录")
                        expect(page.locator("#asset-imports")).not_to_contain_text("anonymous-reference.png")
                        page.locator("#asset-confirm-check").check()
                        execute_confirmation(page, "#asset-confirm-execute", "/assets/actions/execute")
                        page.locator(".asset-state-staged").wait_for()
                        capture(page, "03-asset-staged", "文件检查通过且资产仍等待人工审核")
                        page.locator("[data-asset-review]").click()
                        page.locator("#asset-confirmation").wait_for()
                        page.locator("#asset-confirm-check").check()
                        execute_confirmation(page, "#asset-confirm-execute", "/assets/actions/execute")
                        page.locator(".asset-state-reviewed").wait_for()
                        capture(page, "04-asset-reviewed", "同一导入记录已人工审核但尚未发布登记")
                        page.locator("[data-asset-publish]").click()
                        page.locator("#asset-confirmation").wait_for()
                        expect(page.locator("#asset-confirm-title")).to_have_text("确认发布登记")
                        page.locator("#asset-confirm-check").check()
                        execute_confirmation(page, "#asset-confirm-execute", "/assets/publish/execute")
                        page.locator(".asset-state-published").wait_for()
                        page.locator("#asset-search-query").fill("anonymous-reference")
                        page.locator("#asset-search").click()
                        page.locator("#asset-search-results", has_text="anonymous-reference.png").wait_for()
                        page.reload()
                        page.locator(".asset-state-published").wait_for()
                        capture(page, "05-asset-published-reloaded", "资产已发布、可检索，刷新后真实登记仍存在")
                        catalog = fixture_root / "runtime" / "asset_imports.sqlite3"
                        imports = _rows(catalog, "SELECT * FROM asset_imports")
                        items = _rows(catalog, "SELECT * FROM asset_import_items")
                        events = _rows(catalog, "SELECT event_type, payload_json FROM asset_import_events ORDER BY sequence_no")
                        self.assertEqual(len(imports), 1)
                        self.assertEqual((imports[0]["state"], imports[0]["revision"]), ("published", 3))
                        self.assertEqual(imports[0]["target_project_id"], project_id)
                        self.assertEqual(len(items), 1)
                        self.assertEqual(items[0]["content_sha256"], expected_sha)
                        self.assertEqual(items[0]["availability"], "available")
                        self.assertEqual([event["event_type"] for event in events], [
                            "asset_import.staged", "asset_import.reviewed",
                            "asset_publication.prepared", "asset_publication.planned",
                            "asset_publication.built", "asset_import.published",
                            "asset_publication.completed",
                        ])
                        commands = [json.loads(event["payload_json"])["command"]
                            for event in events if event["event_type"].startswith("asset_publication.")]
                        self.assertEqual(len(commands), 4)
                        self.assertTrue(all(command == commands[0] for command in commands))
                        self.assertEqual(commands[0]["project_id"], project_id)
                        self.assertEqual(len(_rows(database, "SELECT * FROM manual_command_results WHERE command_id=?",
                            (commands[0]["command_id"],))), 1)
                        self.assertEqual(original_package, {
                            str(path.relative_to(fixture_root / "package")): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in (fixture_root / "package").rglob("*") if path.is_file()
                        })
                        registry = json.loads((fixture_root / "runtime" / "package_registry.json").read_text())
                        published = [record for record in registry["packages"].values() if record["package_version"] == 2]
                        self.assertEqual(len(published), 1)
                        self.assertTrue((Path(published[0]["local_path"]) / "asset_package.json").is_file())

                    if section == "knowledge":
                        page.goto(f"{base_url}/knowledge?project_id={project_id}")
                        page.locator("#knowledge-role", has_text="管理员可审批").wait_for()
                        statement = "当前项目核心事实必须在情绪铺陈前出现"
                        page.locator(".knowledge-proposal-panel > summary").click()
                        page.locator("#knowledge-statement").fill(statement)
                        page.locator("#knowledge-evidence option:not([disabled])").first.wait_for(state="attached")
                        page.locator("#knowledge-evidence").select_option(index=0)
                        page.locator("#knowledge-propose").click()
                        page.locator("#governance-confirmation").wait_for()
                        capture(page, "06-knowledge-before-proposal", "规则建议在确认前未写入候选")
                        self.assertEqual(_rows(database, "SELECT * FROM knowledge_candidates"), [])
                        page.locator("#governance-confirm-check").check()
                        execute_confirmation(page, "#governance-confirm-execute", "/knowledge/candidates/execute")
                        page.locator("[data-knowledge-approve]").wait_for()
                        expect(page.locator("#knowledge-rules")).not_to_contain_text(statement)
                        self.assertEqual(_rows(database, "SELECT * FROM knowledge_rules"), [])
                        capture(page, "07-knowledge-pending", "建议进入真实待审核记录，尚未进入生效规则")
                        page.locator("[data-approval-evidence]").fill("管理员已核对 Brief 事实与当前项目一致")
                        page.locator("[data-knowledge-approve]").click()
                        page.locator("#governance-confirmation").wait_for()
                        page.locator("#governance-confirm-check").check()
                        execute_confirmation(page, "#governance-confirm-execute", "/knowledge/approvals/execute")
                        page.locator("#knowledge-rules", has_text=statement).wait_for()
                        expect(page.locator("#pending-knowledge-count")).to_have_text("0 条建议")
                        page.reload()
                        page.locator("#knowledge-rules", has_text=statement).wait_for()
                        capture(page, "08-knowledge-effective-reloaded", "管理员批准原候选后规则生效，刷新后仍存在")
                        candidates = _rows(database, "SELECT * FROM knowledge_candidates")
                        rules = _rows(database, "SELECT * FROM knowledge_rules")
                        self.assertEqual(len(candidates), 1)
                        self.assertEqual(len(rules), 1)
                        self.assertEqual(rules[0]["statement"], statement)
                        self.assertEqual(json.loads(rules[0]["evidence_refs_json"]), [brief_ref])
                        self.assertEqual(rules[0]["target_id"], project_id)
                    # This fixture intentionally has no model credentials. The
                    # optional advisor can report its unavailability while the
                    # confirmed Brief and asset/knowledge workflow remain usable.
                    self.assertLessEqual(len(failed_responses), 1)
                    for failure in failed_responses:
                        self.assertTrue(failure["path"].startswith("/api/agent/sessions/"))
                        self.assertTrue(failure["path"].endswith("/brief-analysis"))
                        self.assertEqual(failure["status"], 409)
                        self.assertEqual(json.loads(failure["body"])["detail"]["code"],
                                         "FORMAL_ADVISOR_UNAVAILABLE")
                    expected_console = ["Failed to load resource: the server responded with a status of 409 (Conflict)"] * len(failed_responses)
                    self.assertEqual(errors, expected_console)
                except Exception:
                    capture(page, "failure", "当前可见控件未完成下一步，保留真实失败状态")
                    raise
                finally:
                    context.close()
                    browser.close()
        finally:
            (evidence / "journey.json").write_text(json.dumps({
                "steps": steps, "browser_errors": errors, "failed_responses": failed_responses,
                "production_business_services": True,
                "limitations": [
                    "浏览器file chooser上传匿名文件；另一实体设备尚未验收",
                    "知识证据从页面授权资料选项选择，独立数据库读取只用于比对最终冻结引用",
                    "匿名文件与本地管理员，不代表真实团队权限和生产验收",
                ],
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
