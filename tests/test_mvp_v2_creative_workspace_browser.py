from __future__ import annotations

from copy import deepcopy
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
from threading import Thread
from urllib.parse import quote, urlparse
import unittest

from playwright.sync_api import expect, sync_playwright

from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID, UI_ROOT, _handler
from tests.test_mvp_v2_workbench_clarity_browser import _ClarityFixture


class _CreativeFixture(_ClarityFixture):
    """Anonymous UI boundary fixture; real service journeys remain separate."""

    def __init__(self):
        super().__init__()
        self.image_workspace_enabled = True
        self.script_reads = 0
        self.body = {}
        self.writes = []
        self.progress_revision = 0
        self.progress_selections = {}
        self.fail_progress_once = False
        self.script_revision = 0
        self.base_version_id = "version-browser-current"
        self.preview_review = False
        self.trusted_source = False
        self.analysis_enabled = False
        self.analysis_confirmed = False
        self.replace_identity_on_save = False
        self.shots = [{
            "shot_id": f"shot-stable-{index}", "shot_no": str(index),
            "structure": "开场" if index == 1 else "主体", "duration_seconds": 20,
            "visual": f"匿名活动画面 {index}", "sound": "现场环境声", "voiceover": "",
            "subtitle": "", "notes": "", "camera_type": "平视", "shot_size": "中景",
            "camera_move": "固定", "assistant_locked": False,
            "source": {"file": "匿名来源.xlsx", "sheet": "镜头", "row": index + 1},
        } for index in (1, 2, 3)]

    def script(self):
        return {
            "state": "editing" if self.script_revision else "ready",
            "project_name": "匿名创作项目", "base_version_id": self.base_version_id,
            "project_version": self.project_version, "version_label": "版本 1",
            "revision": self.script_revision, "can_save_version": bool(self.script_revision),
            "can_undo": bool(self.script_revision), "can_redo": False,
            "shots": deepcopy(self.shots), "versions": [], "pending_confirmations": [],
        }

    def image_workspace(self):
        value = super().image_workspace()
        value["base_version_id"] = self.base_version_id
        identities = {shot["shot_no"]: shot["shot_id"] for shot in self.shots}
        for shot in value.get("shots", []):
            shot["shot_id"] = identities[shot["shot_no"]]
        return value

    def response(self, method, path):
        route = urlparse(path).path
        if method in {"POST", "PATCH"}:
            self.writes.append((route, deepcopy(self.body)))
        if route.endswith("/image-workspace/progress"):
            if method == "PATCH":
                if self.fail_progress_once:
                    self.fail_progress_once = False
                    return 503, {"detail": {"code": "UNAVAILABLE", "message": "匿名保存中断"}}
                if self.body["expected_revision"] != self.progress_revision:
                    return 409, {"detail": {"code": "IMAGE_PROGRESS_CONFLICT", "message": "进度已变化"}}
                self.progress_selections.update({item["shot_id"]: deepcopy(item) for item in self.body["selections"]})
                self.progress_revision += 1
            return 200, {"base_version_id": self.base_version_id, "project_version": self.project_version,
                "revision": self.progress_revision, "script_revision": self.script_revision,
                "selections": list(self.progress_selections.values()), "shot_count": 3,
                "selected_count": len(self.progress_selections), "pending_count": 3 - len(self.progress_selections),
                "needs_check_count": 0, "saved_at": "2026-09-10T03:00:00Z" if self.progress_revision else ""}
        if route == "/api/formal/setup":
            status, value = super().response(method, path)
            return status, {**value, "script_availability": "available"}
        if route.endswith("/script"):
            self.script_reads += 1
            return 200, self.script()
        if "/image-workspace/shots/" in route and route.endswith("/candidates"):
            number = route.split("/shots/")[1].split("/")[0]
            shot = next(item for item in self.image_workspace()["shots"] if item["shot_no"] == number)
            return 200, {"candidates": shot["candidates"], "candidate_snapshot_digest": shot["candidate_snapshot_digest"]}
        if route.endswith("/script/table") and method == "POST":
            shot = next(item for item in self.shots if item["shot_id"] == self.body["shot_id"])
            shot[self.body["field"]] = self.body["value"]
            self.script_revision += 1
            return 200, self.script()
        if route.endswith("/script/save") and self.replace_identity_on_save:
            return 200, {"task_id": "identity-change-save", "response": {
                "type": "action_proposal", "draft_only": False, "proposal_digest": "c" * 64,
                "summary": "保存结构变化后的匿名版本", "affected_shots": ["3"],
            }}
        if route == "/api/agent/tasks/identity-change-save/confirm" and self.replace_identity_on_save:
            self.base_version_id = "version-browser-restructured"
            self.project_version += 1
            self.script_revision = 0
            self.shots[2] = {**self.shots[2], "shot_id": "shot-split-new-3", "visual": "新版拆分后的第3镜画面"}
            self.image_selections[2] = "candidate-2"
            self.image_version_posts = 1
            return 200, {"task": {"status": "completed"}, "response": {"type": "formal_script_version_saved"}}
        if route.endswith("/script/save"):
            return 200, {"task_id": "unified-save", "response": {
                "type": "action_proposal", "draft_only": False, "proposal_digest": "d" * 64,
                "summary": "统一保存匿名文字与配图", "affected_shots": [shot["shot_no"] for shot in self.shots],
                "preview": {"changes": [], "image_changes": [dict(shot_id=key,
                    shot_no=key.rsplit("-", 1)[-1], before_caption="原参考图", after_caption="已选图片")
                    for key in self.progress_selections]}}}
        if route == "/api/agent/tasks/unified-save/confirm":
            for index, shot in enumerate(self.shots):
                selection = self.progress_selections.get(shot["shot_id"])
                if selection:
                    self.image_selections[index] = selection["image_key"]
            self.progress_selections.clear()
            self.script_revision = 0
            self.image_version_posts += 1
            self.project_version += 1
            return 200, {"task": {"status": "completed"}, "response": {"type": "formal_script_version_saved"}}
        if route.endswith("/review") and self.preview_review:
            return 200, {"state": "blocked", "project_version": self.project_version,
                "base_version_id": self.base_version_id, "review_started": False, "ready_for_final": False,
                "issues": [{"shot_id": "shot-stable-3", "shot_no": "3", "issue_key": "", "read_only": True,
                    "preview_only": True, "status": "open", "title": "第3镜需补图", "visual": "匿名活动画面3",
                    "repair_action": {"label": "处理本镜配图", "panel": "images", "shot_id": "shot-stable-3",
                        "href": f"/projects/{PROJECT_ID}/script?panel=images&shot_id=shot-stable-3"}}]}
        if route.endswith("/script/draft-proposals"):
            return 200, self.suggestion_response(["shot-stable-1", "shot-stable-2"])
        if route.endswith("/images/candidate-1/analysis") and method == "POST":
            return 200, self.image_readiness()
        if "/script/revisions/" in route and route.endswith("/selection"):
            return 200, self.suggestion_response(self.body["retained_shot_ids"], task_id="revised-suggestion")
        if self.analysis_enabled and (route.endswith("/images") or route.endswith("/images/analysis-confirmations")):
            if method == "POST":
                self.analysis_confirmed = True
            summaries = [{"image_key": f"candidate-{index}", "analysis_key": f"analysis-{index}",
                "status": "pass" if index < 3 else "blocked", "sources": [],
                "summary": {"caption": f"匿名图片{index}", "scene": "活动现场", "purpose": "活动记录", "subjects": ["活动"],
                    "tags": ["活动"], "quality": "清晰", "brand": "无",
                    "evidence_status": "verified" if self.analysis_confirmed and index == 2 else "provider_generated"}}
                for index in (1, 2, 3)]
            return 200, {"images": summaries, "total": 3, "pass": 2, "blocked": 1, "pending": 0,
                "project_version": self.project_version, "confirmed_count": 1 if self.analysis_confirmed else 0,
                "newly_confirmed_count": 1 if self.analysis_confirmed else 0, "already_confirmed_count": 0}
        if route.endswith("/images/default-candidates") and self.trusted_source:
            status, value = super().response(method, path)
            for item in value.get("library_candidates", []):
                item["source"] = {"file": "media/c3/" + "a" * 64 + ".png", "sheet": "media_assets", "row": 45}
                item["source_status_label"] = "仅有可信包内素材来源"
            return status, value
        return super().response(method, path)

    def suggestion_response(self, retained, task_id="original-suggestion"):
        suggestions = [{"shot_id": identity, "shot_no": identity.rsplit("-", 1)[1],
            "fields": ["visual", "source"], "reason": "按当前要求缩短画面表达"} for identity in retained]
        return {"task_id": task_id, "response": {"type": "action_proposal" if retained else "no_changes",
            "draft_only": True, "proposal_digest": "b" * 64, "suggestions": suggestions,
            "summary": "请核对本次修改", "affected_shots": [item["shot_no"] for item in suggestions],
            "preview": {"changes": [{"shot_id": item["shot_id"], "shot_no": item["shot_no"], "field": field,
                "before": "原内容", "after": "新内容"} for item in suggestions for field in ("visual", "source")]}}}


