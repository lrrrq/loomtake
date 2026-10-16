from __future__ import annotations

from copy import deepcopy
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp_v2.director_history import history_context, messages
from mvp_v2.workspace_store import WorkspaceServiceError


class ConversationTasks:
    """Anonymous event records, with the same authority API used by projection."""

    def __init__(self, turns):
        self.cache = {}
        self.reads = []
        self.read_options = []
        self.rows, self.events = [], {}
        for index, turn in enumerate(turns):
            task_id = turn.get('task_id', f'task_{index:04d}')
            self.rows.append({'task_id': task_id, 'task_type': turn.get('task_type', 'formal_production_advisor'),
                              'status': 'completed', 'created_at': f'2026-10-12T{index:06d}'})
            self.events[task_id] = [
                {'sequence_no': 1, 'details': {'workbench': {'request': {'content': turn['user']}}}},
                {'sequence_no': 2, 'details': {'response': {'summary': '简短结论', 'content': turn['assistant'],
                    **deepcopy(turn.get('response', {}))}}},
            ]

    def get_agent_session(self, token, session):
        if (token, session) != ('normal-actor', 'owned-session'):
            raise WorkspaceServiceError('PERMISSION_DENIED', '会话不属于当前操作者')
        return {'agent_session_id': session}

    def list_agent_session_tasks(self, token, session):
        self.get_agent_session(token, session)
        return deepcopy(self.rows)

    def list_task_events(self, token, task_id, *, detail_paths=None, latest_with_detail=None,
                         first_with_detail=None, after_sequence=0):
        if token != 'normal-actor':
            raise WorkspaceServiceError('PERMISSION_DENIED', '不能读取任务')
        self.reads.append(task_id)
        self.read_options.append({'task_id': task_id, 'detail_paths': detail_paths,
            'latest_with_detail': latest_with_detail, 'first_with_detail': first_with_detail,
            'after_sequence': after_sequence})
        events = [deepcopy(event) for event in self.events[task_id] if event['sequence_no'] > after_sequence]
        if latest_with_detail:
            events = [event for event in events if isinstance(event['details'].get(latest_with_detail), dict)
                      and event['details'][latest_with_detail]][-1:]
        if first_with_detail:
            events = [event for event in events if isinstance(event['details'].get(first_with_detail), (str, dict))
                      and event['details'][first_with_detail]][:1]
        if detail_paths:
            for event in events:
                original, projected = event['details'], {}
                for path in detail_paths:
                    parts, value = path.split('.'), original
                    for part in parts:
                        if not isinstance(value, dict) or part not in value:
                            break
                        value = value[part]
                    else:
                        target = projected
                        for part in parts[:-1]:
                            target = target.setdefault(part, {})
                        target[parts[-1]] = value
                event['details'] = projected
        return events

    def conversation_summary(self, token, session, *, value=None):
        self.get_agent_session(token, session)
        if value is not None:
            self.cache = deepcopy(value)
        return deepcopy(self.cache)


class SummaryProvider:
    def __init__(self, *, fail=False, unknown_source=False, content_size=1000):
        self.calls = []
        self.fail = fail
        self.unknown_source = unknown_source
        self.content_size = content_size

    def summarize_conversation(self, rows):
        self.calls.append(deepcopy(rows))
        if self.fail:
            raise WorkspaceServiceError('FORMAL_ADVISOR_SUMMARY_UNAVAILABLE', '匿名模型不可用')
        return {'content': '历史摘要' + '原' * self.content_size,
                'source_message_ids': ['other-private-message'] if self.unknown_source else
                                      [row['message_id'] for row in rows]}


