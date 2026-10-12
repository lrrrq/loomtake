import json
import threading
import unittest
from unittest.mock import patch

from mvp_v2.creative_generation import CreativeBlockStream, repository
from mvp_v2.formal_generation_service import FormalTextProviderPolicy
from mvp_v2.text_providers import TextProviderError
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_generation_service as formal_tests


BLOCKS = [{"段落": name, "画面描述": f"{name}的画面", "声音": "环境声", "时长": "建议 5 秒"}
    for name in ("开场", "核心展示", "结尾")]


class CreativeGenerationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = formal_tests.FormalGenerationServiceTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.calls = []
        self.service = self.fixture._service(FormalTextProviderPolicy(
            writer=lambda *args: self.fail("legacy writer must not run"),
            editor=lambda *args: self.fail("legacy editor must not run"),
            writer_provider="anonymous", writer_model="anonymous-model",
            editor_provider="anonymous", editor_model="anonymous-model"))
        self.service.creative_stream_provider = self.stream
        self.scope = {"workspace_id": self.fixture.workspace_id, "project_id": self.fixture.project["project_id"]}

    def stream(self, prompt, model, tokens, emit):
        self.calls.append(prompt)
        text = json.dumps({"blocks": BLOCKS}, ensure_ascii=False)
        for offset in range(0, len(text), 11):
            emit(text[offset:offset + 11])
        return text

    def start(self, key="creative-operation-1", instruction="写一个活动短片", **changes):
        current = self.service.read_creative_draft(self.fixture.token, **self.scope)
        return self.service.start_creative_draft(self.fixture.token, **self.scope,
            **{"expected_project_version": current["project_version"], "expected_revision": current["revision"],
                "instruction": instruction, "operation_key": key, "start_background": False, **changes})

    def execute(self, started):
        self.service.execute_creative_draft(self.fixture.token, **self.scope, task_id=started["task"]["task_id"])

    def test_no_formal_version_true_incremental_blocks_and_restart(self):
        started = self.start()
        first = threading.Event()
        release = threading.Event()

        def gated(prompt, model, tokens, emit):
            self.calls.append(prompt)
            prefix = '{"blocks":[' + json.dumps(BLOCKS[0], ensure_ascii=False)
            emit(prefix)
            first.set()
            release.wait(5)
            tail = "," + ",".join(json.dumps(block, ensure_ascii=False) for block in BLOCKS[1:]) + "]}"
            emit(tail)
            return prefix + tail

        self.service.creative_stream_provider = gated
        thread = threading.Thread(target=self.execute, args=(started,))
        thread.start()
        try:
            self.assertTrue(first.wait(5))
            partial = self.service.read_creative_draft(self.fixture.token, **self.scope)
            self.assertEqual(len(partial["stream_blocks"]), 1)
            self.assertEqual(partial["blocks"], [])
            self.assertEqual(partial["task"]["status"], "executing")
        finally:
            release.set()
            thread.join(10)
        current = self.service.read_creative_draft(self.fixture.token, **self.scope)
        self.assertEqual(current["task"]["status"], "completed")
        self.assertEqual(len(current["blocks"]), 3)
        continuation = self.service.read_creative_draft(self.fixture.token, **self.scope,
            since_task_id=current["task"]["task_id"], after_sequence=partial["event_sequence"])
        self.assertFalse(continuation["stream_reset"])
        self.assertEqual(len(continuation["stream_blocks"]), 2)
        self.assertEqual(continuation["event_sequence"], current["event_sequence"])
        restarted = self.service.read_creative_draft(self.fixture.token, **self.scope,
            since_task_id="old-task", after_sequence=999)
        self.assertTrue(restarted["stream_reset"])
        self.assertEqual(len(restarted["stream_blocks"]), 3)
        self.assertEqual(current["stream_blocks"][0]["block_id"], current["blocks"][0]["block_id"])
        self.assertNotIn("artifact://", json.dumps(current))
        restored = self.fixture._service()
        self.assertEqual(restored.read_creative_draft(self.fixture.token, **self.scope), current)
        with self.fixture.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM project_versions").fetchone()[0], 0)

    def test_disconnect_keeps_previous_complete_draft_and_partial_attempt(self):
        first = self.start()
        self.execute(first)
        before = self.service.read_creative_draft(self.fixture.token, **self.scope)

        def interrupted(prompt, model, tokens, emit):
            emit('{"blocks":[' + json.dumps(BLOCKS[0], ensure_ascii=False))
            raise TextProviderError("fixture disconnected")

        self.service.creative_stream_provider = interrupted
        second = self.start(key="creative-operation-2")
        self.execute(second)
        current = self.service.read_creative_draft(self.fixture.token, **self.scope)
        self.assertEqual(current["task"]["status"], "failed")
        self.assertEqual(current["blocks"], before["blocks"])
        self.assertEqual(len(current["stream_blocks"]), 1)

    def test_duplicate_request_and_execution_do_not_call_provider_twice(self):
        started = self.start()
        repeated = self.start(expected_project_version=2, expected_revision=0)
        self.assertEqual(repeated["task"], started["task"])
        self.execute(started)
        self.execute(started)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(WorkspaceServiceError):
            self.start(instruction="同一标识不同内容", expected_project_version=2, expected_revision=0)

    def test_provider_gets_selected_facts_and_persisted_basis_before_writing(self):
        started = self.start()
        original = self.service.services.briefs.read_current_formal
        def with_conflict(*args, **kwargs):
            value = original(*args, **kwargs)
            value['brief']['facts'].extend([
                {'fact_id': 'retired-brand', 'field_path': '品牌或客户', 'value_json': '"已弃用品牌XYZ"', 'certainty': 'confirmed'},
                {'fact_id': 'selected-brand', 'field_path': '品牌或客户', 'value_json': '"当前品牌ABC"', 'certainty': 'confirmed'}])
            value['brief']['conflicts'].append({'field_path': '品牌或客户', 'fact_ids': ['retired-brand', 'selected-brand'],
                'resolved_fact_id': 'selected-brand', 'resolution_status': 'resolved'})
            return value
        def inspect(prompt, model, tokens, emit):
            state = repository(self.service, self.fixture.token, **self.scope).read()
            self.assertTrue(state['body']['generation_basis']['snapshot_sha256'])
            self.assertNotIn('已弃用品牌XYZ', prompt)
            self.assertIn('当前品牌ABC', prompt)
            self.assertIn('effective_knowledge', prompt)
            self.assertIn('creative_standard', prompt)
            return self.stream(prompt, model, tokens, emit)
        self.service.creative_stream_provider = inspect
        with patch.object(self.service.services.briefs, 'read_current_formal', side_effect=with_conflict):
            self.execute(started)
        self.assertEqual(len(self.calls), 1)
        saved = repository(self.service, self.fixture.token, **self.scope).read()
        self.assertEqual(saved['body']['generation_basis']['context']['brief']['facts'][-1]['value'], '当前品牌ABC')

    def test_late_generation_cannot_overwrite_user_edit(self):
        started = self.start()
        self.execute(started)
        next_attempt = self.start(key="creative-operation-2")
        repo = repository(self.service, self.fixture.token, **self.scope)
        state = repo.read()
        from mvp_v2.version_review_service import VersionReviewService
        with repo.transaction(expected_project_version=state["project_version"], expected_revision=state["revision"]):
            VersionReviewService(repo).apply_table_patch(state["body"]["draft"]["draft_id"],
                expected_revision=0, actor_id=repo.actor_id, shot_no="2", field="声音", value="用户保留的音乐")
        self.execute(next_attempt)
        current = self.service.read_creative_draft(self.fixture.token, **self.scope)
        self.assertEqual(current["blocks"][1]["fields"]["声音"], "用户保留的音乐")
        self.assertEqual(current["task"]["status"], "failed")
        self.assertEqual(len(self.calls), 1)

    def test_confirmed_count_and_duration_preserve_old_draft_on_bad_output_then_recover(self):
        initial = self.start()
        self.execute(initial)
        old = self.service.read_creative_draft(self.fixture.token, **self.scope)
        self.fixture._confirmed_brief(expected_project_version=old['project_version'],
            target_duration='30秒，共6镜，每镜5秒。')
        bad = self.start(key='confirmed-bad-count', use_confirmed_brief=True)
        self.execute(bad)
        failed = self.service.read_creative_draft(self.fixture.token, **self.scope)
        self.assertEqual(failed['task']['error_code'], 'CREATIVE_OUTPUT_CONSTRAINT_MISMATCH')
        self.assertEqual(failed['blocks'], old['blocks'])
        self.assertIn('6镜', failed['notice'])
        basis = repository(self.service, self.fixture.token, **self.scope).read()['body']['generation_basis']
        self.assertEqual(basis['context']['confirmed_output_requirements']['shot_count'], 6)
        self.assertEqual(basis['context']['confirmed_output_requirements']['total_seconds'], 30)
        self.assertEqual(basis['context']['previous_draft'], [])
        self.assertEqual(json.loads(self.calls[-1].split('项目上下文：', 1)[1])['previous_draft'], [])
        def six(prompt, model, limit, emit):
            text = json.dumps({'blocks': [{**BLOCKS[i % 3], '时长': '5'} for i in range(6)]}, ensure_ascii=False)
            emit(text)
            return text
        self.service.creative_stream_provider = six
        recovered = self.start(key='confirmed-six-shots', use_confirmed_brief=True)
        self.execute(recovered)
        final = self.service.read_creative_draft(self.fixture.token, **self.scope)
        self.assertEqual(final['task']['status'], 'completed')
        self.assertEqual(len(final['blocks']), 6)
        self.assertEqual(sum(int(b['fields']['时长']) for b in final['blocks']), 30)

    def test_partial_parser_does_not_emit_incomplete_or_altered_final_output(self):
        emitted = []
        parser = CreativeBlockStream(lambda index, block: emitted.append(block))
        text = json.dumps({"blocks": BLOCKS}, ensure_ascii=False)
        for character in text:
            parser.feed(character)
        self.assertEqual(emitted, BLOCKS)
        self.assertEqual(parser.complete(text), BLOCKS)
        with self.assertRaises(WorkspaceServiceError):
            parser.complete(json.dumps({"blocks": [*BLOCKS, BLOCKS[0]]}, ensure_ascii=False))

    def test_duration_projection_uses_confirmed_target_and_actual_whole_draft(self):
        from mvp_v2.creative_generation import confirmed_duration_status, _output_constraint_mismatches
        for field in ('目标时长', '视频时长'):
            brief = {'confirmation_status': 'confirmed', 'brief_version_id': 'current-confirmed',
                'brief': {'facts': [{'fact_id': 'duration', 'field_path': field, 'value_json': '"60s"'}]}}
            blocks = [{'时长': '3' if index < 6 else '2'} for index in range(25)]
            status = confirmed_duration_status(blocks, brief)
            self.assertEqual(status, {'target_seconds': 60, 'actual_seconds': 56,
                'pending_block_count': 0, 'difference_seconds': -4, 'status': 'mismatch',
                'brief_version_id': 'current-confirmed'})
            blocks[0]['时长'] = '建议7秒'
            self.assertEqual(confirmed_duration_status(blocks, brief)['status'], 'matched')
            blocks[0]['时长'] = ''
            pending = confirmed_duration_status(blocks, brief)
            self.assertEqual(pending['status'], 'pending')
            self.assertEqual(pending['pending_block_count'], 1)
            self.assertIsNone(pending['difference_seconds'])
            self.assertEqual(_output_constraint_mismatches(blocks, {'total_seconds': 60}), {'total_seconds'})
            brief['confirmation_status'] = 'pending'
            self.assertEqual(confirmed_duration_status(blocks, brief)['status'], 'unbounded')
            self.assertIsNone(confirmed_duration_status(blocks, brief)['target_seconds'])
        approximate = {'confirmation_status': 'confirmed', 'brief_version_id': 'approximate',
            'brief': {'facts': [{'fact_id': 'duration', 'field_path': '目标时长', 'value_json': '"约60秒"'}]}}
        self.assertEqual(confirmed_duration_status([{'时长': '56'}], approximate)['status'], 'unbounded')
        approximate['brief']['facts'][0]['value_json'] = '"60"'
        self.assertEqual(confirmed_duration_status([{'时长': '60'}], approximate)['status'], 'matched')


if __name__ == "__main__":
    unittest.main()
