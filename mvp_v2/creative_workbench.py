"""Workbench orchestration for the existing Formal agent and revision services.

Task events hold proposals, never a second editable script. Every target, image
identity and write is resolved by the existing authenticated authority services.
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging

from .creative_revision import _repo, effective_brief_values
from .formal_advisor import _redact_runtime_secrets
from .formal_script_revision import _stored_proposal
from .identity_service import require_permission
from .run_contract import sha256_json
from .run_state import try_exclusive_lock
from .workspace_store import WorkspaceServiceError


_CREATIVE_FIELDS = {'visual': '画面描述', 'subtitle': '画面描述', 'sound': '声音', 'structure': '段落', 'duration_text': '时长'}
_FORMAL_FIELDS = {'visual': ['visual'], 'sound': ['sound', 'voiceover'], 'structure': ['structure'],
    'duration_text': ['duration_seconds'], 'camera': ['camera_type', 'shot_size', 'camera_move'],
    'subtitle': ['subtitle'], 'note': ['notes']}
_LOG = logging.getLogger(__name__)


def _scope(project):
    return {key: project[key] for key in ('workspace_id', 'project_id')}


def snapshot(agent, auth_token, project, stage):
    revision = agent.script_revision
    if stage == 'creative':
        if project.get('current_version_id'):
            raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '项目已进入正式稿，请在当前稿继续')
        repo = _repo(revision, auth_token, **_scope(project))
        state = repo.read()
        draft = state['body'].get('draft') or {}
        targets = [{'id': row['shot_id'], 'number': int(row['shot_no']), 'fields': deepcopy(row['fields']),
                    'protected': row['shot_id'] in state['body'].get('assistant_protected_ids', [])}
                   for row in draft.get('shots', [])]
        frozen = (state['body'].get('generation_basis') or {}).get('context', {})
        brief = revision.brief_adapter.service.read_current_formal(auth_token, **_scope(project))
        evidence = {**{key: frozen[key] for key in ('creative_standard', 'effective_knowledge',
            'template_semantics', 'template_rules', 'material_bindings') if key in frozen},
            'brief': effective_brief_values(brief), 'brief_version_id': brief.get('brief_version_id')}
        return {'stage': stage, 'revision': state['revision'], 'project_version': state['project_version'],
            'draft_id': draft.get('draft_id'), 'draft_revision': draft.get('revision'), 'targets': targets,
            'images': deepcopy(state['body'].get('image_references', [])), 'evidence': evidence}
    current = revision.workspace.read_current(auth_token, **_scope(project))
    progress = revision.image_workspace.read_progress(auth_token, **_scope(project),
        expected_project_version=project['project_version'], library_service=agent.default_images)
    targets = [{'id': row['shot_id'], 'number': int(row['shot_no']),
                'fields': {field: deepcopy(row.get(field)) for fields in _FORMAL_FIELDS.values() for field in fields},
                'protected': bool(row.get('assistant_locked'))} for row in current.get('shots', [])]
    return {'stage': stage, 'revision': current['revision'], 'project_version': project['project_version'],
        'base_version_id': current.get('base_version_id'), 'targets': targets, 'images': deepcopy(progress),
        'evidence': revision.workspace.assistant_context(auth_token, **_scope(project))}


def _basis(value):
    return sha256_json({key: value.get(key) for key in ('stage', 'revision', 'project_version',
        'draft_id', 'draft_revision', 'base_version_id', 'targets', 'images', 'evidence')})


def _load(agent, auth_token, task_id):
    task = agent.services.tasks.get_task(auth_token, task_id)
    events = agent.services.tasks.list_task_events(auth_token, task_id)
    value = next((event['details'] for event in reversed(events)
                  if event.get('details', {}).get('workbench')), None)
    if not value:
        raise WorkspaceServiceError('INVALID_INPUT', '当前任务不是创作建议')
    return task, deepcopy(value['workbench']), deepcopy(value['response'])


def _record(agent, auth_token, task, data, response, status=None):
    tasks = agent.services.tasks
    latest = tasks.get_task(auth_token, task['task_id'])
    if status == 'needs_clarification' and latest['status'] == 'executing':
        status = 'waiting_user'
    details = {'workbench': data, 'response': response}
    if status and status != latest['status']:
        task = tasks.transition_task(auth_token, task_id=task['task_id'],
            expected_task_version=latest['task_version'], status=status, details=details)
    else:
        task = tasks.record_task_progress(auth_token, task_id=task['task_id'],
            expected_task_version=latest['task_version'], details=details)
    return {'task': task, 'response': response}


def start(agent, auth_token, agent_session_id, *, content, context, operation_key):
    if not agent.writes_enabled or agent.script_revision is None:
        raise WorkspaceServiceError('FORMAL_SCRIPT_READ_ONLY', '当前创作助手不可写入')
    if not isinstance(operation_key, str) or not 8 <= len(operation_key) <= 160:
        raise WorkspaceServiceError('INVALID_INPUT', '请保留输入并重新提交本次操作')
    session = agent.services.tasks.get_agent_session(auth_token, agent_session_id)
    project = agent.services.projects.get_project(auth_token, session['project_id'])
    request = {'content': str(content).strip(), 'context': context, 'operation_key': operation_key}
    with agent.services.store.atomic() as conn:
        actor = require_permission(conn, auth_token, **_scope(project), permission='project.update')
        previous = conn.execute("""SELECT t.task_id, e.details_json FROM tasks t JOIN task_events e ON e.task_id=t.task_id
            WHERE t.project_id=? AND t.actor_id=? AND json_extract(e.details_json,'$.workbench.operation_key')=?
            ORDER BY t.created_at DESC, e.sequence_no DESC LIMIT 1""",
            (project['project_id'], actor['actor_id'], operation_key)).fetchone()
        if previous:
            old = json.loads(previous['details_json'])['workbench']
            if old['request_sha256'] != sha256_json(request):
                raise WorkspaceServiceError('IDEMPOTENCY_CONFLICT', '同一请求不能换成其他范围或内容')
            return read(agent, auth_token, previous['task_id'])
        value = snapshot(agent, auth_token, project, context['stage'])
        if (value['project_version'] != context['expected_project_version']
                or value['revision'] != context['expected_revision']):
            raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '稿件已变化，输入已保留，请重新读取后预览')
        if context.get('expected_progress_revision') is not None and context['stage'] == 'formal':
            if value['images']['revision'] != context['expected_progress_revision']:
                raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '图片选择已变化，请重新读取后预览')
        known = {target['id'] for target in value['targets']}
        selected = context.get('selected_target_ids') or []
        current = context.get('current_target_id') or ''
        if (len(selected) != len(set(selected)) or not set(selected) <= known
                or current and current not in known):
            raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '所选对象已变化，请重新选择')
        if not known:
            raise WorkspaceServiceError('CREATIVE_DRAFT_UNAVAILABLE', '请先生成初稿，再提出修改要求')
        if context['scope'] == 'current' and not current or context['scope'] == 'selected' and not selected:
            raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '请先选择当前段落或本次要修改的段落')
        data = {**_scope(project), 'operation_key': operation_key, 'request_sha256': sha256_json(request),
            'request': request, 'before': value, 'basis': _basis(value), 'text_task_id': None, 'image_task_id': None}
        task = agent.services.tasks.create_task(auth_token, agent_session_id=agent_session_id,
            task_type='formal_production_advisor', payload_sha256=sha256_json(request),
            expected_project_version=project['project_version'])
        response = {'type': 'workbench_progress', 'task_id': task['task_id'], 'stage': value['stage'],
            'summary': '正在理解完整要求与修改范围。', 'progress': {'completed': 0, 'total': 3}}
        return _record(agent, auth_token, task, data, response, 'understanding')


def _resolve_action(action, before, ui):
    targets = before['targets']
    ids = [item['id'] for item in targets]
    allowed = set(ids if ui['scope'] == 'all' else ui['selected_target_ids'] if ui['scope'] == 'selected'
                  else [ui['current_target_id']])
    scope = action['target_scope']
    if scope == 'numbers':
        numbers = action['target_numbers']
        selected = [row['id'] for row in targets if row['number'] in numbers]
        if len(selected) != len(set(numbers)):
            raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '要求中的段落或镜号不存在')
    elif scope == 'current':
        selected = [ui.get('current_target_id')]
    elif scope == 'selected':
        selected = ui.get('selected_target_ids') or []
    elif scope == 'first_half':
        selected = ids[:(len(ids) + 1) // 2]
    elif scope == 'second_half':
        selected = ids[(len(ids) + 1) // 2:]
    else:
        selected = ids
    if not selected or not set(selected) <= allowed:
        raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '文字范围与当前选择不一致，请调整范围后重新预览')
    return [identity for identity in selected if not next(t for t in targets if t['id'] == identity)['protected']]


def _text_subject(agent, auth_token, task_id, stage):
    events = agent.services.tasks.list_task_events(auth_token, task_id)
    if stage == 'creative':
        for event in reversed(events):
            details = event.get('details', {})
            if details.get('creative_subject'):
                return details['creative_subject'], details['creative_proposal']
        return None, next(({'summary': e['details']['creative_notice']} for e in reversed(events)
                           if e.get('details', {}).get('creative_notice')), {})
    response, subject = _stored_proposal(events)
    return subject, response or {}


def _changes(subject, before):
    if not subject:
        return []
    if before['stage'] == 'creative':
        return [{'target_id': change['block_id'], **change} for change in subject['changes']]
    targets = {row['id']: row for row in before['targets']}
    return [{'target_id': op['shot_id'], 'field': op['field'],
             'before': targets[op['shot_id']]['fields'][op['field']], 'after': op['value']}
            for op in subject['operations'] if op.get('operation') != 'source_rebind']


def _duration_total(before, changes=()):
    from .models import parse_positive_seconds_strict
    field = '时长' if before['stage'] == 'creative' else 'duration_seconds'
    values = {row['id']: row['fields'].get(field) for row in before['targets']}
    for change in changes:
        if change['field'] == field:
            values[change['target_id']] = change['after']
    seconds = [parse_positive_seconds_strict(str(value).removeprefix('建议').strip()) for value in values.values()]
    return sum(seconds) if all(value is not None for value in seconds) else None


def _duration_target(before, parsed, project):
    from .creative_generation import _confirmed_output_requirements
    stated = parsed.get('constraints', {}).get('total_seconds')
    confirmed = _confirmed_output_requirements(before['evidence'].get('brief', {}))['total_seconds']
    if stated is not None and int(stated) != stated:
        return None, {'type': 'needs_clarification', 'summary': '当前镜头时长按整秒分配，请明确正整数总秒数。'}
    if stated is not None and confirmed is not None and int(stated) != confirmed:
        return None, {'type': 'needs_clarification', 'summary': f'本次要求 {int(stated)} 秒，与已确认需求 {confirmed} 秒冲突。请先修订需求，再重新预览。',
            'next_href': f"/projects/{project['project_id']}/brief", 'next_label': '修订需求'}
    target = int(stated) if stated is not None else confirmed
    if target is None:
        return None, {'type': 'needs_clarification', 'summary': '当前没有明确的全片总时长，请说明需要对齐的总秒数。'}
    return target, None


def _formal_text_proposal(agent, auth_token, task, project, before, actions, parsed, target=None, *, original_instruction=''):
    """Combine existing semantic edits and the canonical duration allocator once."""
    from .formal_script_assistant import analyze_formal_script_instruction
    from .planning_core import _shot_durations
    revision = agent.script_revision
    rows = [{'shot_id': row['id'], 'shot_no': str(row['number']),
             'assistant_locked': row['protected'], **deepcopy(row['fields'])} for row in before['targets']]
    by_id = {row['shot_id']: row for row in rows}
    operations, analysis = [], {}
    edits = [action for action in actions if action['kind'] == 'revise_text']
    if edits:
        selected = list(dict.fromkeys(identity for action in edits for identity in action['target_ids']))
        permitted_fields = {identity: list(dict.fromkeys(field for action in edits if identity in action['target_ids']
            for semantic in action['fields'] for field in _FORMAL_FIELDS[semantic])) for identity in selected}
        instruction = '\n'.join(action['instruction'] + '；允许镜号：' +
            '、'.join(str(by_id[identity]['shot_no']) for identity in action['target_ids']) for action in edits)
        instruction += '\n保留与禁止要求：' + json.dumps(parsed.get('constraints', {}), ensure_ascii=False)
        analysis = analyze_formal_script_instruction(instruction=instruction, shots=rows,
            assistant_locked_shot_ids=[row['shot_id'] for row in rows if row['assistant_locked']],
            selected_shot_ids=selected, editor_policy=revision.assistant_editor_policy,
            allowed_fields_by_shot=permitted_fields,
            context={**before['evidence'], 'recent_conversation': [{'role': 'user', 'content': original_instruction}]})
        if analysis.get('type') != 'rewrite_operations' and analysis.get('analysis_code') != 'script_no_changes':
            return None, analysis
        for item in analysis.get('operations', []):
            row = by_id[item['shot_id']]
            operations.append({**item, 'operation': item.get('operation', 'replace_field'), 'shot_no': row['shot_no']})
    if any(action['kind'] == 'align_duration' for action in actions):
        editable = {identity for action in actions if action['kind'] == 'align_duration' for identity in action['target_ids']}
        preferred = [row['duration_seconds'] for row in rows]
        fixed = {index: row['duration_seconds'] for index, row in enumerate(rows)
                 if row['shot_id'] not in editable or row['assistant_locked']}
        try:
            durations, _ = _shot_durations({'视频时长': str(target)}, [[] for _ in rows], [],
                preferred_durations=preferred, fixed_durations=fixed)
        except ValueError as exc:
            return None, {'type': 'needs_clarification', 'summary': str(exc)}
        operations.extend({'operation': 'replace_field', 'shot_id': row['shot_id'], 'shot_no': row['shot_no'],
            'field': 'duration_seconds', 'value': seconds} for row, seconds in zip(rows, durations)
            if seconds != row['duration_seconds'])
    if not operations:
        return None, {'type': 'no_changes', 'summary': f'当前全片已为 {target} 秒，所选范围没有需要采用的时长差异。'
            if target is not None else '所选范围没有需要采用的文字差异。'}
    _validate_changes([{'target_id': op['shot_id'], 'field': op['field']} for op in operations], actions, before)
    with agent.services.store.atomic():
        child = agent.services.tasks.create_task(auth_token, agent_session_id=task['agent_session_id'],
            task_type='formal_script_revision', payload_sha256=sha256_json({'workbench': task['task_id'], 'operations': operations}),
            expected_project_version=project['project_version'])
        child = agent.services.tasks.transition_task(auth_token, task_id=child['task_id'],
            expected_task_version=child['task_version'], status='understanding',
            details={'workbench_parent_task_id': task['task_id']})
    result = revision._propose_operations_on_task(auth_token, task=child, project=project, operations=operations,
        expected_revision=before['revision'], idempotency_key='workbench-text:' + task['task_id'],
        entrypoint='assistant', draft_only=True, model_evidence=analysis.get('model_evidence'),
        permitted_shot_ids=list(dict.fromkeys(identity for action in actions for identity in action['target_ids'])))
    return result['task_id'], None


def execute(agent, auth_token, task_id):
    task, data, response = _load(agent, auth_token, task_id)
    lock_path = agent.services.store.database_path.parent / '.workbench-actions' / (task_id + '.lock')
    with try_exclusive_lock(lock_path, run_id=task_id) as acquired:
        if not acquired:
            return read(agent, auth_token, task_id)
        task, data, response = _load(agent, auth_token, task_id)
        if task['status'] != 'understanding':
            return read(agent, auth_token, task_id)
        _record(agent, auth_token, task, data, response, 'executing')
        try:
            return _execute(agent, auth_token, task, data, response)
        except Exception as exc:
            _LOG.exception('Workbench task %s failed', task_id)
            response.update(type='workbench_failed', summary=getattr(exc, 'message', '本次分析未完成，原稿和已保存图片已保留。请重新读取后重试。'),
                error_code=getattr(exc, 'code', 'WORKBENCH_INTERRUPTED'))
            return _record(agent, auth_token, task, data, response, 'failed')


def _execute(agent, auth_token, task, data, response):
    before, request = data['before'], data['request']
    project = agent.services.projects.get_project(auth_token, data['project_id'])
    current = snapshot(agent, auth_token, project, before['stage'])
    if _basis(current) != data['basis']:
        raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '分析开始前稿件已变化，请重新预览')
    ui = request['context']
    numbers = {target['id']: target['number'] for target in before['targets']}
    decision = agent._decision(request['content'], {'mode': 'creative_workbench', 'evidence_ids': [],
        **before['evidence'], 'snapshot': {'stage': before['stage'],
        'targets': [{key: value for key, value in row.items() if key != 'id'} for row in before['targets']],
        'ui_scope': {'mode': ui['scope'], 'current_number': numbers.get(ui.get('current_target_id')),
                     'selected_numbers': [numbers[i] for i in ui.get('selected_target_ids', [])]}}})
    parsed = decision.model_dump()
    data['decision'] = parsed
    if decision.intent == 'needs_clarification' or not parsed.get('actions'):
        result = {'type': 'needs_clarification' if decision.intent == 'needs_clarification' else 'read_response',
            'summary': decision.summary, 'questions': decision.questions, 'choices': parsed.get('choices', []),
            'constraints': parsed.get('constraints', {}), 'task_id': task['task_id']}
        return _record(agent, auth_token, task, data, result,
                       'needs_clarification' if decision.intent == 'needs_clarification' else 'completed')
    actions = []
    for index, action in enumerate(parsed['actions']):
        identities = _resolve_action(action, before, ui)
        actions.append({**action, 'action_index': index, 'target_ids': identities,
            'target_labels': [f"第 {numbers[identity]} {'段' if before['stage'] == 'creative' else '镜'}" for identity in identities]})
    data['actions'] = actions
    handoff_actions = [action for action in actions if action['kind'] in {'revise_brief', 'replan'}]
    if handoff_actions:
        return _special_actions(agent, auth_token, task, data, parsed, handoff_actions)
    text_actions = [a for a in actions if a['kind'] in {'revise_text', 'align_duration'}]
    image_actions = [a for a in actions if a['kind'] == 'find_images']
    creation_actions = [a for a in actions if a['kind'] == 'create_image']
    data['pending_actions'] = [{**action, 'status': 'awaiting_adoption'} for action in creation_actions]
    if creation_actions and not text_actions and not image_actions:
        requests = _image_creation_requests(agent, auth_token, task, data, creation_actions)
        result = {**_action_response(data, task['task_id']), 'type': 'image_creation_requests',
            'summary': '已按所选对象保留生图要求，请逐项核对后在原入口生成。', 'image_creation_requests': requests,
            'pending_actions': []}
        return _record(agent, auth_token, task, data, result, 'completed')
    duration_actions = [a for a in actions if a['kind'] == 'align_duration']
    if duration_actions or parsed.get('constraints', {}).get('total_seconds') is not None:
        target, notice = _duration_target(before, parsed, project)
        if notice:
            notice.update(_action_response(data, task['task_id']), pending_actions=data['pending_actions'])
            return _record(agent, auth_token, task, data, notice, 'needs_clarification')
        data['duration_target_seconds'] = target
    response.update(summary='正在按范围生成文字差异。', progress={'completed': 1, 'total': 3})
    _record(agent, auth_token, task, data, response)
    text_subject, text_public = None, {}
    if text_actions:
        permitted = list(dict.fromkeys(identity for action in text_actions for identity in action['target_ids']))
        if not permitted:
            raise WorkspaceServiceError('SCRIPT_TARGET_LOCKED', '所选内容都已保护，请调整选择或先解除对应保护')
        instruction = request['content'] + '\n本次文字动作：' + json.dumps([
            {**{key: value for key, value in action.items() if key not in {'target_ids', 'depends_on'}},
             '允许段落或镜号': [numbers[i] for i in action['target_ids']]} for action in text_actions], ensure_ascii=False)
        instruction += '\n保留与禁止要求：' + json.dumps(parsed.get('constraints', {}), ensure_ascii=False)
        revision = agent.script_revision
        if before['stage'] == 'creative':
            permitted_fields = {identity: list(dict.fromkeys(field for action in text_actions if identity in action['target_ids']
                for semantic in (action['fields'] or ['duration_text'])
                for field in ['visual' if semantic == 'subtitle' else semantic])) for identity in permitted}
            result = revision.propose_creative_draft_change(auth_token, **_scope(project),
                expected_project_version=project['project_version'], expected_revision=before['revision'],
                instruction=instruction, selected_block_ids=permitted,
                operation_key='workbench-text:' + task['task_id'], agent_session_id=task['agent_session_id'],
                workbench_parent_task_id=task['task_id'],
                input_updates=[], answer_ids=[], allowed_fields_by_shot=permitted_fields,
                **({'duration_target_ids': list(dict.fromkeys(identity for action in duration_actions for identity in action['target_ids'])),
                    'duration_target_seconds': data['duration_target_seconds']} if duration_actions else {}))
            data['text_task_id'] = result['task_id']
        else:
            text_task_id, notice = _formal_text_proposal(agent, auth_token, task, project,
                before, text_actions, parsed, data.get('duration_target_seconds'), original_instruction=request['content'])
            if notice and (notice.get('type') != 'no_changes' or not image_actions and not creation_actions):
                if data.get('duration_target_seconds') is not None:
                    notice['duration_status'] = {'target_seconds': data['duration_target_seconds'],
                        'actual_seconds': _duration_total(before),
                        'status': 'matched' if _duration_total(before) == data['duration_target_seconds'] else 'mismatch'}
                notice.update(_action_response(data, task['task_id']), pending_actions=data['pending_actions'])
                return _record(agent, auth_token, task, data, notice,
                    'completed' if notice.get('type') == 'no_changes' else 'needs_clarification')
            text_public = notice or {}
            data['text_task_id'] = text_task_id
        if data.get('text_task_id'):
            text_subject, text_public = _text_subject(agent, auth_token, data['text_task_id'], before['stage'])
        if not text_subject and text_public.get('type') != 'no_changes':
            return _record(agent, auth_token, task, data, {**text_public, 'type': 'needs_clarification',
                **_action_response(data, task['task_id']), 'pending_actions': data['pending_actions'],
                'summary': text_public.get('summary') or '没有可采用的文字修改，请明确要求后重试。'}, 'needs_clarification')
        _validate_changes(_changes(text_subject, before), text_actions, before)
    response.update(summary='正在根据提议的画面寻找参考图。' if image_actions else '正在核对修改范围。',
                    progress={'completed': 2, 'total': 3})
    _record(agent, auth_token, task, data, response)
    image_public = {}
    if image_actions:
        project = agent.services.projects.get_project(auth_token, data['project_id'])
        current = snapshot(agent, auth_token, project, before['stage'])
        selected = list(dict.fromkeys(identity for action in image_actions for identity in action['target_ids']))
        extra = ({'target_kind': 'creative_block', 'selected_block_ids': selected,
                  'expected_creative_revision': current['revision']} if before['stage'] == 'creative'
                 else {'selected_shot_ids': selected})
        image_instruction = '为所选段落寻找参考图。' + '；'.join(a['instruction'] for a in image_actions)
        result = agent.script_revision.start_image_batch(auth_token, **_scope(project),
            instruction=image_instruction[:4000], expected_project_version=project['project_version'],
            idempotency_key='workbench-images:' + task['task_id'], agent_session_id=task['agent_session_id'],
            projection_task_id=data['text_task_id'], workbench_parent_task_id=task['task_id'], **extra)
        data['image_task_id'] = result['task']['task_id']
        _record(agent, auth_token, task, data, response)
        result = agent.script_revision.execute_image_batch(auth_token, task_id=data['image_task_id'])
        image_public = result['response']
    changes = _changes(text_subject, before)
    groups = []
    for target in before['targets']:
        edits = [change for change in changes if change['target_id'] == target['id']]
        picture = next((item for item in image_public.get('shots', []) if item['target_id'] == target['id']), None)
        creation = [deepcopy(action) for action in data['pending_actions'] if target['id'] in action['target_ids']]
        if edits or picture or creation:
            groups.append({'group_id': target['id'], 'target_id': target['id'], 'number': target['number'],
                'label': f"第 {target['number']} {'段' if before['stage'] == 'creative' else '镜'}",
                'changes': edits, 'image': picture, 'depends_on': [],
                'action_only': bool(creation and not edits and not picture), 'pending_actions': creation,
                'selected': bool(edits or picture and picture.get('included') or creation)})
    group_ids = {group['group_id'] for group in groups}
    by_index = {action['action_index']: action for action in actions}
    for group in groups:
        group['depends_on'] = list(dict.fromkeys(identity for action in group['pending_actions']
            for dependency in action['depends_on'] for identity in by_index[dependency]['target_ids']
            if identity != group['target_id'] and identity in group_ids))
    duration_field = '时长' if before['stage'] == 'creative' else 'duration_seconds'
    duration_ids = [group['group_id'] for group in groups if any(c['field'] == duration_field for c in group['changes'])]
    if data.get('duration_target_seconds') is not None:
        for group in groups:
            if group['group_id'] in duration_ids:
                group['depends_on'] = list(dict.fromkeys(group['depends_on'] + [identity for identity in duration_ids
                    if identity != group['group_id']]))
        actual = _duration_total(before, changes)
        text_public['duration_status'] = {'target_seconds': data['duration_target_seconds'],
            'actual_seconds': actual, 'status': 'matched' if actual == data['duration_target_seconds'] else 'mismatch'}
    project = agent.services.projects.get_project(auth_token, data['project_id'])
    data.update(groups=groups, text_subject=text_subject, text_public=text_public, image_public=image_public,
                ready_basis=_basis(snapshot(agent, auth_token, project, before['stage'])))
    digest = sha256_json(data)
    response = {**_action_response(data, task['task_id']), 'type': 'workbench_proposal',
        'summary': decision.summary, 'proposal_digest': digest, 'groups': groups,
        'constraints': parsed.get('constraints', {}), 'stale': False,
        'pending_actions': data['pending_actions'],
        'duration_status': text_public.get('duration_status'), 'progress': {'completed': 3, 'total': 3}}
    data['proposal_digest'] = digest
    return _record(agent, auth_token, task, data, response, 'waiting_user')


def _validate_changes(changes, actions, before):
    allowed = {}
    for action in actions:
        semantic = action['fields'] or (['duration_text'] if action['kind'] == 'align_duration' else [])
        fields = ([_CREATIVE_FIELDS[field] for field in semantic if field in _CREATIVE_FIELDS]
                  if before['stage'] == 'creative' else [name for field in semantic for name in _FORMAL_FIELDS[field]])
        for identity in action['target_ids']:
            allowed.setdefault(identity, set()).update(fields)
    for change in changes:
        if change['field'] not in allowed.get(change['target_id'], set()):
            raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '文字建议超出了本次对象或字段范围，未应用；请调整要求后重新预览')


def _special_actions(agent, auth_token, task, data, parsed, actions):
    # These operations retain their existing review UI and authority contracts.
    handoffs = []
    for action in actions:
        kind = 'brief' if action['kind'] == 'revise_brief' else 'plan'
        handoffs.append({'kind': kind, 'task_id': task['task_id'], 'action_index': action['action_index'],
            'instruction': action['instruction'], 'constraints': deepcopy(parsed.get('constraints', {})),
            'stage': data['before']['stage'], 'target_ids': action['target_ids'], 'labels': action['target_labels'],
            'depends_on': action['depends_on'],
            'next_href': f"/projects/{data['project_id']}/{kind}?workbench_task_id={task['task_id']}",
            'next_label': '核对需求修订' if kind == 'brief' else '核对生成规划'})
    data['handoffs'] = handoffs
    data['pending_actions'] = [{**action, 'status': 'awaiting_handoff'} for action in data['actions']
        if action['kind'] not in {'revise_brief', 'replan'}]
    response = {**_action_response(data, task['task_id']), 'type': 'workbench_handoff',
        'summary': '本次要求涉及需求或规划，请先到原入口核对并确认；其余动作与依赖已保留，当前稿未修改。',
        'questions': parsed.get('questions', []), 'handoff': handoffs[0], 'handoffs': handoffs,
        'pending_actions': data['pending_actions'], 'next_href': handoffs[0]['next_href'],
        'next_label': handoffs[0]['next_label'],
        'next_steps': ['完成对应需求或规划核对后，按当前稿重新预览保留的后续动作。']}
    return _record(agent, auth_token, task, data, response, 'waiting_user')


def _action_response(data, task_id):
    return {**_scope(data), 'task_id': task_id, 'stage': data['before']['stage'],
        'original_instruction': data['request']['content'], 'actions': deepcopy(data.get('actions', [])),
        'constraints': deepcopy(data.get('decision', {}).get('constraints', {}))}


def _image_creation_requests(agent, auth_token, task, data, actions):
    kind = 'creative_block' if data['before']['stage'] == 'creative' else 'formal_shot'
    requests = []
    for action in actions:
        for identity in action['target_ids']:
            context = agent.generation.image_creation_context(auth_token, **_scope(data), target_kind=kind, target_id=identity)
            requests.append({**_scope(data), 'type': 'image_creation_request', 'task_id': task['task_id'],
                'action_index': action['action_index'], 'stage': data['before']['stage'],
                'target_kind': kind, 'target_id': identity, 'target_label': context['target']['label'],
                'instruction': action['instruction'], 'depends_on': action['depends_on'],
                'constraints': deepcopy(data.get('decision', {}).get('constraints', {})),
                **{key: deepcopy(context[key]) for key in ('target_basis', 'basis_label', 'original_shot',
                    'project_version', 'creative_revision', 'script_revision', 'available', 'unavailable_reason') if key in context}})
    return requests


def _adopted_creation_actions(data, chosen):
    """Resolve per-target dependencies against the exact adopted text and images."""
    changes = _changes(data.get('text_subject'), data['before'])
    adopted = {}
    for action in data.get('actions', []):
        identities = chosen & set(action['target_ids'])
        if action['kind'] in {'revise_text', 'align_duration'}:
            fields = action['fields'] or ['duration_text']
            names = ({_CREATIVE_FIELDS[name] for name in fields} if data['before']['stage'] == 'creative'
                     else {field for name in fields for field in _FORMAL_FIELDS[name]})
            adopted[action['action_index']] = {change['target_id'] for change in changes
                if change['target_id'] in identities and change['field'] in names and change['before'] != change['after']}
        elif action['kind'] == 'find_images':
            # A successful search can legitimately find no eligible picture.
            # Selecting its creation group acknowledges that result without
            # pretending an image was saved or requiring a missing candidate.
            adopted[action['action_index']] = {group['target_id'] for group in data['groups']
                if group['target_id'] in identities and group.get('image')
                and group['image'].get('state') in {'ready', 'gap', 'reference', 'unchanged'}
                and group['image'].get('reason_code') != 'read_failed'}
    actions = {action['action_index']: action for action in data.get('actions', [])}
    ready, pending = [], []
    for action in data.get('pending_actions', []):
        targets = []
        for identity in action['target_ids']:
            if identity not in chosen:
                continue
            if all((identity in adopted.get(dependency, set()) if identity in actions[dependency]['target_ids']
                    else bool(actions[dependency]['target_ids']) and set(actions[dependency]['target_ids']) <= adopted.get(dependency, set()))
                   for dependency in action['depends_on']):
                targets.append(identity)
        if targets:
            ready.append({**action, 'target_ids': targets})
        remaining = [identity for identity in action['target_ids'] if identity not in targets]
        if remaining:
            labels = dict(zip(action['target_ids'], action['target_labels']))
            pending.append({**action, 'target_ids': remaining, 'target_labels': [labels[identity] for identity in remaining],
                'status': 'not_adopted'})
    return ready, pending


def read(agent, auth_token, task_id):
    task, data, response = _load(agent, auth_token, task_id)
    if task['status'] == 'waiting_user' and response.get('type') == 'workbench_proposal':
        project = agent.services.projects.get_project(auth_token, data['project_id'])
        response['stale'] = _basis(snapshot(agent, auth_token, project, data['before']['stage'])) != data.get('ready_basis')
        if response['stale']:
            response['summary'] = '稿件或图片依据已变化，旧建议已保留。请按当前稿重新预览后采用。'
    if task['status'] == 'executing' and data.get('image_task_id'):
        child = agent.script_revision.read_image_batch(auth_token, **_scope(data), task_id=data['image_task_id'])
        response['image_progress'] = child['response'].get('progress')
    return {'task': task, 'response': response}


def confirm(agent, auth_token, task_id, *, proposal_digest, retained_group_ids):
    if not agent.writes_enabled or agent.script_revision is None or not agent.script_revision.writes_enabled:
        raise WorkspaceServiceError('FORMAL_SCRIPT_READ_ONLY', '当前创作助手不可写入')
    with agent.services.store.atomic():
        task, data, response = _load(agent, auth_token, task_id)
        if proposal_digest != data.get('proposal_digest'):
            raise WorkspaceServiceError('PROPOSAL_DIGEST_MISMATCH', '建议已变化，请重新读取')
        chosen = set(retained_group_ids)
        if len(chosen) != len(retained_group_ids) or not chosen <= {g['group_id'] for g in data['groups']}:
            raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '请选择当前建议中的完整段落组')
        if data.get('applied'):
            if chosen != set(data['applied'].get('requested_groups', data['applied']['groups'])):
                raise WorkspaceServiceError('IDEMPOTENCY_CONFLICT', '本建议已按另一组选择采用，请读取已保存结果')
            return {'task': task, 'response': response}
        if task['status'] != 'waiting_user':
            raise WorkspaceServiceError('INVALID_TASK_TRANSITION', '本建议当前不能采用，请重新读取')
        project = agent.services.projects.get_project(auth_token, data['project_id'])
        before = snapshot(agent, auth_token, project, data['before']['stage'])
        if _basis(before) != data['ready_basis']:
            raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '稿件或图片已变化，未应用任何修改，请重新预览')
        requested_groups = sorted(chosen)
        changed_groups = {group['group_id'] for group in data['groups']
            if any(change['before'] != change['after'] for change in group.get('changes', []))
            or group.get('image') and group['image'].get('included') and group['image'].get('selected_candidate_key')}
        ready_actions, _ = _adopted_creation_actions(data, chosen)
        creation_groups = {identity for action in ready_actions for identity in action['target_ids']}
        chosen &= changed_groups | creation_groups
        target = data.get('duration_target_seconds')
        if chosen and target is not None:
            actual = _duration_total(before, [change for change in _changes(data.get('text_subject'), data['before'])
                if change['target_id'] in chosen])
            if actual != target:
                raise WorkspaceServiceError('DURATION_CONSTRAINT_MISMATCH',
                    f'当前选择不能满足全片 {target} 秒，请一起采用相关时长组，或取消本次建议；尚未写入任何修改。')
        _record(agent, auth_token, task, data, response, 'executing')
        text_ids = chosen & {c['target_id'] for c in _changes(data.get('text_subject'), data['before'])}
        if data.get('text_task_id'):
            _apply_text(agent, auth_token, data, text_ids)
        image_keys = [g['image']['selected_candidate_key'] for g in data['groups']
                      if g['group_id'] in chosen and g.get('image') and g['image'].get('selected_candidate_key')
                      and g['image'].get('included')]
        if image_keys:
            from .formal_image_batch import confirm_composed
            confirm_composed(agent.script_revision, auth_token, task_id=data['image_task_id'],
                proposal_digest=data['image_public']['proposal_digest'], selected_candidate_keys=image_keys,
                parent_task_id=task_id, retained_target_ids=sorted(chosen))
        project = agent.services.projects.get_project(auth_token, data['project_id'])
        after = snapshot(agent, auth_token, project, data['before']['stage'])
        data['applied'] = {'groups': sorted(chosen), 'requested_groups': requested_groups, 'before': before, 'after': after}
        saved_count = len(chosen & changed_groups)
        response = {**_action_response(data, task_id), 'type': 'workbench_applied',
            'summary': f'已采用 {saved_count} 组修改并保存。' if saved_count else (
                '已保留所选生图要求，当前稿未修改。' if chosen else '未采用本次建议，当前稿已保留。'),
            'can_undo': bool(chosen), 'project_version': after['project_version'], 'revision': after['revision']}
        ready_actions, pending_actions = _adopted_creation_actions(data, chosen)
        response['pending_actions'] = pending_actions
        response['image_creation_requests'] = _image_creation_requests(agent, auth_token, task, data, ready_actions)
        if target is not None:
            response['duration_status'] = {'target_seconds': target, 'actual_seconds': _duration_total(after),
                'status': 'matched' if _duration_total(after) == target else 'mismatch'}
        return _record(agent, auth_token, task, data, response, 'completed' if chosen else 'cancelled')


def _apply_text(agent, auth_token, data, chosen):
    revision, subject = agent.script_revision, data['text_subject']
    if data['before']['stage'] == 'creative':
        revision.confirm_creative_draft_change(auth_token, **_scope(data), task_id=data['text_task_id'],
            proposal_digest=data['text_public']['digest'], retained_block_ids=sorted(chosen))
        return
    child = agent.services.tasks.get_task(auth_token, data['text_task_id'])
    if not chosen:
        agent.services.tasks.transition_task(auth_token, task_id=child['task_id'],
            expected_task_version=child['task_version'], status='cancelled')
        return
    params = {**subject, 'operations': [op for op in subject['operations'] if op['shot_id'] in chosen]}
    child = agent.services.tasks.transition_task(auth_token, task_id=child['task_id'],
        expected_task_version=child['task_version'], status='executing', details={'workbench_parent': data['operation_key']})
    project = agent.services.projects.get_project(auth_token, data['project_id'])
    result = revision.execute(auth_token, project=project, parameters=params)
    agent.services.tasks.transition_task(auth_token, task_id=child['task_id'],
        expected_task_version=child['task_version'], status='completed', details={'response': result})


def undo(agent, auth_token, task_id):
    from .version_review_service import VersionReviewService
    if not agent.writes_enabled or agent.script_revision is None or not agent.script_revision.writes_enabled:
        raise WorkspaceServiceError('FORMAL_SCRIPT_READ_ONLY', '当前创作助手不可写入')
    with agent.services.store.atomic():
        task, data, response = _load(agent, auth_token, task_id)
        if data.get('undone'):
            return {'task': task, 'response': response}
        applied = data.get('applied')
        if not applied or not applied['groups']:
            raise WorkspaceServiceError('INVALID_INPUT', '本次没有可撤销的已采用修改')
        project = agent.services.projects.get_project(auth_token, data['project_id'])
        current = snapshot(agent, auth_token, project, data['before']['stage'])
        before, after = applied['before'], applied['after']
        targets = {t['id']: t for t in current['targets']}
        original = {t['id']: t for t in before['targets']}
        saved = {t['id']: t for t in after['targets']}
        changes = []
        for identity in applied['groups']:
            for field, value in original[identity]['fields'].items():
                if value != saved[identity]['fields'].get(field):
                    if identity not in targets or targets[identity]['protected'] or targets[identity]['fields'].get(field) != saved[identity]['fields'][field]:
                        raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '这些内容后来已编辑或保护，请保留当前修改并手动核对要恢复的字段')
                    changes.append({'shot_no': str(targets[identity]['number']), 'shot_id': identity,
                                    'operation': 'replace_field', 'field': field, 'value': value})
        changed_images = {identity for identity in applied['groups']
                          if _target_images(before, identity) != _target_images(after, identity)}
        for identity in changed_images:
            if identity not in targets or targets[identity]['protected'] or _target_images(current, identity) != _target_images(after, identity):
                raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '该段已保护、删除或图片后来已变更，请在当前稿中核对后手动恢复')
        if current['stage'] == 'creative' and (changes or changed_images):
            repo = _repo(agent.script_revision, auth_token, **_scope(project))
            with repo.transaction(expected_project_version=current['project_version'], expected_revision=current['revision']):
                if changes:
                    VersionReviewService(repo).apply_agent_change_set(current['draft_id'],
                        expected_revision=current['draft_revision'], actor_id=repo.actor_id,
                        operations=[{key: c[key] for key in ('shot_no', 'field', 'value')} for c in changes],
                        command_id='workbench-undo:' + task_id)
                refs = repo.body.get('image_references', [])
                repo.body['image_references'] = [ref for ref in refs if ref['block_id'] not in changed_images] + [
                    deepcopy(ref) for ref in before['images'] if ref['block_id'] in changed_images]
        elif current['stage'] == 'formal':
            if changes:
                if any(op.get('operation') == 'source_rebind' for op in (data.get('text_subject') or {}).get('operations', [])):
                    rematch = agent.script_revision.workspace.preflight_source_rematches(auth_token, **_scope(project),
                        expected_project_version=current['project_version'], expected_revision=current['revision'],
                        operations=changes, allow_original_adaptation=True)
                    if not rematch.get('ready'):
                        raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '原技术字段暂时无法恢复对应来源，请保留当前稿并核对模板后重试')
                    changes = list(rematch.get('operations') or changes)
                agent.script_revision.workspace.apply_revision_operations(auth_token, **_scope(project),
                    expected_project_version=current['project_version'], expected_revision=current['revision'],
                    operations=changes, idempotency_key='workbench-undo:' + task_id)
            if changed_images:
                _undo_formal_images(agent, auth_token, project, before, changed_images, task_id)
        data['undone'] = True
        response = {**response, 'summary': '已撤销本次采用的变化，其他修改已保留。', 'can_undo': False, 'undone': True,
            'image_creation_requests': [], 'pending_actions': [{**action, 'status': 'adoption_undone'}
                for action in data.get('pending_actions', [])]}
        # Completed task receipts are append-only; do not reopen its state machine.
        agent.services.tasks.record_task_progress(auth_token, task_id=task_id, expected_task_version=task['task_version'],
            result_annotation=True, advance_version=True, details={'workbench': data, 'response': response})
        return read(agent, auth_token, task_id)


def _target_images(value, identity):
    if value['stage'] == 'creative':
        return [ref for ref in value['images'] if ref['block_id'] == identity]
    return [ref for ref in value['images']['selections'] if ref['shot_id'] == identity]


def _undo_formal_images(agent, auth_token, project, before, identities, task_id):
    workspace = agent.script_revision.image_workspace
    project = agent.services.projects.get_project(auth_token, project['project_id'])
    progress = workspace.read_progress(auth_token, **_scope(project), expected_project_version=project['project_version'],
        library_service=agent.default_images)
    originals = {s['shot_id']: s for s in before['images']['selections']}
    selections = []
    for identity in identities:
        old = originals.get(identity) or {}
        candidate = old.get('candidate') or {}
        selections.append({'shot_id': identity, 'image_key': candidate.get('image_key', ''),
            'library_candidate_key': candidate.get('library_candidate_key', ''), 'checked': False,
            'selection_active': bool(old.get('selection_active')), 'reuse_reason': old.get('reuse_reason', '')})
    workspace.save_progress(auth_token, **_scope(project), base_version_id=progress['base_version_id'],
        expected_project_version=project['project_version'], expected_revision=progress['revision'],
        expected_script_revision=progress['script_revision'], selections=selections,
        idempotency_key='workbench-undo-images:' + task_id, library_service=agent.default_images)


def text_projection(tasks, auth_token, *, project_id, project_version, revision, task_id, stage):
    """Load an authenticated immutable proposal; callers cannot supply fields."""
    task = tasks.get_task(auth_token, task_id)
    if task['project_id'] != project_id:
        raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '文字建议不属于当前项目')
    events = tasks.list_task_events(auth_token, task_id)
    if stage == 'creative':
        subject = next((e['details']['creative_subject'] for e in reversed(events)
                        if e.get('details', {}).get('creative_subject')), None)
        if not subject or task['status'] != 'waiting_user':
            raise WorkspaceServiceError('CREATIVE_PROPOSAL_UNAVAILABLE', '文字建议已结束，请重新预览')
        if subject['project_version'] != project_version or subject['revision'] != revision:
            raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '文字建议依据已变化，请重新预览')
        return deepcopy(subject['changes'])
    _, subject = _stored_proposal(events)
    if not subject or task['status'] not in {'proposed', 'waiting_confirmation'} or not subject.get('draft_only'):
        raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '文字建议不可用于当前配图，请重新预览')
    if subject['expected_project_version'] != project_version or subject['expected_revision'] != revision:
        raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '文字建议依据已变化，请重新预览')
    return [{'block_id': op['shot_id'], 'field': op['field'], 'after': op['value']}
            for op in subject['operations'] if op.get('operation') != 'source_rebind']