class DirectorHistoryTests(unittest.TestCase):
    def make_agent(self, turns=None, provider=None):
        if turns is None:
            turns = [{'user': '保留人物回望的结尾。', 'assistant': '可以先用单人观察方向讨论。'}]
        return SimpleNamespace(services=SimpleNamespace(tasks=ConversationTasks(turns)),
                               advisor=provider if provider is not None else SummaryProvider())

    def history(self, agent, **kwargs):
        return history_context(agent, 'normal-actor', 'owned-session', **kwargs)

    def long_turns(self, count=50):
        # 100 messages of 8,000 characters, much larger than the actual budget.
        return [{'user': f'第{i}轮用户原始讨论。' + '甲' * (8000 - len(f'第{i}轮用户原始讨论。')),
                 'assistant': f'第{i}轮助手原始讨论。' + '乙' * (8000 - len(f'第{i}轮助手原始讨论。'))}
                for i in range(count)]

    def test_body_and_receipt_keep_distinct_event_references_and_original_choices(self):
        choices = [{'label': '方向二', 'instruction': '采用单人观察'}]
        agent = self.make_agent([{'user': '改第2段。', 'assistant': '原始创作说明',
                                 'response': {'choices': choices}}])
        agent.services.tasks.events['task_0000'].append(
            {'sequence_no': 3, 'details': {'response': {'summary': '已采用第2段', 'type': 'applied'}}})
        result = messages(agent, 'normal-actor', 'owned-session')
        assistant = result[1]
        self.assertEqual(assistant['content'], '原始创作说明')
        self.assertEqual(assistant['source_ref'], {'task_id': 'task_0000', 'sequence_no': 2})
        self.assertEqual(assistant['receipt_source_ref'], {'task_id': 'task_0000', 'sequence_no': 3})
        self.assertEqual(assistant['operation_receipt'], '已采用第2段')
        projected = self.history(agent)['conversation'][1]
        self.assertEqual(projected['choices'], choices)
        self.assertEqual(projected['receipt_source_ref']['sequence_no'], 3)

    def test_complete_ui_messages_are_unchanged_and_children_are_filtered_even_if_listed_first(self):
        agent = self.make_agent([{'task_id': 'child', 'user': '内部编辑', 'assistant': '内部结果'},
                                 {'task_id': 'parent', 'user': '用户原话' * 3000, 'assistant': '完整回答' * 4000}])
        agent.services.tasks.events['parent'][0]['details']['workbench']['text_task_id'] = 'child'
        result = messages(agent, 'normal-actor', 'owned-session')
        self.assertEqual([item['message_id'] for item in result], ['parent:user', 'parent:assistant'])
        self.assertEqual(result[0]['content'], '用户原话' * 3000)
        self.assertEqual(result[1]['content'], '完整回答' * 4000)
        self.assertEqual(agent.services.tasks.reads, ['child', 'parent'])

    def test_image_task_preserves_first_user_and_only_latest_response_with_exact_event_sources(self):
        original = '请给第2、3段配图，保留“原有主图”。\n只补缺图。'
        final = {'type': 'image_batch_proposal', 'summary': '已查到两处候选，等待采用。',
                 'shots': [{'block_id': 'block_2'}, {'block_id': 'block_3'}]}
        agent = self.make_agent([{'task_id': 'images', 'task_type': 'formal_image_candidates',
                                 'user': original, 'assistant': '初始进度'}])
        tasks = agent.services.tasks
        tasks.events['images'] = [
            {'sequence_no': 1, 'details': {'user_message': ''}},
            {'sequence_no': 2, 'details': {'user_message': original, 'response': {'summary': '正在开始'}}},
            *[{'sequence_no': index, 'details': {
                'user_message': '后续进度里的指令副本',
                'response': {'summary': '尚未完成', 'shots': [{'candidates': ['anonymous-padding' * 4000]}]},
                'internal_image_batch': {'choices': {'candidate': 'anonymous-padding' * 4000}},
            }} for index in range(3, 15)],
            {'sequence_no': 15, 'details': {'response': final}},
            {'sequence_no': 16, 'details': {'response': {}, 'operation_marker': 'waiting_user'}},
        ]
        before = deepcopy(tasks.events)

        result = messages(agent, 'normal-actor', 'owned-session')

        self.assertEqual([item['message_id'] for item in result], ['images:user', 'images:assistant'])
        self.assertEqual(result[0]['content'], original)
        self.assertEqual(result[0]['source_ref'], {'task_id': 'images', 'sequence_no': 2})
        self.assertEqual(result[1]['content'], final['summary'])
        self.assertEqual(result[1]['response'], final)
        self.assertEqual(result[1]['source_ref'], {'task_id': 'images', 'sequence_no': 15})
        self.assertNotIn('anonymous-padding', json.dumps(result))
        self.assertEqual(tasks.events, before)
        self.assertEqual(len(tasks.read_options), 2)
        first, latest = tasks.read_options
        self.assertEqual(first['first_with_detail'], 'user_message')
        self.assertIn('workbench_parent_task_id', first['detail_paths'])
        self.assertNotIn('response', first['detail_paths'])
        self.assertIsNone(first['latest_with_detail'])
        self.assertEqual(latest['latest_with_detail'], 'response')
        self.assertEqual(latest['detail_paths'], ('response', 'internal_image_batch'))
        self.assertIsNone(latest['first_with_detail'])

    def test_image_children_are_hidden_from_explicit_parent_marker_or_later_parent_link(self):
        for relation in ('child_marker', 'parent_link'):
            with self.subTest(relation=relation):
                agent = self.make_agent([
                    {'task_id': 'image-child', 'task_type': 'formal_image_candidates',
                     'user': '内部配图指令', 'assistant': '内部配图结果'},
                    {'task_id': 'parent', 'user': '改开头并给两段补图', 'assistant': '先核对创作与配图建议'},
                ])
                tasks = agent.services.tasks
                child = tasks.events['image-child']
                child[0]['details'] = {'user_message': '内部配图指令'}
                if relation == 'child_marker':
                    child[0]['details']['workbench_parent_task_id'] = 'parent'
                else:
                    tasks.events['parent'][0]['details']['workbench']['image_task_id'] = 'image-child'
                before = deepcopy(tasks.events)

                result = messages(agent, 'normal-actor', 'owned-session')

                self.assertEqual([item['message_id'] for item in result], ['parent:user', 'parent:assistant'])
                self.assertEqual(result[0]['content'], '改开头并给两段补图')
                self.assertEqual(result[1]['content'], '先核对创作与配图建议')
                self.assertEqual(tasks.events, before)
                self.assertEqual(messages(agent, 'normal-actor', 'owned-session', exclude_task_id='parent'), [])
                if relation == 'child_marker':
                    self.assertFalse(any(option['latest_with_detail'] for option in tasks.read_options
                                         if option['task_id'] == 'image-child'))

    def test_one_hundred_large_messages_share_one_real_budget_and_one_new_summary_call(self):
        for budget in (16000, 60000):
            with self.subTest(budget=budget), patch.dict(os.environ, {'MPLUS_DIRECTOR_HISTORY_CHARS': str(budget)}):
                provider = SummaryProvider(content_size=10000)
                agent = self.make_agent(self.long_turns(), provider)
                before = deepcopy(agent.services.tasks.events)
                result = self.history(agent, instruction='比较第二个方向，保留已明确的结尾。')
                self.assertLessEqual(len(json.dumps(result, ensure_ascii=False, sort_keys=True)), budget)
                self.assertLessEqual(len(provider.calls), 1)
                self.assertTrue(result['conversation'])
                self.assertTrue(result['history_warnings'])
                self.assertTrue(result['history_context_incomplete'])
                self.assertEqual(agent.services.tasks.events, before)
                self.assertEqual(len(messages(agent, 'normal-actor', 'owned-session')), 100)

    def test_oversized_single_message_has_explicit_source_locatable_excerpts(self):
        agent = self.make_agent([{'user': '保留开头。' + '很长的说明。' * 25000 + '结尾不要口号。',
                                 'assistant': '先核对具体方向。'}])
        with patch.dict(os.environ, {'MPLUS_DIRECTOR_HISTORY_CHARS': '16000'}):
            result = self.history(agent, instruction='保留结尾')
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 16000)
        original = messages(agent, 'normal-actor', 'owned-session')[0]['content']
        excerpt = next(item for item in result['conversation'] if item['role'] == 'user')
        self.assertTrue(excerpt['content_is_excerpt'])
        self.assertEqual(excerpt['original_content_characters'], len(original))
        self.assertTrue(excerpt['excerpt_spans'])
        for span in excerpt['excerpt_spans']:
            self.assertIn(original[span['start']:span['end']], excerpt['content'])
        self.assertIn('不能将片段当作整条消息', ''.join(result['history_warnings']))
        self.assertEqual(agent.advisor.calls, [])

    def test_retirement_preserves_original_audit_and_marks_paraphrased_constraints_inactive(self):
        statement = '本项目每镜加品牌标志'
        agent = self.make_agent([
            {'user': statement, 'assistant': '好的，之后所有镜头均露出品牌Logo。',
             'response': {'constraints': {'preserve': ['每个镜头保留品牌Logo']}}},
            {'user': '再比较两个方向。', 'assistant': '仍保留全片品牌露出。',
             'response': {'constraints': {'preserve': ['逐镜包含品牌标识']}}},
        ])
        retired = [{'convention_id': 'rule-old', 'rule_id': 'knowledge-old', 'statement': statement,
                    'status': 'disabled', 'evidence_refs': ['message:task_0000:user'], 'knowledge_version': 3}]
        before = messages(agent, 'normal-actor', 'owned-session')
        result = self.history(agent, retired=retired)
        self.assertEqual(result['retired_conventions'], retired)
        paired = result['conversation'][1]
        self.assertEqual(paired['retired_convention_ids'], ['rule-old'])
        self.assertEqual(paired['retired_convention_use'], 'inactive_including_paraphrases')
        self.assertNotIn('constraints', paired)
        self.assertIn('每个镜头保留品牌Logo', paired['historical_constraints']['preserve'])
        unrelated_paraphrase = result['conversation'][3]
        self.assertNotIn('constraints', unrelated_paraphrase)
        self.assertEqual(unrelated_paraphrase['historical_constraints_use'], 'verify_against_current_conventions_and_retirements')
        self.assertIn('同义表述都不再是有效约束', result['history_policy'])
        self.assertEqual(messages(agent, 'normal-actor', 'owned-session'), before)

    def test_short_retired_statement_never_rewrites_json_keys_message_ids_or_confirmed_fact_text(self):
        agent = self.make_agent([{'task_id': 'content', 'user': '已确认Brief的品牌事实保持。',
                                 'assistant': 'content只是历史术语。'}])
        result = self.history(agent, retired=[{'convention_id': 'c', 'statement': 'content', 'status': 'deleted'}])
        self.assertEqual(result['conversation'][0]['message_id'], 'content:user')
        self.assertEqual(result['conversation'][0]['source_ref']['task_id'], 'content')
        self.assertEqual(result['conversation'][0]['content'], '已确认Brief的品牌事实保持。')
        self.assertIn('不删除Brief事实', result['history_policy'])

    def test_removed_source_is_historical_only_and_invalidates_summary_scope(self):
        provider = SummaryProvider()
        turns = self.long_turns()
        for turn in turns:
            turn['response'] = {'sources': [{'id': 'project:project_images', 'title': '项目素材'}]}
        agent = self.make_agent(turns, provider)
        self.history(agent)
        old_digest = agent.services.tasks.cache['policy_digest']
        result = self.history(agent, excluded_source_ids=['project:project_images'])
        self.assertNotEqual(agent.services.tasks.cache['policy_digest'], old_digest)
        self.assertEqual(result['excluded_source_ids'], ['project:project_images'])
        assistant = next(item for item in result['conversation'] if item['role'] == 'assistant')
        self.assertEqual(assistant['excluded_source_ids'], ['project:project_images'])
        self.assertEqual(assistant['source_use'], 'excluded_sources_are_history_only_not_current_evidence')
        self.assertEqual(provider.calls[-1][0]['current_history_policy']['excluded_source_ids'], ['project:project_images'])

    def test_retirement_invalidates_cached_summaries_and_reaches_summary_input_with_full_sources(self):
        provider = SummaryProvider()
        agent = self.make_agent(self.long_turns(), provider)
        self.history(agent)
        old_digest = agent.services.tasks.cache['policy_digest']
        retired = [{'convention_id': 'c1', 'rule_id': 'r1', 'statement': '全片使用急促节奏',
                    'status': 'superseded', 'evidence_refs': ['artifact://project-conversation/owned-session/task_0000:user']}]
        result = self.history(agent, retired=retired)
        self.assertNotEqual(agent.services.tasks.cache['policy_digest'], old_digest)
        self.assertEqual(provider.calls[-1][0]['current_history_policy']['retired_conventions'], retired)
        self.assertEqual(result['retired_conventions'], retired)

    def test_unknown_summary_source_is_discarded_with_originals_and_obligations_available(self):
        turns = self.long_turns()
        turns[0]['response'] = {'constraints': {'prohibit': ['不可虚构产品型号']},
                                'pending_actions': [{'kind': 'revise_brief', 'instruction': '型号尚待客户确认'}]}
        provider = SummaryProvider(unknown_source=True)
        agent = self.make_agent(turns, provider)
        result = self.history(agent)
        self.assertNotIn('other-private-message', json.dumps(result, ensure_ascii=False))
        obligation = next(item for item in result['history_obligations'] if item['source_message_id'] == 'task_0000:assistant')
        self.assertEqual(obligation['constraints']['prohibit'], ['不可虚构产品型号'])
        self.assertEqual(obligation['pending_actions'][0]['instruction'], '型号尚待客户确认')
        self.assertIn('暂不可用', ''.join(result['history_warnings']))
        self.assertEqual(len(messages(agent, 'normal-actor', 'owned-session')), 100)

    def test_corrupt_cached_source_is_never_reused(self):
        agent = self.make_agent(self.long_turns())
        self.history(agent)
        self.assertTrue(agent.services.tasks.cache['chunks'])
        for value in agent.services.tasks.cache['chunks'].values():
            value.update(content='不属于本会话的秘密摘要', source_message_ids=['other-private-message'])
        agent.advisor = None
        result = self.history(agent)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('不属于本会话的秘密摘要', serialized)
        self.assertNotIn('other-private-message', serialized)
        self.assertIn('引用无法核对', ''.join(result['history_warnings']))
        self.assertFalse(agent.services.tasks.cache['chunks'])

    def test_sealed_chunks_are_not_resummarized_when_later_turns_arrive(self):
        provider = SummaryProvider()
        agent = self.make_agent(self.long_turns(), provider)
        self.history(agent)
        first_ids = tuple(item['message_id'] for item in provider.calls[0])
        self.history(agent, instruction='继续讨论其他方向')
        self.assertEqual(len(provider.calls), 2)
        self.assertNotEqual(tuple(item['message_id'] for item in provider.calls[1]), first_ids)
        self.assertEqual(len(agent.services.tasks.cache['chunks']), 2)
        old_cache = deepcopy(agent.services.tasks.cache['chunks'])
        task_id = 'task_later'
        agent.services.tasks.rows.append({'task_id': task_id, 'task_type': 'formal_production_advisor',
                                         'status': 'completed', 'created_at': '2026-10-12T999999'})
        agent.services.tasks.events[task_id] = [
            {'sequence_no': 1, 'details': {'user_message': '这轮先不改稿。'}},
            {'sequence_no': 2, 'details': {'response': {'content': '继续讨论。', 'summary': '讨论中'}}},
        ]
        self.history(agent)
        self.assertEqual(len(provider.calls), 3)
        self.assertTrue(set(old_cache) <= set(agent.services.tasks.cache['chunks']))
        ids = [tuple(row['message_id'] for row in call) for call in provider.calls]
        self.assertEqual(len(set(ids)), len(ids))

    def test_provider_failure_falls_back_without_retrying_same_unchanged_chunk_or_changing_events(self):
        provider = SummaryProvider(fail=True)
        agent = self.make_agent(self.long_turns(), provider)
        before = deepcopy(agent.services.tasks.events)
        first = self.history(agent)
        first_ids = tuple(item['message_id'] for item in provider.calls[0])
        second = self.history(agent)
        self.assertLessEqual(len(provider.calls), 2)
        self.assertNotEqual(tuple(item['message_id'] for item in provider.calls[-1]), first_ids)
        for result in [first, second]:
            self.assertTrue(result['conversation'])
            self.assertTrue(result['history_readback'])
            self.assertTrue(result['history_warnings'])
            self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 60000)
        self.assertEqual(agent.services.tasks.events, before)

    def test_new_user_request_can_recover_a_failed_auxiliary_summary(self):
        provider = SummaryProvider(fail=True)
        agent = self.make_agent(self.long_turns(), provider)
        self.history(agent, exclude_task_id='first-request')
        failed_ids = tuple(row['message_id'] for row in provider.calls[0])
        provider.fail = False
        result = self.history(agent, exclude_task_id='new-request')
        self.assertEqual(tuple(row['message_id'] for row in provider.calls[-1]), failed_ids)
        self.assertEqual(len(provider.calls), 2)
        self.assertTrue(result['history_summary'])
        self.assertFalse(agent.services.tasks.cache['failed_chunks'])

    def test_excessive_retirement_metadata_withholds_model_history_but_preserves_ui_audit(self):
        agent = self.make_agent()
        retired = [{'convention_id': 'large-rule', 'statement': '历史约定' * 10000,
                    'status': 'deleted', 'evidence_refs': ['message:task_0000:user']}]
        with patch.dict(os.environ, {'MPLUS_DIRECTOR_HISTORY_CHARS': '16000'}):
            result = self.history(agent, retired=retired)
        self.assertLessEqual(len(json.dumps(result, ensure_ascii=False)), 16000)
        self.assertEqual(result['retired_convention_count'], 1)
        self.assertEqual(result['conversation'], [])
        self.assertTrue(result['history_context_incomplete'])
        self.assertEqual(len(messages(agent, 'normal-actor', 'owned-session')), 2)
        self.assertEqual(agent.advisor.calls, [])

    def test_current_task_is_excluded_and_other_actor_is_denied_before_summary_or_event_read(self):
        agent = self.make_agent()
        self.assertEqual(self.history(agent, exclude_task_id='task_0000')['conversation'], [])
        agent.services.tasks.reads.clear()
        with self.assertRaises(WorkspaceServiceError) as caught:
            history_context(agent, 'other-actor', 'owned-session')
        self.assertEqual(caught.exception.code, 'PERMISSION_DENIED')
        self.assertEqual(agent.services.tasks.reads, [])
        self.assertEqual(agent.advisor.calls, [])


if __name__ == '__main__':
    unittest.main()
