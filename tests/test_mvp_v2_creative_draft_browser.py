from copy import deepcopy
from urllib.parse import unquote, urlparse
import unittest

from playwright.sync_api import expect
from tests import test_mvp_v2_creative_workspace_browser as workspace_tests
from tests.test_mvp_v2_ui00_journey3 import PROJECT_ID


class CreativeDraftBrowserTests(unittest.TestCase):
    def setUp(self):
        self.helper = workspace_tests.CreativeWorkspaceBrowserTests("runTest")
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.page = self.helper.page
        fixture = self.helper.fixture
        self.value = {"project_id": PROJECT_ID, "project_version": fixture.project_version,
            "revision": 0, "draft_id": None, "draft_revision": None, "blocks": [],
            "can_undo": False, "can_redo": False, "task": None, "stream_blocks": [], "input_text": "",
            "understanding": {"status": "ready", "confirmation_status": "confirmed"}}
        self.generated = 0
        self.generation_requests = []
        self.changes = []
        self.fail_save = False
        self.protections = []
        self.revisions = []
        self.answers = []
        self.confirmations = []
        original = fixture.response

        def response(method, path):
            route = urlparse(path).path
            if route.endswith("/brief/current"):
                status, result = original(method, path)
                result = deepcopy(result)
                result["confirmation_status"] = self.value["understanding"]["confirmation_status"]
                return status, result
            if route.endswith("/script"):
                return 200, {"state": "empty", "project_id": PROJECT_ID, "project_version": fixture.project_version,
                    "project_name": "匿名初稿项目", "shots": []}
            if route.endswith("/creative-draft"):
                return 200, deepcopy(self.value)
            if route.endswith("/creative-draft/generate"):
                self.generated += 1
                self.generation_requests.append(deepcopy(fixture.body))
                self.value.update(revision=self.value["revision"] + 1, project_version=self.value["project_version"] + 1,
                    input_text=fixture.body["instruction"],
                    task={"task_id": "anonymous-creative-task", "status": "executing", "is_running": True},
                    stream_blocks=[{"block_id": "first-block", "fields": {"段落": "开场", "画面描述": "第一段已出现", "声音": "环境声", "时长": "建议 5 秒"}}])
                return 200, deepcopy(self.value)
            if route.endswith("/creative-draft/changes"):
                body = deepcopy(fixture.body)
                self.changes.append(body)
                if self.fail_save:
                    self.fail_save = False
                    return 503, {"detail": {"message": "匿名网络中断"}}
                if body["expected_revision"] != self.value["revision"]:
                    return 409, {"detail": {"code": "DRAFT_REVISION_CONFLICT", "message": "另一处已更新"}}
                if body["action"] == "patch":
                    block = next(item for item in self.value["blocks"] if item["block_id"] == body["block_id"])
                    block["fields"][body["field"]] = body["value"]
                self.value.update(revision=self.value["revision"] + 1,
                    project_version=self.value["project_version"] + 1,
                    draft_revision=self.value["draft_revision"] + 1, can_undo=True)
                return 200, deepcopy(self.value)
            if route.endswith("/creative-draft/protection"):
                self.protections.append(deepcopy(fixture.body))
                self.value.update(revision=self.value["revision"] + 1,
                    project_version=self.value["project_version"] + 1,
                    protected_block_ids=fixture.body["protected_block_ids"])
                return 200, deepcopy(self.value)
            if route.endswith("/creative-draft/revisions"):
                self.revisions.append(deepcopy(fixture.body))
                self.value.update(revision=self.value["revision"] + 1,
                    project_version=self.value["project_version"] + 1,
                    task={"task_id": "anonymous-revision", "status": "waiting_user", "is_running": False})
                self.value["proposal"] = {"digest": "d" * 64, "revision": self.value["revision"],
                    "summary": "建议修改 1 段，请核对后应用。", "changes": [{"block_id": "block-2",
                        "field": "声音", "before": "环境声", "after": "现场掌声渐入"}]}
                self.value['proposal']['groups'] = [{'block_id': 'block-2', 'changes': self.value['proposal']['changes']}]
                return 200, deepcopy(self.value)
            if route.endswith('/creative-draft/revisions/confirm'):
                self.confirmations.append(deepcopy(fixture.body))
                for change in self.value['proposal']['changes']:
                    if change['block_id'] in fixture.body['retained_block_ids']:
                        block = next(item for item in self.value['blocks'] if item['block_id'] == change['block_id'])
                        block['fields'][change['field']] = change['after']
                self.value.update(revision=self.value['revision'] + 1, proposal=None,
                    task={'task_id': 'anonymous-revision', 'status': 'completed', 'is_running': False})
                return 200, deepcopy(self.value)
            if route.endswith("/creative-draft/answers"):
                self.answers.append(deepcopy(fixture.body))
                self.value.update(revision=self.value["revision"] + 1,
                    pending_answers=[{"field": fixture.body["field"], "value": fixture.body["value"]}],
                    input_notice="补充内容已保存，将更新相关段落。")
                return 200, deepcopy(self.value)
            return original(method, path)

        fixture.response = response
        def imported(route):
            self.value['project_version'] += 1
            route.fulfill(status=200, json={'status': 'ready', 'import_status': 'appended'})
        self.page.route('**/brief/files?*', imported)

    def complete(self):
        blocks = [{"block_id": f"block-{index}", "shot_no": str(index),
            "fields": {"段落": f"第{index}段", "画面描述": f"匿名画面{index}", "声音": "环境声", "时长": "建议 5 秒"}}
            for index in range(1, 4)]
        self.value.update(revision=2, draft_revision=0, draft_id="anonymous-draft", blocks=blocks,
            task={"task_id": "anonymous-creative-task", "status": "completed", "is_running": False})

    def open(self):
        self.page.goto(f"{self.helper.base}/projects/{PROJECT_ID}/script")
        expect(self.page.locator("#creative-first-draft")).to_be_visible()
        # These established behavior tests edit several blocks. Use the supported
        # whole-script view; the application-flow suite exercises the default
        # outline and single-block editor separately.
        self.page.locator('[data-first-view="table"]').click()
        tools = self.page.locator('.creative-first-tools')
        if tools.get_attribute('open') is None:
            tools.locator(':scope > summary').click()

    def test_project_order_uses_public_summary_timestamp_before_paging_and_search(self):
        # ReadonlyAgentService omits top-level timestamps. The actual list API
        # adds summary.updated_at, which must drive both ordering and its label.
        projects = [{"project_id": f"project-{index:02}", "name": f"项目 {index:02}",
            "status": "active", "project_version": 0, "has_brief": False,
            "summary": {"updated_at": f"2026-09-{index:02}T10:00:00Z", "stage": "待核对"}}
            for index in range(1, 16)]
        original = self.helper.fixture.response

        def response(method, path):
            if urlparse(path).path == "/api/formal/workspaces/browser-workspace/projects":
                return 200, {"items": deepcopy(projects)}
            return original(method, path)

        self.helper.fixture.response = response
        self.page.goto(f"{self.helper.base}/projects")
        names = self.page.locator("#formal-project-list article strong")
        expect(names).to_have_text([f"项目 {index:02}" for index in range(15, 3, -1)])
        self.page.get_by_role("button", name="下一页", exact=True).click()
        expect(names).to_have_text(["项目 03", "项目 02", "项目 01"])
        self.page.locator("#formal-project-search").fill("项目 1")
        expect(names).to_have_text([f"项目 {index:02}" for index in range(15, 9, -1)])
        self.page.locator("#formal-project-search").fill("")
        expect(names).to_have_text([f"项目 {index:02}" for index in range(15, 3, -1)])
        self.page.reload()
        expect(names.first).to_have_text("项目 15")
        self.assertEqual(self.helper.errors, [])

    def test_unconfirmed_requirement_returns_to_intake_without_generation(self):
        self.value['understanding']['confirmation_status'] = 'pending'
        self.open()
        expect(self.page.locator('#creative-first-confirmed-start')).to_be_hidden()
        expect(self.page.locator('#creative-first-input')).to_have_count(0)
        self.page.locator('#creative-first-intake-link').click()
        expect(self.page.locator('#page-brief')).to_be_visible()
        expect(self.page.locator('#formal-brief-text')).to_be_visible()
        self.assertEqual(self.generated, 0)
        self.assertEqual(self.helper.errors, [])

    def test_failed_generation_keeps_confirmed_requirement_and_retry_available(self):
        self.open()
        self.page.locator('#creative-first-confirmed-start').click()
        expect(self.page.locator('#creative-first-assistant-status')).to_contain_text('正在按需求创作')
        self.value['task'].update(status='failed', is_running=False)
        self.value['revision'] += 1
        expect(self.page.locator('#creative-first-assistant-status')).to_contain_text('本次创作尚未完成')
        expect(self.page.locator('#creative-first-confirmed-start')).to_be_enabled()
        self.assertTrue(self.generation_requests[0]['use_confirmed_brief'])
        self.assertEqual(self.value['understanding']['confirmation_status'], 'confirmed')

    def test_complete_draft_appears_after_generation_then_edit_and_reload(self):
        self.open()
        self.page.locator("#creative-first-confirmed-start").click()
        expect(self.page.locator("#creative-first-blocks [data-first-field]")).to_have_count(0)
        expect(self.page.locator("#creative-first-assistant-status")).to_contain_text("正在按需求创作")
        self.assertEqual(self.value["blocks"], [])
        self.complete()
        expect(self.page.locator("#creative-first-status")).to_contain_text("初稿已保存")
        cell = self.page.locator('[data-first-block="block-2"][data-first-field="声音"]')
        cell.fill("保留现场掌声")
        self.page.locator("#creative-first-save").click()
        expect(self.page.locator("#creative-first-status")).to_contain_text("初稿已保存")
        self.page.reload()
        expect(self.page.locator('[data-first-block="block-2"][data-first-field="声音"]')).to_have_value("保留现场掌声")
        self.assertEqual(self.generated, 1)
        self.assertEqual(len(self.changes), 1)
        self.assertFalse(self.helper.errors)
        self.helper.capture("creative-first-draft-desktop")
        self.page.set_viewport_size({"width": 390, "height": 844})
        expect(self.page.locator("#creative-first-draft")).to_be_visible()
        self.helper.capture("creative-first-draft-mobile")

    def test_regenerate_existing_draft_uses_page_confirmation_and_keeps_old_blocks(self):
        self.complete()
        before = deepcopy(self.value['blocks'])
        native_dialogs = []
        self.page.on('dialog', lambda dialog: (native_dialogs.append(dialog.type), dialog.dismiss()))
        self.open()
        start = self.page.locator('#creative-first-confirmed-start')
        start.click()
        dialog = self.page.get_by_role('dialog', name='重新起稿', exact=True)
        expect(dialog).to_be_visible()
        self.page.get_by_role('button', name='保留当前稿', exact=True).click()
        expect(dialog).to_have_count(0)
        expect(start).to_be_focused()
        self.assertEqual(self.generated, 0)
        start.click()
        self.page.get_by_role('button', name='确认重新起稿', exact=True).click()
        expect(self.page.locator('#creative-first-assistant-status')).to_contain_text('正在按需求创作')
        self.assertEqual(self.generated, 1)
        self.assertEqual(self.value['blocks'], before)
        self.assertEqual(native_dialogs, [])
        self.assertEqual(self.helper.errors, [])

    def test_failed_save_retries_the_exact_request_without_losing_local_text(self):
        self.complete()
        self.open()
        self.fail_save = True
        cell = self.page.locator('[data-first-block="block-3"][data-first-field="画面描述"]')
        cell.fill("保留用户修改的第三段画面")
        self.page.locator("#creative-first-save").click()
        expect(self.page.locator("#creative-first-status")).to_contain_text("此处修改已保留")
        expect(cell).to_have_value("保留用户修改的第三段画面")
        self.page.locator("#creative-first-save").click()
        expect(self.page.locator("#creative-first-status")).to_contain_text("初稿已保存")
        self.assertEqual(self.changes[0], self.changes[1])
        self.assertFalse(self.helper.errors)

    def test_overlapping_reads_share_request_and_keep_input_entered_while_waiting(self):
        self.complete()
        self.value.update(brief_marker="verified-brief-marker", response_kind="full")
        self.open()
        cell = self.page.locator('[data-first-block="block-3"][data-first-field="画面描述"]')
        expect(cell).to_have_value("匿名画面3")
        pending = []
        self.page.route("**/creative-draft?*", lambda route: pending.append(route))
        self.page.evaluate("""() => {
            const view = firstCreativeView();
            window.pollResults = null;
            Promise.all([refreshFirstCreative(view), refreshFirstCreative(view)])
                .then(results => { window.pollResults = results; });
        }""")
        cell.fill("等待读取时输入的内容")
        self.page.evaluate("clearTimeout(firstCreativeView().saveTimer)")
        self.assertEqual(len(pending), 1)
        self.assertIn("known_brief_marker=verified-brief-marker", pending[0].request.url)
        delta = deepcopy(self.value)
        delta["response_kind"] = "delta"
        for field in ("blocks", "understanding", "input_questions", "input_text"):
            delta.pop(field, None)
        pending[0].fulfill(status=200, json=delta)
        self.page.wait_for_function("window.pollResults !== null")
        self.assertEqual(self.page.evaluate("window.pollResults"), [True, True])
        expect(cell).to_have_value("等待读取时输入的内容")
        self.assertEqual(len(pending), 1)
        self.page.locator("#creative-first-save").click()
        expect(self.page.locator("#creative-first-status")).to_contain_text("初稿已保存")
        self.assertEqual(self.value["blocks"][2]["fields"]["画面描述"], "等待读取时输入的内容")
        self.assertFalse(self.helper.errors)

    def test_leaving_project_ignores_late_poll_response(self):
        self.complete()
        self.open()
        expect(self.page.locator('[data-first-field="画面描述"]').first).to_have_value("匿名画面1")
        self.page.wait_for_function("!firstCreativeView().reading")
        pending = []
        self.page.route("**/creative-draft?*", lambda route: pending.append(route))
        with self.page.expect_request("**/creative-draft?*"):
            self.page.evaluate("""() => {
                window.oldDraftView = firstCreativeView();
                window.latePollResult = null;
                refreshFirstCreative(window.oldDraftView).then(value => { window.latePollResult = value; });
            }""")
        self.page.locator('.project-exit').click()
        expect(self.page).to_have_url(self.helper.base + '/projects')
        self.assertEqual(len(pending), 1)
        late = deepcopy(self.value)
        late['revision'] += 1
        late['blocks'][0]['fields']['画面描述'] = '迟到结果不应覆盖'
        pending[0].fulfill(status=200, json=late)
        self.page.wait_for_function('window.latePollResult !== null')
        self.assertFalse(self.page.evaluate('window.latePollResult'))
        self.assertEqual(self.page.evaluate('window.oldDraftView.data.revision'), self.value['revision'])
        self.assertFalse(self.helper.errors)

    def test_polling_reaches_saved_draft_after_worker_unlocks_before_terminal_event(self):
        self.open()
        self.page.locator("#creative-first-confirmed-start").click()
        expect(self.page.locator("#creative-first-assistant-status")).to_contain_text("正在按需求创作")
        with self.page.expect_response(lambda response: response.url.endswith("/creative-draft?workspace_id=browser-workspace")
                and response.request.method == "GET" and response.json().get("task", {}).get("is_running") is False):
            self.value["task"]["is_running"] = False
        self.complete()
        expect(self.page.locator("#creative-first-status")).to_contain_text("初稿已保存")
        expect(self.page.locator('[data-first-block="block-3"][data-first-field="画面描述"]')).to_have_value("匿名画面3")
        self.assertFalse(self.helper.errors)

    def test_selected_assistant_scope_and_protection_persist_with_manual_edit(self):
        self.complete()
        self.open()
        self.page.locator('[data-first-protect="block-1"]').check()
        self.page.reload()
        expect(self.page.locator('[data-first-protect="block-1"]')).to_be_checked()
        protected_cell = self.page.locator('[data-first-block="block-1"][data-first-field="声音"]')
        protected_cell.fill("用户指定配乐")
        self.page.locator("#creative-first-save").click()
        expect(self.page.locator("#creative-first-status")).to_contain_text("初稿已保存")
        self.page.locator('[data-first-select="block-2"]').check()
        self.page.locator("#creative-first-revision-input").fill("让第2段声音更自然")
        self.page.locator("#creative-first-revision-submit").click()
        expect(self.page.locator("#creative-first-proposal")).to_contain_text("现场掌声渐入")
        self.assertEqual(self.revisions[0]["selected_block_ids"], ["block-2"])
        self.assertEqual(self.protections[0]["protected_block_ids"], ["block-1"])
        self.assertEqual(self.value["blocks"][0]["fields"]["声音"], "用户指定配乐")
        self.assertEqual(self.value["blocks"][1]["fields"]["声音"], "环境声")
        expect(self.page.locator("#creative-first-apply")).to_be_enabled()
        self.helper.capture("creative-first-draft-protected-proposal")
        # A manual edit invalidates the old suggestion; the browser cannot apply it.
        self.page.locator('[data-first-block="block-2"][data-first-field="声音"]').fill("用户后来选择的声音")
        self.page.locator("#creative-first-save").click()
        expect(self.page.locator("#creative-first-proposal")).to_contain_text("需求或初稿已更新，请重新预览建议")
        expect(self.page.locator("#creative-first-apply")).to_be_disabled()
        self.assertFalse(self.helper.errors)

    def test_generation_has_no_second_requirement_form_or_conflict_editor(self):
        self.value["input_questions"] = [{"field": "目标时长", "label": "目标时长", "answer_type": "text", "options": []}]
        self.value["understanding"]["brief"] = {"facts": [{"label": "视频目标", "value": '"原需求"'}],
            "conflicts": [{"conflict_key": "old-conflict", "resolution_status": "unresolved", "choices": []}]}
        self.open()
        self.page.locator("#creative-first-confirmed-start").click()
        expect(self.page.locator("#creative-first-assistant-status")).to_contain_text("正在按需求创作")
        for selector in ("#creative-first-input", "#creative-first-files", "#creative-first-understanding", "[data-first-answer]", "[data-first-conflict]"):
            expect(self.page.locator(selector)).to_have_count(0)
        self.assertEqual(unquote(self.page.locator("#creative-first-intake-link").get_attribute("href")), f"/projects/{PROJECT_ID}/brief")
        self.assertEqual(self.answers, [])
        self.assertEqual(self.helper.errors, [])

    def test_saved_input_prepares_diff_without_recreating_accepted_draft(self):
        self.complete()
        original = deepcopy(self.value['blocks'])
        self.open()
        self.page.locator('#creative-first-revision-input').fill('第二段声音增加现场掌声，其他段落保留')
        self.page.locator('#creative-first-revision-submit').click()
        expect(self.page.locator('#creative-first-proposal')).to_contain_text('现场掌声渐入')
        self.assertEqual(self.generated, 0)
        self.assertEqual(self.value['blocks'], original)
        self.assertEqual(self.revisions[0]['selected_block_ids'], ['block-1', 'block-2', 'block-3'])
        self.assertFalse(self.helper.errors)

    def test_partial_proposal_selection_survives_refresh_and_adopts_whole_block_group(self):
        self.complete()
        changes = [{'block_id': identity, 'field': field, 'before': '环境声' if field == '声音' else '原画面',
                    'after': '只采用这段的' + field}
                   for identity in ['block-2', 'block-3'] for field in ['画面描述', '声音']]
        self.value.update(task={'task_id': 'anonymous-revision', 'status': 'waiting_user', 'is_running': False},
            proposal={'digest': 'e' * 64, 'revision': self.value['revision'], 'summary': '建议修改 2 段',
                      'changes': changes, 'groups': [{'block_id': identity, 'changes': [c for c in changes if c['block_id'] == identity]}
                                                    for identity in ['block-2', 'block-3']]})
        self.open()
        self.page.locator('[data-first-retain="block-3"]').uncheck()
        self.value['notice'] = '本次建议仍待选择'
        self.page.get_by_role('link', name='需求', exact=True).click()
        self.page.get_by_role('link', name='创作', exact=True).click()
        expect(self.page.locator('[data-first-retain="block-3"]')).not_to_be_checked()
        expect(self.page.get_by_role('button', name='采用所选 1 组')).to_be_enabled()
        self.page.get_by_role('button', name='采用所选 1 组').click()
        expect(self.page.locator('#creative-first-proposal')).to_be_hidden()
        self.assertEqual(self.confirmations[0]['retained_block_ids'], ['block-2'])
        self.assertEqual(self.value['blocks'][1]['fields']['声音'], '只采用这段的声音')
        self.assertEqual(self.value['blocks'][1]['fields']['画面描述'], '只采用这段的画面描述')
        self.assertEqual(self.value['blocks'][2]['fields']['声音'], '环境声')
        self.assertFalse(self.helper.errors)

    def test_requirement_return_uses_one_entry_without_changing_existing_draft(self):
        self.complete()
        original = deepcopy(self.value['blocks'])
        self.open()
        self.page.locator('#creative-first-intake-link').click()
        expect(self.page.locator('#page-brief')).to_be_visible()
        self.page.locator('#brief-source-panel > summary').click()
        expect(self.page.locator('#formal-brief-text')).to_be_visible()
        expect(self.page.locator('#formal-brief-files')).to_have_count(1)
        self.page.get_by_role('link', name='创作', exact=True).click()
        self.page.locator('.creative-first-tools > summary').click()
        expect(self.page.locator('#creative-first-revision-input')).to_be_visible()
        expect(self.page.locator('#creative-first-files')).to_have_count(0)
        self.assertEqual(self.value['blocks'], original)
        self.assertEqual(self.generated, 0)
        self.assertFalse(self.helper.errors)

    def test_existing_draft_remains_editable_while_new_attempt_streams(self):
        self.complete()
        # An old in-flight attempt remains readable after the entry migration.
        self.value['task'] = {"task_id": "anonymous-creative-task", "status": "executing", "is_running": True}
        self.value['stream_blocks'] = [{"block_id": "partial", "fields": {"画面描述": "未完成片段"}}]
        self.open()
        expect(self.page.locator("#creative-first-partial")).to_have_count(0)
        cell = self.page.locator('[data-first-block="block-2"][data-first-field="声音"]')
        expect(cell).to_be_editable()
        cell.fill("等待生成时用户保留的音乐")
        self.page.locator("#creative-first-save").click()
        expect(self.page.locator("#creative-first-assistant-status")).to_contain_text("正在按需求创作")
        self.value["task"] = {"task_id": "anonymous-creative-task", "status": "failed", "is_running": False}
        self.value["revision"] += 1
        expect(self.page.locator("#creative-first-assistant-status")).to_contain_text("本次创作尚未完成")
        expect(cell).to_have_value("等待生成时用户保留的音乐")
        self.page.reload()
        expect(cell).to_have_value("等待生成时用户保留的音乐")
        self.assertEqual(len(self.changes), 1)
        self.assertFalse(self.helper.errors)


if __name__ == "__main__":
    unittest.main()
