import json
import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from starlette.testclient import TestClient
from unittest.mock import patch

from mvp_v2.formal_script_revision import FormalRevisionService
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from tests import test_mvp_v2_formal_console as console_tests
from tests.test_mvp_v2_creative_generation import BLOCKS
from mvp_v2.version_review_service import VersionReviewService


class CreativeDraftApiTests(unittest.TestCase):
    def setUp(self):
        self.fixture = console_tests.FormalConsoleTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.project = self.fixture._project()
        self.fixture.agent.script_revision = FormalRevisionService(projects=self.fixture.projects,
            tasks=self.fixture.agent.services.tasks, workspace=None, writes_enabled=True,
            brief_adapter=self.fixture.adapter)
        self.calls = 0

        def stream(prompt, model, limit, emit):
            self.calls += 1
            context = json.loads(prompt.split('项目上下文：', 1)[1])
            blocks = [{**block, '时长': '20'} for block in BLOCKS] if context.get(
                'confirmed_output_requirements', {}).get('total_seconds') == 60 else BLOCKS
            text = json.dumps({"blocks": blocks}, ensure_ascii=False)
            emit(text)
            return text

        self.fixture.generation.creative_stream_provider = stream
        self.fixture.generation.brief_input_adapter = self.fixture.adapter
        self.fixture.generation.creative_revision = self.fixture.agent.script_revision
        self.path = f"/api/formal/projects/{self.project['project_id']}/creative-draft"
        self.params = {"workspace_id": self.fixture.workspace_id}

    def request(self, method="get", action="", **kwargs):
        return getattr(self.fixture.client, method)(self.path + ("/" + action if action else ""),
            params=self.params, headers=self.fixture.headers, **kwargs)

    def generate(self):
        response = self.request("post", "generate", json={"expected_project_version": 0,
            "expected_revision": 0, "instruction": "先写三段活动创意，未知信息留待补", "operation_key": "api-creative-operation"})
        self.assertEqual(response.status_code, 200, response.text)
        for _ in range(100):
            current = self.request().json()
            if current.get("task", {}).get("status") == "completed":
                return current
            time.sleep(.02)
        self.fail(str(current))

    def test_delta_poll_requires_verified_cache_and_pending_brief_invalidates_it(self):
        self.fixture._confirm_formal_brief(self.project["project_id"])
        first = self.request().json()
        self.assertEqual(first["response_kind"], "full")
        params = {**self.params, "known_revision": first["revision"], "known_brief_marker": first["brief_marker"]}
        def poll():
            return self.fixture.client.get(self.path, params=params, headers=self.fixture.headers)
        adapter = self.fixture.adapter
        with patch.object(adapter, "_verify_sources", wraps=adapter._verify_sources) as sources, \
                patch.object(adapter, "_verify_ocr_evidence", wraps=adapter._verify_ocr_evidence) as ocr, \
                patch.object(self.fixture.generation.services.briefs.artifacts, "read",
                    wraps=self.fixture.generation.services.briefs.artifacts.read) as artifacts:
            for _ in range(10):
                response = poll()
                self.assertEqual(response.status_code, 200, response.text)
                delta = response.json()
                self.assertEqual(delta["response_kind"], "delta")
                for field in ("blocks", "understanding", "input_questions", "input_text"):
                    self.assertNotIn(field, delta)
            self.assertEqual(sources.call_count, 0)
            self.assertEqual(ocr.call_count, 0)
            self.assertEqual(artifacts.call_count, 0)
        self.fixture.generation._creative_read_cache.clear()
        with patch.object(adapter, "_verify_sources", wraps=adapter._verify_sources) as sources:
            self.assertEqual(poll().json()["response_kind"], "full")
            self.assertEqual(sources.call_count, 1)
        for entry in self.fixture.generation._creative_read_cache.values():
            entry["created"] -= 11
        with patch.object(adapter, "_verify_sources", wraps=adapter._verify_sources) as sources:
            self.assertEqual(poll().json()["response_kind"], "full")
            self.assertEqual(sources.call_count, 1)
        adapter.import_inline_answer(self.fixture.token, workspace_id=self.fixture.workspace_id,
            project_id=self.project["project_id"], field="风格调性", value="新的待确认风格",
            expected_project_version=first["project_version"])
        changed = poll()
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["response_kind"], "full")
        self.assertNotEqual(changed.json()["brief_marker"], first["brief_marker"])
        self.assertEqual(changed.json()["understanding"]["confirmation_status"], "pending")

    def test_cached_poll_rechecks_revoked_identity(self):
        self.fixture._confirm_formal_brief(self.project["project_id"])
        first = self.request().json()
        self.assertTrue(self.fixture.generation._creative_read_cache)
        with self.fixture.store.transaction() as connection:
            connection.execute("UPDATE auth_sessions SET revoked_at='2026-09-18T00:00:00Z'")
        response = self.fixture.client.get(self.path, params={**self.params,
            "known_revision": first["revision"], "known_brief_marker": first["brief_marker"]},
            headers=self.fixture.headers)
        self.assertEqual(response.status_code, 401, response.text)
        self.assertNotIn("understanding", response.json())

    def test_cancelled_answer_task_is_not_hidden_by_cached_draft(self):
        current = self.generate()
        def analysis(**kwargs):
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": kwargs["blocks"][0]["shot_id"], "field": "visual", "value": "高级风格画面"}]}
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            proposal = self._answer(current, "风格调性", "高级", "cached-cancel-answer").json()
        self.assertTrue(proposal["unimplemented_answers"])
        tasks = self.fixture.generation.services.tasks
        task = tasks.get_task(self.fixture.token, proposal["task"]["task_id"])
        tasks.transition_task(self.fixture.token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="cancelled")
        response = self.fixture.client.get(self.path, params={**self.params,
            "known_revision": proposal["revision"], "known_brief_marker": proposal["brief_marker"]},
            headers=self.fixture.headers)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["revision"], proposal["revision"])
        self.assertEqual(value["brief_marker"], proposal["brief_marker"])
        self.assertEqual(value["task"]["status"], "cancelled")
        self.assertEqual(value["unimplemented_answers"], [])

    def test_terminal_poll_returns_complete_payload_even_with_matching_cache_identity(self):
        current = self.generate()
        response = self.fixture.client.get(self.path, params={**self.params,
            "known_revision": current["revision"], "known_brief_marker": current["brief_marker"]},
            headers=self.fixture.headers)
        self.assertEqual(response.status_code, 200, response.text)
        value = response.json()
        self.assertEqual(value["task"]["status"], "completed")
        self.assertEqual(value["response_kind"], "full")
        self.assertEqual(value["blocks"], current["blocks"])

    def test_poll_uses_one_authorized_brief_snapshot_and_still_verifies_sources(self):
        self.fixture._confirm_formal_brief(self.project["project_id"])
        adapter = self.fixture.adapter
        scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.project["project_id"]}
        expected = adapter.read_current_formal(self.fixture.token, **scope)
        questions = adapter.input_questions(self.fixture.token, **scope)
        briefs = self.fixture.generation.services.briefs
        with patch.object(briefs, "read_current_formal", wraps=briefs.read_current_formal) as reads, \
                patch.object(adapter, "_verify_sources", wraps=adapter._verify_sources) as sources, \
                patch.object(adapter, "_verify_ocr_evidence", wraps=adapter._verify_ocr_evidence) as ocr:
            for _ in range(3):
                response = self.request()
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["understanding"], expected)
                self.assertEqual(response.json()["input_questions"], questions)
            self.assertEqual(sum(not call.kwargs.get("_metadata_only") for call in reads.call_args_list), 3)
            self.assertEqual(sources.call_count, 3)
            self.assertEqual(ocr.call_count, 3)

    def test_empty_poll_uses_one_brief_snapshot_without_changing_optional_questions(self):
        adapter = self.fixture.adapter
        questions = adapter.input_questions(self.fixture.token, workspace_id=self.fixture.workspace_id,
            project_id=self.project["project_id"])
        briefs = self.fixture.generation.services.briefs
        with patch.object(briefs, "read_current_formal", wraps=briefs.read_current_formal) as reads:
            response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["understanding"]["status"], "empty")
        self.assertEqual(response.json()["input_questions"], questions)
        self.assertEqual(sum(not call.kwargs.get("_metadata_only") for call in reads.call_args_list), 1)

    def test_answer_before_first_draft_returns_and_keeps_saved_fact_without_model(self):
        from mvp_v2 import creative_revision
        consume = creative_revision.consume_answers
        attempts = []

        def bounded_consume(*args, **kwargs):
            # Detect the original non-terminating loop without hanging the suite.
            if len(attempts) >= 3:
                raise AssertionError("已保存的补充在没有初稿时被反复入队")
            result = consume(*args, **kwargs)
            attempts.append(result)
            return result

        with patch.object(creative_revision, "consume_answers", side_effect=bounded_consume):
            result = self.request("post", "answers", json={"expected_project_version": 0,
                "expected_revision": 0, "field": "风格调性", "value": "高级，国风",
                "operation_key": "before-first-draft-answer"})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(attempts, [True, False])
        self.assertEqual(result.json()["blocks"], [])
        self.assertEqual(result.json()["pending_answers"], [])
        current = self.request().json()
        facts = current["understanding"]["brief"]["facts"]
        self.assertIn("高级，国风", [json.loads(fact["value"]) for fact in facts])
        self.assertEqual(self.calls, 0)

    def test_confirmed_requirement_generation_does_not_reimport_or_unconfirm_brief(self):
        version = self.fixture._confirm_formal_brief(self.project["project_id"])
        before = self.fixture.adapter.read_current_formal(self.fixture.token,
            workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"])
        current = self.request().json()
        with patch.object(self.fixture.adapter, "import_uploads", side_effect=AssertionError("确认后起稿不能重新导入指令")):
            result = self.fixture.generation.start_creative_draft(self.fixture.token,
                workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"],
                expected_project_version=version, expected_revision=current["revision"],
                instruction="依据已确认需求创作", operation_key="confirmed-requirement-generation",
                use_confirmed_brief=True, start_background=False)
            self.fixture.generation.execute_creative_draft(self.fixture.token,
                workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"],
                task_id=result["task"]["task_id"])
        after = self.request().json()
        self.assertEqual(after["task"]["status"], "completed", after)
        self.assertEqual(len(after["blocks"]), 3)
        self.assertEqual(after["understanding"]["confirmation_status"], "confirmed")
        self.assertEqual(after["understanding"]["brief_key"], before["brief_key"])

    def _confirmed_generation(self):
        version = self.fixture._confirm_formal_brief(self.project["project_id"])
        current = self.request().json()
        scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.project["project_id"]}
        task = self.fixture.generation.start_creative_draft(self.fixture.token, **scope,
            expected_project_version=version, expected_revision=current["revision"],
            instruction="依据已确认需求创作", operation_key="confirmed-generation-test",
            use_confirmed_brief=True, start_background=False)
        self.fixture.generation.execute_creative_draft(self.fixture.token, **scope,
            task_id=task["task"]["task_id"])
        return scope

    def _update_confirmed_requirement(self, scope, field, value):
        from mvp_v2.creative_revision import effective_brief_values
        from mvp_v2.formal_brief_input_adapter import UploadedDocument
        from io import BytesIO
        adapter = self.fixture.adapter
        raw = adapter.service.read_current_formal(self.fixture.token, **scope)
        values = {**effective_brief_values(raw), field: value}
        # This journey explicitly replaces one field. Appending another
        # source is additive and must not silently erase earlier style facts.
        adapter.update_field(self.fixture.token, **scope, brief_version_id=raw['brief_version_id'],
            field_label=field, value=value,
            expected_project_version=self.request().json()["project_version"])
        raw = adapter.service.read_current_formal(self.fixture.token, **scope)
        while True:
            conflict = next((item for item in raw["brief"]["conflicts"]
                if item["resolution_status"] != "resolved"), None)
            if conflict is None:
                break
            selected = next(fact["fact_id"] for fact in raw["brief"]["facts"]
                if fact["fact_id"] in conflict["fact_ids"]
                and json.loads(fact["value_json"]) == values[conflict["field_path"]])
            adapter.resolve_conflict(self.fixture.token, **scope, brief_version_id=raw["brief_version_id"],
                conflict_id=conflict["conflict_id"], selected_fact_id=selected,
                expected_project_version=self.request().json()["project_version"])
            raw = adapter.service.read_current_formal(self.fixture.token, **scope)
        self.assertFalse(self.request().json()["answers_need_preview"])
        adapter.confirm(self.fixture.token, **scope, brief_version_id=raw["brief_version_id"],
            confirmed_at="2026-10-02T00:00:00Z", expected_project_version=self.request().json()["project_version"])
        return self.request().json()

    def test_reconfirmed_requirements_preview_once_preserve_brief_and_reject_stale_adoption(self):
        scope = self._confirmed_generation()
        original = self.request().json()["blocks"]
        observed = []
        def analysis(**kwargs):
            updates = json.loads(kwargs["context"]["recent_conversation"][0]["content"].split("：", 1)[1])
            observed.append(updates)
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": "sound", "value": updates[0]["value"]}
                for block in kwargs["blocks"]]}
        current = self._update_confirmed_requirement(scope, "风格调性", "新风格甲")
        self.assertEqual(current["unimplemented_answers"], [{"field": "风格调性", "value": "新风格甲"}])
        self.assertTrue(current["answers_need_preview"])
        self.assertEqual(current["blocks"], original)
        adapter = self.fixture.adapter
        with patch.object(adapter, "import_inline_answer", side_effect=AssertionError("不得重新导入已确认事实")), \
                patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            for _ in range(3):
                read = self.request().json()
                self.assertEqual(read["revision"], current["revision"])
                self.assertEqual(read["task"], current["task"])
            response = self.request("post", "answers/continue", json={})
            self.assertEqual(response.status_code, 200, response.text)
            first = response.json()
        self.assertEqual(first["understanding"]["confirmation_status"], "confirmed")
        self.assertEqual(first["understanding"]["brief_key"], current["understanding"]["brief_key"])
        self.assertEqual(observed[-1], [{"field": "风格调性", "value": "新风格甲"}])
        current = self._update_confirmed_requirement(scope, "风格调性", "新风格乙")
        self.assertTrue(current["answers_need_preview"])
        stale = self.request("post", "revisions/confirm", json={"task_id": first["task"]["task_id"],
            "proposal_digest": first["proposal"]["digest"]})
        self.assertEqual(stale.status_code, 409, stale.text)
        for value in ("新风格乙", "新风格甲"):
            if value == "新风格甲":
                current = self._update_confirmed_requirement(scope, "风格调性", value)
                self.assertTrue(current["answers_need_preview"])
            with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
                response = self.request("post", "answers/continue", json={})
                self.assertEqual(response.status_code, 200, response.text)
                preview = response.json()
            accepted = self.request("post", "revisions/confirm", json={"task_id": preview["task"]["task_id"],
                "proposal_digest": preview["proposal"]["digest"], "retained_block_ids": []})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            saved = self.request().json()
            self.assertFalse(saved["answers_need_preview"])
            self.assertEqual(saved["unimplemented_answers"], [])
            self.assertEqual(saved["blocks"], original)
        self.assertEqual(self.request().json()["understanding"]["confirmation_status"], "confirmed")

    def _confirmed_draft_without_duration(self):
        from io import BytesIO
        from mvp_v2.formal_brief_input_adapter import UploadedDocument
        scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.project["project_id"]}
        adapter = self.fixture.adapter
        imported = adapter.import_uploads(self.fixture.token, **scope, expected_project_version=0,
            uploads=[UploadedDocument("需求.txt", BytesIO(
                "项目名称：匿名时长续改\n脚本类型：发布会\n风格调性：自然\n视频目标：呈现产品体验".encode()), True)])
        adapter.confirm(self.fixture.token, **scope, brief_version_id=imported["brief_key"],
            expected_project_version=1, confirmed_at="2026-10-10T00:00:00Z")
        def stream(_prompt, _model, _limit, emit):
            blocks = [{**BLOCKS[index % len(BLOCKS)], "时长": "3" if index < 6 else "2"}
                for index in range(25)]
            text = json.dumps({"blocks": blocks}, ensure_ascii=False)
            emit(text)
            return text
        self.fixture.generation.creative_stream_provider = stream
        current = self.request().json()
        started = self.fixture.generation.start_creative_draft(self.fixture.token, **scope,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            instruction="按已确认需求创作", use_confirmed_brief=True,
            operation_key="without-duration-confirmed-generation", start_background=False)
        self.fixture.generation.execute_creative_draft(self.fixture.token, **scope,
            task_id=started["task"]["task_id"])
        current = self.request().json()
        self.assertEqual(current["task"]["status"], "completed", current)
        self.assertEqual(sum(int(block["fields"]["时长"]) for block in current["blocks"]), 56)
        return scope, current

    def test_later_confirmed_duration_survives_rejection_partial_adoption_and_recovers(self):
        scope, original = self._confirmed_draft_without_duration()
        current = self._update_confirmed_requirement(scope, "目标时长", "60s")
        self.assertEqual(current["blocks"], original["blocks"])
        self.assertEqual(current["unimplemented_answers"], [{"field": "目标时长", "value": "60s"}])
        def adjust(**kwargs):
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": "duration_text", "value": "5"}
                for block in kwargs["blocks"][:2] if block["fields"]["时长"] != "5"]}
        def preview():
            with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=adjust):
                response = self.request("post", "answers/continue", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["task"]["status"], "waiting_user", response.text)
            return response.json()
        def adopt(proposal, retained):
            response = self.request("post", "revisions/confirm", json={
                "task_id": proposal["task"]["task_id"], "proposal_digest": proposal["proposal"]["digest"],
                "retained_block_ids": retained})
            self.assertEqual(response.status_code, 200, response.text)
            self.fixture.generation._creative_read_cache.clear()
            return self.request().json()
        rejected = adopt(preview(), [])
        self.assertEqual(rejected["blocks"], original["blocks"])
        self.assertTrue(rejected["answers_need_preview"])
        self.assertEqual(rejected["unimplemented_answers"], [{"field": "目标时长", "value": "60s"}])
        first_id = original["blocks"][0]["block_id"]
        partial = adopt(preview(), [first_id])
        self.assertEqual(sum(int(block["fields"]["时长"]) for block in partial["blocks"]), 58)
        self.assertTrue(partial["answers_need_preview"])
        self.assertEqual(partial["unimplemented_answers"], [{"field": "目标时长", "value": "60s"}])
        restored = adopt(preview(), [original["blocks"][1]["block_id"]])
        self.assertEqual(sum(int(block["fields"]["时长"]) for block in restored["blocks"]), 60)
        self.assertFalse(restored["answers_need_preview"])
        self.assertEqual(restored["unimplemented_answers"], [])
        self.assertEqual([block["fields"]["画面描述"] for block in restored["blocks"]],
            [block["fields"]["画面描述"] for block in original["blocks"]])
        # Free editing stays available; satisfying a requirement once does not
        # certify a later edited total or an undo/redo of the actual draft.
        edited = self.request("post", "changes", json={"expected_project_version": restored["project_version"],
            "expected_revision": restored["revision"], "expected_draft_revision": restored["draft_revision"],
            "action": "patch", "block_id": first_id, "field": "时长", "value": "6",
            "operation_key": "free-edit-after-duration-recovery"})
        self.assertEqual(edited.status_code, 200, edited.text)
        edited = edited.json()
        self.assertEqual(sum(int(block["fields"]["时长"]) for block in edited["blocks"]), 61)
        self.assertTrue(edited["answers_need_preview"])
        undone = self.request("post", "changes", json={"expected_project_version": edited["project_version"],
            "expected_revision": edited["revision"], "expected_draft_revision": edited["draft_revision"],
            "action": "undo", "operation_key": "undo-after-duration-free-edit"})
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(undone.json()["unimplemented_answers"], [])

    def test_unrelated_adoption_does_not_satisfy_later_confirmed_duration(self):
        scope, original = self._confirmed_draft_without_duration()
        self._update_confirmed_requirement(scope, "目标时长", "60s")
        def unrelated(**kwargs):
            return {"type": "rewrite_operations", "operations": [{
                "shot_id": kwargs["blocks"][0]["shot_id"], "field": "sound", "value": "环境声延续"}]}
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=unrelated):
            preview = self.request("post", "answers/continue", json={}).json()
        response = self.request("post", "revisions/confirm", json={
            "task_id": preview["task"]["task_id"], "proposal_digest": preview["proposal"]["digest"]})
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.request().json()
        self.assertEqual(saved["blocks"][0]["fields"]["声音"], "环境声延续")
        self.assertEqual(sum(int(block["fields"]["时长"]) for block in saved["blocks"]), 56)
        self.assertTrue(saved["answers_need_preview"])
        self.assertEqual(saved["unimplemented_answers"], [{"field": "目标时长", "value": "60s"}])

    def test_historical_reviewed_basis_does_not_hide_unsatisfied_duration(self):
        from mvp_v2.creative_generation import repository
        from mvp_v2.creative_revision import effective_brief_values
        scope, original = self._confirmed_draft_without_duration()
        current = self._update_confirmed_requirement(scope, "目标时长", "60s")
        raw = self.fixture.adapter.service.read_current_formal(self.fixture.token, **scope)
        repo = repository(self.fixture.generation, self.fixture.token, **scope)
        with repo.transaction(expected_project_version=current['project_version'], expected_revision=current['revision']):
            repo.body['requirement_basis'] = {'brief_version_id': raw['brief_version_id'],
                'values': effective_brief_values(raw)}
        self.fixture.generation._creative_read_cache.clear()
        saved = self.request().json()
        self.assertEqual(saved['blocks'], original['blocks'])
        self.assertEqual(saved['unimplemented_answers'], [{'field': '目标时长', 'value': '60s'}])
        self.assertTrue(saved['answers_need_preview'])

    def test_duration_continue_uses_editor_duration_scope_and_preserves_protected_block(self):
        scope, original = self._confirmed_draft_without_duration()
        current = self._update_confirmed_requirement(scope, '目标时长', '60s')
        ids = [block['block_id'] for block in original['blocks']]
        protected = self.request('post', 'protection', json={
            'expected_project_version': current['project_version'], 'expected_revision': current['revision'],
            'protected_block_ids': [ids[0]], 'operation_key': 'duration-protect-first-block'})
        self.assertEqual(protected.status_code, 200, protected.text)
        observed = []
        def editor(prompt, _model, _limit):
            data = json.loads(prompt.split('\nDATA:', 1)[1])
            observed.append(data)
            return json.dumps({'schema_version': 'mplus-formal-script-assistant-editor/v1',
                'operations': [{'shot_id': item['shot_id'], 'field': item['field'], 'value': '5'}
                    for item in data['targets'] if item['shot_id'] in ids[1:3]]}, ensure_ascii=False)
        self.fixture.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=editor, provider='anonymous', model='anonymous')
        response = self.request('post', 'answers/continue', json={})
        self.assertEqual(response.status_code, 200, response.text)
        proposal = response.json()
        self.assertEqual(proposal['task']['status'], 'waiting_user', proposal)
        self.assertEqual({target['field'] for target in observed[0]['targets']}, {'duration_text'})
        self.assertNotIn(ids[0], {target['shot_id'] for target in observed[0]['targets']})
        accepted = self.request('post', 'revisions/confirm', json={
            'task_id': proposal['task']['task_id'], 'proposal_digest': proposal['proposal']['digest']})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        result = accepted.json()
        self.assertEqual(sum(int(block['fields']['时长']) for block in result['blocks']), 60)
        self.assertEqual(result['blocks'][0], original['blocks'][0])
        self.assertEqual(result['unimplemented_answers'], [])

    def test_legacy_confirmed_draft_recovers_requirement_basis_from_frozen_input(self):
        from mvp_v2.creative_generation import repository
        scope = self._confirmed_generation()
        repo = repository(self.fixture.generation, self.fixture.token, **scope)
        current = repo.read()
        with repo.transaction(expected_project_version=current["project_version"], expected_revision=current["revision"]):
            repo.body.pop("requirement_basis")
            # A newer failed attempt must not replace the accepted draft's basis.
            repo.body["inputs"].append({"text": "未成功的新输入"})
        self.assertFalse(self.request().json()["answers_need_preview"])
        current = self._update_confirmed_requirement(scope, "风格调性", "后来确认的风格")
        self.assertEqual(current["unimplemented_answers"], [{"field": "风格调性", "value": "后来确认的风格"}])

    def test_pre_draft_answer_included_in_generation_is_not_proposed_again(self):
        answer = self.request("post", "answers", json={"expected_project_version": 0,
            "expected_revision": 0, "field": "风格调性", "value": "高级，国风",
            "operation_key": "before-generation-saved-answer"})
        self.assertEqual(answer.status_code, 200, answer.text)
        current = answer.json()
        result = self.fixture.generation.start_creative_draft(self.fixture.token,
            workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"],
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            instruction="按已提供资料创作", operation_key="generation-after-saved-answer", start_background=False)
        self.fixture.generation.execute_creative_draft(self.fixture.token,
            workspace_id=self.fixture.workspace_id, project_id=self.project["project_id"],
            task_id=result["task"]["task_id"])
        current = self.request().json()
        self.assertEqual(current["task"]["status"], "completed")
        self.assertEqual(current["task_kind"], "generation")
        self.assertEqual(current["unimplemented_answers"], [])
        self.assertFalse(current["answers_need_preview"])
        self.assertIsNone(current["proposal"])

    def test_empty_brief_can_generate_then_edit_undo_and_replay_without_formal_writes(self):
        current = self.generate()
        self.assertEqual(len(current["blocks"]), 3)
        body = {"expected_project_version": current["project_version"], "expected_revision": current["revision"],
            "expected_draft_revision": current["draft_revision"], "action": "patch", "operation_key": "api-edit-operation",
            "block_id": current["blocks"][1]["block_id"], "field": "声音", "value": "用户指定的音乐"}
        edited = self.request("post", "changes", json=body)
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["blocks"][1]["fields"]["声音"], "用户指定的音乐")
        replay = self.request("post", "changes", json=body)
        self.assertEqual(replay.json(), edited.json())
        current = edited.json()
        undone = self.request("post", "changes", json={"expected_project_version": current["project_version"],
            "expected_revision": current["revision"], "expected_draft_revision": current["draft_revision"],
            "action": "undo", "operation_key": "api-undo-operation"})
        self.assertEqual(undone.status_code, 200, undone.text)
        self.assertEqual(undone.json()["blocks"][1]["fields"]["声音"], "环境声")
        self.assertEqual(self.calls, 1)
        with self.fixture.store.connection() as connection:
            for table in ("project_versions", "brief_confirmations", "version_review_human_decisions"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_current_draft_shows_authoritative_understanding_and_source_labels(self):
        current = self.generate()
        body = {'expected_project_version': current['project_version'], 'expected_revision': current['revision'],
                'field': '目标时长', 'value': '60秒', 'operation_key': 'understanding-source-01'}
        self.fixture.adapter.import_inline_answer(self.fixture.token, workspace_id=self.fixture.workspace_id,
            project_id=self.project['project_id'], field='目标时长', value='60秒',
            expected_project_version=current['project_version'])
        understanding = self.request().json()['understanding']
        self.assertEqual(understanding['confirmation_status'], 'pending')
        facts = understanding['brief']['facts']
        duration = next(fact for fact in facts if fact['label'] == '目标时长')
        self.assertTrue(duration['sources'])
        public = json.dumps(understanding, ensure_ascii=False)
        self.assertNotIn(str(self.fixture.root), public)
        self.assertNotIn('artifact://', public)
        self.assertEqual(self.fixture._table_count('brief_confirmations'), 0)

    def test_auth_scope_readonly_and_provider_overrides_are_rejected(self):
        body = {"expected_project_version": 0, "expected_revision": 0, "instruction": "匿名初稿",
            "operation_key": "api-creative-operation"}
        self.assertEqual(self.request("post", "generate", json={**body, "model": "client-model"}).status_code, 422)
        self.fixture.generation.creative_writes_enabled = False
        self.assertEqual(self.request("post", "generate", json=body).status_code, 409)
        self.fixture.generation.creative_writes_enabled = True
        invalid = self.fixture.client.post(self.path + "/generate", params=self.params,
            headers={"X-MPlus-Agent-Auth": "invalid"}, json=body)
        self.assertEqual(invalid.status_code, 401)
        self.assertEqual(self.calls, 0)

    def propose_sound(self, current):
        self.editor_calls = []

        def editor(prompt, model, limit):
            data = json.loads(prompt.split("\nDATA:", 1)[1])
            self.editor_calls.append(data)
            target = data["targets"][0]
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{"shot_id": target["shot_id"], "field": target["field"], "value": "前段保留音乐，现场掌声渐入"}]}, ensure_ascii=False)

        self.fixture.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=editor, provider="anonymous-editor", model="anonymous-model")
        response = self.request("post", "revisions", json={"expected_project_version": current["project_version"],
            "expected_revision": current["revision"], "instruction": "修改第2段声音，让掌声自然衔接音乐",
            "selected_block_ids": [item["block_id"] for item in current["blocks"]],
            "operation_key": "api-assistant-operation"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_semantic_proposal_reuses_controlled_editor_and_changes_only_confirmed_targets(self):
        before = self.generate()
        proposal = self.propose_sound(before)
        self.assertEqual(proposal["blocks"], before["blocks"])
        self.assertEqual(proposal["task"]["status"], "waiting_user")
        self.assertEqual({item["field"] for item in self.editor_calls[0]["targets"]}, {"sound"})
        self.assertEqual(len(self.editor_calls[0]["targets"]), 1)
        body = {"task_id": proposal["task"]["task_id"], "proposal_digest": proposal["proposal"]["digest"]}
        applied = self.request("post", "revisions/confirm", json=body)
        self.assertEqual(applied.status_code, 200, applied.text)
        current = applied.json()
        self.assertEqual(current["blocks"][0], before["blocks"][0])
        self.assertEqual(current["blocks"][2], before["blocks"][2])
        self.assertEqual(current["blocks"][1]["fields"]["声音"], "前段保留音乐，现场掌声渐入")
        self.assertEqual(self.request("post", "revisions/confirm", json=body).json(), current)
        self.assertEqual(len(self.editor_calls), 1)
        self.assertEqual(self.fixture._table_count("project_versions"), 0)

    def test_preserving_existing_block_count_does_not_narrow_rewrite_to_last_block(self):
        from mvp_v2.formal_script_assistant import analyze_creative_blocks
        current = self.generate()
        targets = []
        def editor(prompt, model, limit):
            data = json.loads(prompt.split('\nDATA:', 1)[1])
            targets.extend(data['targets'])
            return json.dumps({'schema_version': 'mplus-formal-script-assistant-editor/v1',
                'operations': [{'shot_id': item['shot_id'], 'field': item['field'], 'value': '调整后的具体表达'}
                               for item in data['targets']]}, ensure_ascii=False)
        ids = [block['block_id'] for block in current['blocks']]
        blocks = [{'shot_id': block['block_id'], 'shot_no': str(index), 'fields': block['fields']}
                  for index, block in enumerate(current['blocks'], 1)]
        policy = FormalScriptAssistantEditorPolicy(invoke=editor, provider='anonymous', model='anonymous')
        result = analyze_creative_blocks(instruction='根据补充资料改成60秒的活动回顾短片，保留现有三段顺序，突出客户交流与品牌产品',
            blocks=blocks, selected_block_ids=ids, protected_block_ids=[ids[0]], editor_policy=policy)
        self.assertEqual(result['type'], 'rewrite_operations')
        self.assertEqual({item['shot_id'] for item in targets}, set(ids[1:]))

    def test_lost_confirmation_receipt_reconciles_without_reapplying_draft_patch(self):
        proposal = self.propose_sound(self.generate())
        revision = self.fixture.agent.script_revision
        original = revision.tasks.append_task_event

        def fail_receipt(*args, **kwargs):
            if kwargs.get("status") == "completed":
                raise RuntimeError("anonymous receipt failure")
            return original(*args, **kwargs)

        scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.project["project_id"],
            "task_id": proposal["task"]["task_id"], "proposal_digest": proposal["proposal"]["digest"]}
        with patch.object(revision.tasks, "append_task_event", side_effect=fail_receipt), self.assertRaises(RuntimeError):
            revision.confirm_creative_draft_change(self.fixture.token, **scope)
        changed = self.request().json()
        revision.confirm_creative_draft_change(self.fixture.token, **scope)
        recovered = self.request().json()
        self.assertEqual(recovered["draft_revision"], changed["draft_revision"])
        self.assertEqual(recovered["blocks"], changed["blocks"])
        self.assertEqual(recovered["task"]["status"], "completed")

    def test_partial_adoption_keeps_block_fields_together_and_shares_manual_undo_history(self):
        before = self.generate()
        ids = [block['block_id'] for block in before['blocks']]
        manual = self.request('post', 'changes', json={
            'expected_project_version': before['project_version'], 'expected_revision': before['revision'],
            'expected_draft_revision': before['draft_revision'], 'action': 'patch', 'operation_key': 'manual-before-partial',
            'block_id': ids[0], 'field': '声音', 'value': '用户保留的开场声音'}).json()
        protected = self.request('post', 'protection', json={
            'expected_project_version': manual['project_version'], 'expected_revision': manual['revision'],
            'protected_block_ids': [ids[0]], 'operation_key': 'protect-before-partial'}).json()
        def editor(prompt, model, limit):
            data = json.loads(prompt.split('\nDATA:', 1)[1])
            return json.dumps({'schema_version': 'mplus-formal-script-assistant-editor/v1',
                'operations': [{'shot_id': item['shot_id'], 'field': item['field'],
                    'value': '建议表达：' + item['field']} for item in data['targets']]}, ensure_ascii=False)
        self.fixture.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=editor, provider='anonymous', model='anonymous')
        response = self.request('post', 'revisions', json={
            'expected_project_version': protected['project_version'], 'expected_revision': protected['revision'],
            'instruction': '调整整份初稿的画面和声音', 'selected_block_ids': ids, 'operation_key': 'partial-proposal-01'})
        self.assertEqual(response.status_code, 200, response.text)
        proposal = response.json()
        body = {'task_id': proposal['task']['task_id'], 'proposal_digest': proposal['proposal']['digest'],
                'retained_block_ids': [ids[1]]}
        for invalid in [[ids[0]], ['foreign-block'], [ids[1], ids[1]]]:
            response = self.request('post', 'revisions/confirm', json={**body, 'retained_block_ids': invalid})
            self.assertIn(response.status_code, (400, 409, 422), response.text)
            self.assertEqual(self.request().json()['blocks'], protected['blocks'])
        response = self.request('post', 'revisions/confirm', json=body)
        self.assertEqual(response.status_code, 200, response.text)
        applied = response.json()
        self.assertEqual(applied['blocks'][0], manual['blocks'][0])
        self.assertEqual(applied['blocks'][2], manual['blocks'][2])
        self.assertEqual(applied['blocks'][1]['fields']['画面描述'], '建议表达：visual')
        self.assertEqual(applied['blocks'][1]['fields']['声音'], '建议表达：sound')
        self.assertEqual(len(proposal['proposal']['groups']), 2)
        self.assertEqual(self.request('post', 'revisions/confirm', json=body).json(), applied)
        altered = self.request('post', 'revisions/confirm', json={**body, 'retained_block_ids': [ids[2]]})
        self.assertEqual(altered.status_code, 409, altered.text)
        current = applied
        for action, expected in [('undo', manual['blocks']), ('redo', applied['blocks'])]:
            response = self.request('post', 'changes', json={
                'expected_project_version': current['project_version'], 'expected_revision': current['revision'],
                'expected_draft_revision': current['draft_revision'], 'action': action,
                'operation_key': 'partial-history-' + action})
            self.assertEqual(response.status_code, 200, response.text)
            current = response.json()
            self.assertEqual(current['blocks'], expected)
        self.assertEqual(self.fixture._table_count('project_versions'), 0)

    def test_protected_block_stays_out_of_assistant_targets_but_remains_manually_editable(self):
        before = self.generate()
        protected_id = before["blocks"][1]["block_id"]
        response = self.request("post", "protection", json={
            "expected_project_version": before["project_version"], "expected_revision": before["revision"],
            "protected_block_ids": [protected_id], "operation_key": "protect-second-block"})
        self.assertEqual(response.status_code, 200, response.text)
        protected = response.json()
        self.assertEqual(self.request().json()["protected_block_ids"], [protected_id])
        calls = []

        def editor(prompt, model, limit):
            data = json.loads(prompt.split("\nDATA:", 1)[1])
            calls.append(data)
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{"shot_id": item["shot_id"], "field": item["field"], "value": "现场掌声自然渐入"}
                    for item in data["targets"]]}, ensure_ascii=False)

        self.fixture.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=editor, provider="anonymous-editor", model="anonymous-model")
        response = self.request("post", "revisions", json={
            "expected_project_version": protected["project_version"], "expected_revision": protected["revision"],
            "instruction": "修改整稿声音，让掌声自然衔接音乐", "selected_block_ids": [b["block_id"] for b in before["blocks"]],
            "operation_key": "protected-assistant-operation"})
        self.assertEqual(response.status_code, 200, response.text)
        proposal = response.json()
        self.assertEqual({target["shot_id"] for target in calls[0]["targets"]},
            {before["blocks"][0]["block_id"], before["blocks"][2]["block_id"]})
        self.assertEqual({target["field"] for target in calls[0]["targets"]}, {"sound"})
        response = self.request("post", "revisions/confirm", json={
            "task_id": proposal["task"]["task_id"], "proposal_digest": proposal["proposal"]["digest"]})
        self.assertEqual(response.status_code, 200, response.text)
        current = response.json()
        self.assertEqual(current["blocks"][1], before["blocks"][1])
        edited = self.request("post", "changes", json={"expected_project_version": current["project_version"],
            "expected_revision": current["revision"], "expected_draft_revision": current["draft_revision"],
            "action": "patch", "block_id": protected_id, "field": "声音", "value": "用户保留的配乐",
            "operation_key": "manual-protected-operation"})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()["blocks"][1]["fields"]["声音"], "用户保留的配乐")
        self.assertEqual(self.fixture._table_count("project_versions"), 0)

    def test_protection_added_after_proposal_rejects_stale_confirmation_without_draft_write(self):
        proposal = self.propose_sound(self.generate())
        response = self.request("post", "protection", json={
            "expected_project_version": proposal["project_version"], "expected_revision": proposal["revision"],
            "protected_block_ids": [proposal["blocks"][1]["block_id"]], "operation_key": "protect-after-proposal"})
        self.assertEqual(response.status_code, 200, response.text)
        protected = response.json()
        response = self.request("post", "revisions/confirm", json={
            "task_id": proposal["task"]["task_id"], "proposal_digest": proposal["proposal"]["digest"]})
        self.assertEqual(response.status_code, 409, response.text)
        current = self.request().json()
        self.assertEqual(current["blocks"], protected["blocks"])
        self.assertEqual(current["draft_revision"], protected["draft_revision"])

    def test_editor_output_outside_selected_scope_rejects_entire_suggestion(self):
        before = self.generate()

        def editor(prompt, model, limit):
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{"shot_id": before["blocks"][index]["block_id"], "field": "sound",
                    "value": "模型越过许可范围的声音"} for index in (1, 0)]}, ensure_ascii=False)

        self.fixture.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=editor, provider="anonymous-editor", model="anonymous-model")
        response = self.request("post", "revisions", json={
            "expected_project_version": before["project_version"], "expected_revision": before["revision"],
            "instruction": "让声音更自然", "selected_block_ids": [before["blocks"][1]["block_id"]],
            "operation_key": "out-of-scope-model-operation"})
        self.assertEqual(response.status_code, 200, response.text)
        current = response.json()
        self.assertEqual(current["task"]["status"], "failed")
        self.assertIsNone(current["proposal"])
        self.assertEqual(current["blocks"], before["blocks"])
        self.assertEqual(current["draft_revision"], before["draft_revision"])
        self.assertEqual(self.fixture._table_count("project_versions"), 0)

    def test_concurrent_confirmations_return_one_applied_revision(self):
        proposal = self.propose_sound(self.generate())
        first_writing, release = threading.Event(), threading.Event()
        original_apply = VersionReviewService.apply_agent_change_set
        writes = []

        def gated_apply(core, *args, **kwargs):
            writes.append(args)
            first_writing.set()
            if not release.wait(10):
                raise RuntimeError("fixture confirmation release timed out")
            return original_apply(core, *args, **kwargs)

        body = {"task_id": proposal["task"]["task_id"], "proposal_digest": proposal["proposal"]["digest"]}
        client = TestClient(self.fixture.client.app, raise_server_exceptions=False)
        self.addCleanup(client.close)

        def confirm():
            return client.post(self.path + "/revisions/confirm", params=self.params,
                headers=self.fixture.headers, json=body)

        with patch.object(VersionReviewService, "apply_agent_change_set", gated_apply), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(confirm)
            try:
                self.assertTrue(first_writing.wait(5))
                second = pool.submit(confirm)
                second_response = second.result(5)
            finally:
                release.set()
            responses = [first.result(10), second_response]
        self.assertEqual(responses[0].status_code, 200, responses[0].text)
        self.assertIn(responses[1].status_code, (200, 409), responses[1].text)
        if responses[1].status_code == 409:
            self.assertIsInstance(responses[1].json()["detail"]["message"], str)
            self.assertNotIn(str(self.fixture.store.database_path), responses[1].text)
        replay = self.request("post", "revisions/confirm", json=body)
        self.assertEqual(replay.status_code, 200, replay.text)
        self.assertEqual(len(writes), 1)
        current = self.request().json()
        self.assertEqual(current["draft_revision"], proposal["draft_revision"] + 1)
        self.assertEqual(current["task"]["status"], "completed")

    def test_edit_while_provider_is_streaming_preserves_user_text_when_completion_arrives(self):
        before = self.generate()
        first, release = threading.Event(), threading.Event()

        def stream(prompt, model, limit, emit):
            self.calls += 1
            prefix = '{"blocks":[' + json.dumps(BLOCKS[0], ensure_ascii=False)
            emit(prefix)
            first.set()
            if not release.wait(10):
                raise RuntimeError("fixture stream release timed out")
            tail = "," + ",".join(json.dumps(block, ensure_ascii=False) for block in BLOCKS[1:]) + "]}"
            emit(tail)
            return prefix + tail

        self.fixture.generation.creative_stream_provider = stream
        response = self.request("post", "generate", json={
            "expected_project_version": before["project_version"], "expected_revision": before["revision"],
            "instruction": "重新写三段初稿", "operation_key": "stream-edit-new-attempt"})
        self.assertEqual(response.status_code, 200, response.text)
        try:
            self.assertTrue(first.wait(5))
            current = self.request().json()
            edited = self.request("post", "changes", json={"expected_project_version": current["project_version"],
                "expected_revision": current["revision"], "expected_draft_revision": current["draft_revision"],
                "action": "patch", "block_id": current["blocks"][1]["block_id"], "field": "声音",
                "value": "生成过程中用户保留的音乐", "operation_key": "edit-during-streaming"})
            self.assertEqual(edited.status_code, 200, edited.text)
        finally:
            release.set()
        for _ in range(150):
            current = self.request().json()
            if current["task"]["status"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(.02)
        self.assertEqual(current["task"]["status"], "failed", current)
        self.assertEqual(current["blocks"], edited.json()["blocks"])
        self.assertEqual(current["draft_revision"], edited.json()["draft_revision"])
        self.assertEqual(self.calls, 2)

    def _answer(self, current, field, value, key):
        return self.request("post", "answers", json={
            "expected_project_version": current["project_version"], "expected_revision": current["revision"],
            "field": field, "value": value, "operation_key": key})

    def test_continuous_answers_merge_unaccepted_values_and_reject_old_proposal(self):
        current = self.generate()
        observed = []

        def analysis(**kwargs):
            updates = json.loads(kwargs["context"]["recent_conversation"][0]["content"].split("：", 1)[1])
            observed.append(updates)
            values = {item["field"]: item["value"] for item in updates}
            fields = {"脚本类型": "visual", "目标时长": "duration_text", "风格调性": "sound"}
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": fields[field], "value": value}
                for block in kwargs["blocks"] for field, value in values.items()]}

        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            first = self._answer(current, "脚本类型", "年会", "continuous-year-meeting").json()
            second = self._answer(first, "目标时长", "60秒", "continuous-duration").json()
            current = self._answer(second, "风格调性", "高级", "continuous-tone").json()
        self.assertEqual({item["field"]: item["value"] for item in observed[-1]},
            {"脚本类型": "年会", "目标时长": "60秒", "风格调性": "高级"})
        self.assertEqual(len(current["unimplemented_answers"]), 3)
        self.assertEqual(current["superseded_proposals"][first["task"]["task_id"]], second["task"]["task_id"])
        stale = self.request("post", "revisions/confirm", json={"task_id": first["task"]["task_id"],
            "proposal_digest": first["proposal"]["digest"]})
        self.assertEqual(stale.status_code, 409, stale.text)
        accepted = self.request("post", "revisions/confirm", json={"task_id": current["task"]["task_id"],
            "proposal_digest": current["proposal"]["digest"],
            "retained_block_ids": [current["blocks"][0]["block_id"]]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        saved = self.request().json()
        self.assertEqual(saved["unimplemented_answers"], [])
        self.assertEqual(saved["blocks"][0]["fields"]["画面描述"], "年会")
        self.assertEqual(saved["blocks"][1]["fields"], BLOCKS[1])
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            later = self._answer(saved, "目标时长", "90秒", "continuous-new-duration").json()
        self.assertEqual(observed[-1], [{"field": "目标时长", "value": "90秒"}])
        self.assertEqual(len(later["unimplemented_answers"]), 1)

    def test_failed_merged_preview_preserves_answers_and_manual_fields_on_continue(self):
        from mvp_v2.workspace_store import WorkspaceServiceError
        current = self.generate()
        edited = self.request("post", "changes", json={
            "expected_project_version": current["project_version"], "expected_revision": current["revision"],
            "expected_draft_revision": current["draft_revision"], "action": "patch",
            "block_id": current["blocks"][0]["block_id"], "field": "声音", "value": "用户指定音乐",
            "operation_key": "manual-before-input-merge"}).json()
        with patch("mvp_v2.creative_revision.analyze_creative_blocks",
                   side_effect=WorkspaceServiceError("CREATIVE_REVISION_INTERRUPTED", "模型暂不可用")):
            self._answer(edited, "风格调性", "高级", "failed-tone-answer")
        failed = self.request().json()
        self.assertEqual(failed["unimplemented_answers"], [{"field": "风格调性", "value": "高级"}])
        self.assertTrue(failed["answers_need_preview"])
        count = self.fixture._table_count("brief_versions")
        def analysis(**kwargs):
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": "sound", "value": "克制高级音乐"}
                for block in kwargs["blocks"]]}
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            recovered = self.request("post", "answers/continue", json={}).json()
        self.assertEqual(self.fixture._table_count("brief_versions"), count)
        self.assertEqual({change["block_id"] for change in recovered["proposal"]["changes"]},
            {block["block_id"] for block in edited["blocks"][1:]})
        accepted = self.request("post", "revisions/confirm", json={"task_id": recovered["task"]["task_id"],
            "proposal_digest": recovered["proposal"]["digest"]})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["blocks"][0]["fields"]["声音"], "用户指定音乐")

    def test_legacy_orphan_answer_is_recovered_from_immutable_heads(self):
        from mvp_v2.creative_generation import repository
        from mvp_v2.formal_brief_domain import ArtifactPayload
        current = self.generate()
        def analysis(**kwargs):
            updates = json.loads(kwargs["context"]["recent_conversation"][0]["content"].split("：", 1)[1])
            fields = {"脚本类型": "visual", "目标时长": "duration_text", "风格调性": "sound"}
            # Emulate the legacy editor which acted only on this round's answer.
            item = updates[-1]
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": fields[item["field"]], "value": item["value"]}
                for block in kwargs["blocks"]]}
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            first = self._answer(current, "脚本类型", "年会", "legacy-year-meeting").json()
            second = self._answer(first, "目标时长", "60秒", "legacy-duration").json()
            current = self.request("post", "revisions/confirm", json={"task_id": second["task"]["task_id"],
                "proposal_digest": second["proposal"]["digest"]}).json()
            third = self._answer(current, "风格调性", "高级", "legacy-tone").json()
            self.request("post", "revisions/confirm", json={"task_id": third["task"]["task_id"],
                "proposal_digest": third["proposal"]["digest"]})
        scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.project["project_id"]}
        repo = repository(self.fixture.generation, self.fixture.token, **scope)
        state = repo.read()
        bodies, body = [], state["body"]
        while body:
            bodies.append(body)
            ref = body.get("previous_payload_ref")
            body = json.loads(repo.artifacts.read(ArtifactPayload(ref, ref.rsplit("/", 1)[-1]))) if ref else None
        ref = None
        for body in reversed(bodies):
            for key in ("answer_tasks", "answer_tasks_initialized", "superseded_proposals"):
                body.pop(key, None)
            body["previous_payload_ref"] = ref
            payload = repo.artifacts.write(json.dumps(body, ensure_ascii=False).encode())
            ref = payload.ref
        # Only this anonymous test database is reset to the old payload format.
        with self.fixture.store.transaction() as connection:
            connection.execute("UPDATE creative_draft_heads SET payload_ref=?, payload_sha256=? WHERE project_id=?",
                (payload.ref, payload.sha256, self.project["project_id"]))
        restored = self.request().json()
        self.assertEqual(restored["unimplemented_answers"], [{"field": "脚本类型", "value": "年会"}])
        self.assertTrue(restored["answers_need_preview"])
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            response = self.request("post", "answers/continue", json={})
        self.assertEqual(response.status_code, 200, response.text)
        recovered = response.json()
        self.assertEqual({change["after"] for change in recovered["proposal"]["changes"]}, {"年会"})
        self.assertEqual(recovered["blocks"], restored["blocks"])
        self.assertNotEqual(recovered["task"]["task_id"], first["task"]["task_id"])

    def test_decline_is_durable_idempotent_and_not_silently_proposed_again(self):
        current = self.generate()
        calls = []
        def analysis(**kwargs):
            updates = json.loads(kwargs["context"]["recent_conversation"][0]["content"].split("：", 1)[1])
            calls.append(updates)
            kind = next((item["value"] for item in updates if item["field"] == "脚本类型"), "年会")
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": field, "value": value}
                for block in kwargs["blocks"] for field, value in (("visual", kind), ("duration_text", "20秒"))]}
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            proposal = self._answer(current, "脚本类型", "年会", "decline-year-meeting").json()
        body = {"task_id": proposal["task"]["task_id"], "proposal_digest": proposal["proposal"]["digest"],
            "retained_block_ids": []}
        response = self.request("post", "revisions/confirm", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        cancelled = response.json()
        self.assertEqual(cancelled["task"]["status"], "cancelled")
        self.assertEqual(cancelled["blocks"], current["blocks"])
        self.assertEqual(cancelled["draft_revision"], current["draft_revision"])
        self.assertFalse(cancelled["unimplemented_answers"])
        self.assertEqual(self.request("post", "revisions/confirm", json=body).json(), cancelled)
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            next_result = self._answer(cancelled, "风格调性", "高级", "after-declined-tone").json()
        self.assertEqual(calls[-1], [{"field": "风格调性", "value": "高级"}])
        self.assertIsNone(next_result["proposal"])
        self.assertEqual(next_result["blocks"], current["blocks"])
        self.assertTrue(next_result["answers_need_preview"])
        def sound_only(**kwargs):
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": "sound", "value": "高级的环境声与音乐"}
                for block in kwargs["blocks"]]}
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=sound_only) as retry:
            response = self.request("post", "answers/continue", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(retry.call_count, 1)
        next_result = response.json()
        self.assertEqual({change["field"] for change in next_result["proposal"]["changes"]}, {"声音"})
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            reopened = self._answer(next_result, "脚本类型", "音乐节", "explicit-new-kind-after-decline").json()
        self.assertEqual({item["after"] for item in reopened["proposal"]["changes"] if item["field"] == "画面描述"}, {"音乐节"})
        self.assertNotIn({"field": "脚本类型", "value": "年会"}, calls[-1])

    def test_undo_redo_keeps_restored_manual_content_out_of_automatic_suggestions(self):
        from mvp_v2.creative_generation import repository
        current = self.generate()
        block_id = current["blocks"][0]["block_id"]
        def edit(current, action, key, **extra):
            response = self.request("post", "changes", json={
                "expected_project_version": current["project_version"], "expected_revision": current["revision"],
                "expected_draft_revision": current["draft_revision"], "action": action,
                "operation_key": key, **extra})
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()
        current = edit(current, "patch", "manual-first-music", block_id=block_id, field="声音", value="用户音乐甲")
        current = edit(current, "patch", "manual-second-music", block_id=block_id, field="声音", value="用户音乐乙")
        current = edit(current, "undo", "manual-undo-music")
        self.assertEqual(current["blocks"][0]["fields"]["声音"], "用户音乐甲")
        current = edit(current, "redo", "manual-redo-music")
        self.assertEqual(current["blocks"][0]["fields"]["声音"], "用户音乐乙")
        def analysis(**kwargs):
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": "sound", "value": "模型音乐"} for block in kwargs["blocks"]]}
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            proposed = self._answer(current, "风格调性", "高级", "manual-redo-tone").json()
        self.assertNotIn(block_id, {item["block_id"] for item in proposed["proposal"]["changes"]})
        proposed = self.request("post", "revisions/confirm", json={"task_id": proposed["task"]["task_id"],
            "proposal_digest": proposed["proposal"]["digest"], "retained_block_ids": []}).json()
        response = self.request("post", "generate", json={
            "expected_project_version": proposed["project_version"], "expected_revision": proposed["revision"],
            "instruction": "重新起草三段", "operation_key": "manual-fresh-generation"})
        self.assertEqual(response.status_code, 200, response.text)
        for _ in range(150):
            current = self.request().json()
            if current["task"]["status"] == "completed":
                break
            time.sleep(.02)
        scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.project["project_id"]}
        saved = repository(self.fixture.generation, self.fixture.token, **scope).read()
        self.assertEqual(saved["body"]["manual_field_values"], {})
        self.assertNotIn(block_id, {item["block_id"] for item in current["blocks"]})

    def test_later_manual_brief_choice_survives_an_unrelated_answer(self):
        from mvp_v2.creative_revision import effective_brief_values
        from mvp_v2.formal_brief_input_adapter import UploadedDocument
        from io import BytesIO
        current = self.generate()
        scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.project["project_id"]}
        self.fixture.adapter.import_inline_answer(self.fixture.token, **scope,
            field="脚本类型", value="发布会", expected_project_version=current["project_version"])
        current = self.request().json()
        observed = []
        def analysis(**kwargs):
            observed.append(json.loads(kwargs["context"]["recent_conversation"][0]["content"].split("：", 1)[1]))
            return {"type": "rewrite_operations", "operations": [
                {"shot_id": block["shot_id"], "field": "duration_text", "value": "20秒"}
                for block in kwargs["blocks"]]}
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            current = self._answer(current, "脚本类型", "年会", "authority-original-annual").json()
        self.fixture.adapter.import_uploads(self.fixture.token, **scope,
            uploads=[UploadedDocument("用户重新选择类型.txt", BytesIO("脚本类型：发布会\n补充说明：用户重新核对类型".encode()))],
            expected_project_version=current["project_version"])
        raw = self.fixture.adapter.service.read_current_formal(self.fixture.token, **scope)
        conflict = next(row for row in raw["brief"]["conflicts"] if row["field_path"] == "脚本类型" and row["resolution_status"] == "unresolved")
        selected = next(row["fact_id"] for row in raw["brief"]["facts"]
            if row["fact_id"] in conflict["fact_ids"] and json.loads(row["value_json"]) == "发布会")
        self.fixture.adapter.resolve_conflict(self.fixture.token, **scope, brief_version_id=raw["brief_version_id"],
            conflict_id=conflict["conflict_id"], selected_fact_id=selected,
            expected_project_version=self.request().json()["project_version"])
        current = self.request().json()
        self.assertFalse(current["unimplemented_answers"])
        with patch("mvp_v2.creative_revision.analyze_creative_blocks", side_effect=analysis):
            response = self._answer(current, "目标时长", "60秒", "authority-later-duration")
        self.assertEqual(response.status_code, 200, response.text)
        raw = self.fixture.adapter.service.read_current_formal(self.fixture.token, **scope)
        self.assertEqual(raw["unresolved_conflict_count"], 0)
        self.assertEqual(effective_brief_values(raw)["脚本类型"], "发布会")
        self.assertEqual(observed[-1], [{"field": "目标时长", "value": "60秒"}])

    def test_answer_during_stream_prepares_related_changes_until_explicit_adoption(self):
        first, release = threading.Event(), threading.Event()

        def stream(prompt, model, limit, emit):
            self.calls += 1
            prefix = '{"blocks":[' + json.dumps(BLOCKS[0], ensure_ascii=False)
            emit(prefix)
            first.set()
            release.wait(10)
            tail = "," + ",".join(json.dumps(block, ensure_ascii=False) for block in BLOCKS[1:]) + "]}"
            emit(tail)
            return prefix + tail

        editor_calls = []

        def editor(prompt, model, limit):
            data = json.loads(prompt.split("\nDATA:", 1)[1])
            editor_calls.append(data)
            values = {"visual": "年会现场的团队互动", "sound": "高级而克制的现场音乐", "duration_text": "20秒"}
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": [{"shot_id": item["shot_id"], "field": item["field"], "value": values[item["field"]]}
                    for item in data["targets"] if item["field"] in values]}, ensure_ascii=False)

        self.fixture.generation.creative_stream_provider = stream
        self.fixture.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=editor, provider="anonymous", model="anonymous")
        try:
            response = self.request("post", "generate", json={"expected_project_version": 0,
                "expected_revision": 0, "instruction": "先写三段活动创意", "operation_key": "queue-test-generation"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(first.wait(5))
            current = self.request().json()
            for index, (field, value) in enumerate((("脚本类型", "年会"), ("目标时长", "60秒"), ("风格调性", "高级")), 1):
                queued = self._answer(current, field, value, f"queue-test-answer-{index}")
                self.assertEqual(queued.status_code, 200, queued.text)
                current = queued.json()
                self.assertEqual(len(current["pending_answers"]), index)
                self.assertTrue(current["task"]["is_running"])
        finally:
            release.set()
        for _ in range(150):
            current = self.request().json()
            if not current["pending_answers"] and current["task"]["status"] == "waiting_user":
                break
            time.sleep(.02)
        self.assertFalse(current["pending_answers"], current)
        self.assertEqual([block["fields"]["时长"] for block in current["blocks"]], [item['时长'] for item in BLOCKS])
        self.assertEqual(current['proposal']['revision'], current['revision'])
        applied = self.request('post', 'revisions/confirm', json={'task_id': current['task']['task_id'],
            'proposal_digest': current['proposal']['digest']})
        self.assertEqual(applied.status_code, 200, applied.text)
        current = applied.json()
        self.assertEqual([block["fields"]["时长"] for block in current["blocks"]], ["20秒"] * 3)
        self.assertEqual([block["fields"]["声音"] for block in current["blocks"]], ["高级而克制的现场音乐"] * 3)
        self.assertEqual([block["fields"]["画面描述"] for block in current["blocks"]], ["年会现场的团队互动"] * 3)
        self.assertEqual(self.calls, 1)
        self.assertEqual(len(editor_calls), 1)
        self.assertEqual(self.fixture._table_count("brief_confirmations"), 0)


if __name__ == "__main__":
    unittest.main()
