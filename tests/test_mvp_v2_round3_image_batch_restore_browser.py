"""Anonymous real HTTP/SQLite regressions for current image proposal restoration.

All task creation, cancellation, candidate search and persistence use existing
authority services. Only a static JavaScript response is substituted for the
old-source negative control; no business response or application state is fake.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import unittest
from urllib.parse import urlparse

from playwright.sync_api import expect
from starlette.concurrency import run_in_threadpool

from tests import test_mvp_v2_image_scope_recovery_browser as scope_fixture


OLD_RESTORE = """async function restoreImageBatch(projectId) {
  const host = formalRoute().panel === 'images' ? document.querySelector('#creative-image-flow') : elements.formalScriptRevisionProposal;
  const existing = [...document.querySelectorAll('[data-image-batch]')].find(batch => imageBatchProposals.get(batch.dataset.imageBatch)?.project_id === projectId);
  if (host && existing) {
    if (existing.parentElement !== host) { host.replaceChildren(existing); host.hidden = false; }
    return;
  }
  const alreadyVisible = () => [...document.querySelectorAll('[data-image-batch]')].some(batch => imageBatchProposals.get(batch.dataset.imageBatch)?.project_id === projectId);
  if (!host || alreadyVisible()) return;
  try {
    const result = await api(imageBatchPath({project_id: projectId, workspace_id: state.agentWorkspaceId}));
    if (formalRoute().projectId !== projectId || !result.response || alreadyVisible()) return;
    host.hidden = false; host.innerHTML = imageBatchProposalHtml(result.response, result.task.task_id);
    void loadImageBatchPreviews(host);
  } catch (error) { if (formalRoute().projectId === projectId) showToast(`配图建议恢复失败：${formalError(error)}`); }
}

