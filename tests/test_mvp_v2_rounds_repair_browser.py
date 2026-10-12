"""Two-round UI regressions on anonymous HTTP, authority services and SQLite.

Only external creative/OCR model output is deterministic. These tests do not
certify the retained user projects, external model quality or final delivery.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import re
import time
import unittest
from urllib.parse import urlparse

from playwright.sync_api import expect

from mvp_v2 import local_ui
from mvp_v2.ai_spec import refresh_ai_spec
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.formal_advisor import FormalAdvisorPolicy, FormalAdvisorService
from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, FormalBriefOcrPolicy
from mvp_v2.formal_generation_service import FormalTextProviderPolicy
from mvp_v2.formal_runtime import build_formal_runtime, uploaded_document
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.package_publication import AssetPackagePublicationService
from mvp_v2.package_registry import LocalPackageRegistry
from tests.test_mvp_v2_formal_brief_input_adapter import _image_bytes
from tests import test_mvp_v2_formal_generation_service as generation_fixture
from tests import test_mvp_v2_manual_receipt_browser as manual_fixture


BRIEF = "\n".join((
    "项目名称: 匿名双轮回归项目", "脚本类型: 发布会", "品牌或客户: 匿名团队",
    "目标时长: 60秒", "风格调性: 自然、明亮", "视频目标: 呈现现场与产品体验",
    "关键事件与流程: 来宾入场、签到、产品陈列、体验交流、舞台介绍、互动、合影、片尾",
    "特殊需求: 8个镜头；无旁白；少字幕", "禁止或不能出现的内容: 不得虚构事实"))


class RoundsRepairBrowserTests(unittest.TestCase):
    open_page = manual_fixture.ManualReceiptBrowserTests.open_page

    def setUp(self):
        helper = generation_fixture.FormalGenerationServiceTests("runTest")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.fixture = helper
        self.ocr_calls = []
        self.ocr_failures = 0
        self.editor_inputs = []
        self.requests = []
        self.errors = []
        self.measurements = []

        def ocr(_path, _prompt):
            self.ocr_calls.append("image")
            if self.ocr_failures:
                self.ocr_failures -= 1
                raise RuntimeError("anonymous OCR interruption")
            return json.dumps({"status": "no_text", "text": ""})

        self.adapter = FormalBriefInputAdapter(helper.briefs, data_root=helper.root / "runtime",
            ocr_policy=FormalBriefOcrPolicy(invoke=ocr, provider="anonymous", model="rounds-ocr"))
        packages = AssetPackagePublicationService(registry=LocalPackageRegistry(helper.root / "registry.json"),
            storage=LocalAssetStorage(helper.root / "assets"), packages_root=helper.root / "packages")
        workbook = helper.source_root / "M+发布会类通用脚本库.xlsx"
        refresh_ai_spec(workbook)
        package = packages.import_template(category_id="launch", filename=workbook.name,
            content=workbook.read_bytes(), display_name="匿名发布会模板")
        packages.registry.promote("launch", package["package_id"], package["package_version"])
        runtime = build_formal_runtime({"data_root": str(helper.root / "runtime"),
            "runs_root": str(helper.root / "runs"), "source_root": str(helper.source_root),
            "asset_package": package["local_path"], "asset_packages": {"launch": package["local_path"]},
            "package_registry_path": str(helper.root / "registry.json"),
            "packages_root": str(helper.root / "packages"), "formal_agent_read_only": False})
        policy = FormalTextProviderPolicy(writer=lambda *_: "", editor=lambda *_: "",
            writer_provider="anonymous", writer_model="rounds-writer",
            editor_provider="anonymous", editor_model="rounds-editor")
        service = runtime.generation
        service.provider_policy = policy
        service.brief_input_adapter = self.adapter

        def stream(_prompt, _model, _tokens, emit):
            blocks = [{"段落": f"镜头{index}｜匿名段落{index}",
                "画面描述": f"匿名活动画面{index}", "声音": "木吉他与现场环境声，无旁白",
                "时长": f"建议{7 if index % 2 else 8}秒"} for index in range(1, 9)]
            result = json.dumps({"blocks": blocks}, ensure_ascii=False)
            emit(result)
            return result

        def revise(prompt, _model, _tokens):
            data = json.loads(prompt.split("\nDATA:", 1)[1])
            self.editor_inputs.append(deepcopy(data))
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{"shot_id": target["shot_id"], "field": "sound",
                    "value": "木吉他延续，增加杯盖轻放声，无旁白"}
                    for target in data["targets"] if target["field"] == "sound"]}, ensure_ascii=False)

        service.creative_stream_provider = stream
        service.creative_revision.brief_adapter = self.adapter
        service.creative_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(invoke=revise,
            provider="anonymous", model="rounds-revision")
        self.generation = service
        workspace = helper.projects.get_workspace(helper.token, helper.workspace_id)
        self.project = helper.projects.create_project(helper.token, workspace_id=helper.workspace_id,
            name="匿名双轮回归项目", category_id="launch",
            expected_workspace_version=workspace["workspace_version"])
        def advise(prompt, _model, _tokens):
            context = json.loads(prompt.split("\nCONTEXT:", 1)[1].split("\nUSER:", 1)[0])
            return json.dumps({"intent": "explain_status", "summary": "需求已整理，请核对摘要和来源。",
                "evidence_ids": context["evidence_ids"], "questions": [], "candidate_action": "none"}, ensure_ascii=False)

        runtime.agent.advisor = FormalAdvisorService(FormalAdvisorPolicy(invoke=advise,
            provider="anonymous", model="rounds-advisor"))
        runtime.agent.script_revision.brief_adapter = self.adapter
        app = local_ui.create_app(agent_api=runtime.agent, formal_brief_adapter=self.adapter,
            formal_brief_upload_factory=uploaded_document, formal_generation_service=service,
            formal_script_service=runtime.script_workspace, formal_image_service=runtime.image_workspace,
            formal_review_service=runtime.review_workspace, formal_admin_service=runtime.admin)
        self.page, self.origin = self.open_page(app, helper.token, helper.workspace_id)
        self.page.set_viewport_size({"width": 1440, "height": 1000})
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.page.on("response", lambda response: self.requests.append({
            "method": response.request.method, "path": urlparse(response.url).path,
            "status": response.status}) if any(path in response.url for path in ("/api/formal/", "/api/agent/")) else None)
        self.route = self.origin + "/projects/" + self.project["project_id"]
        self.page.goto(self.route + "/brief")
        expect(self.page.locator("#formal-brief-upload")).to_be_visible()

    def tearDown(self):
        configured = os.environ.get("MPLUS_ROUNDS_BROWSER_ARTIFACTS", "").strip()
        if not configured:
            return
        target = Path(configured).resolve()
        target.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(target / (self._testMethodName + ".png")), full_page=True)
        (target / (self._testMethodName + ".json")).write_text(json.dumps({
            "url": self.page.url, "requests": self.requests, "layout": self.measurements,
            "page_errors": self.errors, "actual_http_authorities_and_sqlite": True,
            "external_models": "deterministic", "retained_user_projects_verified": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def upload(self, filename, content, *, image=False):
        details = self.page.locator("#brief-document-upload")
        if details.get_attribute("open") is None:
            details.locator("summary").click()
        self.page.locator("#formal-brief-files").set_input_files({"name": filename,
            "mimeType": "image/png" if image else "text/plain",
            "buffer": content if isinstance(content, bytes) else content.encode()})
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/brief/files?" in response.url) as completed:
            self.page.locator("#formal-brief-upload-button").click()
        self.assertEqual(completed.value.status, 200, completed.value.text())
        expect(self.page.locator("#formal-brief-upload-button")).to_be_enabled()
        return completed.value.json()

    def ready_brief(self, *, image=False):
        self.upload("anonymous-brief.txt", BRIEF)
        if image:
            self.upload("anonymous-reference.png", _image_bytes("PNG"), image=True)
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/brief/confirm?" in response.url) as confirmed:
            self.page.locator("#formal-brief-confirm").click()
        self.assertEqual(confirmed.value.status, 200, confirmed.value.text())
        expect(self.page.locator("#formal-brief-readiness")).to_have_text("已确认")

    def draft(self):
        result = self.page.request.get(self.route.replace("/projects/", "/api/formal/projects/")
            + "/creative-draft?workspace_id=" + self.fixture.workspace_id,
            headers={"X-MPlus-Agent-Auth": self.fixture.token})
        self.assertEqual(result.status, 200, result.text())
        return result.json()

    def open_fact_history(self):
        for selector in ("#brief-source-panel", ".brief-source-evidence", ".brief-fact-history"):
            details = self.page.locator(selector)
            if details.get_attribute("open") is None:
                details.locator(":scope > summary").click()
        return self.page.locator(".brief-fact-history")

    def test_confirmed_summary_and_narrow_sources_are_readable_and_clickable(self):
        self.ready_brief()
        facts = self.page.locator("#formal-fact-list")
        grid = self.page.locator("#page-brief .ui00-brief-grid")
        width = facts.bounding_box()["width"]
        self.assertGreaterEqual(width, grid.bounding_box()["width"] * .85)
        for width in (685, 375):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 1000})
                source = self.page.locator("#brief-source-panel")
                if source.get_attribute("open") is None:
                    source.locator(":scope > summary").click()
                evidence = self.page.locator(".brief-source-evidence")
                if evidence.get_attribute("open") is None:
                    evidence.locator(":scope > summary").click()
                document = self.page.locator("#formal-brief-files-state details").filter(has_text="anonymous-brief.txt")
                if document.get_attribute("open") is None:
                    document.locator("summary").click()
                expect(document.locator("p")).to_be_visible()
                layout = grid.evaluate("""grid => ({width: grid.clientWidth, scroll: grid.scrollWidth,
                    panels: [...grid.children].filter(x => x.getBoundingClientRect().height > 0)
                    .map(x => ({left: x.getBoundingClientRect().left,
                        width: x.getBoundingClientRect().width}))})""")
                self.measurements.append({"viewport": width, **layout})
                self.assertLessEqual(layout["scroll"], layout["width"] + 1)
                self.assertTrue(all(panel["width"] >= layout["width"] * .85 for panel in layout["panels"]))
                self.assertLessEqual(max(panel["left"] for panel in layout["panels"])
                    - min(panel["left"] for panel in layout["panels"]), 2)
        self.page.reload()
        expect(self.page.locator("#formal-brief-readiness")).to_have_text("已确认")
        self.assertFalse(self.errors)

    def test_conflict_shows_sources_and_refresh_keeps_only_adopted_summary(self):
        self.upload("anonymous-brief.txt", BRIEF)
        self.upload("anonymous-update.txt", "目标时长: 90秒")
        conflict = self.page.locator("#formal-conflict-list .ui00-conflict-item").filter(has=self.page.locator("select"))
        expect(conflict).to_have_count(1)
        expect(conflict).to_contain_text("目标时长")
        expect(conflict).to_contain_text("anonymous-brief.txt")
        expect(conflict).to_contain_text("anonymous-update.txt")
        adopted = conflict.locator("select option").filter(has_text="90秒")
        expect(adopted).to_have_count(1)
        conflict.locator("select").select_option(adopted.get_attribute("value"))
        conflict.get_by_role("button", name="确认选择", exact=True).click()
        expect(self.page.locator("#formal-conflict-list select")).to_have_count(0)
        expect(self.page.locator("#formal-conflict-list")).to_contain_text("90秒")
        expect(self.page.locator("#formal-conflict-list")).to_contain_text(re.compile("采用|确认"))
        self.page.reload()
        current = self.page.locator("#formal-fact-list .ui00-fact-item").filter(has=self.page.get_by_text("目标时长", exact=True))
        expect(current).to_have_count(1)
        expect(current).to_contain_text("90秒")
        expect(current).not_to_contain_text("60秒")
        self.assertFalse(self.errors)

    def test_draft_local_scope_protection_and_positive_duration_survive_reload(self):
        self.ready_brief()
        self.page.locator("#brief-enter-creative").click()
        self.page.locator("#creative-first-confirmed-start").click()
        expect(self.page.locator("#creative-first-blocks .creative-first-block")).to_have_count(8)
        expect(self.page.locator("[data-first-protect]")).to_have_count(8)
        expect(self.page.locator("#creative-first-summary")).to_contain_text("60")
        before = self.draft()
        first, second = before["blocks"][:2]
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/creative-draft/protection?" in response.url) as protected:
            self.page.locator(f'[data-first-protect="{first["block_id"]}"]').check()
        self.assertEqual(protected.value.status, 200, protected.value.text())
        self.page.locator(f'[data-first-select="{second["block_id"]}"]').check()
        self.page.locator("#creative-first-revision-scope").select_option("selected")
        expect(self.page.locator("#creative-first-revision-scope-copy")).to_contain_text("2")
        expect(self.page.locator("#creative-first-revision-scope-copy")).to_contain_text("1")
        duration = self.page.locator(f'[data-first-block="{first["block_id"]}"][data-first-field="时长"]')
        duration.fill("-1")
        duration.press("Tab")
        expect(self.page.locator("#creative-first-status")).to_contain_text("时长")
        # A normal pause after invalid input must not let auto-save commit it.
        self.page.wait_for_timeout(1000)
        self.assertEqual(self.draft()["blocks"][0]["fields"]["时长"], first["fields"]["时长"])
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and response.url.endswith("/changes?workspace_id=" + self.fixture.workspace_id)) as saved:
            duration.fill("8")
            duration.press("Tab")
        self.assertEqual(saved.value.status, 200, saved.value.text())
        expect(self.page.locator("#creative-first-summary")).to_contain_text("61")
        self.page.reload()
        expect(self.page.locator("#creative-first-summary")).to_contain_text("61")
        self.page.locator(f'[data-first-select="{second["block_id"]}"]').check()
        self.page.locator("#creative-first-revision-scope").select_option("selected")
        before_preview = self.draft()
        self.page.locator("#creative-first-revision-input").fill("声音增加杯盖轻放声。")
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/creative-draft/revisions?" in response.url) as revised:
            self.page.locator("#creative-first-revision-submit").click()
        self.assertEqual(revised.value.status, 200, revised.value.text())
        expect(self.page.locator("#creative-first-apply")).to_be_enabled()
        preview = self.draft()
        self.assertEqual(preview["blocks"], before_preview["blocks"])
        self.assertEqual(preview["blocks"][1]["fields"], second["fields"])
        self.assertTrue(self.editor_inputs)
        self.assertEqual({target["shot_id"] for target in self.editor_inputs[-1]["targets"]}, {second["block_id"]})
        # Selection and protection must each work without named-shot instructions.
        self.page.locator(f'[data-first-select="{first["block_id"]}"]').check()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/creative-draft/revisions?" in response.url) as revised_again:
            self.page.locator("#creative-first-revision-submit").click()
        self.assertEqual(revised_again.value.status, 200, revised_again.value.text())
        expect(self.page.locator("#creative-first-apply")).to_be_enabled()
        self.assertEqual({target["shot_id"] for target in self.editor_inputs[-1]["targets"]}, {second["block_id"]})
        self.assertEqual(self.draft()["blocks"], before_preview["blocks"])
        self.page.locator("#creative-first-apply").click()
        sound = self.page.locator(f'[data-first-block="{second["block_id"]}"][data-first-field="声音"]')
        expect(sound).to_have_value("木吉他延续，增加杯盖轻放声，无旁白")
        self.page.reload()
        expect(sound).to_have_value("木吉他延续，增加杯盖轻放声，无旁白")
        after = self.draft()
        for index, block in enumerate(after["blocks"]):
            original = before["blocks"][index]
            for field, value in block["fields"].items():
                if (index, field) not in {(0, "时长"), (1, "声音")}:
                    self.assertEqual(value, original["fields"][field])
        self.assertIn(first["block_id"], after["protected_block_ids"])
        self.assertFalse(self.errors)

    def test_manual_risk_choices_and_reference_note_roundtrip(self):
        self.ready_brief(image=True)
        self.page.goto(self.route + "/materials")
        expect(self.page.locator("#formal-image-synchronize")).to_be_visible()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/images/synchronize?" in response.url) as connected:
            self.page.locator("#formal-image-synchronize").click()
        self.assertEqual(connected.value.status, 200, connected.value.text())
        expect(self.page.locator("[data-formal-image-form]")).to_have_count(1)
        form = self.page.locator("[data-formal-image-form]")
        form.locator("..").locator(":scope > summary").click()
        for field, value in {"caption": "匿名空间构图参考", "tags": "空间", "subjects": "背景",
                "scene": "室内", "purpose": "非实拍参考，仅用于构图", "quality": "清晰",
                "brand": "无", "evidence": "已查看匿名原图，只核对构图与用途"}.items():
            form.locator(f'[name="{field}"]').fill(value)
        form.locator('[name="status"]').select_option("blocked")
        chosen_flags = {"quality_flags": ["slight_blur", "bad_crop"],
            "negative_flags": ["external_brand", "forbidden_content"]}
        for field, values in chosen_flags.items():
            flags = form.locator(f'[name="{field}"]')
            self.assertEqual(flags.evaluate_all("nodes => nodes.filter(x => x.tagName === 'INPUT' && x.type === 'text').length"), 0)
            if flags.first.evaluate("x => x.tagName") == "SELECT":
                flags.select_option(values)
            else:
                for value in values:
                    form.locator(f'[name="{field}"][value="{value}"]').check()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/analysis?workspace_id=" in response.url) as saved:
            form.get_by_role("button", name="保存人工理解", exact=True).click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        self.page.reload()
        form = self.page.locator("[data-formal-image-form]")
        form.locator("..").locator(":scope > summary").click()
        expect(form.locator('[name="purpose"]')).to_have_value("非实拍参考，仅用于构图")
        expect(form.locator('[name="status"]')).to_have_value("blocked")
        for field, values in chosen_flags.items():
            flags = form.locator(f'[name="{field}"]')
            if flags.first.evaluate("x => x.tagName") == "SELECT":
                self.assertEqual(set(values), set(flags.evaluate("x => [...x.selectedOptions].map(y => y.value)")))
            else:
                for value in values:
                    expect(form.locator(f'[name="{field}"][value="{value}"]')).to_be_checked()
        self.assertFalse(self.errors)

    def test_renamed_duplicate_upload_reuses_one_authority_image_and_reports_each_file(self):
        self.ready_brief()
        self.page.goto(self.route + "/materials")
        expect(self.page.locator("#formal-image-total")).to_have_text("0")
        pixels = _image_bytes("PNG")
        upload_state = self.page.locator("#formal-image-upload-state")
        received = []

        def submit_files(names):
            self.page.locator("#formal-image-upload-files").set_input_files([
                {"name": name, "mimeType": "image/png", "buffer": pixels}
                for name in names])
            with self.page.expect_response(lambda response: response.request.method == "POST"
                    and "/image-workspace/supplements?" in response.url) as submitted:
                self.page.locator("#formal-image-upload-button").click()
            self.assertEqual(submitted.value.status, 200, submitted.value.text())
            expect(self.page.locator("#formal-image-upload-button")).to_be_enabled()
            result = submitted.value.json()
            received.append({"filenames": names, "image_key": result["image_key"],
                "receipt": upload_state.inner_text()})
            return result

        first = submit_files(["anonymous-first.png"])
        expect(upload_state.locator("strong")).to_have_text("新增 1 张图片。")
        expect(upload_state.locator("li")).to_have_text("anonymous-first.png：已新增")
        expect(self.page.locator("#formal-image-total")).to_have_text("1")
        expect(self.page.locator("[data-formal-image-form]")).to_have_count(1)

        renamed = submit_files(["anonymous-renamed.png"])
        self.assertEqual(renamed["image_key"], first["image_key"])
        expect(upload_state.locator("strong")).to_have_text("新增 0 张图片，复用已有 1 张。")
        expect(upload_state.locator("li")).to_have_text("anonymous-renamed.png：已有相同图片，已复用")
        expect(self.page.locator("#formal-image-total")).to_have_text("1")

        copies = ["anonymous-copy-one.png", "anonymous-copy-two.png"]
        repeated_batch = submit_files(copies)
        self.assertEqual(repeated_batch["image_key"], first["image_key"])
        expect(upload_state.locator("strong")).to_have_text("新增 0 张图片，复用已有 2 张。")
        expect(upload_state.locator("li")).to_have_count(2)
        for name in copies:
            expect(upload_state.locator("li").filter(has_text=name)).to_have_text(
                name + "：已有相同图片，已复用")
        expect(self.page.locator("#formal-image-total")).to_have_text("1")
        expect(self.page.locator("[data-formal-image-form]")).to_have_count(1)
        with self.page.expect_response(lambda response: response.request.method == "GET"
                and "/images?" in response.url) as refreshed:
            self.page.reload()
        self.assertEqual(refreshed.value.status, 200, refreshed.value.text())
        readiness = refreshed.value.json()
        self.assertEqual(readiness["total"], 1)
        self.assertEqual([image["image_key"] for image in readiness["images"]], [first["image_key"]])
        expect(self.page.locator("#formal-image-total")).to_have_text("1")
        expect(self.page.locator("[data-formal-image-form]")).to_have_count(1)
        self.measurements.append({"upload_receipts": received,
            "persisted_image_keys": [image["image_key"] for image in readiness["images"]],
            "persisted_total": readiness["total"]})
        self.assertFalse(self.errors)

    def test_failed_ocr_exposes_direct_retry_and_preserves_good_source(self):
        self.upload("anonymous-brief.txt", BRIEF)
        good_brief = self.adapter.read_current_formal(self.fixture.token,
            workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"])["brief"]
        good_source, good_facts = good_brief["documents"][0], good_brief["facts"]
        self.ocr_failures = 1
        pixels = _image_bytes("PNG")
        self.upload("anonymous-reference.png", pixels, image=True)
        source = self.page.locator(".brief-source-evidence")
        if source.get_attribute("open") is None:
            source.locator(":scope > summary").click()
        failed = self.page.locator("#formal-brief-files-state details[data-state='failed']")
        expect(failed).to_have_count(1)
        if failed.get_attribute("open") is None:
            failed.locator("summary").click()
        retry = failed.locator("[data-brief-file-retry]")
        expect(retry).to_be_visible()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/brief/" in response.url) as retried:
            retry.click()
        self.assertEqual(retried.value.status, 200, retried.value.text())
        self.page.reload()
        if self.page.locator(".brief-source-evidence").get_attribute("open") is None:
            self.page.locator(".brief-source-evidence > summary").click()
        expect(self.page.locator("#formal-brief-files-state")).to_contain_text("anonymous-brief.txt")
        expect(self.page.locator("#formal-brief-files-state details[data-state='failed']")).to_have_count(0)
        self.assertEqual(len(self.ocr_calls), 2)
        restored = self.adapter.read_current_formal(self.fixture.token,
            workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"])["brief"]
        documents = restored["documents"]
        self.assertEqual(len(documents), 2)
        self.assertTrue(all(document["status"] == "succeeded" for document in documents))
        self.assertEqual(next(document for document in documents
            if document["filename"] == "anonymous-brief.txt"), good_source)
        def business_facts(facts):
            return [{key: value for key, value in fact.items() if key != "fact_key"} for fact in facts]
        self.assertEqual(business_facts(restored["facts"]), business_facts(good_facts))
        expect(self.page.locator("#formal-fact-list")).to_contain_text("60秒")
        expect(self.page.locator("#formal-fact-list")).to_contain_text("来宾入场")
        self.assertFalse(self.errors)

    def test_replacing_failed_source_then_appending_does_not_restore_failure(self):
        self.upload("anonymous-brief.txt", BRIEF)
        original = self.adapter.read_current_formal(self.fixture.token,
            workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"])["brief"]
        self.ocr_failures = 1
        self.upload("anonymous-reference.png", _image_bytes("PNG"), image=True)
        source = self.page.locator(".brief-source-evidence")
        if source.get_attribute("open") is None:
            source.locator(":scope > summary").click()
        failed = self.page.locator("#formal-brief-files-state details[data-state='failed']")
        expect(failed).to_have_count(1)
        if failed.get_attribute("open") is None:
            failed.locator(":scope > summary").click()
        with self.page.expect_file_chooser() as chooser:
            failed.locator("[data-brief-file-replace]").click()
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/replace?" in response.url) as replaced:
            chooser.value.set_files({"name": "anonymous-replacement.txt", "mimeType": "text/plain",
                "buffer": "参考方案: 替换后的匿名构图说明".encode()})
        self.assertEqual(replaced.value.status, 200, replaced.value.text())
        expect(self.page.locator("#formal-brief-files-state")).to_contain_text("anonymous-replacement.txt")
        self.page.reload()
        self.upload("anonymous-delivery.txt", "交付要求: 横版")
        self.page.reload()
        if self.page.locator(".brief-source-evidence").get_attribute("open") is None:
            self.page.locator(".brief-source-evidence > summary").click()
        files = self.page.locator("#formal-brief-files-state")
        expect(files).to_contain_text("anonymous-replacement.txt")
        expect(files).to_contain_text("anonymous-delivery.txt")
        expect(files).not_to_contain_text("anonymous-reference.png")
        expect(files.locator("details[data-state='failed']")).to_have_count(0)
        current = self.adapter.read_current_formal(self.fixture.token,
            workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"])["brief"]
        self.assertEqual(len(current["documents"]), 3)
        self.assertTrue(all(document["status"] == "succeeded" for document in current["documents"]))
        self.assertEqual(next(document for document in current["documents"]
            if document["filename"] == "anonymous-brief.txt"), original["documents"][0])
        for fact in original["facts"]:
            matching = next(item for item in current["facts"] if item["label"] == fact["label"])
            self.assertEqual({key: value for key, value in matching.items() if key != "fact_key"},
                {key: value for key, value in fact.items() if key != "fact_key"})
        self.assertEqual(len(self.ocr_calls), 1)
        self.assertFalse(self.errors)

    def test_inline_fields_keep_drafts_sources_adopted_values_and_keyboard_focus(self):
        self.ready_brief()
        grid = self.page.locator("#page-brief .ui00-brief-grid")
        for width in (1024, 1440):
            with self.subTest(width=width):
                self.page.set_viewport_size({"width": width, "height": 1000})
                layout = grid.evaluate("""grid => ({width: grid.clientWidth, scroll: grid.scrollWidth,
                    viewport: document.documentElement.clientWidth,
                    documentScroll: document.documentElement.scrollWidth})""")
                self.measurements.append({"viewport": width, **layout})
                self.assertLessEqual(layout["scroll"], layout["width"] + 1)
                self.assertLessEqual(layout["documentScroll"], layout["viewport"] + 1)
        duration = self.page.locator('[data-edit-formal-fact="目标时长"]')
        duration.focus()
        duration.press("Enter")
        editor = self.page.locator('[data-inline-fact="目标时长"]')
        value = editor.locator("[data-inline-fact-value]")
        expect(value).to_be_focused()
        value.fill("90秒")
        value.press("Tab")
        expect(editor.get_by_role("button", name="保存此字段", exact=True)).to_be_focused()
        self.page.reload()
        editor = self.page.locator('[data-inline-fact="目标时长"]')
        expect(editor.locator("[data-inline-fact-value]")).to_have_value("90秒")
        self.page.locator('#formal-project-navigation a[data-route="overview"]').click()
        self.page.go_back()
        expect(self.page.locator('[data-inline-fact="目标时长"] [data-inline-fact-value]')).to_have_value("90秒")
        editor = self.page.locator('[data-inline-fact="目标时长"]')
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/brief/field?" in response.url) as saved:
            editor.get_by_role("button", name="保存此字段", exact=True).click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        expect(self.page.locator('[data-inline-fact="目标时长"]')).to_have_count(0)
        current = self.page.locator("#formal-fact-list .ui00-fact-item").filter(has=self.page.get_by_text("目标时长", exact=True))
        expect(current).to_have_count(1)
        expect(current).to_contain_text("90秒")
        expect(current).not_to_contain_text("60秒")
        expect(self.page.locator("#formal-standard-card-state")).to_have_text("待确认")
        history = self.open_fact_history().locator(".ui00-fact-item").filter(has=self.page.get_by_text("目标时长", exact=True))
        expect(history).to_have_count(1)
        expect(history).to_contain_text("60秒")
        expect(history).to_contain_text("anonymous-brief.txt")
        self.page.locator("#formal-brief-confirm").click()
        expect(self.page.locator("#formal-brief-readiness")).to_have_text("已确认")
        self.page.reload()
        expect(current).to_contain_text("90秒")
        self.page.locator('[data-edit-formal-fact="风格调性"]').click()
        style = self.page.locator('[data-inline-fact="风格调性"]')
        style.locator("[data-inline-fact-value]").fill("克制、成熟")
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/brief/field?" in response.url) as saved_style:
            style.get_by_role("button", name="保存此字段", exact=True).click()
        self.assertEqual(saved_style.value.status, 200, saved_style.value.text())
        expect(self.page.locator('[data-inline-fact="风格调性"]')).to_have_count(0)
        changed_style = self.page.locator("#formal-fact-list .ui00-fact-item").filter(has=self.page.get_by_text("风格调性", exact=True))
        expect(changed_style).to_have_count(1)
        expect(changed_style.locator(":scope > strong")).to_have_text(re.compile(r"^克制[；、]成熟$"))
        expect(current).to_have_count(1)
        expect(current).to_contain_text("90秒")
        expect(current).not_to_contain_text("60秒")
        expect(self.page.locator("#formal-conflict-list select")).to_have_count(0)
        expect(self.page.locator("#formal-standard-card-state")).to_have_text("待确认")
        history = self.open_fact_history()
        expect(history).to_contain_text("自然")
        expect(history).to_contain_text("明亮")
        expect(history).to_contain_text("anonymous-brief.txt")
        self.page.locator("#formal-brief-confirm").click()
        expect(self.page.locator("#formal-brief-readiness")).to_have_text("已确认")
        self.page.reload()
        expect(changed_style.locator(":scope > strong")).to_have_text(re.compile(r"^克制[；、]成熟$"))
        expect(current).to_contain_text("90秒")
        self.assertFalse(self.errors)

    def test_late_inline_save_response_does_not_overwrite_another_project_draft(self):
        self.ready_brief()
        workspace = self.fixture.projects.get_workspace(self.fixture.token, self.fixture.workspace_id)
        second = self.fixture.projects.create_project(self.fixture.token,
            workspace_id=self.fixture.workspace_id, name="匿名隔离项目B", category_id="launch",
            expected_workspace_version=workspace["workspace_version"])
        second_route = self.origin + "/projects/" + second["project_id"]
        self.page.goto(second_route + "/brief")
        self.upload("anonymous-second.txt", BRIEF.replace("匿名双轮回归项目", "匿名隔离项目B"))
        self.page.locator('[data-edit-formal-fact="风格调性"]').click()
        self.page.locator('[data-inline-fact="风格调性"] [data-inline-fact-value]').fill("B项目独立未提交内容")
        self.page.goto(self.route + "/brief")
        self.page.locator('[data-edit-formal-fact="目标时长"]').click()
        self.page.locator('[data-inline-fact="目标时长"] [data-inline-fact-value]').fill("90秒")
        pending = []

        def delay_result(route):
            # Execute the real authority; delay only delivery while users switch.
            actual = route.fetch()
            self.assertEqual(actual.status, 200, actual.text())
            pending.append((route, actual))

        self.page.route("**/brief/field?*", delay_result)
        self.page.locator('[data-inline-fact="目标时长"]').get_by_role("button", name="保存此字段", exact=True).click()
        deadline = time.monotonic() + 15
        while not pending and time.monotonic() < deadline:
            self.page.wait_for_timeout(50)
        self.assertEqual(len(pending), 1)
        self.page.locator('#formal-project-navigation a[data-route="projects"]').click()
        row = self.page.locator("#formal-project-list .ui00-project-row").filter(has_text="匿名隔离项目B")
        expect(row).to_be_visible()
        row.locator("a[data-list-page]").click()
        self.page.wait_for_url("**/projects/" + second["project_id"] + "/brief")
        second_editor = self.page.locator('[data-inline-fact="风格调性"] [data-inline-fact-value]')
        expect(second_editor).to_have_value("B项目独立未提交内容")
        expect(self.page.locator("#formal-brief-text")).to_have_value("风格调性: B项目独立未提交内容")
        pending[0][0].fulfill(response=pending[0][1])
        self.page.wait_for_timeout(500)
        after_delivery = self.page.locator("#formal-brief-text").input_value()
        after_inline = second_editor.input_value()
        self.measurements.append({"late_save_response_status": pending[0][1].status,
            "other_project_composer_after_delivery": after_delivery,
            "other_project_inline_after_delivery": after_inline})
        self.page.reload()
        expect(second_editor).to_have_value("B项目独立未提交内容")
        self.assertEqual(after_delivery, "风格调性: B项目独立未提交内容")
        self.assertEqual(after_inline, "B项目独立未提交内容")
        self.assertFalse(self.errors)

    def test_inline_displayed_specific_type_adopts_replacement_and_keeps_history(self):
        self.upload("anonymous-specific.txt", BRIEF + "\n具体类型: 新品发布")
        self.page.locator('[data-edit-formal-fact="具体类型"]').click()
        editor = self.page.locator('[data-inline-fact="具体类型"]')
        editor.locator("[data-inline-fact-value]").fill("品牌发布")
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and "/brief/field?" in response.url) as saved:
            editor.get_by_role("button", name="保存此字段", exact=True).click()
        self.assertEqual(saved.value.status, 200, saved.value.text())
        expect(self.page.locator('[data-inline-fact="具体类型"]')).to_have_count(0)
        current = self.page.locator("#formal-fact-list .ui00-fact-item").filter(has=self.page.get_by_text("具体类型", exact=True))
        expect(current).to_have_count(1)
        expect(current.locator(":scope > strong")).to_have_text("品牌发布")
        expect(self.page.locator("#formal-conflict-list select")).to_have_count(0)
        self.page.reload()
        expect(current.locator(":scope > strong")).to_have_text("品牌发布")
        returned = self.adapter.read_current_formal(self.fixture.token,
            workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"])["brief"]
        older = [fact for fact in returned["facts"] if fact["label"] == "具体类型" and fact["adopted"] is False]
        self.assertEqual(len(older), 1)
        self.assertIn("新品发布", older[0]["value"])
        self.assertTrue(any("anonymous-specific.txt" in source for source in older[0]["sources"]))
        self.assertFalse(self.errors)


if __name__ == "__main__":
    unittest.main()
