"""Anonymous HTTP/SQLite regression for accepted-draft planning round trips.

Navigation, authority services and persistence are real. Creative/OCR/vision
outputs are deterministic fixtures; retained user projects and actual image
recognition/human quality are outside this regression's scope.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import unittest
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect

from mvp_v2.creative_generation import repository
from mvp_v2.formal_generation_service import FormalVisionProviderPolicy
from mvp_v2.image_understanding import ProjectImageVisionResult
from tests import test_mvp_v2_rounds_repair_browser as rounds_fixture


class Round3PlanMaterialsReturnBrowserTests(unittest.TestCase):
    def setUp(self):
        self.helper = rounds_fixture.RoundsRepairBrowserTests("runTest")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.page = self.helper.page
        self.steps = []
        self.plan_requests = []
        self.vision_calls = []
        self.helper.generation.vision_provider_policy = FormalVisionProviderPolicy(
            invoke=self.anonymous_vision, provider="anonymous", model="return-route-vision",
            prompt_version="anonymous-navigation-v1")
        self.page.on("request", self.capture_plan_request)

    def anonymous_vision(self, path):
        self.vision_calls.append(Path(path).name)
        return ProjectImageVisionResult("pass", "匿名活动参考图", ("活动",), ("参考图",),
            "匿名活动场景", "用于导航回归", "匿名 fixture", "匿名团队", ("确定性图片理解 fixture",))

    def capture_plan_request(self, request):
        if request.method == "POST" and urlparse(request.url).path.endswith("/plans"):
            self.plan_requests.append(request.post_data_json)

    def snapshot(self, label):
        self.steps.append({"label": label, "url": self.page.url,
            "plan_state": self.page.locator("#formal-plan-state").text_content(),
            "image_return_href": self.page.locator("#formal-image-to-plan").get_attribute("href")})

    def tearDown(self):
        directory = os.environ.get("MPLUS_ROUND3_RETURN_BROWSER_ARTIFACTS", "").strip()
        if not directory:
            return
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{self._testMethodName}.json").write_text(json.dumps({
            "steps": self.steps, "plan_requests": self.plan_requests,
            "http_requests": self.helper.requests, "page_errors": self.helper.errors,
            "vision_provider_calls": self.vision_calls,
            "actual_http_authorities_and_sqlite": True,
            "external_models": "deterministic creative/OCR/vision fixtures",
            "retained_user_projects_verified": False, "human_image_quality_verified": False,
            "browser_application_state_injection": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def accepted_draft(self, *, image=False):
        self.helper.ready_brief(image=image)
        self.page.locator("#brief-enter-creative").click()
        self.page.locator("#workbench-recommend-after-generation").uncheck()
        self.page.locator("#creative-first-confirmed-start").click()
        expect(self.page.locator("#creative-first-blocks .creative-first-block")).to_have_count(8)
        expect(self.page.locator("#creative-first-plan-link")).to_have_attribute("aria-disabled", "false")
        draft = self.helper.draft()
        self.assertEqual(len(draft["blocks"]), 8)
        persisted = repository(self.helper.generation, self.helper.fixture.token,
            self.helper.fixture.workspace_id, self.helper.project["project_id"]).read()
        self.accepted_sources = {shot["shot_id"]: shot["source_refs"]
            for shot in persisted["body"]["draft"]["shots"]}
        return draft

    def assert_query(self, url, *, creative, text_only=False, materials=False):
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query.get("creative"), ["1"] if creative else None)
        self.assertEqual(query.get("mode"), ["text-only"] if text_only else None)
        self.assertEqual(query.get("return_to"), ["plan"] if materials else None)

    def go_to_materials(self, *, creative, text_only=False):
        block = self.page.locator("#formal-plan-image-block")
        expect(block).to_be_visible()
        link = block.locator("a").nth(0)
        self.assert_query(link.get_attribute("href"), creative=creative,
            text_only=text_only, materials=True)
        link.click()
        expect(self.page.locator("#formal-image-workbench")).to_be_visible()
        expect(self.page.locator("#formal-image-state")).to_have_text(
            re.compile("需要处理|待选择|全部可用|部分可用"))
        self.assert_query(self.page.url, creative=creative, text_only=text_only, materials=True)
        self.snapshot("project-materials")

    def return_and_create(self, *, creative, text_only, draft):
        link = self.page.locator("#formal-image-to-plan")
        self.assert_query(link.get_attribute("href"), creative=creative, text_only=text_only)
        expect(link).not_to_have_attribute("aria-disabled", "true")
        link.click()
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        self.assert_query(self.page.url, creative=creative, text_only=text_only)
        self.page.reload()
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        self.assert_query(self.page.url, creative=creative, text_only=text_only)
        self.snapshot("returned-and-reloaded")
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and urlparse(response.url).path.endswith("/plans")) as completed:
            self.page.locator("#formal-plan-create").click()
        self.assertEqual(completed.value.status, 200, completed.value.text())
        self.assertEqual(len(self.plan_requests), 1)
        payload = self.plan_requests[0]
        self.assertEqual(payload["generation_mode"], "text_only_draft" if text_only else "image_backed")
        preview = completed.value.json()
        if creative:
            self.assertEqual(payload["expected_creative_revision"], draft["revision"])
            self.assertEqual(preview["creative_conversion"]["block_count"], 8)
            event = self.helper.generation._plan_event(self.helper.fixture.token, preview["plan_id"])
            binding = event["creative_draft_binding"]
            self.assertEqual(binding["draft_id"], draft["draft_id"])
            self.assertEqual(binding["blocks"], [{"block_id": block["block_id"],
                "fields": block["fields"], "source_refs": self.accepted_sources[block["block_id"]]}
                for block in draft["blocks"]])
        else:
            self.assertNotIn("expected_creative_revision", payload)
            self.assertNotIn("creative_conversion", preview)
        self.assertEqual(self.helper.draft()["revision"], draft["revision"])
        self.assertEqual(self.helper.errors, [])
        self.snapshot("actual-plan-created")

    def test_image_backed_draft_binding_survives_normal_materials_return_and_reload(self):
        draft = self.accepted_draft(image=True)
        self.page.locator("#creative-first-plan-link").click()
        self.assert_query(self.page.url, creative=True)
        self.go_to_materials(creative=True)
        synchronize = self.page.locator("#formal-image-synchronize")
        expect(synchronize).to_be_visible()
        synchronize.click()
        expect(self.page.locator("#formal-image-understand")).to_be_enabled()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/images/understand?" in response.url) as understood:
            self.page.locator("#formal-image-understand").click()
        self.assertEqual(understood.value.status, 200, understood.value.text())
        self.assertEqual(understood.value.json()["pass"], 1)
        self.assertEqual(len(self.vision_calls), 1)
        self.return_and_create(creative=True, text_only=False, draft=draft)

    def test_text_only_and_creative_both_survive_normal_materials_return(self):
        draft = self.accepted_draft()
        self.page.locator("#creative-first-plan-link").click()
        block = self.page.locator("#formal-plan-image-block")
        expect(block).to_be_visible()
        block.locator("a").nth(1).click()
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        self.assert_query(self.page.url, creative=True, text_only=True)
        self.go_to_materials(creative=True, text_only=True)
        self.return_and_create(creative=True, text_only=True, draft=draft)

    def test_normal_structure_entry_binds_existing_initial_draft_through_materials(self):
        draft = self.accepted_draft()
        self.page.goto(self.helper.route + "/script?panel=structure&mode=text-only")
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        self.go_to_materials(creative=True, text_only=True)
        self.return_and_create(creative=True, text_only=True, draft=draft)

    def test_structure_tab_saves_pending_edits_then_converts_the_current_initial_draft(self):
        original = self.accepted_draft()
        first = original["blocks"][0]
        self.page.locator(f'[data-workbench-open="{first["block_id"]}"]').click()
        field = self.page.locator(f'[data-first-block="{first["block_id"]}"][data-first-field="声音"]')
        pending = []
        self.page.route('**/creative-draft/changes?*', lambda route: pending.append(route))
        changed = "木吉他延续，加入现场轻声交流，无旁白"
        field.fill(changed)
        with self.page.expect_request(lambda request: request.method == 'POST'
                and '/creative-draft/changes?' in request.url):
            self.page.locator('[data-creative-panel="structure"]').click()
        self.assertEqual(len(pending), 1)
        self.assertNotEqual(parse_qs(urlparse(self.page.url).query).get('panel'), ['structure'])
        pending[0].continue_()
        expect(self.page.locator('#workbench-current-structure li')).to_have_count(8)
        self.assert_query(self.page.url, creative=True)
        expect(self.page.locator('.ui00-script-toolbar')).not_to_be_visible()
        expect(self.page.locator('#creative-next-step')).not_to_be_visible()
        self.assertNotIn('尚无脚本版本', self.page.locator('#page-script').inner_text())
        self.assertNotIn('当前项目还没有脚本版本', self.page.locator('#page-script').inner_text())
        saved = self.helper.draft()
        self.assertGreater(saved['revision'], original['revision'])
        self.assertEqual(saved['blocks'][0]['fields']['声音'], changed)
        expect(self.page.locator('#formal-plan-create')).to_have_text('选择文字草稿')
        self.page.locator('#formal-plan-create').click()
        expect(self.page.locator('#formal-plan-create')).to_have_text('生成文字规划')
        self.page.reload()
        expect(self.page.locator('#formal-plan-create')).to_be_enabled()
        with self.page.expect_response(lambda response: response.request.method == 'POST'
                and urlparse(response.url).path.endswith('/plans')) as completed:
            self.page.locator('#formal-plan-create').click()
        self.assertEqual(completed.value.status, 200, completed.value.text())
        preview = completed.value.json()
        self.assertEqual(self.plan_requests[-1]['expected_creative_revision'], saved['revision'])
        self.assertEqual(self.plan_requests[-1]['generation_mode'], 'text_only_draft')
        self.assertEqual(preview['creative_conversion']['mapped_count'], 8)
        binding = self.helper.generation._plan_event(self.helper.fixture.token, preview['plan_id'])['creative_draft_binding']
        self.assertEqual(binding['blocks'][0]['fields']['声音'], changed)
        self.assertEqual([block['block_id'] for block in binding['blocks']],
            [block['block_id'] for block in saved['blocks']])
        self.assertEqual(self.helper.errors, [])


if __name__ == "__main__":
    unittest.main()