"""


class Round3ImageBatchRestoreBrowserTests(unittest.TestCase):
    open_page = scope_fixture.ImageScopeRecoveryBrowserTests.open_page

    def setUp(self):
        support = scope_fixture.ImageScopeRecoveryBrowserTests("runTest")
        (self.support, self.f, self.script, self.current, self.target,
         self.before, self.other, self.app) = support.setup_scope()
        self.addCleanup(support.doCleanups)
        self.steps = []
        self.requests = []
        self.errors = []
        self.source_sha = None
        self.old_source_sha = None
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.armed = threading.Event()
        self.held = threading.Event()
        self.old_task_id = None
        self.new_task_id = None
        self.late_get_receipt = None
        self.versions_before = self.f.projects.list_project_versions(self.f.token, self.f.project_id)
        self.original_shots = self.script.read_current(self.f.token, **self.support.base.scope)["shots"]

    def open_browser(self, *, old_source=False, delayed_get=False):
        if delayed_get:
            @self.app.middleware("http")
            async def hold_first_actual_restore(request, call_next):
                response = await call_next(request)
                if (request.method == "GET"
                        and request.url.path.endswith("/image-workspace/assistant")
                        and self.armed.is_set() and not self.held.is_set()):
                    self.held.set()
                    await run_in_threadpool(self.release.wait, 45)
                return response

        self.page, self.origin = self.open_page(self.app, self.f.token, self.f.workspace_id)
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("response", lambda response: self.requests.append({
            "method": response.request.method, "path": urlparse(response.url).path,
            "status": response.status}) if "/api/" in response.url else None)
        source = Path(__file__).resolve().parents[1] / "mvp_v2" / "ui" / "app.js"
        self.source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
        if old_source:
            def serve_old_restore(route):
                actual = route.fetch()
                self.assertEqual(actual.status, 200)
                text = actual.text()
                start = text.index("async function restoreImageBatch(projectId) {")
                end = text.index("async function saveImageBatch(button)", start)
                replaced = text[:start] + OLD_RESTORE + text[end:]
                self.old_source_sha = hashlib.sha256(replaced.encode()).hexdigest()
                route.fulfill(response=actual, body=replaced)
            self.page.route("**/assets/app.js?*", serve_old_restore)
        self.route = self.origin + f"/projects/{self.f.project_id}"

    def snapshot(self, label):
        self.steps.append({"label": label, "url": self.page.url,
            "document_batches": self.page.locator("[data-image-batch]").evaluate_all(
                "nodes => nodes.map(node => ({task_id:node.dataset.imageBatch, "
                "parent_id:node.parentElement.id, in_feed:Boolean(node.closest('#agent-feed')), "
                "visible:node.getClientRects().length>0, parent_hidden:node.parentElement.hidden}))"),
            "host_text": self.page.locator("#creative-image-flow").text_content(),
            "script_host_hidden": self.page.locator("#formal-script-revision-proposal").get_attribute("hidden"),
            "assistant_open": self.page.locator("#creative-assistant").get_attribute("open") is not None})

    def open_assistant_summary(self):
        assistant = self.page.locator("#creative-assistant")
        if assistant.get_attribute("open") is None:
            assistant.locator(":scope > summary").click()
        expect(assistant).to_have_attribute("open", "")
        return assistant

    def assert_script_host_visible(self):
        host = self.page.locator("#formal-script-revision-proposal")
        expect(host.locator("[data-image-batch]")).to_have_count(1)
        expect(host.locator("[data-image-batch]")).to_have_attribute("data-image-batch", self.new_task_id)
        expect(host).to_be_visible()
        expect(host.locator("[data-image-batch]")).to_be_visible()
        expect(host.locator("[data-image-batch-cancel]")).to_be_visible()
        expect(host).not_to_contain_text("已取消建议")

    def proposal_write_count(self):
        return sum(item["method"] == "POST"
            and item["path"].endswith("/image-workspace/assistant") for item in self.requests)

    def old_cancelled_feed(self, *, delay_images_navigation=False):
        self.page.goto(self.route + "/assistant")
        expect(self.page.locator("#agent-session-status")).to_have_text("会话已连接")
        self.page.locator("#agent-message").fill("第2镜和第3镜换图")
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and urlparse(response.url).path.endswith("/messages")) as completed:
            self.page.locator("#agent-message-submit").click()
        self.assertEqual(completed.value.status, 200, completed.value.text())
        self.old_task_id = completed.value.json()["task"]["task_id"]
        old_card = self.page.locator(f'#agent-feed [data-image-batch="{self.old_task_id}"]')
        expect(old_card).to_be_visible()
        self.assertEqual(len(completed.value.json()["response"]["shots"]), 2)
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and urlparse(response.url).path.endswith("/cancel")) as cancelled:
            old_card.locator("[data-image-batch-cancel]").click()
        self.assertEqual(cancelled.value.status, 200, cancelled.value.text())
        self.assertEqual(cancelled.value.json()["task"]["status"], "cancelled")
        expect(old_card).to_contain_text("已取消建议")
        self.snapshot("old-cancelled-task-in-earlier-agent-feed")
        if delay_images_navigation:
            self.armed.set()
            old_card.locator("[data-image-batch-check]").click()
        else:
            self.page.locator('.project-nav [data-route="script"]').click()
        expect(self.page.locator("#creative-shot-count")).to_have_text(
            f'{len(self.original_shots)} 镜')
        if not delay_images_navigation:
            self.page.wait_for_load_state("networkidle")

    def enter_images(self):
        images_tab = self.page.locator('[data-creative-panel="images"]')
        if images_tab.get_attribute("aria-selected") != "true":
            images_tab.click()
        expect(self.page.locator("#creative-image-scope")).to_be_visible()
        self.page.locator("[data-creative-batch-toggle]").click()
        for shot in self.current["shots"][1:3]:
            self.page.locator(f'[data-creative-select-shot="{shot["shot_id"]}"]').check()
        self.page.locator("#creative-image-scope").select_option("all")

    def new_proposal(self):
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and urlparse(response.url).path.endswith("/image-workspace/assistant")) as completed:
            self.page.locator('[data-creative-match-images][data-image-batch-mode="replace_selected"]').click()
        self.assertEqual(completed.value.status, 200, completed.value.text())
        result = completed.value.json()
        self.new_task_id = result["task"]["task_id"]
        self.assertNotEqual(self.new_task_id, self.old_task_id)
        self.assertEqual(result["task"]["status"], "waiting_user")
        self.assertEqual(result["response"]["affected_shot_ids"],
            [shot["shot_id"] for shot in self.current["shots"][1:3]])
        return result

    def assert_new_host(self):
        host = self.page.locator("#creative-image-flow")
        expect(host.locator("[data-image-batch]")).to_have_count(1)
        expect(host.locator("[data-image-batch]")).to_have_attribute("data-image-batch", self.new_task_id)
        expect(host.locator("[data-image-batch-cancel]")).to_be_visible()
        expect(host).not_to_contain_text("已取消建议")
        expect(self.page.locator(f'#agent-feed [data-image-batch="{self.old_task_id}"]')).to_have_count(1)
        self.assert_unchanged_business()
        self.assertEqual(self.errors, [])

    def assert_unchanged_business(self):
        self.assertEqual(self.f.workspace.read_progress(self.f.token, **self.support.base.scope), self.before)
        self.assertEqual(self.script.read_current(self.f.token, **self.support.base.scope)["shots"], self.original_shots)
        self.assertEqual(self.f.projects.list_project_versions(self.f.token, self.f.project_id), self.versions_before)
        for task_id, status in ((self.old_task_id, "cancelled"), (self.new_task_id, "waiting_user")):
            actual = self.support.revision.read_image_batch(self.f.token,
                **self.support.base.scope, task_id=task_id)
            self.assertEqual(actual["task"]["status"], status)

    def test_old_cancelled_feed_cannot_replace_new_post_result_after_workspace_reread(self):
        self.open_browser()
        self.old_cancelled_feed()
        self.open_assistant_summary()
        expect(self.page.locator('#formal-script-revision-proposal [data-image-batch]')).to_have_attribute(
            "data-image-batch", self.old_task_id)
        expect(self.page.locator("#formal-script-revision-proposal")).to_be_visible()
        self.snapshot("ordinary-summary-opens-existing-cancelled-proposal-before-new-post")
        self.enter_images()
        self.page.wait_for_load_state("networkidle")
        self.new_proposal()
        self.page.wait_for_load_state("networkidle")
        self.assert_new_host()
        self.snapshot("new-post-and-workspace-reread-retain-current-host")
        self.page.locator('[data-creative-panel="script"]').click()
        self.snapshot("script-host-before-latest-task-assertion")
        expect(self.page.locator("#creative-assistant")).to_have_attribute("open", "")
        self.assert_script_host_visible()
        self.snapshot("already-open-assistant-shows-new-task-after-crosspanel-return")
        for index in range(2):
            summary = self.page.locator("#creative-assistant > summary")
            summary.click()
            expect(self.page.locator("#creative-assistant")).not_to_have_attribute("open", "")
            summary.click()
            self.assert_script_host_visible()
            self.snapshot(f"ordinary-summary-reopen-{index + 1}-keeps-new-visible-task")
        self.page.locator('[data-creative-panel="images"]').click()
        self.assert_new_host()
        self.snapshot("return-to-images-retains-same-new-task")
        self.page.reload()
        expect(self.page.locator('#creative-image-flow [data-image-batch]')).to_have_attribute(
            "data-image-batch", self.new_task_id)
        self.assert_unchanged_business()
        self.snapshot("reload-restores-latest-authoritative-task")
        self.page.locator('[data-creative-panel="script"]').click()
        self.open_assistant_summary()
        self.assert_script_host_visible()
        self.snapshot("reloaded-script-summary-restores-current-task-without-post")
        before_posts = self.proposal_write_count()
        self.assertEqual(before_posts, 1)
        self.page.locator('.project-nav [data-route="assistant"]').click()
        expect(self.page.locator("#agent-session-status")).to_have_text("会话已连接")
        with self.page.expect_response(lambda response: response.request.method == "GET"
                and "/api/agent/sessions/" in response.url) as refreshed:
            self.page.locator("#agent-refresh").click()
        self.assertEqual(refreshed.value.status, 200, refreshed.value.text())
        expect(self.page.locator("#agent-session-status")).to_have_text("会话已连接")
        self.snapshot("project-assistant-normal-refresh-does-not-create-another-proposal")
        self.page.locator('.project-nav [data-route="script"]').click()
        expect(self.page.locator("#creative-shot-count")).to_have_text(f'{len(self.original_shots)} 镜')
        self.open_assistant_summary()
        self.assert_script_host_visible()
        self.assertEqual(self.proposal_write_count(), before_posts)
        self.assert_unchanged_business()
        self.assertEqual(self.errors, [])
        self.snapshot("same-host-unhidden-after-normal-assistant-exit-and-script-summary-return")

    def test_late_real_get_does_not_replace_newer_post_host(self):
        self.open_browser(delayed_get=True)
        self.old_cancelled_feed(delay_images_navigation=True)
        self.enter_images()
        self.assertTrue(self.held.wait(15), "normal image navigation did not start restore GET")
        self.new_proposal()
        self.assert_new_host()
        self.snapshot("new-post-completed-before-old-get-response")
        with self.page.expect_response(lambda response: response.request.method == "GET"
                and urlparse(response.url).path.endswith("/image-workspace/assistant")) as delayed:
            self.release.set()
        self.assertEqual(delayed.value.status, 200, delayed.value.text())
        self.assertEqual(delayed.value.json()["task"]["task_id"], self.old_task_id)
        self.late_get_receipt = {"task_id": delayed.value.json()["task"]["task_id"],
            "status": delayed.value.json()["task"]["status"], "http_status": delayed.value.status,
            "released_after_new_post": self.new_task_id is not None}
        self.page.wait_for_load_state("networkidle")
        self.assert_new_host()
        self.snapshot("late-old-get-keeps-new-post-card")

    def test_old_source_negative_control_moves_cancelled_card_over_new_task(self):
        self.open_browser(old_source=True)
        self.old_cancelled_feed()
        self.enter_images()
        self.page.wait_for_load_state("networkidle")
        self.new_proposal()
        self.page.wait_for_load_state("networkidle")
        expect(self.page.locator('#creative-image-flow [data-image-batch]')).to_have_attribute(
            "data-image-batch", self.old_task_id)
        expect(self.page.locator("#creative-image-flow")).to_contain_text("已取消建议")
        expect(self.page.locator(f'[data-image-batch="{self.new_task_id}"]')).to_have_count(0)
        expect(self.page.locator(f'#agent-feed [data-image-batch="{self.old_task_id}"]')).to_have_count(0)
        self.assert_unchanged_business()
        self.assertEqual(self.errors, [])
        self.snapshot("old-source-reproduces-cancelled-card-move-and-new-card-loss")

    def tearDown(self):
        configured = os.environ.get("MPLUS_ROUND3_BATCH_RESTORE_ARTIFACTS", "").strip()
        if not configured or not hasattr(self, "page"):
            return
        directory = Path(configured).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(directory / f"{self._testMethodName}.png"), full_page=True)
        (directory / f"{self._testMethodName}.json").write_text(json.dumps({
            "steps": self.steps, "requests": self.requests, "page_errors": self.errors,
            "old_task_id": self.old_task_id, "new_task_id": self.new_task_id,
            "late_get_receipt": self.late_get_receipt,
            "source_app_js_sha256": self.source_sha, "negative_app_js_sha256": self.old_source_sha,
            "actual_http_authorities_and_sqlite": True,
            "browser_application_state_injection": False, "business_api_mock": False,
            "external_provider_calls": 0, "external_generation_setup": "anonymous deterministic fixture",
            "scope": "task card restoration only; fixture preview failures do not certify thumbnail or image quality",
            "retained_user_projects_verified": False, "production_runtime_verified": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
