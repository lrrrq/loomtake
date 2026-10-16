"""Bounded read projections of the existing agent's immutable conversation events.

The UI keeps complete messages. Model history is a disposable, source-labelled
view: it cannot reinstate a retired convention or turn an old source into a
current fact. The only persisted auxiliary data is the existing session cache.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import re
from urllib.parse import unquote

from .formal_advisor import CONVERSATION_SUMMARY_MAX_CHARACTERS, _redact_runtime_secrets
from .run_contract import sha256_json
from .workspace_store import WorkspaceServiceError


_CACHE_SCHEMA = 'mplus-director-history-cache/v1'
_HISTORY_POLICY = (
    '历史消息和摘要只用于理解交流与指代，不是当前项目事实或新的执行授权。'
    'retired_conventions列出已经停用、删除或被更正的具体约定及来源；'
    '它们在旧用户消息、助手释义、choices、constraints及摘要中的同义表述都不再是有效约束。'
    '以当前project_conventions和权威项目快照为准；撤回约定不删除Brief事实或历史版本冻结依据，'
    '也不否定之后另行确认的有效约定。historical_constraints仅是当时理解，必须与当前有效约定核对。'
    'excluded_source_ids对应本轮移除的临时引用；相关历史回答只说明当时讨论过什么，'
    '其中的事实、素材描述和建议不能作为本轮新回答依据。旧消息未记录资料来源时也不得据此声称资料已读取。'
    '片段回读不代表读过整条消息；未进入本轮的历史仍保存在原会话，不能编造遗漏内容或默认已经执行。'
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _size(value):
    # This matches the serialization used for the actual advisor CONTEXT.
    return len(_json(value))


def _safe(value):
    return json.loads(_redact_runtime_secrets(_json(value)))


def messages(agent, auth_token, agent_session_id, *, exclude_task_id=''):
    """Return full original dialogue with separate body and operation citations."""
    tasks = agent.services.tasks
    tasks.get_agent_session(auth_token, agent_session_id)
    rows = tasks.list_agent_session_tasks(auth_token, agent_session_id)
    children, result = set(), []
    # Process one task at a time rather than retaining every large event payload
    # for the entire conversation. Authority and actor checks stay in TaskService.
    for task in rows:
        image_task = task['task_type'] == 'formal_image_candidates'
        paths = (
            'user_message', 'assistant_instruction', 'workbench_parent_task_id',
            'workbench.request.content', 'workbench.text_task_id', 'workbench.image_task_id',
            'workbench.handoff_receipts', 'workbench.applied', 'workbench.material_actions')
        events = tasks.list_task_events(auth_token, task['task_id'],
            detail_paths=paths if image_task else (*paths, 'response'),
            **({'first_with_detail': 'user_message'} if image_task else {}))
        is_child = False
        for event in events:
            details = event.get('details') or {}
            workbench = details.get('workbench') or {}
            children.update(workbench[key] for key in ('text_task_id', 'image_task_id') if workbench.get(key))
            is_child = is_child or bool(details.get('workbench_parent_task_id'))
        if task['task_id'] == exclude_task_id or is_child:
            continue
        user_event, instruction = None, ''
        for event in events:
            details = event.get('details') or {}
            instruction = ((details.get('workbench') or {}).get('request') or {}).get('content') or \
                details.get('user_message') or details.get('assistant_instruction') or ''
            if instruction:
                user_event = event
                break
        if not user_event:
            continue
        common = {'task_id': task['task_id'], 'status': task['status'], 'created_at': task['created_at']}
        result.append({**common, 'message_id': task['task_id'] + ':user', 'role': 'user',
            'content': _redact_runtime_secrets(str(instruction)),
            'source_ref': {'task_id': task['task_id'], 'sequence_no': user_event['sequence_no']}})
        response_events = tasks.list_task_events(auth_token, task['task_id'], detail_paths=('response', 'internal_image_batch'),
            latest_with_detail='response') if image_task else events
        metadata = {event['sequence_no']: event['details'] for event in events}
        for event in response_events:
            event['details'].update(metadata.get(event['sequence_no'], {}))
        replies = [(event, event['details']['response']) for event in response_events
            if isinstance((event.get('details') or {}).get('response'), dict)]
        if not replies:
            continue
        receipt_event, response = replies[-1]
        if image_task and receipt_event['details'].get('internal_image_batch'):
            from .formal_image_batch import public_response
            response = public_response(agent.script_revision, auth_token, task,
                receipt_event['details']['internal_image_batch'], response)
        saved = receipt_event['details'].get('workbench') or {}
        if (response.get('type') == 'workbench_applied' and saved.get('handoff_receipts') and not saved.get('applied')
                or saved.get('material_actions') or response.get('image_creation_requests')):
            from .creative_workbench import read
            response = read(agent, auth_token, task['task_id'])['response']
        content_event, content_response = next(((event, reply) for event, reply in reversed(replies)
                                               if reply.get('content')), (receipt_event, response))
        text = str(content_response.get('content') or content_response.get('summary') or '')
        if text or response:
            reply = {**common, 'message_id': task['task_id'] + ':assistant', 'role': 'assistant',
                'content': _redact_runtime_secrets(text), 'response': _safe(response),
                'content_metadata': _safe({key: content_response[key] for key in
                    ('choices', 'constraints', 'sources') if content_response.get(key)}),
                'source_ref': {'task_id': task['task_id'], 'sequence_no': content_event['sequence_no']}}
            if (receipt_event['sequence_no'] != content_event['sequence_no']
                    or response.get('material_analysis_batches') or response.get('image_creation_results')):
                reply.update(operation_receipt=_redact_runtime_secrets(str(response.get('summary') or '')),
                    receipt_source_ref={'task_id': task['task_id'], 'sequence_no': receipt_event['sequence_no']})
            result.append(reply)
    return [item for item in result if item['task_id'] not in children]


def _model_message(item):
    result = {key: deepcopy(item[key]) for key in
        ('message_id', 'task_id', 'role', 'content', 'source_ref', 'status', 'receipt_source_ref') if key in item}
    response = item.get('response') or {}
    original = item.get('content_metadata') or response
    for key in ('choices', 'constraints', 'sources'):
        if original.get(key):
            result[key] = deepcopy(original[key])
    for key in ('handoffs', 'pending_actions'):
        if response.get(key):
            result[key] = deepcopy(response[key])
    for key in ('material_analysis_batches', 'image_creation_results'):
        if response.get(key):
            result[key] = [{name: deepcopy(value[name]) for name in
                ('task_id', 'action_index', 'command', 'status', 'summary', 'progress', 'target',
                 'is_running', 'error_message', 'adoption_receipts') if name in value}
                for value in response[key]]
    if item.get('operation_receipt'):
        result['operation_receipt'] = item['operation_receipt']
    return result


def _retirement_records(retired):
    keys = ('convention_id', 'rule_id', 'statement', 'status', 'evidence_refs', 'active_version', 'knowledge_version')
    return [_safe({key: item[key] for key in keys if key in item}) for item in retired if isinstance(item, dict)]


def _source_ids(values):
    return {value if isinstance(value, str) else str(value.get('id') or value.get('source_id') or '')
            for value in values if isinstance(value, (str, dict))} - {''}


def _retired_links(history, retired):
    """Use explicit message references; text only adds a non-destructive label."""
    links, linked_tasks = {}, {}
    for record in retired:
        key = str(record.get('convention_id') or record.get('rule_id') or '')
        refs = {unquote(str(ref)) for ref in record.get('evidence_refs') or []}
        statement = str(record.get('statement') or '').strip()
        for item in history:
            mid, source = item['message_id'], item.get('source_ref') or {}
            event_ref = f"task:{source.get('task_id', '')}:event:{source.get('sequence_no', '')}"
            referenced = any(ref in {mid, 'message:' + mid, event_ref}
                or ref.startswith('artifact://project-conversation/') and ref.rstrip('/').split('/')[-1] == mid
                for ref in refs)
            content = str(item.get('content') or '')
            mentioned = bool(statement and (content.strip() == statement or len(statement) >= 4 and statement in content))
            if referenced or mentioned:
                links.setdefault(mid, set()).add(key)
                task_id = item.get('task_id') or source.get('task_id')
                if task_id:
                    linked_tasks.setdefault(task_id, set()).add(key)
    for item in history:
        task_id = item.get('task_id') or (item.get('source_ref') or {}).get('task_id')
        if task_id in linked_tasks:
            links.setdefault(item['message_id'], set()).update(linked_tasks[task_id])
    return links


def _label_history(history, retired, excluded):
    links = _retired_links(history, retired)
    for item in history:
        item['context_use'] = 'historical_context_not_current_facts_or_authorization'
        if item['message_id'] in links:
            item['retired_convention_ids'] = sorted(links[item['message_id']])
            item['retired_convention_use'] = 'inactive_including_paraphrases'
        # A paraphrase need not contain the retired statement verbatim or share
        # the original event. Do not present any old inferred constraint as an
        # effective rule after retirement; the full retirement map travels too.
        if retired and item.get('constraints'):
            item['historical_constraints'] = item.pop('constraints')
            item['historical_constraints_use'] = 'verify_against_current_conventions_and_retirements'
        removed = sorted(_source_ids(item.get('sources') or []) & excluded)
        if removed:
            item['excluded_source_ids'] = removed
            item['source_use'] = 'excluded_sources_are_history_only_not_current_evidence'
        elif excluded:
            item['source_use'] = 'historical_sources_require_current_source_selection'
    return history


def _terms(instruction):
    # This ranks readback only. It never decides which business objects may change.
    terms = set(re.findall(r'[A-Za-z0-9_]{3,}', instruction.casefold()))
    for span in re.findall(r'[\u4e00-\u9fff]+', instruction):
        if len(span) <= 6:
            terms.add(span)
        terms.update(span[index:index + 2] for index in range(len(span) - 1))
    return {term for term in terms if len(term) > 1}


def _rank(item, terms):
    text = str(item.get('content') or '').casefold()
    return (sum(term in text for term in terms), bool(item.get('choices')),
            bool(item.get('constraints') or item.get('historical_constraints') or item.get('pending_actions')))


def _excerpt(item, limit, terms):
    """Return marked, source-locatable excerpts, never a silent prefix cutoff."""
    result = {key: deepcopy(item[key]) for key in ('message_id', 'task_id', 'role', 'source_ref', 'status',
        'receipt_source_ref', 'context_use', 'retired_convention_ids', 'retired_convention_use',
        'excluded_source_ids', 'source_use') if key in item}
    text = str(item.get('content') or '')
    result.update(content='', content_is_excerpt=True, original_content_characters=len(text), excerpt_spans=[])
    remaining = max(0, limit - _size(result) - 250)
    if remaining > 0 and text:
        windows = [(0, min(len(text), remaining // 3))]
        positions = [text.casefold().find(term) for term in terms if term in text.casefold()]
        middle = min(positions) if positions else max(0, len(text) // 2 - remaining // 6)
        windows += [(max(0, middle - remaining // 6), min(len(text), middle + remaining // 6)),
                    (max(0, len(text) - remaining // 3), len(text))]
        spans = []
        for start, end in sorted(windows):
            if end <= start:
                continue
            if spans and start <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], end)
            else:
                spans.append([start, end])
        result['content'] = '\n［以下为原文片段，中间内容未带入本轮］\n'.join(text[start:end] for start, end in spans)
        result['excerpt_spans'] = [{'start': start, 'end': end} for start, end in spans]
    omitted = [key for key in item if key not in result]
    if omitted:
        result['omitted_fields'] = omitted
    # Metadata may be large too. Identity/citation remain intact; omit an excerpt
    # completely if it cannot fit, rather than corrupting identifiers or JSON.
    if _size(result) > limit:
        result['content'], result['excerpt_spans'] = '', []
    return result if _size(result) <= limit else None


def _try_add(pack, key, value, budget):
    pack[key].append(value)
    if _size(pack) <= budget:
        return True
    pack[key].pop()
    return False


def _turns(history):
    turns = []
    for item in history:
        task = item.get('task_id') or (item.get('source_ref') or {}).get('task_id')
        previous = turns[-1][-1] if turns else {}
        previous_task = previous.get('task_id') or (previous.get('source_ref') or {}).get('task_id')
        same_turn = bool(turns and ((task and task == previous_task) or
                                   not task and item.get('role') == 'assistant' and previous.get('role') == 'user'))
        if same_turn:
            turns[-1].append(item)
        else:
            turns.append([item])
    return turns


def _sealed_chunks(history, limit):
    chunks, current = [], []
    for item in history:
        if _size([item]) > limit:
            if current:
                chunks.append(current)
                current = []
            continue  # An incomplete excerpt must not be advertised as a summary of the full message.
        if current and _size([*current, item]) > limit:
            chunks.append(current)
            current = []
        current.append(item)
    # The tail is deliberately left open. New turns do not resummarize a growing
    # partial chunk on every request; it stays eligible for original readback.
    return chunks


def _valid_summary(value, chunk):
    if not isinstance(value, dict) or set(value) != {'content', 'source_message_ids'}:
        return False
    content, refs = value['content'], value['source_message_ids']
    known = {item['message_id'] for item in chunk}
    return (isinstance(content, str) and bool(content.strip())
        and len(content) <= CONVERSATION_SUMMARY_MAX_CHARACTERS
        and isinstance(refs, list) and bool(refs)
        and all(isinstance(ref, str) and ref in known for ref in refs)
        and len(set(refs)) == len(refs))


def history_context(agent, auth_token, agent_session_id, *, instruction='', exclude_task_id='',
                    retired=(), excluded_source_ids=()):
    original = messages(agent, auth_token, agent_session_id, exclude_task_id=exclude_task_id)
    retired = _retirement_records(retired)
    excluded = {str(value) for value in excluded_source_ids if isinstance(value, str) and value}
    history = _label_history([_model_message(item) for item in original], retired, excluded)
    try:
        budget = max(16000, min(160000, int(os.environ.get('MPLUS_DIRECTOR_HISTORY_CHARS', '60000'))))
    except ValueError:
        budget = 60000
    policy = {'retired_conventions': retired, 'excluded_source_ids': sorted(excluded)}
    pack = {'conversation': [], 'history_summary': [], 'history_readback': [], 'history_obligations': [],
            'history_warnings': [], 'history_policy': _HISTORY_POLICY, **deepcopy(policy)}
    if _size(pack) > budget // 2:
        # Very large governance metadata leaves too little room to interpret the
        # history safely. Preserve original audit data, withhold model history.
        return {'conversation': [], 'history_summary': None, 'history_readback': [],
            'history_warnings': ['历史停用或引用记录超过本轮容量，本轮只使用当前项目依据；原消息和完整记录仍可查看。'],
            'history_policy': _HISTORY_POLICY, 'history_context_incomplete': True,
            'history_policy_sha256': sha256_json(policy), 'retired_convention_count': len(retired),
            'excluded_source_count': len(excluded)}
    pack['conversation'] = history
    if _size(pack) <= budget:
        pack['history_summary'] = None
        return pack
    pack['conversation'] = []
    warnings = ['历史较长，本轮按来源读取近期交流、相关原文与摘要；未带入的原消息仍完整保存在会话中。']
    # Reserve space for notices even when the rest of the pack exactly fills up.
    payload_budget = budget - 1500
    terms = _terms(instruction)
    recent_limit = min(payload_budget, _size(pack) + int((payload_budget - _size(pack)) * .50))
    for turn in reversed(_turns(history)):
        proposal = [*turn, *pack['conversation']]
        previous = pack['conversation']
        pack['conversation'] = proposal
        if _size(pack) > recent_limit:
            pack['conversation'] = previous
            if not previous:
                for item in turn:
                    excerpt = _excerpt(item, max(1000, (recent_limit - _size(pack)) // len(turn)), terms)
                    if excerpt:
                        _try_add(pack, 'conversation', excerpt, recent_limit)
                warnings.append('近期有较长消息，本轮仅回读带位置的片段；不能将片段当作整条消息。')
            break
    recent_ids = {item['message_id'] for item in pack['conversation']}
    older = [item for item in history if item['message_id'] not in recent_ids]
    # Keep explicit constraints and pending dependencies traceable when the
    # relevant old prose is too large or the optional summary provider fails.
    obligation_limit = min(payload_budget, _size(pack) + int(budget * .15))
    obligation_count = 0
    for item in reversed(older):
        fields = {key: item[key] for key in ('constraints', 'historical_constraints', 'pending_actions') if item.get(key)}
        if not fields:
            continue
        obligation_count += 1
        value = {'source_message_id': item['message_id'], 'source_ref': item.get('source_ref'),
                 'context_use': item['context_use'], **fields}
        for key in ('retired_convention_ids', 'historical_constraints_use', 'excluded_source_ids', 'source_use'):
            if key in item:
                value[key] = item[key]
        _try_add(pack, 'history_obligations', value, obligation_limit)
    if obligation_count > len(pack['history_obligations']):
        warnings.append('部分历史约束或待办超出本轮容量；涉及它们的操作需回查原消息，不能假定要求已经读全。')
    policy_digest = sha256_json(policy)
    cached = agent.services.tasks.conversation_summary(auth_token, agent_session_id)
    cache_valid = isinstance(cached, dict) and cached.get('schema') == _CACHE_SCHEMA and cached.get('policy_digest') == policy_digest
    entries = cached.get('chunks', {}) if cache_valid and isinstance(cached.get('chunks'), dict) else {}
    failed = cached.get('failed_chunks', {}) if cache_valid and isinstance(cached.get('failed_chunks'), dict) else {}
    request_key = exclude_task_id or sha256_json([item['message_id'] for item in history])
    chunk_limit = min(24000, max(4000, budget // 2 - _size(policy) - 1000))
    chunks = [chunk for chunk in _sealed_chunks(history, chunk_limit)
              if not any(item['message_id'] in recent_ids for item in chunk)]
    positions = {item['message_id']: index for index, item in enumerate(history)}
    chunks.sort(key=lambda chunk: (max((_rank(item, terms) for item in chunk), default=(0, False, False)),
                                   positions[chunk[-1]['message_id']]), reverse=True)
    valid_entries, valid_failed, summaries, attempts = {}, {}, [], 0
    summarize = getattr(agent.advisor, 'summarize_conversation', None)
    for chunk in chunks:
        digest = sha256_json(chunk)
        value = entries.get(digest)
        if value is not None and not _valid_summary(value, chunk):
            warnings.append('一份历史摘要的原文引用无法核对，已停止使用并保留原消息。')
            value = None
        if digest in failed:
            valid_failed[digest] = failed[digest]
        failed_here = isinstance(failed.get(digest), dict) and failed[digest].get('request_key') == request_key
        if value is None and not failed_here and callable(summarize) and attempts == 0:
            attempts += 1
            try:
                summary_input = deepcopy(chunk)
                summary_input[0]['current_history_policy'] = {**deepcopy(policy), 'instruction': _HISTORY_POLICY}
                value = summarize(summary_input)
                if not _valid_summary(value, chunk):
                    raise WorkspaceServiceError('FORMAL_ADVISOR_SUMMARY_INVALID', '历史摘要来源无法核对')
            except WorkspaceServiceError as exc:
                # Polling/re-entry of the same request cannot repeatedly invoke
                # a failed summary. A new user turn can recover the auxiliary
                # index after provider availability changes.
                valid_failed[digest] = {'code': exc.code, 'request_key': request_key}
                warnings.append('部分历史摘要暂不可用，本轮继续使用原文和有来源的约束；没有删除原消息。')
                value = None
        if value is not None:
            valid_failed.pop(digest, None)
            value = _safe(value)
            valid_entries[digest] = value
            summary = {**value, 'covers_message_ids': [item['message_id'] for item in chunk],
                'context_use': 'historical_summary_subject_to_current_retirements_and_sources'}
            for key in ('retired_convention_ids', 'excluded_source_ids'):
                ids = sorted({identity for item in chunk for identity in item.get(key, [])})
                if ids:
                    summary[key] = ids
            summaries.append(summary)
    updated = {'schema': _CACHE_SCHEMA, 'policy_digest': policy_digest,
               'chunks': valid_entries, 'failed_chunks': valid_failed}
    if updated != cached:
        agent.services.tasks.conversation_summary(auth_token, agent_session_id, value=updated)
    # Related originals win over an uncited impression from a summary. Include
    # their neighboring turn so a short direction reference retains its question.
    ranked = sorted(enumerate(older), key=lambda pair: (*_rank(pair[1], terms), pair[0]), reverse=True)
    turns_by_id = {item['message_id']: turn for turn in _turns(older) for item in turn}
    considered = set()
    readback_limit = min(payload_budget, _size(pack) + int(budget * .22))
    for _, item in ranked:
        if len(pack['history_readback']) >= 8:
            break
        if item['message_id'] in considered:
            continue
        turn = turns_by_id[item['message_id']]
        considered.update(value['message_id'] for value in turn)
        previous = pack['history_readback']
        pack['history_readback'] = [*previous, *turn]
        if _size(pack) > readback_limit:
            pack['history_readback'] = previous
            available = readback_limit - _size(pack) - 2
            if available >= 1000:
                per_item = min(6000, available // len(turn) - 2)
                for value in turn:
                    excerpt = _excerpt(value, per_item, terms)
                    if excerpt and _try_add(pack, 'history_readback', excerpt, readback_limit):
                        warnings.append('部分历史消息以带原文位置的片段回读，遗漏部分未作为本轮事实依据。')
    # Summaries use only the remaining aggregate budget, never N times a limit.
    for summary in summaries:
        _try_add(pack, 'history_summary', summary, payload_budget)
    # Allocation shares are preferences, not separate hard limits. Reuse spare
    # capacity for complete recent turns after higher-priority readbacks and
    # summaries have been selected, still checking the one aggregate budget.
    included_ids = {item['message_id'] for key in ('conversation', 'history_readback') for item in pack[key]}
    for turn in reversed(_turns(older)):
        if any(item['message_id'] in included_ids for item in turn):
            continue
        previous = pack['conversation']
        pack['conversation'] = [*turn, *previous]
        if _size(pack) > payload_budget:
            pack['conversation'] = previous
        else:
            included_ids.update(item['message_id'] for item in turn)
    if not pack['history_summary']:
        pack['history_summary'] = None
    if len(pack['history_summary'] or []) < len(summaries):
        warnings.append('其余已保存摘要未带入本轮，仍可根据原消息继续回查。')
    pack['history_context_incomplete'] = True
    for warning in dict.fromkeys(warnings):
        _try_add(pack, 'history_warnings', warning, budget)
    return pack
