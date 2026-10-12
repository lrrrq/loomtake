"""Anonymous UI recovery after a real service handles a fixture audit failure.

The pipeline audit result is deliberately simulated at the existing generation
seam. HTTP, task state, planning, frozen draft binding and SQLite are real.
"""
from __future__ import annotations

from io import BytesIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlparse

from playwright.sync_api import expect

from mvp_v2.formal_brief_input_adapter import UploadedDocument
from tests import test_mvp_v2_round3_plan_materials_return_browser as navigation_fixture


class Round3PlanAuditRecoveryBrowserTests(unittest.TestCase):
    def setUp(self):
        self.navigation = navigation_fixture.Round3PlanMaterialsReturnBrowserTests("runTest")
        self.navigation.setUp()
        self.addCleanup(self.navigation.doCleanups)
        self.helper = self.navigation.helper
        self.page = self.navigation.page
        self.failures = []
        self.previews = []
        self.readiness_mutation = None

        def stream(_prompt, _model, _tokens, emit):
            events = ("来宾入场", "签到", "产品陈列", "体验交流", "舞台介绍", "互动", "合影", "片尾")
            result = json.dumps({"blocks": [{"段落": f"镜头{index}｜{event}",
                "画面描述": f"匿名活动现场完成{event}，保留可见的人物与现场动作。",
                "声音": "木吉他与现场环境声，无旁白", "时长": f"建议{7 if index % 2 else 8}秒"}
                for index, event in enumerate(events, 1)]}, ensure_ascii=False)
            emit(result)
            return result

        self.helper.generation.creative_stream_provider = stream

    def tearDown(self):
        configured = os.environ.get("MPLUS_ROUND3_AUDIT_BROWSER_ARTIFACTS", "").strip()
        if not configured:
            return
        target = Path(configured)
        target.mkdir(parents=True, exist_ok=True)
        (target / f"{self._testMethodName}.json").write_text(json.dumps({
            "steps": self.navigation.steps, "plan_requests": self.navigation.plan_requests,
            "failed_confirmations": self.failures, "preview_plan_ids": [p["plan_id"] for p in self.previews],
            "requests": self.helper.requests, "page_errors": self.helper.errors,
            "persisted_project_version_count": self.helper.fixture._table_count("project_versions"),
            "actual_http_authorities_and_sqlite": True,
            "simulated_scope": "run_generation_once audit result plus creative/OCR Provider output",
            "readiness_mutation": self.readiness_mutation,
            "browser_application_state_injection": False,
            "retained_user_projects_verified": False, "production_runtime_verified": False,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def create_plan(self):
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and urlparse(response.url).path.endswith("/plans"), timeout=5000) as completed:
            self.page.locator("#formal-plan-create").click()
        self.assertEqual(completed.value.status, 200, completed.value.text())
        preview = completed.value.json()
        self.previews.append(preview)
        self.assertEqual(preview["audit"]["status"], "pass", preview["audit"])
        self.assertEqual(preview["creative_conversion"]["block_count"], 8)
        self.assertEqual(preview["creative_conversion"]["mapped_count"], 8)
        expect(self.page.locator("#formal-plan-confirmation")).to_be_visible()
        return preview

    def ready_accepted_plan(self):
        draft = self.navigation.accepted_draft()
        self.page.locator("#creative-first-plan-link").click()
        block = self.page.locator("#formal-plan-image-block")
        expect(block).to_be_visible()
        block.locator("a").nth(1).click()
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        self.create_plan()
        return draft

    def fail_confirmation(self):
        expect(self.page.locator("#formal-plan-confirm-check")).to_be_enabled()
        self.page.locator("#formal-plan-confirm-check").check()
        with patch("mvp_v2.formal_generation_service.run_generation_once",
                return_value=SimpleNamespace(audit={"status": "fail",
                    "blocking_items": ["required_content_missing"]})) as audit:
            with self.page.expect_response(lambda response: response.request.method == "POST"
                    and "/plans/" in response.url and "/confirm?" in response.url) as completed:
                self.page.locator("#formal-plan-confirm").click()
            self.assertEqual(completed.value.status, 409, completed.value.text())
            detail = completed.value.json()["detail"]
            self.assertEqual(detail["code"], "FORMAL_GENERATION_AUDIT_FAILED")
            self.assertEqual(audit.call_count, 1)
        self.failures.append(detail)
        expect(self.page.locator("#formal-plan-status-copy")).to_contain_text("本次未创建版本")
        self.assert_no_failed_version()

    def assert_no_failed_version(self):
        f = self.helper.fixture
        self.assertEqual(f._table_count("project_versions"), 0)
        project = f.projects.get_project(f.token, self.helper.project["project_id"])
        self.assertIsNone(project["current_version_id"])
        with f.store.connection() as connection:
            statuses = [row["status"] for row in connection.execute(
                "SELECT status FROM tasks WHERE project_id=? AND task_type='formal_generation_plan'",
                (self.helper.project["project_id"],))]
        self.assertIn("blocked", statuses)

    def test_failed_audit_replans_without_reload_and_preserves_accepted_draft_binding(self):
        draft = self.ready_accepted_plan()
        self.fail_confirmation()
        expect(self.page.locator("#formal-plan-confirmation")).to_be_hidden()
        expect(self.page.locator("#formal-plan-create")).to_be_visible()
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        self.navigation.snapshot("failed-audit-ready-to-replan")
        # No reload or navigation here: the same visible recovery action must work.
        new_plan = self.create_plan()
        self.assertNotEqual(new_plan["plan_id"], self.previews[0]["plan_id"])
        self.assertEqual(len(self.navigation.plan_requests), 2)
        for request in self.navigation.plan_requests:
            self.assertEqual(request["expected_creative_revision"], draft["revision"])
            self.assertEqual(request["generation_mode"], "text_only_draft")
        event = self.helper.generation._plan_event(self.helper.fixture.token, new_plan["plan_id"])
        binding = event["creative_draft_binding"]
        self.assertEqual(binding["draft_id"], draft["draft_id"])
        self.assertEqual(binding["blocks"], [{"block_id": block["block_id"], "fields": block["fields"],
            "source_refs": self.navigation.accepted_sources[block["block_id"]]} for block in draft["blocks"]])
        self.assertEqual(self.helper.draft()["revision"], draft["revision"])
        self.assert_no_failed_version()
        self.assertEqual(self.helper.errors, [])
        self.navigation.snapshot("new-real-plan-created-without-reload")

    def test_latest_failed_plan_does_not_restore_an_older_waiting_plan(self):
        self.ready_accepted_plan()
        old_id = self.previews[0]["plan_id"]
        # A newer real plan is created while the earlier proposal remains history.
        with self.page.expect_response(lambda response: response.request.method == "POST"
                and urlparse(response.url).path.endswith("/plans"), timeout=15000) as completed:
            self.page.get_by_role("button", name="重新生成规划", exact=True).click()
        self.assertEqual(completed.value.status, 200, completed.value.text())
        self.previews.append(completed.value.json())
        self.fail_confirmation()
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        expect(self.page.locator("#formal-plan-confirmation")).to_be_hidden()
        self.page.reload()
        expect(self.page.locator("#formal-plan-create")).to_be_enabled()
        expect(self.page.locator("#formal-plan-confirmation")).to_be_hidden()
        f = self.helper.fixture
        self.assertEqual(self.helper.generation.services.tasks.get_task(f.token, old_id)["status"], "waiting_user")

    def test_failed_audit_keeps_real_unconfirmed_brief_readiness_blocked(self):
        self.ready_accepted_plan()

        def add_pending_brief_before_browser_receives_failure(route):
            response = route.fetch()
            self.assertEqual(response.status, 409, response.text())
            f = self.helper.fixture
            project = f.projects.get_project(f.token, self.helper.project["project_id"])
            self.helper.adapter.import_uploads(f.token, workspace_id=f.workspace_id,
                project_id=project["project_id"], expected_project_version=project["project_version"],
                uploads=[UploadedDocument("anonymous-pending-update.txt",
                    BytesIO("视频目标: 新补充资料等待核对".encode("utf-8")))])
            self.readiness_mutation = "authority imports pending Brief after real audit failure before UI reread"
            route.fulfill(response=response)

        self.page.route("**/plans/*/confirm?*", add_pending_brief_before_browser_receives_failure)
        self.fail_confirmation()
        expect(self.page.locator("#formal-plan-state")).to_have_text("暂不能生成规划")
        expect(self.page.locator("#formal-plan-brief-block")).to_be_visible()
        expect(self.page.locator("#formal-plan-create")).to_be_hidden()
        expect(self.page.locator("#formal-plan-confirmation")).to_be_hidden()
        self.assertEqual(len(self.navigation.plan_requests), 1)
        self.assertEqual(self.helper.errors, [])
        self.navigation.snapshot("failed-audit-still-blocked-by-real-readiness")


if __name__ == "__main__":
    unittest.main()
