"""Two real browser plans generate concurrently through production composition.

Only external model output/latency is deterministic; no business state is mocked.
Text-only review copies are deliberately distinct from approved deliverables.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse

from openpyxl import load_workbook
from playwright.async_api import async_playwright, expect

from mvp_v2.artifact_service import RunArtifactResolver
from tests.test_mvp_v2_plan_recovery_browser import _StabilizationServer, read_rows


class FormalConcurrencyBrowserTests(unittest.TestCase):
    def test_two_formal_generations_keep_versions_files_and_readback_isolated(self):
        configured = os.environ.get("MPLUS_FORMAL_CONCURRENCY_ROOT")
        if configured:
            root = Path(configured).resolve()
            root.mkdir(parents=True, exist_ok=True)
            self.assertEqual(list(root.iterdir()), [], "Use a fresh evidence directory")
        else:
            temporary = tempfile.TemporaryDirectory(prefix="mplus-formal-concurrency-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
        (root / "concurrent-formal.enabled").touch()
        server = _StabilizationServer(root)
        server.start()
        self.addCleanup(server.stop)
        asyncio.run(self.exercise(server, root))

    async def exercise(self, server, root):
        evidence, requests, errors = [], [], []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            pages = [await context.new_page(), await context.new_page()]
            for page in pages:
                page.set_default_timeout(20000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("response", lambda response: requests.append({
                    "method": response.request.method, "path": urlparse(response.url).path,
                    "status": response.status}) if "/api/" in response.url else None)
            try:
                await pages[0].goto(server.base_url + "/projects")
                await pages[0].locator("#formal-owner-confirm").check()
                await pages[0].locator("#formal-owner-enable").click()
                await expect(pages[0].locator("#formal-owner-onboarding")).to_be_hidden()
                projects = []
                for index, page in enumerate(pages):
                    marker = ("匿名甲展示蓝色模型", "匿名乙展示红色模型")[index]
                    name = "匿名正式并发" + ("甲", "乙")[index]
                    await page.goto(server.base_url + "/projects")
                    await page.locator("#formal-project-new").click()
                    await page.locator("#formal-project-name").fill(name)
                    await page.locator("#formal-project-create-submit").click()
                    await page.wait_for_url("**/projects/*/brief")
                    project_id = urlparse(page.url).path.split("/")[2]
                    await page.locator("#formal-brief-text").fill("\n".join((
                        "项目名称: " + name, "脚本类型: 发布会", "品牌或客户: 匿名团队",
                        "目标时长: 60秒", "风格调性: 克制", "视频目标: 记录现场互动",
                        "必须出现的内容: " + marker,
                        "关键事件与流程: 来宾入场、互动体验、主舞台活动、合影收束")))
                    await page.locator("#formal-brief-upload-button").click()
                    await expect(page.locator("#formal-standard-card-state")).to_have_text("待确认")
                    await page.locator("#formal-brief-confirm").click()
                    await expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                    await page.goto(f"{server.base_url}/projects/{project_id}/script?panel=structure&mode=text-only")
                    async with page.expect_response(lambda r: r.request.method == "POST" and urlparse(r.url).path.endswith("/plans")) as pending:
                        await page.locator("#formal-plan-create").click()
                    response = await pending.value
                    self.assertEqual(response.status, 200, await response.text())
                    plan = await response.json()
                    await expect(page.locator("#formal-plan-confirmation")).to_be_visible()
                    await page.locator("#formal-plan-confirm-check").check()
                    projects.append({"project_id": project_id, "marker": marker, "plan": plan})

                async def generate(page):
                    async with page.expect_response(lambda r: r.request.method == "POST" and "/plans/" in r.url and "/confirm" in r.url, timeout=90000) as pending:
                        await page.locator("#formal-plan-confirm").click()
                    response = await pending.value
                    self.assertEqual(response.status, 200, await response.text())
                    result = await response.json()
                    self.assertEqual(result["status"], "completed", result)
                    return result, response.request

                generated = await asyncio.gather(*(generate(page) for page in pages))
                results = [item[0] for item in generated]
                for _, original in generated:
                    replay = await context.request.post(original.url,
                        headers={"X-MPlus-Agent-Auth": original.headers["x-mplus-agent-auth"],
                                 "Origin": server.base_url}, data=original.post_data_json)
                    self.assertEqual(replay.status, 409, await replay.text())
                    self.assertEqual((await replay.json())["detail"]["code"], "PROJECT_VERSION_CONFLICT")
                calls = [json.loads(line) for line in (root / "formal-provider-calls.jsonl").read_text().splitlines()]
                self.assertEqual([c["model"] for c in calls[:2]], ["anonymous-writer"] * 2)
                self.assertEqual(sorted(c["model"] for c in calls), ["anonymous-editor"] * 2 + ["anonymous-writer"] * 2)
                versions = read_rows(root, "SELECT * FROM project_versions ORDER BY project_id")
                self.assertEqual(len(versions), 2)
                self.assertEqual(len({v["run_id"] for v in versions}), 2)
                self.assertEqual(len({v["version_id"] for v in versions}), 2)
                resolver = RunArtifactResolver(root / "runs")
                frozen = {}
                for project, result in zip(projects, results):
                    self.assertEqual(len(result["script_rows"]), project["plan"]["shot_count"])
                    text = json.dumps(result["script_rows"], ensure_ascii=False)
                    self.assertIn(project["marker"], text)
                    other = next(p for p in projects if p is not project)
                    self.assertNotIn(other["marker"], text)
                    version = next(v for v in versions if v["project_id"] == project["project_id"])
                    self.assertEqual(version["version_id"], result["version_id"])
                    current = read_rows(root, "SELECT current_version_id FROM projects WHERE project_id=?", (project["project_id"],))[0]
                    self.assertEqual(current["current_version_id"], version["version_id"])
                    workbook = resolver.resolve_artifact(version["run_id"], "workbook").path
                    frozen[str(workbook)] = hashlib.sha256(workbook.read_bytes()).hexdigest()
                    project["version_id"] = version["version_id"]
                    evidence.append({"project_id": project["project_id"], "version_id": version["version_id"],
                                     "run_id": version["run_id"], "shot_count": project["plan"]["shot_count"],
                                     "workbook_sha256": frozen[str(workbook)]})

                async def read_and_download(page, project, suffix):
                    await page.goto(f"{server.base_url}/projects/{project['project_id']}/script?panel=script")
                    await expect(page.locator("#formal-script-review-workbook")).to_be_enabled()
                    await page.locator("details.creative-more > summary").click()
                    async with page.expect_download(timeout=30000) as pending:
                        await page.locator("#formal-script-review-workbook").click()
                    download = await pending.value
                    self.assertIsNone(await download.failure())
                    path = root / f"{project['project_id']}-{suffix}.xlsx"
                    await download.save_as(path)
                    book = load_workbook(path)
                    try:
                        values = "\n".join(str(cell.value or "") for sheet in book for row in sheet for cell in row)
                        self.assertIn(project["marker"], values)
                        self.assertIn("复核稿", values)
                        self.assertNotIn(next(p["marker"] for p in projects if p is not project), values)
                    finally:
                        book.close()
                    await page.reload()
                    await expect(page.locator("#formal-script-review-workbook")).to_be_enabled()
                    await page.screenshot(path=str(root / f"{project['project_id']}-{suffix}.png"), full_page=True)

                await asyncio.gather(*(read_and_download(page, project, "before-restart") for page, project in zip(pages, projects)))
                await context.storage_state(path=str(root / "browser-state.json"))
                await context.tracing.stop(path=str(root / "concurrency-trace.zip"))
                await context.close()
                server.stop()
                server.start()
                context = await browser.new_context(storage_state=str(root / "browser-state.json"))
                await context.tracing.start(screenshots=True, snapshots=True, sources=True)
                pages = [await context.new_page(), await context.new_page()]
                for page in pages:
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on("response", lambda response: requests.append({
                        "method": response.request.method, "path": urlparse(response.url).path,
                        "status": response.status}) if "/api/" in response.url else None)
                await asyncio.gather(*(read_and_download(page, project, "after-restart") for page, project in zip(pages, projects)))
                self.assertEqual(read_rows(root, "SELECT * FROM project_versions ORDER BY project_id"), versions)
                self.assertEqual({path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in frozen}, frozen)
                self.assertEqual(len((root / "formal-provider-calls.jsonl").read_text().splitlines()), 4)
                self.assertEqual(read_rows(root, "PRAGMA integrity_check"), [{"integrity_check": "ok"}])
                self.assertEqual(read_rows(root, "PRAGMA foreign_key_check"), [])
                self.assertEqual(read_rows(root, "SELECT count(*) n FROM version_reviews")[0]["n"], 0)
                self.assertEqual(read_rows(root, "SELECT count(*) n FROM version_publications")[0]["n"], 0)
                self.assertEqual(errors, [])
                self.assertEqual([r for r in requests if r["status"] >= 400], [])
                evidence.append({"concurrent_writers": 2, "provider_calls": 4, "restart_readback": True,
                                 "duplicate_posts_rejected": 2,
                                 "frozen_workbooks_unchanged": True, "formal_approval": False})
            except Exception:
                for index, page in enumerate(pages):
                    if not page.is_closed():
                        await page.screenshot(path=str(root / f"failure-{index}.png"), full_page=True)
                        (root / f"failure-{index}.txt").write_text(await page.locator("body").inner_text())
                raise
            finally:
                (root / "result.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
                (root / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
                await context.tracing.stop(path=str(root / "readback-trace.zip"))
                await context.close()
                await browser.close()
