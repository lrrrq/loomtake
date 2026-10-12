"""Delayed real API responses cannot restore another project's planning view."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlparse

from playwright.async_api import async_playwright, expect
from tests.test_mvp_v2_plan_recovery_browser import _StabilizationServer, read_rows


class PlanNavigationBrowserTests(unittest.TestCase):
    def test_delayed_preview_does_not_replace_new_project(self):
        configured = os.environ.get("MPLUS_PLAN_NAVIGATION_ROOT")
        if configured:
            root = Path(configured).resolve()
        else:
            temporary = tempfile.TemporaryDirectory(prefix="mplus-plan-navigation-")
            self.addCleanup(temporary.cleanup)
            root = Path(temporary.name)
        root.mkdir(parents=True, exist_ok=True)
        server = _StabilizationServer(root)
        server.start()
        self.addCleanup(server.stop)
        asyncio.run(self.exercise(server.base_url, root))

    async def exercise(self, base, root):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            context = await browser.new_context(viewport={"width": 1440, "height": 1000})
            await context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = await context.new_page()
            page.set_default_timeout(15000)
            evidence = []
            requests = []
            page.on("response", lambda response: requests.append({"method":response.request.method,
                "path":urlparse(response.url).path,"status":response.status}) if "/api/" in response.url else None)
            release = asyncio.Event()
            intercepted = asyncio.Event()
            try:
                await page.goto(base + "/projects")
                await page.locator("#formal-owner-confirm").check()
                await page.locator("#formal-owner-enable").click()
                await expect(page.locator("#formal-owner-onboarding")).to_be_hidden()
                projects = []
                for name, duration in (("匿名迟到规划A", "60秒"), ("匿名当前规划B", "60秒")):
                    await page.goto(base + "/projects")
                    await page.locator("#formal-project-new").click()
                    await page.locator("#formal-project-name").fill(name)
                    await page.locator("#formal-project-create-submit").click()
                    await page.wait_for_url("**/projects/*/brief")
                    project_id = urlparse(page.url).path.split("/")[2]
                    await expect(page.locator("#formal-brief-text")).to_be_visible()
                    await page.locator("#formal-brief-text").fill("\n".join((
                        "项目名称: " + name, "脚本类型: 发布会", "品牌或客户: 匿名团队",
                        "目标时长: " + duration, "风格调性: 克制", "视频目标: 记录现场互动",
                        "关键事件与流程: 来宾入场、互动体验、主舞台活动、合影收束")))
                    await page.locator("#formal-brief-upload-button").click()
                    await expect(page.locator("#formal-standard-card-state")).to_have_text("待确认")
                    await page.locator("#formal-brief-confirm").click()
                    await expect(page.locator("#formal-standard-card-state")).to_have_text("已确认")
                    await page.goto(f"{base}/projects/{project_id}/script?panel=structure&mode=text-only")
                    async with page.expect_response(lambda response: response.request.method == "POST" and urlparse(response.url).path.endswith("/plans")) as planned:
                        await page.locator("#formal-plan-create").click()
                    response = await planned.value
                    self.assertEqual(response.status, 200, await response.text())
                    plan = await response.json()
                    projects.append((project_id, plan["plan_id"], name))
                first, second = projects

                async def delayed(route):
                    response = await route.fetch()
                    evidence.append({"path": urlparse(route.request.url).path, "status": response.status})
                    intercepted.set()
                    await release.wait()
                    await route.fulfill(response=response)

                await page.route(f"**/projects/{first[0]}/plans/{first[1]}?*", delayed)
                await page.goto(f"{base}/projects/{first[0]}/script?panel=structure&mode=text-only", wait_until="domcontentloaded")
                await asyncio.wait_for(intercepted.wait(), 15)
                # Keep the same document alive: a full navigation would cancel
                # the old fetch and could hide this route-switching regression.
                await page.locator('.project-exit[data-route="projects"]').click()
                await page.locator(".ui00-project-row").filter(has_text=second[2]).locator("a").click()
                if "panel=structure" not in page.url:
                    await page.locator('[data-creative-panel="structure"]').click()
                await expect(page.locator("#formal-plan-confirmation")).to_be_visible()
                await page.wait_for_function("id => state.formalPlan?.plan_id === id", arg=second[1])
                self.assertEqual(await page.evaluate("state.formalPlan.plan_id"), second[1])
                release.set()
                await page.wait_for_load_state("networkidle")
                self.assertEqual(await page.evaluate("state.formalPlan.plan_id"), second[1])
                self.assertIn(second[0], page.url)
                evidence.append({"active_project": second[0], "active_plan": second[1], "late_plan_ignored": True})
                await page.unroute_all(behavior="wait")
                release.clear()
                intercepted.clear()
                confirmations = []
                page.on("request", lambda request: confirmations.append(request.url)
                        if request.method == "POST" and "/plans/" in request.url and "/confirm" in request.url else None)
                await page.route(f"**/api/formal/projects/{second[0]}", delayed)
                await page.locator("#formal-plan-confirm-check").check()
                await page.locator("#formal-plan-confirm").click()
                await asyncio.wait_for(intercepted.wait(), 15)
                await page.locator('.project-exit[data-route="projects"]').click()
                await page.locator(".ui00-project-row").filter(has_text=first[2]).locator("a").click()
                if "panel=structure" not in page.url:
                    await page.locator('[data-creative-panel="structure"]').click()
                await expect(page.locator("#formal-plan-confirmation")).to_be_visible()
                await page.wait_for_function("id => state.formalPlan?.plan_id === id", arg=first[1])
                release.set()
                await page.wait_for_load_state("networkidle")
                self.assertEqual(await page.evaluate("state.formalPlan.plan_id"), first[1])
                self.assertEqual(confirmations, [])
                self.assertEqual(read_rows(root, "SELECT count(*) n FROM project_versions")[0]["n"], 0)
                evidence.append({"leaving_before_confirmation_post": "no write", "versions": 0})
            except Exception:
                evidence.append({"failed_url":page.url,"state":await page.evaluate("({plan:state.formalPlan?.plan_id, planOwner:state.formalPlanProjectId, request:state.formalPlanRequest, route:formalRoute(), creative:{projectId:creativeState().projectId, loaded:creativeState().loaded, planKey:creativeState().planKey, loading:Boolean(creativeState().loadPromise)}})")})
                await page.screenshot(path=str(root / "failure.png"), full_page=True)
                (root / "failure.txt").write_text(await page.locator("body").inner_text(), encoding="utf-8")
                raise
            finally:
                release.set()
                (root / "navigation-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
                (root / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2), encoding="utf-8")
                await context.tracing.stop(path=str(root / "navigation-trace.zip"))
                await context.close()
                await browser.close()