class CreativeWorkspaceBrowserTests(unittest.TestCase):
    def setUp(self):
        self.fixture = _CreativeFixture()
        fixture = self.fixture

        class Handler(_handler(fixture)):
            def _api(handler, method):
                fixture.body = {}
                if method in {"POST", "PATCH"} and "application/json" in handler.headers.get("Content-Type", ""):
                    fixture.body = json.loads(handler.rfile.read(int(handler.headers.get("Content-Length", "0"))) or "{}")
                return super()._api(method)

            def _file(handler):
                if urlparse(handler.path).path.startswith("/assets/"):
                    return super()._file()
                body = (UI_ROOT / "index.html").read_text().replace("__MPLUS_UI_BUILD__", "updated-anonymous-build").encode()
                handler.send_response(200)
                handler.send_header("Content-Type", "text/html; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.playwright = sync_playwright().start()
        self.addCleanup(self.playwright.stop)
        self.browser = self.playwright.chromium.launch(headless=True)
        self.addCleanup(self.browser.close)
        self.page = self.browser.new_page(viewport={"width": 1440, "height": 1000})
        self.page.set_default_timeout(7000)
        self.page.add_init_script("localStorage.setItem('mplus.agent.auth', 'browser-session');localStorage.setItem('mplus.agent.workspace', 'browser-workspace');")
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def test_pending_issues_show_current_shot_and_global_jump_survives_refresh(self):
        original = self.fixture.script
        def script_with_issues():
            model = original()
            model["pending_confirmations"] = [
                {"shot_id": "shot-stable-1", "shot_no": "1", "question": "核对第一镜场地"},
                {"shot_id": "shot-stable-3", "shot_no": "3", "question": "核对第三镜人物"},
            ]
            return model
        self.fixture.script = script_with_issues
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        pending = page.locator('#formal-script-pending')
        expect(pending.locator(':scope > ul')).to_contain_text('核对第一镜场地')
        expect(pending.locator(':scope > ul')).not_to_contain_text('核对第三镜人物')
        pending.locator('summary').click()
        expect(pending.locator('details li')).to_have_count(2)
        pending.locator('details [data-pending-shot="shot-stable-3"]').click()
        expect(page.locator('[data-creative-shot="shot-stable-3"]')).to_have_attribute('aria-current', 'true')
        expect(pending.locator(':scope > ul')).to_contain_text('核对第三镜人物')
        expect(pending.locator(':scope > ul')).not_to_contain_text('核对第一镜场地')
        page.reload()
        expect(page.locator('[data-creative-shot="shot-stable-3"]')).to_have_attribute('aria-current', 'true')
        expect(pending.locator(':scope > ul')).to_contain_text('核对第三镜人物')
        page.locator('[data-creative-shot="shot-stable-2"]').click()
        expect(pending.locator(':scope > ul')).to_have_count(0)
        expect(pending.locator('summary')).to_have_text('全稿 2 项待核对')
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.errors, [])

    def test_review_partial_requirement_lists_missing_part_without_passing(self):
        original = self.fixture.response
        def with_partial_evidence(method, path):
            status, value = original(method, path)
            if urlparse(path).path.endswith('/review'):
                value["traceability"] = [{
                    "brief_text": "甲中庭：挂画、LED屏", "plan_shot_nos": ["1"],
                    "final_status": "pending_review", "output_evidence": [{
                        "shot_no": "1", "visual_description": "甲中庭挂画沿墙排布。",
                        "content_evidence": {"missing_requirement_parts": ["LED屏"],
                            "fragments": [{"field": "画面描述", "text": "甲中庭挂画沿墙排布"}]},
                    }],
                }]
            return status, value
        self.fixture.response = with_partial_evidence
        self.page.goto(f"{self.base}/projects/{PROJECT_ID}/review")
        self.page.locator('#formal-review-trace-title').click()
        row = self.page.locator('#formal-review-traceability tbody tr')
        expect(row).to_contain_text('成稿待复核')
        row.locator('summary').click()
        expect(row).to_contain_text('尚未定位：LED屏')
        expect(row).to_contain_text('具体动作与要求是否完成仍需人工核对')
        self.assertEqual(self.fixture.writes, [])
        self.assertEqual(self.errors, [])

    def capture(self, name):
        directory = os.environ.get("MPLUS_CREATIVE_UI_ARTIFACTS")
        if directory:
            target = Path(directory)
            target.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(target / f"{name}.png"), full_page=True)

    def test_direct_materials_batch_reads_existing_confirmed_images_before_selecting(self):
        self.fixture.image_version_posts = 1
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/materials?pick=recommended")
        page.locator('[data-library-scope]').select_option('all')
        page.get_by_role('button', name='选用可推荐图片', exact=True).click()
        expect(page.locator('[data-default-selection-summary]')).to_contain_text('已为 0 镜选图')
        expect(page.locator('[data-propose-default-images]')).to_be_disabled()
        self.assertIn(('GET', f'/api/formal/projects/{PROJECT_ID}/image-workspace'), self.fixture.requests)
        self.assertEqual(self.fixture.default_image_proposal_posts, 0)
        self.assertFalse(self.errors)

    def test_incomplete_image_progress_persists_in_new_browser_without_creating_version(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        card = page.locator('[data-image-shot="0"]')
        expect(card.locator("img").first).to_be_visible()
        card.locator('[data-image-shot-use="0"]').click()
        page.locator("#creative-save-progress").click()
        expect(page.locator("#creative-progress-status")).to_contain_text("已保存")
        self.assertEqual(len(self.fixture.progress_selections), 1)
        self.assertEqual(self.fixture.image_version_posts, 0)
        revision = self.fixture.progress_revision
        page.locator("#creative-save-progress").click()
        self.assertEqual(self.fixture.progress_revision, revision)
        fresh = self.browser.new_page()
        self.addCleanup(fresh.close)
        fresh.add_init_script("localStorage.setItem('mplus.agent.auth', 'browser-session');localStorage.setItem('mplus.agent.workspace', 'browser-workspace');")
        fresh.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        expect(fresh.locator('[data-image-shot-use="0"]')).to_contain_text("已选用")
        expect(fresh.locator("#creative-progress-status")).to_contain_text("已保存")
        self.assertFalse(self.errors)

    def test_inline_picker_browsing_does_not_select_and_dirty_text_allows_library_progress(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        page.locator('[data-script-shot-key="shot-stable-1"][data-script-field="sound"]').fill("现场新的声音")
        page.get_by_role("button", name="换图", exact=True).click()
        picker = page.get_by_role("dialog", name="第 1 镜", exact=True)
        expect(picker).to_be_visible()
        picker.get_by_role("button", name="创意库", exact=True).click()
        picker.locator('[data-picker-candidate="0"]').click()
        expect(picker.locator(".creative-picker-selected img")).to_be_visible()
        self.assertFalse(self.fixture.progress_selections)
        picker.get_by_role("button", name="暂选并保存进度").click()
        expect(picker).not_to_be_visible()
        expect(page.locator("#creative-progress-status")).to_contain_text("已保存")
        self.assertEqual(self.fixture.progress_selections["shot-stable-1"]["library_candidate_key"], "library-1")
        self.assertEqual(self.fixture.script_revision, 1)
        self.assertEqual(self.fixture.default_image_proposal_posts, 0)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertFalse(self.errors)

    def test_global_image_version_includes_choices_from_separate_current_shot_views(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        for index in range(3):
            page.locator(f'[data-creative-shot="shot-stable-{index + 1}"]').click()
            button = page.locator(f'[data-image-shot-use="{index}"]')
            expect(button).to_be_enabled()
            button.click()
        page.locator("#formal-image-version-save").click()
        expect(page.locator("[data-confirm-script-revision]")).to_be_visible()
        request = next(body for route, body in reversed(self.fixture.writes) if route.endswith("/script/save"))
        self.assertEqual(set(self.fixture.progress_selections), {"shot-stable-1", "shot-stable-2", "shot-stable-3"})
        self.assertEqual(request["expected_progress_revision"], self.fixture.progress_revision)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertFalse(self.errors)

    def test_saved_library_choice_reopens_for_adoption_without_selecting_again(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        card = page.locator('[data-image-shot="0"]')
        card.locator('[data-image-picker]').click()
        picker = page.get_by_role('dialog', name='第 1 镜', exact=True)
        picker.get_by_role('button', name='创意库', exact=True).click()
        picker.locator('[data-picker-candidate="0"]').click()
        expect(picker.locator('.creative-picker-selected img')).to_be_visible()
        picker.locator('[data-picker-use]').click()
        expect(picker).not_to_be_visible()
        card.locator('[data-image-picker]').click()
        expect(picker.locator('.creative-picker-selected img')).to_be_visible()
        expect(picker.get_by_role('button', name='预览正式采用', exact=True)).to_be_enabled()
        self.assertEqual(self.fixture.default_image_proposal_posts, 0)
        self.assertEqual(self.fixture.progress_selections['shot-stable-1']['library_candidate_key'], 'library-1')
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertFalse(self.errors)

    def test_image_version_failure_keeps_progress_and_repreviews_before_retry(self):
        page = self.page
        confirms = []

        def fail_first_confirmation(route):
            confirms.append(route.request.url)
            if len(confirms) == 1:
                route.fulfill(status=422, content_type='application/json', body=json.dumps({
                    'detail': {'code': 'IMAGE_VERSION_AUDIT_FAILED',
                               'message': '逐镜图片版本未通过机器校验'},
                }))
            else:
                route.continue_()

        page.route('**/api/agent/tasks/unified-save/confirm', fail_first_confirmation)
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        for index in range(3):
            page.locator(f'[data-creative-shot="shot-stable-{index + 1}"]').click()
            page.locator(f'[data-image-shot-use="{index}"]').click()
        page.locator('#creative-save-progress').click()
        expect(page.locator('#creative-progress-status')).to_contain_text('进度已保存')
        saved = deepcopy(self.fixture.progress_selections)
        page.locator('#formal-image-version-save').click()
        try:
            expect(page.locator('[data-confirm-script-revision]')).to_be_visible()
        except AssertionError:
            self.fail(json.dumps({'writes': self.fixture.writes,
                'save': page.locator('#formal-script-save-proposal').inner_text(),
                'progress': page.locator('#creative-progress-status').inner_text()}, ensure_ascii=False))
        page.locator('[data-confirm-script-revision]').click()
        expect(page.locator('#formal-script-save-proposal')).to_be_visible()
        expect(page.locator('#formal-script-save-proposal')).to_contain_text('文件校验')
        expect(page.locator('#formal-script-save-proposal')).not_to_contain_text('正在保存')
        expect(page.locator('[data-confirm-script-revision]')).to_have_count(0)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertEqual(self.fixture.progress_selections, saved)
        page.locator('#formal-script-save').click()
        expect(page.locator('[data-confirm-script-revision]')).to_be_visible()
        previews = [body for route, body in self.fixture.writes if route.endswith('/script/save')]
        self.assertEqual(len(previews), 2)
        self.assertNotEqual(previews[0]['idempotency_key'], previews[1]['idempotency_key'])
        self.assertEqual(previews[0]['expected_progress_revision'], previews[1]['expected_progress_revision'])
        page.locator('[data-confirm-script-revision]').click()
        expect(page.locator('#formal-script-save-proposal')).to_contain_text('文字与配图已保存为一个新版本')
        expect(page.locator('#creative-next-action')).to_have_text('进入审核')
        self.assertEqual(self.fixture.image_version_posts, 1)
        self.assertFalse(self.errors)

    def test_top_image_action_focuses_existing_confirmation_without_new_proposal(self):
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        for index in range(3):
            page.locator(f'[data-creative-shot="shot-stable-{index + 1}"]').click()
            page.locator(f'[data-image-shot-use="{index}"]').click()
        page.locator('#creative-next-action').click()
        expect(page.locator('[data-confirm-script-revision]')).to_be_visible()
        first = [body for route, body in self.fixture.writes if route.endswith('/script/save')]
        self.assertEqual(len(first), 1)
        page.locator('#creative-next-action').click()
        expect(page.locator('[data-confirm-script-revision]')).to_be_focused()
        self.assertEqual([body for route, body in self.fixture.writes if route.endswith('/script/save')], first)
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertFalse(self.errors)

    def test_saved_image_version_can_enter_review_while_new_previews_load(self):
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        for index in range(3):
            page.locator(f'[data-creative-shot="shot-stable-{index + 1}"]').click()
            page.locator(f'[data-image-shot-use="{index}"]').click()
        page.locator('#formal-image-version-save').click()
        expect(page.locator('[data-confirm-script-revision]')).to_be_visible()
        pending = []
        page.route('**/images/*/preview?*', lambda route: pending.append(route))
        with page.expect_request(lambda request: '/images/' in request.url and '/preview?' in request.url):
            page.locator('[data-confirm-script-revision]').click()
        expect(page.locator('#formal-script-save-proposal')).to_be_visible()
        expect(page.locator('#formal-script-save-proposal')).to_contain_text('文字与配图已保存为一个新版本')
        expect(page.locator('#creative-next-action')).to_have_text('进入审核')
        expect(page.locator('#creative-next-action')).to_have_attribute('href', f'/projects/{quote(PROJECT_ID)}/review')
        self.assertEqual(self.fixture.image_version_posts, 1)
        for route in pending:
            route.fulfill(status=503, content_type='application/json', body='{}')
        expect(page.locator('#creative-next-action')).to_have_text('进入审核')
        self.assertFalse(self.errors)

    def test_progress_retry_reuses_request_and_retains_later_selection(self):
        self.fixture.fail_progress_once = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        page.locator('[data-image-shot-use="0"]').click()
        expect(page.locator("#creative-progress-status")).to_contain_text("可重试保存")
        page.locator('[data-creative-shot="shot-stable-2"]').click()
        page.locator('[data-image-shot-use="1"]').click()
        page.locator("#creative-save-progress").click()
        expect(page.locator("#creative-progress-status")).to_contain_text("进度已保存")
        requests = [body for route, body in self.fixture.writes if route.endswith("/progress")]
        self.assertGreaterEqual(len(requests), 3)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(set(self.fixture.progress_selections), {"shot-stable-1", "shot-stable-2"})
        self.assertEqual(self.fixture.image_version_posts, 0)

    def test_progress_conflict_keeps_local_input_until_explicit_resolution(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        expect(page.locator('[data-image-shot-use="0"]')).to_be_enabled()
        self.fixture.progress_revision = 1
        page.locator('[data-image-shot-use="0"]').click()
        expect(page.locator("#creative-progress-conflict")).to_be_visible()
        self.assertFalse(self.fixture.progress_selections)
        page.locator("[data-progress-conflict-read]").click()
        expect(page.locator("[data-progress-conflict-local]")).to_be_visible()
        self.assertFalse(self.fixture.progress_selections)
        page.locator("[data-progress-conflict-local]").click()
        expect(page.locator("#creative-progress-status")).to_contain_text("进度已保存")
        self.assertEqual(self.fixture.progress_revision, 2)
        self.assertEqual(set(self.fixture.progress_selections), {"shot-stable-1"})

    def test_late_initial_overview_does_not_restore_old_view_over_active_editing(self):
        page = self.page
        key = f"mplus.creative.view.browser-workspace:{PROJECT_ID}"
        stored = {"versionId": self.fixture.base_version_id, "shotId": "shot-stable-1", "editMode": "shot"}
        page.add_init_script("sessionStorage.setItem(%s, %s)" % (json.dumps(key), json.dumps(json.dumps(stored))))
        pending = []
        script_read = []
        page.route("**/script?workspace_id=*", lambda route: script_read.append((route, route.fetch())))
        page.route('**/overview', lambda route: pending.append((route, route.fetch())))
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        page.locator('[data-creative-edit-mode="table"]').click()
        self.assertEqual(len(script_read), 1)
        with page.expect_response(lambda r: '/script?workspace_id=' in r.url):
            script_read[0][0].fulfill(response=script_read[0][1])
        expect(page.locator('[data-creative-edit-mode="table"]')).to_have_attribute('aria-pressed', 'true')
        with page.expect_response(lambda r: r.request.method == 'POST' and '/script/table' in r.url):
            page.locator('[data-script-shot="1"][data-script-field="sound"]').fill('第1镜新声音')
            page.locator('[data-script-shot="1"][data-script-field="sound"]').press('Tab')
        self.assertEqual(len(pending), 1)
        with page.expect_response(lambda r: r.url.endswith('/overview')):
            pending[0][0].fulfill(response=pending[0][1])
        expect(page.locator('[data-creative-edit-mode="table"]')).to_have_attribute('aria-pressed', 'true')
        with page.expect_response(lambda r: r.request.method == 'POST' and '/script/table' in r.url):
            page.locator('[data-script-shot="3"][data-script-field="sound"]').fill('第3镜新声音')
            page.locator('[data-script-shot="3"][data-script-field="sound"]').press('Tab')
        self.assertEqual(self.fixture.shots[0]['sound'], '第1镜新声音')
        self.assertEqual(self.fixture.shots[2]['sound'], '第3镜新声音')
        self.assertEqual(self.errors, [])

    def test_gap_navigation_preserves_write_scope_and_pending_selection(self):
        self.fixture.image_selections[1] = ""
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        page.locator('[data-image-shot-select="0"]').select_option("candidate-2")
        expect(page.locator("#formal-image-next-incomplete")).to_be_enabled()
        before = list(self.fixture.writes)
        page.locator("#formal-image-next-gap").click()
        expect(page.locator('[data-creative-shot="shot-stable-2"]')).to_have_attribute("aria-current", "true")
        expect(page.locator("#creative-image-scope")).to_have_value("current")
        expect(page.locator('[data-image-shot-select="1"]')).to_be_disabled()
        expect(page.locator("#creative-image-scope-copy")).to_contain_text("保留原范围 1 镜")
        page.reload()
        expect(page.locator('[data-image-shot-select="1"]')).to_be_disabled()
        expect(page.locator("#creative-image-scope-copy")).to_contain_text("保留原范围 1 镜")
        page.locator("#formal-image-next-incomplete").click()
        expect(page.locator('[data-creative-shot="shot-stable-1"]')).to_have_attribute("aria-current", "true")
        expect(page.locator('[data-image-shot-select="0"]')).to_have_value("candidate-2")
        self.assertEqual(self.fixture.writes, before)
        self.assertEqual(self.errors, [])
        for width, height in [(1342, 836), (1024, 900), (390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            self.capture(f"image-navigation-{width}")

    def test_confirmed_brief_missing_fields_cannot_advertise_text_planning(self):
        readiness = {"ready": False, "code": "confirmed_brief_needs_clarification",
                     "message": "正式 Brief 缺少规划必需字段: 项目名称、视频时长"}
        self.page.route("**/planning-readiness?*", lambda route: route.fulfill(json=readiness))
        self.page.route("**/plans?*", lambda route: route.fulfill(json={"items": []}))
        self.page.goto(f"{self.base}/projects/{PROJECT_ID}/plan?mode=text-only")
        expect(self.page.locator("#formal-plan-state")).to_have_text("暂不能生成规划")
        expect(self.page.locator("#formal-plan-status-copy")).to_contain_text("项目名称、视频时长")
        expect(self.page.locator("#formal-plan-action")).to_be_hidden()
        expect(self.page.locator("#formal-plan-brief-block")).to_be_visible()
        self.assertFalse(any(path.endswith("/plans") for path, _ in self.fixture.writes))
        readiness.update(ready=True, code="", message="规划字段已核对")
        self.page.reload()
        expect(self.page.locator("#formal-plan-state")).not_to_have_text("暂不能生成规划")
        expect(self.page.locator("#formal-plan-create")).to_be_visible()
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()

    def test_legacy_routes_share_one_workspace_and_panel_switch_keeps_current_shot(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/plan")
        expect(page).to_have_url(f"{self.base}/projects/{quote(PROJECT_ID)}/script?panel=structure")
        expect(page.locator(".page.active")).to_have_count(1)
        expect(page.locator("#page-script #page-plan")).to_be_visible()
        page.locator('[data-creative-panel="script"]').click()
        page.locator('[data-creative-shot="shot-stable-2"]').click()
        page.locator('[data-script-shot-key="shot-stable-2"][data-script-field="visual"]').fill("第 2 镜正在修改的真实输入")
        page.locator('[data-creative-panel="structure"]').click()
        page.locator('[data-creative-panel="script"]').click()
        expect(page.locator('[data-script-shot-key="shot-stable-2"][data-script-field="visual"]')).to_have_value("第 2 镜正在修改的真实输入")
        expect(page.locator('[data-creative-shot="shot-stable-2"]')).to_have_attribute("aria-current", "true")
        self.assertEqual(self.fixture.script_reads, 1)
        self.assertEqual(self.errors, [])

    def test_reference_recommendations_are_not_implicit_adoption_selections(self):
        page = self.page
        proposals = []
        page.on("request", lambda request: proposals.append(request.post_data_json)
                if request.method == "POST" and "/images/default-proposals?" in request.url else None)
        page.goto(f"{self.base}/projects/{PROJECT_ID}/materials?return_to=shot-images&focus_shot=2")
        page.get_by_role("button", name="从创意库选图", exact=True).first.click()
        page.get_by_role("button", name="浏览图库", exact=True).click()
        page.get_by_role("button", name="下一页", exact=True).click()
        selected_key = f"library-{self.fixture.library_page_first_index(2)}"
        page.locator(f'[data-use-library-candidate="{selected_key}"]').click()
        page.locator("[data-propose-default-images]").click()
        expect(page.locator("[data-confirm-default-images]")).to_be_visible()
        self.assertEqual([item["shot_no"] for item in proposals[-1]["selections"]], ["2"])
        self.assertEqual(self.errors, [])

    def test_return_to_script_waits_for_current_shots_before_selection(self):
        self.fixture.preview_review = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        page.locator('[data-creative-shot="shot-stable-3"]').click()
        page.locator('nav[aria-label="项目功能"] a[data-route="review"]').click()
        expect(page.locator("#page-review")).to_be_visible()
        pending = []
        pattern = "**/api/formal/projects/*/script?**"
        page.route(pattern, lambda route: pending.append(route))
        try:
            with page.expect_request(pattern):
                page.locator('nav[aria-label="项目功能"] a[data-route="script"]').click()
            expect(page.locator("#formal-script-table")).to_contain_text("正在读取当前脚本")
            # The preceding page still has an outline in the DOM. It must not
            # offer those shots while the authoritative current script is null.
            expect(page.locator("[data-creative-shot]")).to_have_count(0)
            expect(page.locator("[data-creative-select-shot]")).to_have_count(0)
            self.capture("return-to-script-loading")
            self.assertEqual(len(pending), 1)
            pending.pop().continue_()
            expect(page.locator("[data-creative-shot]")).to_have_count(3)
            page.locator('[data-creative-shot="shot-stable-2"]').click()
            expect(page.locator('[data-creative-shot="shot-stable-2"]')).to_have_attribute("aria-current", "true")
            expect(page.locator('[data-script-shot-key="shot-stable-2"][data-script-field="visual"]')).to_be_visible()
            expect(page.locator("#creative-location-notice")).to_be_hidden()
            page.locator('nav[aria-label="项目功能"] a[data-route="review"]').click()
            expect(page.locator("#page-review")).to_be_visible()
            with page.expect_request(pattern):
                page.locator('nav[aria-label="项目功能"] a[data-route="script"]').click()
            expect(page.locator("[data-creative-shot]")).to_have_count(0)
            self.assertEqual(len(pending), 1)
            pending.pop().fulfill(status=503, content_type="application/json", body='{"detail":{"code":"TEMPORARILY_UNAVAILABLE"}}')
            expect(page.locator("#formal-script-table")).to_contain_text("脚本暂不可用")
            expect(page.locator("#creative-shot-count")).to_have_text("暂不可用")
            expect(page.locator("[data-creative-shot]")).to_have_count(0)
            expect(page.locator("#creative-location-notice")).to_be_hidden()
            self.assertEqual(self.fixture.writes, [])
            self.assertEqual(self.errors, [])
        finally:
            for route in pending:
                route.abort()
            page.unroute(pattern)

    def test_return_during_cancelled_script_read_starts_a_current_read(self):
        self.fixture.preview_review = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        page.locator('[data-creative-shot="shot-stable-3"]').click()
        page.locator('nav[aria-label="项目功能"] a[data-route="review"]').click()
        expect(page.locator("#page-review")).to_be_visible()
        pending, requests = [], []
        pattern = "**/api/formal/projects/*/script?**"

        def delay_first(route):
            requests.append(route.request.url)
            if len(requests) == 1:
                pending.append(route)
            else:
                route.continue_()

        page.route(pattern, delay_first)
        try:
            with page.expect_request(pattern):
                page.locator('nav[aria-label="项目功能"] a[data-route="script"]').click()
            expect(page.locator("#formal-script-table")).to_contain_text("正在读取当前脚本")
            page.locator('nav[aria-label="项目功能"] a[data-route="review"]').click()
            expect(page.locator("#page-review")).to_be_visible()
            page.locator('nav[aria-label="项目功能"] a[data-route="script"]').click()
            expect(page.locator("[data-creative-shot]")).to_have_count(3)
            self.assertEqual(len(requests), 2)
            page.locator('[data-creative-shot="shot-stable-2"]').click()
            with page.expect_response(lambda response: response.url == requests[0]) as obsolete:
                pending.pop().continue_()
            obsolete.value.finished()
            expect(page.locator('[data-creative-shot="shot-stable-2"]')).to_have_attribute("aria-current", "true")
            expect(page.locator("#creative-location-notice")).to_be_hidden()
            self.assertEqual(self.fixture.writes, [])
            self.assertEqual(self.errors, [])
        finally:
            for route in pending:
                route.abort()
            page.unroute(pattern)

    def test_inline_library_choice_preserves_other_query_without_restoring_old_target(self):
        self.fixture.image_version_posts = 1
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        first = page.locator('[data-image-shot="0"]')
        first.locator("details.ui00-image-search > summary").click()
        first.locator('[data-image-shot-query="0"]').fill("第1镜尚未查找的文字")
        expect(page.locator('[data-image-shot-confirm="0"]')).to_be_checked()
        page.locator('[data-creative-shot="shot-stable-2"]').click()
        page.locator('[data-image-picker="1"]').click()
        page.locator('[data-picker-source="library"]').click()
        page.locator('[data-picker-page="2"]').click()
        selected_index = self.fixture.library_page_first_index(2)
        page.locator('.creative-picker-card', has_text=f"匿名活动图片 {selected_index}").click()
        page.locator('[data-picker-use]').click()
        expect(page.locator('[role="dialog"]')).to_be_hidden()
        expect(page.locator('[data-image-shot="1"]')).to_contain_text("创意库暂选")
        expect(page.locator('[data-image-shot-query="0"]')).to_have_value("第1镜尚未查找的文字")
        expect(page.locator('[data-image-shot-confirm="0"]')).to_be_checked()
        self.assertEqual(self.fixture.progress_selections["shot-stable-2"]["library_candidate_key"], f"library-{selected_index}")
        self.assertEqual(self.errors, [])

    def test_library_source_uses_business_label_without_internal_path_or_digest(self):
        self.fixture.trusted_source = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images")
        page.locator('[data-image-picker="0"]').click()
        page.locator('[data-picker-source="library"]').click()
        page.locator('[data-picker-page="2"]').click()
        card = page.locator(".creative-picker-card", has_text=f"匿名活动图片 {self.fixture.library_page_first_index(2)}")
        card.click()
        detail = page.locator(".creative-picker-detail")
        expect(detail).to_contain_text("创意库素材图片")
        expect(detail).not_to_contain_text("media/c3")
        expect(detail).not_to_contain_text("media_assets")
        expect(detail).not_to_contain_text("a" * 64)
        self.assertEqual(self.errors, [])

    def test_read_only_review_repairs_exact_stable_shot_without_review_decisions(self):
        self.fixture.preview_review = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/review")
        expect(page.locator("[data-review-select]")).to_have_count(0)
        expect(page.locator("[data-review-decision]")).to_have_count(0)
        expect(page.locator("#formal-review-start")).to_be_hidden()
        page.locator("[data-review-repair]").click()
        expect(page.locator('[data-creative-shot="shot-stable-3"]')).to_have_attribute("aria-current", "true")
        expect(page.locator("#page-shot-images")).to_be_visible()
        self.assertFalse(any("/review/decisions" in route for route, _ in self.fixture.writes))
        self.assertEqual(self.errors, [])

    def test_assistant_first_request_has_scope_and_cancel_removes_whole_shot(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        page.locator('[data-creative-open-tool="assistant"]:visible').first.click()
        page.locator("#creative-assistant-scope").select_option("all")
        page.locator("#formal-script-instruction").fill("简化画面描述")
        page.locator("#formal-script-instruction-submit").click()
        expect(page.locator(".creative-shot-suggestion")).to_have_count(2)
        request = next(body for route, body in self.fixture.writes if route.endswith("/script/draft-proposals"))
        self.assertEqual(request["selected_shot_ids"], ["shot-stable-1", "shot-stable-2", "shot-stable-3"])
        page.locator('[data-creative-retain-shot="shot-stable-1"]').uncheck()
        expect(page.locator(".creative-shot-suggestion")).to_have_count(1)
        expect(page.locator(".creative-shot-suggestion")).to_contain_text("第 2 镜")
        request = next(body for route, body in self.fixture.writes if route.endswith("/selection"))
        self.assertEqual(request["retained_shot_ids"], ["shot-stable-2"])
        expect(page.locator(".creative-shot-suggestion .ui00-script-proposal-diff article")).to_have_count(2)
        self.assertEqual(self.errors, [])

    def test_new_version_requires_relocation_when_same_number_has_a_different_shot_identity(self):
        self.fixture.replace_identity_on_save = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images&shot_id=shot-stable-3")
        expect(page.locator('[data-creative-shot="shot-stable-3"]')).to_have_attribute("aria-current", "true")
        page.locator("[data-creative-batch-toggle]").click()
        page.locator('[data-creative-select-shot="shot-stable-3"]').check()
        page.locator('[data-image-shot-select="2"]').select_option("candidate-4")
        page.locator('[data-image-shot="2"] .ui00-image-search > summary').click()
        page.locator('[data-image-shot-query="2"]').fill("仅属于旧第3镜的选图意图")
        expect(page.locator('[data-image-shot-select="2"]')).to_have_value("candidate-4")

        page.locator('[data-creative-panel="script"]').click()
        old_input = page.locator('[data-script-shot-key="shot-stable-3"][data-script-field="visual"]')
        old_input.fill("旧第3镜正在编辑的画面，不可套到新版同镜号")
        expect(page.locator("#creative-next-label")).to_have_text("配图进行中 · 全稿 3 镜")
        page.locator("#creative-save > summary").click()
        page.locator("#formal-script-save-reason").fill("保存删除或拆分后的镜头结构")
        page.locator("#formal-script-save").click()
        page.locator('[data-confirm-script-revision]').click()
        expect(page.locator("#formal-script-save-proposal")).to_contain_text("文字与配图已保存为一个新版本")

        expect(page.locator("#creative-location-notice")).to_be_visible()
        expect(page.locator("#creative-location-notice")).to_contain_text("请从列表选择当前镜头")
        expect(page.locator('[data-creative-shot][aria-current="true"]')).to_have_count(0)
        expect(page.locator('[data-creative-shot="shot-stable-3"]')).to_have_count(0)
        expect(page.locator('[data-creative-select-shot="shot-split-new-3"]')).not_to_be_checked()
        fresh_input = page.locator('[data-script-shot-key="shot-split-new-3"][data-script-field="visual"]')
        expect(fresh_input).to_have_value("新版拆分后的第3镜画面")
        expect(fresh_input).to_be_hidden()
        expect(old_input).to_have_count(0)

        page.locator('[data-creative-panel="images"]').click()
        expect(page.locator('[data-creative-shot][aria-current="true"]')).to_have_count(0)
        expect(page.locator('[data-image-shot="2"]')).to_be_hidden()
        expect(page.locator("#creative-location-notice")).to_be_visible()
        page.locator('[data-creative-shot="shot-split-new-3"]').click()
        expect(page.locator("#creative-location-notice")).to_be_hidden()
        expect(page.locator('[data-image-shot-select="2"]')).to_have_value("candidate-2")
        expect(page.locator('[data-image-shot-query="2"]')).to_have_value("")
        page.locator('[data-creative-panel="script"]').click()
        expect(fresh_input).to_be_visible()
        expect(fresh_input).to_have_value("新版拆分后的第3镜画面")
        confirmation_index = next(index for index, (route, _) in enumerate(self.fixture.writes) if route == "/api/agent/tasks/identity-change-save/confirm")
        self.assertFalse(any(route.endswith("/script/table") or "image-workspace/proposals" in route for route, _ in self.fixture.writes[confirmation_index + 1:]))
        self.assertEqual(self.errors, [])

    def test_analysis_confirmation_only_sends_explicit_fresh_analysis(self):
        self.fixture.analysis_enabled = True
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/materials")
        expect(page.locator('[data-analysis-select="candidate-3"]')).to_be_disabled()
        page.locator('[data-analysis-select="candidate-2"]').check()
        page.locator("#creative-analysis-confirm").click()
        expect(page.locator("#creative-analysis-result")).to_contain_text("本次新增确认 1 张")
        request = next(body for route, body in self.fixture.writes if route.endswith("/images/analysis-confirmations"))
        self.assertEqual(request["selections"], [{"image_key": "candidate-2", "analysis_key": "analysis-2"}])
        expect(page.locator('[data-formal-image-key="candidate-2"] .ui00-image-status')).to_have_text("人工已确认")
        expect(page.locator('[data-formal-image-key="candidate-1"] .ui00-image-status')).to_have_text("分析可用")
        self.assertEqual(self.errors, [])

    def test_draft_image_action_opens_save_with_one_reused_reason(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        page.locator('[data-script-shot-key="shot-stable-1"][data-script-field="visual"]').fill("待保存的新画面")
        expect(page.locator("#creative-next-label")).to_have_text("配图进行中 · 全稿 3 镜")
        page.locator('[data-creative-panel="images"]').click()
        expect(page.locator("#creative-image-draft-gate")).to_be_hidden()
        expect(page.locator('[data-image-shot-use="0"]')).to_be_enabled()
        page.locator('[data-image-shot-use="0"]').click()
        expect(page.locator("#creative-progress-status")).to_contain_text("已保存")
        self.assertEqual(self.fixture.script_revision, 1)
        self.assertTrue(self.fixture.progress_selections["shot-stable-1"]["checked"])
        self.assertFalse(any(route.endswith("/script/save") for route, _ in self.fixture.writes))
        self.assertEqual(self.fixture.image_version_posts, 0)
        self.assertEqual(self.errors, [])

    def test_local_supplement_reloads_previously_visited_materials(self):
        from tests.test_mvp_v2_ui00_journey3 import IMAGE_BYTES

        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=images&aux=materials")
        expect(page.locator("#formal-image-total")).not_to_have_text("—")
        before_count = int(page.locator("#formal-image-total").inner_text())
        page.locator("[data-creative-close-materials]").click()
        page.locator('[data-creative-panel="images"]').click()
        expect(page.locator("[data-image-shot='0']")).to_be_visible()
        page.locator("[data-image-shot='0'] details.ui00-image-temporary-options > summary").click()
        page.locator("[data-image-supplement='0']").click()
        with page.expect_response(lambda response: response.request.method == "POST" and "/image-workspace/supplements" in response.url) as upload:
            page.locator("#formal-image-supplement-file").set_input_files({"name": "anonymous-supplement.png", "mimeType": "image/png", "buffer": IMAGE_BYTES})
        self.assertEqual(upload.value.status, 200)
        new_image_key = upload.value.json()["image_key"]
        expect(page.locator("#page-materials")).to_be_visible()
        expect(page.locator(f'[data-formal-image-key="{new_image_key}"]')).to_be_visible()
        expect(page.locator("#formal-image-total")).to_have_text(str(before_count + 1))
        expect(page.locator(f'[data-formal-image-key="{new_image_key}"]')).to_contain_text("待理解")
        self.assertEqual(self.errors, [])

    def test_image_analysis_return_restores_keyboard_focus_to_original_shot(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/materials?return_to=shot-images&focus_shot=1&supplement_image_key=candidate-1")
        card = page.locator('[data-formal-image-key="candidate-1"]')
        expect(card).to_be_visible()
        details = card.locator("details.ui00-image-review")
        if details.get_attribute("open") is None:
            details.locator("summary").click()
        form = card.locator("[data-formal-image-form]")
        for field, value in {"caption": "匿名补图现场", "tags": "活动,现场", "subjects": "匿名人物", "scene": "活动现场", "purpose": "第1镜补图", "quality": "清晰", "brand": "无", "evidence": "已核对实际图片和来源"}.items():
            form.locator(f'[name="{field}"]').fill(value)
        form.locator('button[type="submit"]').click()
        expect(page.locator("#page-shot-images")).to_be_visible()
        expect(page.locator('[data-creative-shot="shot-stable-1"]')).to_have_attribute("aria-current", "true")
        expect(page.locator('[data-image-shot="0"]')).to_be_visible()
        page.wait_for_function("document.activeElement?.closest('[data-image-shot=\"0\"]') !== null", timeout=5000)
        self.assertEqual(self.errors, [])

    def test_more_visible_button_edges_toggle_once_and_close_with_outside_or_escape(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        expect(page.locator('[data-creative-shot="shot-stable-1"]')).to_be_visible()
        menu = page.locator(".creative-more")
        summary = menu.locator("summary")
        for width in (375, 1024, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            box = summary.bounding_box()
            self.assertIsNotNone(box)
            # The visible border and padding belong to the clickable control.
            for x, y in ((3, 3), (box["width"] - 3, box["height"] - 3)):
                menu.click(position={"x": x, "y": y})
                expect(menu).to_have_attribute("open", "")
                menu.click(position={"x": x, "y": y})
                expect(menu).not_to_have_attribute("open", "")
            summary.click()
            page.locator("#creative-tab-script").click()
            expect(menu).not_to_have_attribute("open", "")
            summary.click()
            page.keyboard.press("Escape")
            expect(menu).not_to_have_attribute("open", "")
            expect(summary).to_be_focused()

    def test_structure_adjustments_and_low_frequency_entries_work_with_keyboard_at_three_widths(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        expect(page.locator('[data-creative-shot="shot-stable-1"]')).to_be_visible()
        page.locator("#creative-tab-script").focus()
        page.keyboard.press("ArrowLeft")
        expect(page.locator("#page-plan")).to_be_visible()
        expect(page.locator("#formal-plan-template")).not_to_have_text("正在读取…")
        expect(page.locator(".creative-plan-adjustments")).to_contain_text("主体段补充一镜活动细节")
        expect(page.locator(".creative-plan-adjustments")).to_contain_text("1镜 → 2镜")
        for width in (375, 1024, 1440):
            page.set_viewport_size({"width": width, "height": 1000})
            more = page.locator(".creative-more > summary")
            more.focus()
            page.keyboard.press("Enter")
            expect(page.locator('.creative-more a#formal-script-version-governance')).to_be_visible()
            expect(page.locator('.creative-more [data-creative-open-tool="records"]')).to_be_visible()
            self.assertLessEqual(page.locator("html").evaluate("el => el.scrollWidth"), width + 1)
            self.capture(f"creative-structure-more-{width}")
            more.focus()
            page.keyboard.press("Enter")
            for entry, target, expected in (
                ('[data-creative-open-tool="records"]', "#formal-script-versions", "尚无正式版本"),
            ):
                page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
                expect(page.locator('[data-creative-shot="shot-stable-1"]')).to_be_visible()
                page.locator(".creative-more > summary").focus()
                page.keyboard.press("Enter")
                control = page.locator(f".creative-more {entry}")
                expect(control).to_be_visible()
                for _ in range(12):
                    page.keyboard.press("Tab")
                    if control.evaluate("element => element === document.activeElement"):
                        break
                expect(control).to_be_focused()
                page.keyboard.press("Enter")
                expect(page.locator(target)).to_be_visible()
                expect(page.locator(target)).to_contain_text(expected)
                self.assertLessEqual(page.locator("html").evaluate("el => el.scrollWidth"), width + 1)
                self.capture(f"creative-entry-{target.removeprefix('#')}-{width}")
            page.goto(f"{self.base}/projects/{PROJECT_ID}/script?panel=structure")
        self.assertEqual(self.errors, [])

    def test_keyboard_panel_choice_survives_late_initial_script_response(self):
        page = self.page
        pending = []
        setup_pending = []
        page.route("**/api/formal/setup", lambda route: setup_pending.append(route))
        page.route("**/api/formal/projects/*/script?*", lambda route: pending.append(route))
        with page.expect_request(lambda request: urlparse(request.url).path == "/api/formal/setup"):
            page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        # Playwright focus() can target hidden markup before setup completes;
        # a user cannot interact with these tabs until the page is displayed.
        expect(page.locator("#creative-tab-script")).to_be_hidden()
        self.assertEqual(len(setup_pending), 1)
        with page.expect_request(lambda request: urlparse(request.url).path.endswith("/script") and "/api/formal/projects/" in request.url):
            setup_pending.pop().continue_()
        page.wait_for_function("state.formalCreative?.loadPromise != null")
        self.assertEqual(len(pending), 1)
        tab = page.locator("#creative-tab-script")
        expect(tab).to_be_visible()
        expect(tab).to_be_enabled()
        tab.focus()
        page.keyboard.press("ArrowLeft")
        expect(page.locator("#page-plan")).to_be_visible()
        expect(page.locator("#creative-tab-structure")).to_have_attribute("aria-selected", "true")
        pending.pop().continue_()
        expect(page.locator('[data-creative-shot="shot-stable-1"]')).to_be_attached()
        expect(page.locator("#formal-plan-template")).not_to_have_text("正在读取…")
        expect(page.locator("#page-plan")).to_be_visible()
        expect(page.locator("#creative-tab-structure")).to_have_attribute("aria-selected", "true")
        self.assertEqual(self.errors, [])

    def test_three_widths_keep_one_workspace_and_no_document_overflow(self):
        page = self.page
        page.goto(f"{self.base}/projects/{PROJECT_ID}/script")
        expect(page.locator('[data-creative-shot="shot-stable-1"]')).to_be_visible()
        for width in (1440, 1024, 375):
            page.set_viewport_size({"width": width, "height": 1000})
            expect(page.locator(".page.active")).to_have_count(1)
            self.assertLessEqual(page.locator("html").evaluate("el => el.scrollWidth"), width + 1)
            self.capture(f"creative-workspace-{width}")
        self.assertEqual(self.errors, [])

    def test_reuse_reason_reaches_hidden_same_content_and_preserves_other_images(self):
        original = self.fixture.image_workspace
        def model():
            value = original()
            for shot in value['shots']:
                for candidate in shot['candidates']:
                    candidate['content_sha256'] = ('a' if candidate['image_key'] in ('candidate-1', 'candidate-2') else 'b') * 64
                    candidate['caption'] = '同名图片.png'
                    candidate['sources'] = [
                        {'file': '匿名图库.xlsx', 'sheet': '创意库', 'row': 11},
                        {'file': '匿名图库.xlsx', 'sheet': '创意库', 'row': 42},
                    ]
                shot['reuse_reason'] = '原手工理由' if shot['shot_no'] == '2' else ''
            return value
        self.fixture.image_workspace = model
        self.fixture.image_selections = ['candidate-1', 'candidate-2', 'candidate-3']
        page = self.page
        page.goto(f'{self.base}/projects/{PROJECT_ID}/script?panel=images')
        field = page.locator('[data-image-shot-reuse-reason="0"]')
        expect(field).to_be_visible()
        provenance = "state.formalImageWorkspace.shots.map(s => ({id:s.shot_id, key:s.selected_image_key, candidates:s.candidates}))"
        before_sources = page.evaluate(provenance)
        field.fill('同场景不同角度说明')
        field.blur()
        button = page.locator('[data-image-reuse-apply="0"]')
        expect(button).to_have_text('应用到另外 1 镜')
        dialogs = []
        def accept(dialog):
            dialogs.append(dialog.message)
            dialog.accept()
        page.once('dialog', accept)
        with page.expect_response(lambda r: r.request.method == 'PATCH' and '/image-workspace/progress?' in r.url):
            button.click()
        self.assertIn('原手工理由', dialogs[0])
        self.assertEqual(page.evaluate(provenance), before_sources)
        page.reload()
        page.locator('#creative-image-scope').select_option('all')
        expect(page.locator('[data-image-shot-reuse-reason="1"]')).to_have_value('同场景不同角度说明')
        expect(page.locator('[data-image-shot-reuse-reason="2"]')).to_have_value('')
        self.assertEqual(page.evaluate(provenance), before_sources)
        self.assertFalse(self.fixture.progress_selections['shot-stable-2']['checked'])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
