import copy
import json
import unittest

from mvp_v2.creative_revision import _align_confirmed_duration, proposal_duration_status
from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_draft_api as creative_api_tests


BUDGET_REQUEST = (
    "按已确认的需求，将整份初稿总时长调整为60秒，只调整时长；"
    "不要修改画面描述与声音，不要修改段落名称和顺序。"
)


def confirmed_brief(seconds="60秒", status="confirmed"):
    return {"confirmation_status": status, "brief_version_id": "anonymous-confirmed-duration",
        "brief": {"facts": [{"fact_id": "duration", "field_path": "目标时长",
            "value_json": json.dumps(seconds, ensure_ascii=False)}]}}


def original_shots():
    return [{"shot_id": f"block_{index + 1}", "shot_no": str(index + 1),
        "fields": {"段落": f"匿名段落{index + 1}", "画面描述": f"匿名画面{index + 1}",
            "声音": f"匿名声音{index + 1}", "时长": "3" if index < 6 else "2"}}
        for index in range(25)]


def wrong_model_changes(shots):
    return [{"block_id": shot["shot_id"], "field": "时长", "before": shot["fields"]["时长"],
        "after": "2"} for shot in shots[:4]]


def apply_changes(shots, changes):
    result = copy.deepcopy(shots)
    by_id = {shot["shot_id"]: shot for shot in result}
    for change in changes:
        by_id[change["block_id"]]["fields"][change["field"]] = change["after"]
    return result


class ConfirmedDurationAllocationTests(unittest.TestCase):
    def test_wrong_model_56_to_52_becomes_exact_60_without_rewriting_content(self):
        shots = original_shots()
        changes = wrong_model_changes(shots)
        saved = copy.deepcopy((shots, changes))
        brief = confirmed_brief()
        self.assertEqual(proposal_duration_status(shots, [], brief)["actual_seconds"], 56)
        self.assertEqual(proposal_duration_status(shots, changes, brief)["actual_seconds"], 52)
        adjusted = _align_confirmed_duration(shots, changes, brief, BUDGET_REQUEST,
            [shot["shot_id"] for shot in shots], [], {})
        status = proposal_duration_status(shots, adjusted, brief)
        self.assertEqual((status["actual_seconds"], status["status"]), (60, "matched"))
        self.assertEqual({change["field"] for change in adjusted}, {"时长"})
        result = apply_changes(shots, adjusted)
        self.assertEqual([shot["shot_id"] for shot in result], [shot["shot_id"] for shot in shots])
        for before, after in zip(shots, result):
            self.assertEqual(after["shot_no"], before["shot_no"])
            self.assertEqual({key: value for key, value in after["fields"].items() if key != "时长"},
                {key: value for key, value in before["fields"].items() if key != "时长"})
            self.assertGreaterEqual(int(after["fields"]["时长"]), 1)
        self.assertEqual((shots, changes), saved)

    def test_protected_outside_selection_and_manual_durations_remain_fixed(self):
        shots = original_shots()
        ids = [shot["shot_id"] for shot in shots]
        adjusted = _align_confirmed_duration(shots, wrong_model_changes(shots), confirmed_brief(),
            BUDGET_REQUEST, ids[:-1], [ids[0]], {ids[1]: {"时长": "3"}, ids[2]: {"声音": "匿名声音3"}})
        self.assertEqual(proposal_duration_status(shots, adjusted, confirmed_brief())["actual_seconds"], 60)
        self.assertFalse({ids[0], ids[1], ids[-1]} & {change["block_id"] for change in adjusted})
        result = apply_changes(shots, adjusted)
        for index in (0, 1, 24):
            self.assertEqual(result[index], shots[index])

    def test_ordinary_edits_or_unconfirmed_target_do_not_trigger_whole_draft_allocation(self):
        shots = original_shots()
        ids = [shot["shot_id"] for shot in shots]
        changes = wrong_model_changes(shots)
        cases = [
            ("只调整第1段时长", confirmed_brief(), changes),
            ("修改整份初稿总时长，并润色画面", confirmed_brief(), changes),
            (BUDGET_REQUEST, confirmed_brief(status="pending"), changes),
            (BUDGET_REQUEST, confirmed_brief("约60秒"), changes),
            (BUDGET_REQUEST, confirmed_brief(), changes + [{"block_id": ids[0], "field": "声音",
                "before": shots[0]["fields"]["声音"], "after": "新声音"}]),
        ]
        for instruction, brief, proposed in cases:
            with self.subTest(instruction=instruction, brief=brief, fields=[c["field"] for c in proposed]):
                self.assertEqual(_align_confirmed_duration(shots, proposed, brief, instruction,
                    ids, [], {}), proposed)

    def test_insufficient_fixed_budget_reports_recovery_without_changing_inputs(self):
        shots = original_shots()
        shots[0]["fields"]["时长"] = "40"
        ids = [shot["shot_id"] for shot in shots]
        changes = wrong_model_changes(shots)
        original = copy.deepcopy((shots, changes))
        with self.assertRaises(WorkspaceServiceError) as failed:
            _align_confirmed_duration(shots, changes, confirmed_brief(), BUDGET_REQUEST,
                ids, [ids[1]], {ids[0]: {"时长": "40"}})
        self.assertEqual(failed.exception.code, "INVALID_INPUT")
        self.assertRegex(failed.exception.message, "调整目标|解除.*保护")
        self.assertEqual((shots, changes), original)


class DurationAllocationApiTests(unittest.TestCase):
    def setUp(self):
        self.base = creative_api_tests.CreativeDraftApiTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.scope, self.original = self.base._confirmed_draft_without_duration()
        self.current = self.base._update_confirmed_requirement(self.scope, "目标时长", "60s")
        self.editor_calls = []
        self.model_ids = [block["block_id"] for block in self.original["blocks"][:4]]

        def editor(prompt, _model, _limit):
            data = json.loads(prompt.split("\nDATA:", 1)[1])
            self.editor_calls.append(data)
            operations = [{"shot_id": target["shot_id"], "field": target["field"], "value": "2"}
                for target in data["targets"] if target["shot_id"] in self.model_ids]
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1",
                "operations": operations}, ensure_ascii=False)

        self.base.fixture.agent.script_revision.assistant_editor_policy = FormalScriptAssistantEditorPolicy(
            invoke=editor, provider="anonymous-duration-editor", model="anonymous-duration-model")

    def request(self, method="get", action="", **kwargs):
        return self.base.request(method, action, **kwargs)

    def reread(self):
        self.base.fixture.generation._creative_read_cache.clear()
        response = self.request()
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def propose(self, current, key):
        return self.request("post", "revisions", json={
            "expected_project_version": current["project_version"], "expected_revision": current["revision"],
            "instruction": BUDGET_REQUEST, "selected_block_ids": [b["block_id"] for b in current["blocks"]],
            "operation_key": key})

    def preview(self, current, key):
        response = self.propose(current, key)
        self.assertEqual(response.status_code, 200, response.text)
        proposed = response.json()
        self.assertEqual(proposed["task"]["status"], "waiting_user", proposed)
        self.assertEqual(proposed["proposal"]["duration_status"]["actual_seconds"], 60)
        self.assertEqual(proposed["proposal"]["duration_status"]["status"], "matched")
        self.assertEqual({change["field"] for change in proposed["proposal"]["changes"]}, {"时长"})
        self.assertEqual({target["field"] for target in self.editor_calls[-1]["targets"]}, {"duration_text"})
        self.assertEqual(proposed["blocks"], current["blocks"])
        return proposed

    def adopt(self, proposed, retained=None):
        body = {"task_id": proposed["task"]["task_id"], "proposal_digest": proposed["proposal"]["digest"]}
        if retained is not None:
            body["retained_block_ids"] = retained
        response = self.request("post", "revisions/confirm", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.reread()["blocks"], response.json()["blocks"])
        return response.json()

    def manual_duration(self, current, index, value, key):
        response = self.request("post", "changes", json={
            "expected_project_version": current["project_version"], "expected_revision": current["revision"],
            "expected_draft_revision": current["draft_revision"], "action": "patch",
            "block_id": current["blocks"][index]["block_id"], "field": "时长", "value": str(value),
            "operation_key": key})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def assert_content_preserved(self, current):
        self.assertEqual([b["block_id"] for b in current["blocks"]],
            [b["block_id"] for b in self.original["blocks"]])
        for before, after in zip(self.original["blocks"], current["blocks"]):
            for field in ("段落", "画面描述", "声音"):
                self.assertEqual(after["fields"][field], before["fields"][field])

    def test_real_api_partial_adoption_stays_pending_then_full_adoption_matches(self):
        proposed = self.preview(self.current, "duration-allocation-first")
        changed = proposed["proposal"]["changes"]
        self.assertGreater(len(changed), 1)
        partial = self.adopt(proposed, [changed[0]["block_id"]])
        self.assertEqual(partial["duration_status"]["status"], "mismatch")
        self.assertTrue(partial["answers_need_preview"])
        self.assertEqual(partial["unimplemented_answers"], [{"field": "目标时长", "value": "60s"}])
        self.assert_content_preserved(partial)
        final = self.adopt(self.preview(partial, "duration-allocation-recovery"))
        self.assertEqual((final["duration_status"]["actual_seconds"], final["duration_status"]["status"]),
            (60, "matched"))
        self.assertFalse(final["answers_need_preview"])
        self.assertEqual(final["unimplemented_answers"], [])
        self.assert_content_preserved(final)

    def test_insufficient_manual_and_protected_budget_preserves_draft_and_recovers(self):
        current = self.manual_duration(self.current, 0, 40, "duration-manual-over-capacity")
        protected_id = current["blocks"][1]["block_id"]
        response = self.request("post", "protection", json={
            "expected_project_version": current["project_version"], "expected_revision": current["revision"],
            "protected_block_ids": [protected_id], "operation_key": "duration-protect-over-capacity"})
        self.assertEqual(response.status_code, 200, response.text)
        before = response.json()
        failed = self.propose(before, "duration-over-capacity-proposal")
        self.assertIn(failed.status_code, (400, 409, 422), failed.text)
        self.assertRegex(failed.text, "调整目标|解除.*保护")
        current = self.reread()
        self.assertEqual(current["blocks"], before["blocks"])
        self.assertTrue(current["answers_need_preview"])
        current = self.manual_duration(current, 0, 4, "duration-reduce-manual-budget")
        proposed = self.preview(current, "duration-capacity-recovered")
        fixed_ids = {current["blocks"][0]["block_id"], protected_id}
        self.assertFalse(fixed_ids & {change["block_id"] for change in proposed["proposal"]["changes"]})
        final = self.adopt(proposed)
        self.assertEqual(final["duration_status"]["status"], "matched")
        self.assertEqual(final["blocks"][0], current["blocks"][0])
        self.assertEqual(final["blocks"][1], current["blocks"][1])
        self.assert_content_preserved(final)

    def test_manual_durations_remain_fixed_when_no_requirement_is_pending(self):
        current = self.adopt(self.preview(self.current, "duration-initially-matched"))
        first = int(current["blocks"][0]["fields"]["时长"])
        second = int(current["blocks"][1]["fields"]["时长"])
        current = self.manual_duration(current, 0, first + 1, "duration-manual-longer")
        current = self.manual_duration(current, 1, second - 1, "duration-manual-shorter")
        self.assertEqual(current["duration_status"]["status"], "matched")
        self.assertFalse(current["answers_need_preview"])
        self.model_ids = [block["block_id"] for block in current["blocks"][:6]]
        proposed = self.preview(current, "duration-rebalance-without-pending-input")
        manual_ids = {block["block_id"] for block in current["blocks"][:2]}
        self.assertFalse(manual_ids & {change["block_id"] for change in proposed["proposal"]["changes"]})
        final = self.adopt(proposed)
        self.assertEqual(final["blocks"][:2], current["blocks"][:2])
        self.assertEqual(final["duration_status"]["status"], "matched")
        self.assert_content_preserved(final)

    def test_duration_only_proposal_leaves_pending_style_answer_unimplemented(self):
        from mvp_v2.creative_generation import repository
        from mvp_v2.creative_revision import outstanding_answers

        current = self.base._update_confirmed_requirement(self.scope, "风格调性", "沉稳而克制")
        self.assertEqual({item["field"] for item in current["unimplemented_answers"]}, {"目标时长", "风格调性"})
        fixture = self.base.fixture
        repo = repository(fixture.generation, fixture.token, **self.scope)
        brief = fixture.adapter.service.read_current_formal(fixture.token, **self.scope)
        outstanding, _ = outstanding_answers(repo, repo.read()["body"], fixture.generation.services.tasks,
            fixture.token, brief, fixture.adapter.service)
        duration_ids = {item["answer_id"] for item in outstanding if item["field"] == "目标时长"}
        style_ids = {item["answer_id"] for item in outstanding if item["field"] == "风格调性"}
        self.assertTrue(duration_ids)
        self.assertTrue(style_ids)

        proposed = self.preview(current, "duration-only-with-style-pending")
        self.assertFalse(proposed["proposal"]["stale"])
        task_id = proposed["task"]["task_id"]
        events = fixture.generation.services.tasks.list_task_events(fixture.token, task_id)
        subject = next(event["details"]["creative_subject"] for event in events
            if event.get("details", {}).get("creative_subject"))
        self.assertEqual(set(subject["answer_ids"]), duration_ids)
        self.assertFalse(style_ids & set(repo.read()["body"]["answer_tasks"][task_id]))

        final = self.adopt(proposed)
        self.assertEqual(final["duration_status"]["status"], "matched")
        self.assertTrue(final["answers_need_preview"])
        self.assertEqual(final["unimplemented_answers"], [{"field": "风格调性", "value": "沉稳而克制"}])
        self.assertEqual(self.reread()["unimplemented_answers"], final["unimplemented_answers"])
        self.assert_content_preserved(final)

    def test_new_brief_field_marks_duration_proposal_stale_without_another_model_call(self):
        current = self.base._update_confirmed_requirement(self.scope, "风格调性", "沉稳而克制")
        proposed = self.preview(current, "duration-proposal-before-new-brief")
        self.assertTrue(proposed["answers_need_preview"])
        self.assertFalse(proposed["proposal"]["stale"])
        fixture = self.base.fixture
        response = fixture.client.post(f"/api/formal/projects/{self.scope['project_id']}/brief/field",
            params=self.base.params, headers=fixture.headers, json={
                "brief_version_id": proposed["understanding"]["brief_key"],
                "expected_project_version": proposed["project_version"],
                "field_label": "视频目标", "value": "突出观众体验与现场互动"})
        self.assertEqual(response.status_code, 200, response.text)
        read = self.request()
        self.assertEqual(read.status_code, 200, read.text)
        changed = read.json()
        self.assertEqual(changed["task"]["task_id"], proposed["task"]["task_id"])
        self.assertEqual(changed["proposal"]["digest"], proposed["proposal"]["digest"])
        self.assertTrue(changed["proposal"]["stale"])
        self.assertTrue(self.reread()["proposal"]["stale"])
        self.assertEqual(changed["blocks"], proposed["blocks"])
        self.assertEqual(len(self.editor_calls), 1)


if __name__ == "__main__":
    unittest.main()
