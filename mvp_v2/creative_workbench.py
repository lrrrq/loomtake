"""Workbench orchestration for the existing Formal agent and revision services.

Task events hold proposals, never a second editable script. Every target, image
identity and write is resolved by the existing authenticated authority services.
"""
from __future__ import annotations

from copy import deepcopy
import json
import logging
import re
import threading

from .creative_revision import _repo, effective_brief_values
from .creative_draft_repository import CreativeDraftRepository
from .formal_advisor import _redact_runtime_secrets, FormalAdvisorDecision
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
_MATERIAL_ACTIONS = {'analyze_materials', 'pause_material_analysis', 'resume_material_analysis'}


class _StopRequested(Exception):
    pass


def _scope(project):
    return {key: project[key] for key in ('workspace_id', 'project_id')}


def snapshot(agent, auth_token, project, stage):
    revision = agent.script_revision
    if revision is None:
        return {'stage': 'discussion', 'revision': 0, 'project_version': project['project_version'],
            'targets': [], 'images': [], 'evidence': {}}
    if stage in {'creative', 'discussion'}:
        if project.get('current_version_id'):
            raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '项目已进入正式稿，请在当前稿继续')
        repo = CreativeDraftRepository(agent.services.store,
            artifact_root=revision.brief_adapter.service.artifacts.root.parent,
            auth_token=auth_token, **_scope(project))
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
        # Existing drafts retain their generation basis. Only an empty project
        # discussion reads the currently selected template reference.
        method_reader = getattr(getattr(agent, 'generation', None), 'read_template_method_context', None)
        if not targets and not frozen and callable(method_reader):
            try:
                evidence.update(method_reader(auth_token, **_scope(project)))
            except WorkspaceServiceError as exc:
                evidence['template_reference_error'] = {'code': exc.code, 'message': exc.message}
        return {'stage': 'creative' if targets else 'discussion', 'revision': state['revision'], 'project_version': state['project_version'],
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
    if latest['status'] == 'executing' and not data.get('stop_requested'):
        _, persisted, _ = _load(agent, auth_token, task['task_id'])
        if persisted.get('stop_requested'):
            data['stop_requested'] = True
    if data.get('stop_requested') and status != 'cancelled':
        # Save a completed provider step before stopping, so a later continuation
        # can read its existing result. No new step starts after this boundary.
        response = {**response, 'type': 'workbench_cancelled', 'can_resume': True,
            'summary': '已停止后续步骤。原稿及已准备好的结果已保留，可以读取进展后继续。'}
        _record(agent, auth_token, task, data, response, 'cancelled')
        raise _StopRequested()
    if status == 'needs_clarification' and latest['status'] == 'executing':
        status = 'waiting_user'
    if response.get('type') != 'workbench_progress':
        response.setdefault('content', (data.get('decision') or {}).get('content') or response.get('summary', ''))
        response.setdefault('sources', (data.get('director_context') or {}).get('sources', []))
        if (data.get('director_context') or {}).get('history_warnings'):
            response.setdefault('context_warnings', data['director_context']['history_warnings'])
    if response.get('type') in {'workbench_failed', 'workbench_cancelled'}:
        response.setdefault('can_resume', True)
    if data.get('material_actions') or response.get('image_creation_requests'):
        response.update(_external_results(agent, auth_token, latest, data)[0])
    details = {'workbench': data, 'response': response}
    if status and status != latest['status']:
        task = tasks.transition_task(auth_token, task_id=task['task_id'],
            expected_task_version=latest['task_version'], status=status, details=details)
    else:
        task = tasks.record_task_progress(auth_token, task_id=task['task_id'],
            expected_task_version=latest['task_version'], details=details,
            result_annotation=latest['status'] in {'completed', 'partially_completed', 'cancelled'})
    return {'task': task, 'response': response}


def start(agent, auth_token, agent_session_id, *, content, context, operation_key, continuation=None):
    if not isinstance(operation_key, str) or not 8 <= len(operation_key) <= 160:
        raise WorkspaceServiceError('INVALID_INPUT', '请保留输入并重新提交本次操作')
    session = agent.services.tasks.get_agent_session(auth_token, agent_session_id)
    project = agent.services.projects.get_project(auth_token, session['project_id'])
    if not isinstance(content, str) or not content.strip() or len(content) > 16000:
        raise WorkspaceServiceError('INVALID_INPUT', '请填写完整要求，单条最多 16000 字')
    request = {'content': str(content).strip(), 'context': deepcopy(context), 'operation_key': operation_key}
    incoming_digest = sha256_json(request)
    with agent.services.store.atomic() as conn:
        actor = require_permission(conn, auth_token, **_scope(project), permission='project.read')
        previous = conn.execute("""SELECT t.task_id, e.details_json FROM tasks t JOIN task_events e ON e.task_id=t.task_id
            WHERE t.project_id=? AND t.actor_id=? AND t.agent_session_id=? AND json_extract(e.details_json,'$.workbench.operation_key')=?
            ORDER BY t.created_at DESC, e.sequence_no DESC LIMIT 1""",
            (project['project_id'], actor['actor_id'], agent_session_id, operation_key)).fetchone()
        if previous:
            old = json.loads(previous['details_json'])['workbench']
            if old['request_sha256'] != incoming_digest:
                raise WorkspaceServiceError('IDEMPOTENCY_CONFLICT', '同一请求不能换成其他范围或内容')
            return read(agent, auth_token, previous['task_id'])
        # These complete utterances control this session's pending work. They
        # are not a keyword route for creative instructions mentioning a pause.
        if re.fullmatch(r'(?:请)?(?:先)?(?:停一下|停一停|暂停一下|停止(?:当前回答|生成|后续步骤)?|暂停)(?:吧)?[。！!\s]*', request['content']):
            return _stop_session_work(agent, auth_token, session, project, request, incoming_digest)
        value = snapshot(agent, auth_token, project,
            context['stage'] if context else 'formal' if project.get('current_version_id') else 'creative')
        if context is None:
            context = {'stage': value['stage'], 'scope': 'all', 'current_target_id': '', 'selected_target_ids': [],
                'expected_project_version': project['project_version'], 'expected_revision': value['revision']}
        else:
            context = deepcopy(context)
            context['stage'] = value['stage']
        request['context'] = context
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
        if context['scope'] == 'current' and not current or context['scope'] == 'selected' and not selected:
            raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '请先选择当前段落或本次要修改的段落')
        data = {**_scope(project), 'operation_key': operation_key, 'request_sha256': incoming_digest,
            'request': request, 'before': value, 'basis': _basis(value), 'text_task_id': None, 'image_task_id': None}
        data['source_selection'] = session.get('selected_source_ids')
        if continuation:
            data['continuation_of_task_id'] = continuation['task_id']
            data['continuation'] = deepcopy(continuation)
        task = agent.services.tasks.create_task(auth_token, agent_session_id=agent_session_id,
            task_type='formal_production_advisor', payload_sha256=incoming_digest,
            expected_project_version=project['project_version'])
        agent.services.tasks.update_agent_session(auth_token, agent_session_id,
            **({'title': request['content'][:60]} if not session.get('title') else {}), draft_content='')
        response = {'type': 'workbench_progress', 'task_id': task['task_id'], 'stage': value['stage'],
            'summary': '正在读取项目依据和此前交流。', 'progress': {'completed': 0, 'total': 3}}
        return _record(agent, auth_token, task, data, response, 'understanding')


def _stop_session_work(agent, auth_token, session, project, request, incoming_digest):
    tasks, stopped = agent.services.tasks, []
    for current in tasks.list_agent_session_tasks(auth_token, session['agent_session_id']):
        if current['status'] not in {'understanding', 'executing', 'waiting_user', 'needs_clarification'}:
            continue
        try:
            _load(agent, auth_token, current['task_id'])
        except WorkspaceServiceError as exc:
            if exc.code == 'INVALID_INPUT':
                continue
            raise
        result = cancel(agent, auth_token, current['task_id'])
        stopped.append({'task_id': current['task_id'], 'status': result['task']['status']})
    task = tasks.create_task(auth_token, agent_session_id=session['agent_session_id'],
        task_type='formal_production_advisor', payload_sha256=incoming_digest,
        expected_project_version=project['project_version'])
    data = {**_scope(project), 'operation_key': request['operation_key'],
        'request_sha256': incoming_digest, 'request': request, 'stopped_tasks': stopped}
    response = {'type': 'read_response', 'task_id': task['task_id'], 'stopped_tasks': stopped,
        'summary': ('已请求停止当前会话的后续步骤，正在处理的请求返回后会保留结果。已采用修改保持原样。'
            if any(item['status'] == 'executing' for item in stopped) else
            '已停止当前会话的待办建议，原稿和已准备的结果保留。已采用修改保持原样。' if stopped else
            '当前没有正在处理的步骤。已保存的讨论与已采用修改保持原样。')}
    _record(agent, auth_token, task, data, response, 'executing')
    tasks.update_agent_session(auth_token, session['agent_session_id'], draft_content='')
    return _record(agent, auth_token, task, data, response, 'completed')


def _resolve_action(action, before, ui):
    if action['kind'] in _MATERIAL_ACTIONS:
        return []
    targets = before['targets']
    if not targets and before['stage'] == 'discussion' and action['kind'] in {'revise_brief', 'replan'}:
        return []
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


def _duration_edit_ids(actions):
    """Duration authority can be carried by a text action or an alignment action."""
    return list(dict.fromkeys(identity for action in actions
        if action['kind'] == 'align_duration'
        or action['kind'] == 'revise_text' and 'duration_text' in action['fields']
        for identity in action['target_ids']))


def _formal_text_proposal(agent, auth_token, task, project, before, actions, parsed, target=None, *, original_instruction='', director_context=None):
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
        analysis = analyze_formal_script_instruction(instruction=instruction, shots=rows,
            assistant_locked_shot_ids=[row['shot_id'] for row in rows if row['assistant_locked']],
            selected_shot_ids=selected, editor_policy=revision.assistant_editor_policy,
            allowed_fields_by_shot=permitted_fields,
            context={**before['evidence'], **(director_context or {}),
                'revision_constraints': deepcopy(parsed.get('constraints', {})),
                'recent_conversation': [*(director_context or {}).get('conversation', []),
                    {'role': 'user', 'content': original_instruction}]})
        if analysis.get('type') != 'rewrite_operations' and analysis.get('analysis_code') != 'script_no_changes':
            return None, analysis
        for item in analysis.get('operations', []):
            row = by_id[item['shot_id']]
            operations.append({**item, 'operation': item.get('operation', 'replace_field'), 'shot_no': row['shot_no'],
                **({'value': int(item['value'])} if item['field'] == 'duration_seconds' else {})})
    editable = set(_duration_edit_ids(actions)) if target is not None else set()
    if editable:
        proposed = {op['shot_id']: op['value'] for op in operations if op['field'] == 'duration_seconds'}
        preferred = [proposed.get(row['shot_id'], row['duration_seconds']) for row in rows]
        fixed = {index: row['duration_seconds'] for index, row in enumerate(rows)
                 if row['shot_id'] not in editable or row['assistant_locked']}
        try:
            durations, _ = _shot_durations({'视频时长': str(target)}, [[] for _ in rows], [],
                preferred_durations=preferred, fixed_durations=fixed)
        except ValueError as exc:
            return None, {'type': 'needs_clarification', 'summary': str(exc)}
        operations = [op for op in operations
            if op['field'] != 'duration_seconds' or op['shot_id'] not in editable]
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
            result = _execute(agent, auth_token, task, data, response)
            _launch_material_workers(agent, auth_token, task_id)
            return result
        except _StopRequested:
            return read(agent, auth_token, task_id)
        except Exception as exc:
            _LOG.exception('Workbench task %s failed', task_id)
            response.update(type='workbench_failed', summary=getattr(exc, 'message', '本次分析未完成，原稿和已保存图片已保留。请重新读取后重试。'),
                error_code=getattr(exc, 'code', 'WORKBENCH_INTERRUPTED'))
            try:
                return _record(agent, auth_token, task, data, response, 'failed')
            except _StopRequested:
                return read(agent, auth_token, task_id)


def _execute(agent, auth_token, task, data, response):
    before, request = data['before'], data['request']
    project = agent.services.projects.get_project(auth_token, data['project_id'])
    current = snapshot(agent, auth_token, project, before['stage'])
    if _basis(current) != data['basis']:
        raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '分析开始前稿件已变化，请重新预览')
    ui = request['context']
    numbers = {target['id']: target['number'] for target in before['targets']}
    from .director_conversation import project_context
    context = project_context(agent, auth_token, task['agent_session_id'],
        source_selection=data.get('source_selection'), selection_explicit=True, instruction=request['content'],
        exclude_task_id=task['task_id'], for_model=True)
    frozen = deepcopy(before['evidence'])
    context.update(latest_project_brief=deepcopy(context.get('brief')), frozen_basis=frozen,
        mode='creative_workbench', **frozen,
        snapshot={'stage': before['stage'],
        'targets': [{key: value for key, value in row.items() if key != 'id'} for row in before['targets']],
        'ui_scope': {'mode': ui['scope'], 'current_number': numbers.get(ui.get('current_target_id')),
                     'selected_numbers': [numbers[i] for i in ui.get('selected_target_ids', [])]}})
    context = agent.project_knowledge.project_convention_context(
        auth_token, project['project_id'], context=context)
    data['director_context'] = context
    if agent.generation is not None:
        material = _session_material_batch(agent, auth_token, task, data)
        if material.get('response'):
            context['material_analysis'] = {key: material['response'].get(key) for key in
                ('status', 'summary', 'progress', 'can_pause', 'can_resume', 'basis_changed', 'new_pending_count')}
    # A summary provider may have been in flight when the user stopped. Retain
    # its result but re-read cancellation before starting another model call.
    if _load(agent, auth_token, task['task_id'])[1].get('stop_requested'):
        data['stop_requested'] = True
        _record(agent, auth_token, task, data, response)
    continuation = data.get('continuation') or {}
    context['continuation'] = {key: value for key, value in continuation.items()
        if key in {'task_id', 'completed_group_ids', 'handoff_receipts', 'completed_actions'}}
    decision = (FormalAdvisorDecision.model_validate(continuation['decision'])
        if continuation.get('decision') else agent._decision(request['content'], context))
    parsed = decision.model_dump()
    data['decision'] = parsed
    if decision.intent == 'needs_clarification' or not parsed.get('actions'):
        result = {'type': 'needs_clarification' if decision.intent == 'needs_clarification' else 'read_response',
            'summary': decision.summary, 'questions': decision.questions, 'choices': parsed.get('choices', []),
            'constraints': parsed.get('constraints', {}), 'task_id': task['task_id']}
        return _record(agent, auth_token, task, data, result,
                       'needs_clarification' if decision.intent == 'needs_clarification' else 'completed')
    if not agent.writes_enabled or agent.script_revision is None:
        return _record(agent, auth_token, task, data, {'type': 'read_response',
            'summary': '当前只能讨论和查看建议。修改入口尚未启用，原稿已保留。',
            'content': decision.content or decision.summary, 'task_id': task['task_id']}, 'completed')
    with agent.services.store.connection() as connection:
        require_permission(connection, auth_token, **_scope(project), permission='project.update')
    actions = []
    for index, action in enumerate(parsed['actions']):
        identities = _resolve_action(action, before, ui)
        actions.append({**action, 'action_index': index, 'target_ids': identities,
            'target_labels': [f"第 {numbers[identity]} {'段' if before['stage'] == 'creative' else '镜'}" for identity in identities]})
    data['actions'] = actions
    handoff_actions = [action for action in actions if action['kind'] in {'revise_brief', 'replan'}]
    if handoff_actions:
        blocked = {action['action_index'] for action in handoff_actions}
        for action in actions:
            if set(action['depends_on']) & blocked:
                blocked.add(action['action_index'])
        ready = [action for action in actions if action['action_index'] not in blocked]
        data['waiting_actions'] = [{**action, 'status': 'awaiting_handoff'} for action in actions
            if action['action_index'] in blocked and action['kind'] not in {'revise_brief', 'replan'}]
        notice = _special_actions(agent, auth_token, task, data, parsed, handoff_actions, prepare_only=bool(ready))
        if not ready:
            return notice
        actions = ready
    actions = _prepare_material_actions(agent, auth_token, task, data, actions)
    if not actions and data.get('material_actions'):
        result = {**_action_response(data, task['task_id']), 'type': 'workbench_material_analysis',
            'summary': '素材分析已交给原批次处理，已完成结果保留；本会话持续显示实际进展。',
            'pending_actions': deepcopy(data.get('waiting_actions', []))}
        saved = _record(agent, auth_token, task, data, result, 'waiting_user')
        return read(agent, auth_token, saved['task']['task_id'])
    # A following action cannot consume an image that has only been requested.
    creation_indices = {action['action_index'] for action in actions if action['kind'] == 'create_image'}
    actions = _defer_external_dependents(data, actions, creation_indices, 'awaiting_image_adoption')
    data['prepared_action_indices'] = [action['action_index'] for action in actions]
    text_actions = [a for a in actions if a['kind'] in {'revise_text', 'align_duration'}]
    image_actions = [a for a in actions if a['kind'] == 'find_images']
    creation_actions = [a for a in actions if a['kind'] == 'create_image']
    data['pending_actions'] = [{**action, 'status': 'awaiting_adoption'} for action in creation_actions]
    if creation_actions and not text_actions and not image_actions:
        requests = _image_creation_requests(agent, auth_token, task, data, creation_actions)
        result = {**_action_response(data, task['task_id']), 'type': 'image_creation_requests',
            'summary': '已按所选对象保留生图要求，请逐项核对后在原入口生成。', 'image_creation_requests': requests,
            'pending_actions': deepcopy(data.get('waiting_actions', []))}
        return _record(agent, auth_token, task, data, result, 'completed')
    duration_actions = [a for a in actions if a['kind'] == 'align_duration']
    if duration_actions or parsed.get('constraints', {}).get('total_seconds') is not None:
        target, notice = _duration_target(before, parsed, project)
        if notice:
            data['step_notice'] = notice
            _pause_actions(data, actions, text_actions,
                WorkspaceServiceError('DURATION_TARGET_UNCLEAR', notice['summary']))
            data['text_blocked'] = True
        else:
            data['duration_target_seconds'] = target
    response.update(summary='正在按范围生成文字差异。', progress={'completed': 1, 'total': 3})
    _record(agent, auth_token, task, data, response)
    text_subject, text_public = None, {}
    if text_actions and not data.get('text_blocked'):
        try:
            text_subject, text_public = _prepare_text(agent, auth_token, task, data, project,
                text_actions, image_actions, creation_actions)
        except _StopRequested:
            raise
        except Exception as exc:
            _pause_actions(data, actions, text_actions, exc)
    blocked_indices = {action['action_index'] for action in data.get('waiting_actions', [])}
    image_actions = [action for action in image_actions if action['action_index'] not in blocked_indices]
    data['pending_actions'] = [action for action in data['pending_actions']
        if action['action_index'] not in blocked_indices]
    data.update(text_subject=text_subject, text_public=text_public)
    response.update(summary='正在根据提议的画面寻找参考图。' if image_actions else '正在核对修改范围。',
                    progress={'completed': 2, 'total': 3})
    _record(agent, auth_token, task, data, response)
    image_public = {}
    if image_actions:
        try:
            project = agent.services.projects.get_project(auth_token, data['project_id'])
            current = snapshot(agent, auth_token, project, before['stage'])
            selected = list(dict.fromkeys(identity for action in image_actions for identity in action['target_ids']))
            extra = ({'target_kind': 'creative_block', 'selected_block_ids': selected,
                      'expected_creative_revision': current['revision']} if before['stage'] == 'creative'
                     else {'selected_shot_ids': selected})
            image_instruction = '为所选段落寻找参考图。' + '；'.join(a['instruction'] for a in image_actions)
            if continuation.get('reuse_image_task_id'):
                data['image_task_id'] = continuation['reuse_image_task_id']
            else:
                result = agent.script_revision.start_image_batch(auth_token, **_scope(project),
                    instruction=image_instruction[:4000], expected_project_version=project['project_version'],
                    idempotency_key='workbench-images:' + task['task_id'], agent_session_id=task['agent_session_id'],
                    projection_task_id=data['text_task_id'] if text_subject else None, workbench_parent_task_id=task['task_id'],
                    reuse_candidates_task_id=continuation.get('seed_image_task_id'), **extra)
                data['image_task_id'] = result['task']['task_id']
            _record(agent, auth_token, task, data, response)
            result = agent.script_revision.execute_image_batch(auth_token, task_id=data['image_task_id'])
            image_public = result['response']
            if result['task']['status'] == 'failed':
                raise WorkspaceServiceError(image_public.get('error_code', 'WORKBENCH_IMAGES_NOT_READY'),
                    image_public.get('summary') or '配图步骤尚未完成')
        except _StopRequested:
            raise
        except Exception as exc:
            if data.get('image_task_id'):
                from .formal_image_batch import _load as load_image_batch
                _, _, image_public = load_image_batch(agent.script_revision, auth_token, data['image_task_id'])
            _pause_actions(data, actions, image_actions, exc)
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
    if not groups and not data.get('waiting_actions') and text_public.get('type') == 'no_changes':
        return _record(agent, auth_token, task, data, {**text_public, **_action_response(data, task['task_id'])}, 'completed')
    group_ids = {group['group_id'] for group in groups}
    by_index = {action['action_index']: action for action in data['actions']}
    for group in groups:
        group['depends_on'] = list(dict.fromkeys(identity for action in actions
            if group['target_id'] in action['target_ids']
            for dependency in action['depends_on']
            if group['target_id'] not in by_index[dependency]['target_ids']
            for identity in by_index[dependency]['target_ids'] if identity in group_ids))
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
    ready_images = {item['target_id'] for item in image_public.get('shots', [])}
    waiting_indices = {action['action_index'] for action in data.get('waiting_actions', [])}
    data['prepared_targets_by_action'] = {str(action['action_index']): [identity for identity in action['target_ids']
        if (identity in ready_images if action['kind'] == 'find_images' else action['action_index'] not in waiting_indices)]
        for action in actions}
    project = agent.services.projects.get_project(auth_token, data['project_id'])
    data.update(groups=groups, text_subject=text_subject, text_public=text_public, image_public=image_public,
                ready_basis=_basis(snapshot(agent, auth_token, project, before['stage'])))
    digest = sha256_json(data)
    response = {**_action_response(data, task['task_id']), 'type': 'workbench_proposal',
        'summary': decision.summary, 'proposal_digest': digest, 'groups': groups,
        'constraints': parsed.get('constraints', {}), 'stale': False,
        'pending_actions': [*data.get('waiting_actions', []), *data['pending_actions']],
        'duration_status': text_public.get('duration_status'), 'progress': {'completed': 3, 'total': 3}}
    data['proposal_digest'] = digest
    if data.get('waiting_actions'):
        response['can_resume'] = True
        response['summary'] = ('可完成的独立步骤已准备好；其余步骤和原因已保留，请核对后继续。'
            if groups else '本次步骤尚未完成，原稿已保留。请查看原因后继续。')
        if not groups:
            response.update(data.get('step_notice') or {}, type='needs_clarification')
    return _record(agent, auth_token, task, data, response, 'waiting_user')


def _pause_actions(data, actions, failed_actions, exc):
    failed = {action['action_index'] for action in failed_actions}
    blocked = set(failed)
    for action in actions:
        if set(action['depends_on']) & blocked:
            blocked.add(action['action_index'])
    waiting = {action['action_index']: action for action in data.get('waiting_actions', [])}
    for action in actions:
        if action['action_index'] in blocked:
            waiting[action['action_index']] = {**action,
                'status': 'failed' if action['action_index'] in failed else 'awaiting_dependency',
                'error_code': getattr(exc, 'code', 'WORKBENCH_STEP_FAILED'),
                'summary': getattr(exc, 'message', '此步骤未完成，原稿已保留，请读取进展后继续。')}
    data['waiting_actions'] = list(waiting.values())
    data['prepared_action_indices'] = [index for index in data.get('prepared_action_indices', []) if index not in blocked]


def _session_material_batch(agent, auth_token, task, data):
    """Prefer this conversation's recorded batch; never guess from a model ID."""
    reader = getattr(agent.generation, 'read_image_analysis_batch', None)
    if not callable(reader):
        return {'task': None, 'response': None}
    with agent.services.store.connection() as connection:
        row = connection.execute("""SELECT json_extract(e.details_json,'$.workbench.material_actions') AS actions
            FROM task_events e JOIN tasks t ON t.task_id=e.task_id
            WHERE t.agent_session_id=? AND t.actor_id=?
              AND json_array_length(e.details_json,'$.workbench.material_actions')>0
            ORDER BY e.rowid DESC LIMIT 1""", (task['agent_session_id'], task['actor_id'])).fetchone()
    references = json.loads(row['actions']) if row else []
    return reader(auth_token, **_scope(data),
        **({'task_id': references[-1]['task_id']} if references else {}))


def _defer_external_dependents(data, actions, unfinished, status):
    blocked = set(unfinished)
    waiting = {action['action_index']: action for action in data.get('waiting_actions', [])}
    for action in actions:
        if set(action['depends_on']) & blocked:
            blocked.add(action['action_index'])
            waiting[action['action_index']] = {**action, 'status': status,
                'summary': '前置任务的实际结果尚未完成，原要求保留；完成后可在本会话继续。'}
    data['waiting_actions'] = list(waiting.values())
    return [action for action in actions if action['action_index'] not in blocked - set(unfinished)]


def _prepare_material_actions(agent, auth_token, task, data, actions):
    """Only orchestrate the canonical batch; its service owns all image writes."""
    material = [action for action in actions if action['kind'] in _MATERIAL_ACTIONS]
    if not material:
        return actions
    completed, unfinished = set(), set()
    for action in material:
        if not set(action['depends_on']) <= completed:
            _pause_actions(data, actions, [action], WorkspaceServiceError('WORKBENCH_DEPENDENCY_PENDING',
                '素材操作等待前置步骤的实际确认，原要求已保留。'))
            unfinished.add(action['action_index'])
            continue
        try:
            if action['kind'] == 'analyze_materials':
                project = agent.services.projects.get_project(auth_token, data['project_id'])
                batch = agent.generation.start_image_analysis_batch(auth_token, **_scope(data),
                    expected_project_version=project['project_version'],
                    idempotency_key=f"workbench-materials:{task['task_id']}:{action['action_index']}",
                    agent_session_id=task['agent_session_id'], workbench_parent_task_id=task['task_id'])
            else:
                batch = _session_material_batch(agent, auth_token, task, data)
                if not batch.get('task'):
                    raise WorkspaceServiceError('WORKBENCH_MATERIAL_BATCH_MISSING',
                        '本项目还没有可继续或暂停的素材批次。可先发起分析本次待处理图片。')
                command = (agent.generation.pause_image_analysis_batch if action['kind'] == 'pause_material_analysis'
                           else agent.generation.resume_image_analysis_batch)
                batch = command(auth_token, task_id=batch['task']['task_id'])
            data.setdefault('material_actions', []).append({'action_index': action['action_index'],
                'command': action['kind'], 'task_id': batch['task']['task_id']})
            if action['kind'] != 'pause_material_analysis' and batch['task']['status'] == 'executing':
                data.setdefault('material_worker_task_ids', []).append(batch['task']['task_id'])
            done = (batch['task']['status'] != 'executing' if action['kind'] == 'pause_material_analysis'
                    else batch['task']['status'] in {'completed', 'partially_completed'})
            (completed if done else unfinished).add(action['action_index'])
        except WorkspaceServiceError as exc:
            _pause_actions(data, actions, [action], exc)
            unfinished.add(action['action_index'])
    ready = _defer_external_dependents(data, actions, unfinished, 'awaiting_material_analysis')
    waiting = {action['action_index'] for action in data.get('waiting_actions', [])}
    return [action for action in ready if action['kind'] not in _MATERIAL_ACTIONS
            and action['action_index'] not in waiting]


def _launch_material_workers(agent, auth_token, task_id):
    # Start only after the parent relationship has been durably recorded. The
    # canonical batch lock prevents a second window from dispatching duplicates.
    _, data, _ = _load(agent, auth_token, task_id)
    if data.get('stop_requested'):
        return
    for child_id in dict.fromkeys(data.get('material_worker_task_ids', [])):
        child = agent.services.tasks.get_task(auth_token, child_id)
        if child['status'] == 'executing':
            threading.Thread(target=agent.generation.execute_image_analysis_batch,
                args=(auth_token,), kwargs={'task_id': child_id}, daemon=True).start()


def _external_results(agent, auth_token, task, data):
    """Project durable child results into the original conversation on every read."""
    material, creations, completed = [], [], set()
    for reference in data.get('material_actions', []):
        try:
            result = agent.generation.read_image_analysis_batch(auth_token, **_scope(data), task_id=reference['task_id'])
            public = {**result['response'], 'action_index': reference['action_index'], 'command': reference['command']}
            done = (public['status'] != 'executing' if reference['command'] == 'pause_material_analysis'
                    else public['status'] in {'completed', 'partially_completed'})
            if done:
                completed.add(reference['action_index'])
        except WorkspaceServiceError as exc:
            public = {**reference, **_scope(data), 'status': 'unavailable', 'summary': exc.message,
                'error_code': exc.code, 'can_pause': False, 'can_resume': False}
        material.append(public)
    with agent.services.store.connection() as connection:
        children = connection.execute("""SELECT DISTINCT t.task_id FROM tasks t JOIN task_events e ON e.task_id=t.task_id
            WHERE t.project_id=? AND t.agent_session_id=? AND t.actor_id=? AND t.task_type='formal_image_creation'
              AND json_extract(e.details_json,'$.workbench_parent_task_id')=?
            ORDER BY t.created_at,t.task_id""", (task['project_id'], task['agent_session_id'], task['actor_id'], task['task_id'])).fetchall()
    for child in children:
        value = agent.generation.read_image_creation(auth_token, **_scope(data), task_id=child['task_id'])
        value['action_index'] = value['workbench_action_index']
        receipts = value.get('adoption_receipts') or []
        if receipts:
            value['adoption'] = deepcopy(receipts[-1])
        creations.append(value)
    for action in data.get('actions', []):
        if action['kind'] != 'create_image':
            continue
        adopted = {value['target']['id'] for value in creations if value['action_index'] == action['action_index']
            and any(receipt.get('currently_applied') and receipt['status'] in {'reference_saved', 'progress_saved'}
                    for receipt in value.get('adoption_receipts', []))}
        if action['target_ids'] and set(action['target_ids']) <= adopted:
            completed.add(action['action_index'])
    return {'material_analysis_batches': material, 'image_creation_results': creations,
        'external_tasks_running': any(item['status'] == 'executing' for item in material)
            or any(item.get('is_running') for item in creations)}, completed


def _prepare_text(agent, auth_token, task, data, project, text_actions, image_actions, creation_actions):
    before, request = data['before'], data['request']
    numbers = {target['id']: target['number'] for target in before['targets']}
    parsed, continuation = data['decision'], data.get('continuation') or {}
    duration_target_ids = (_duration_edit_ids(text_actions)
        if data.get('duration_target_seconds') is not None else [])
    text_subject, text_public = None, {}
    permitted = list(dict.fromkeys(identity for action in text_actions for identity in action['target_ids']))
    if not permitted:
        raise WorkspaceServiceError('SCRIPT_TARGET_LOCKED', '所选内容都已保护，请调整选择或先解除对应保护')
    instruction = request['content'] + '\n本次文字动作：' + json.dumps([
        {**{key: value for key, value in action.items() if key not in {'target_ids', 'depends_on'}},
         '允许段落或镜号': [numbers[i] for i in action['target_ids']]} for action in text_actions], ensure_ascii=False)
    instruction += '\n保留与禁止要求：' + json.dumps(parsed.get('constraints', {}), ensure_ascii=False)
    revision = agent.script_revision
    if continuation.get('reuse_text_task_id'):
        data['text_task_id'] = continuation['reuse_text_task_id']
    elif before['stage'] == 'creative':
        permitted_fields = {identity: list(dict.fromkeys(field for action in text_actions if identity in action['target_ids']
            for semantic in (action['fields'] or ['duration_text'])
            for field in ['visual' if semantic == 'subtitle' else semantic])) for identity in permitted}
        result = revision.propose_creative_draft_change(auth_token, **_scope(project),
            expected_project_version=project['project_version'], expected_revision=before['revision'],
            instruction=instruction, selected_block_ids=permitted,
            operation_key='workbench-text:' + task['task_id'], agent_session_id=task['agent_session_id'],
            workbench_parent_task_id=task['task_id'],
            input_updates=[], answer_ids=[], allowed_fields_by_shot=permitted_fields,
            conversation_context=data.get('director_context'),
            **({'duration_target_ids': duration_target_ids,
                'duration_target_seconds': data['duration_target_seconds']} if duration_target_ids else {}))
        data['text_task_id'] = result['task_id']
    else:
        text_task_id, notice = _formal_text_proposal(agent, auth_token, task, project,
            before, text_actions, parsed, data.get('duration_target_seconds'), original_instruction=request['content'],
            director_context=data.get('director_context'))
        if notice and (notice.get('type') != 'no_changes' or not image_actions and not creation_actions):
            if data.get('duration_target_seconds') is not None:
                notice['duration_status'] = {'target_seconds': data['duration_target_seconds'],
                    'actual_seconds': _duration_total(before),
                    'status': 'matched' if _duration_total(before) == data['duration_target_seconds'] else 'mismatch'}
            if notice.get('type') == 'no_changes':
                return None, notice
            data['step_notice'] = notice
            raise WorkspaceServiceError('WORKBENCH_TEXT_NOT_READY', notice.get('summary') or '文字建议尚待明确')
        text_public = notice or {}
        data['text_task_id'] = text_task_id
    if data.get('text_task_id'):
        text_subject, text_public = _text_subject(agent, auth_token, data['text_task_id'], before['stage'])
    if not text_subject and text_public.get('type') != 'no_changes':
        data['step_notice'] = text_public
        raise WorkspaceServiceError('WORKBENCH_TEXT_NOT_READY',
            text_public.get('summary') or '没有可采用的文字修改，请明确要求后重试。')
    _validate_changes(_changes(text_subject, before), text_actions, before)
    return text_subject, text_public


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


def _special_actions(agent, auth_token, task, data, parsed, actions, *, prepare_only=False):
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
    data['handoff_basis'] = _handoff_basis(agent, auth_token, data)
    data['pending_actions'] = []
    if prepare_only:
        return None
    response = {**_action_response(data, task['task_id']), 'type': 'workbench_handoff',
        'summary': '本次要求涉及需求或规划，请先到原入口核对并确认；其余动作与依赖已保留，当前稿未修改。',
        'questions': parsed.get('questions', []), 'handoff': handoffs[0], 'handoffs': handoffs,
        'pending_actions': data.get('waiting_actions', []), 'next_href': handoffs[0]['next_href'],
        'next_label': handoffs[0]['next_label'],
        'next_steps': ['完成对应需求或规划核对后，按当前稿重新预览保留的后续动作。']}
    return _record(agent, auth_token, task, data, response, 'waiting_user')


def _action_response(data, task_id):
    return {**_scope(data), 'task_id': task_id, 'stage': data['before']['stage'],
        'original_instruction': data['request']['content'], 'actions': deepcopy(data.get('actions', [])),
        'constraints': deepcopy(data.get('decision', {}).get('constraints', {})),
        **({'handoffs': deepcopy(data['handoffs']), 'handoff': deepcopy(data['handoffs'][0])}
            if data.get('handoffs') else {})}


def _handoff_basis(agent, auth_token, data):
    project = agent.services.projects.get_project(auth_token, data['project_id'])
    brief = agent.script_revision.brief_adapter.service.read_current_formal(auth_token, **_scope(project))
    return {'brief_version_id': brief.get('brief_version_id'),
        'confirmation_status': brief.get('confirmation_status'),
        'current_version_id': project.get('current_version_id'),
        'project_version': project['project_version']}


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


def _continuation_task(agent, auth_token, task):
    with agent.services.store.connection() as connection:
        row = connection.execute("""SELECT t.task_id FROM tasks t JOIN task_events e ON e.task_id=t.task_id
            WHERE t.agent_session_id=? AND t.actor_id=?
              AND json_extract(e.details_json,'$.workbench.continuation_of_task_id')=?
            ORDER BY t.created_at DESC LIMIT 1""",
            (task['agent_session_id'], task['actor_id'], task['task_id'])).fetchone()
    return agent.services.tasks.get_task(auth_token, row['task_id']) if row else None


def cancel(agent, auth_token, task_id):
    with agent.services.store.atomic():
        task, data, response = _load(agent, auth_token, task_id)
        for reference in data.get('material_actions', []):
            agent.generation.pause_image_analysis_batch(auth_token, task_id=reference['task_id'])
        if task['status'] in {'completed', 'cancelled', 'failed', 'partially_completed'}:
            return read(agent, auth_token, task_id)
        data['stop_requested'] = True
        if task['status'] == 'executing':
            response.update(summary='已请求停止，当前步骤返回后将保留结果并停止后续步骤。', stop_requested=True)
            task = agent.services.tasks.record_task_progress(auth_token, task_id=task_id,
                expected_task_version=task['task_version'], advance_version=True,
                details={'workbench': data, 'response': response})
            return {'task': task, 'response': response}
        response.update(type='workbench_cancelled', can_resume=True,
            summary='已停止本次建议，原稿和已准备好的内容已保留。')
        return _record(agent, auth_token, task, data, response, 'cancelled')


def record_handoff(agent, auth_token, task_id, *, project_id, kind, result):
    """Called only after the ordinary business confirmation has succeeded."""
    with agent.services.store.atomic():
        task, data, response = _load(agent, auth_token, task_id)
        if task['project_id'] != project_id:
            raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '确认结果与编导任务不是同一项目')
        handoffs = [item for item in data.get('handoffs', []) if item['kind'] == kind]
        if not handoffs:
            raise WorkspaceServiceError('INVALID_INPUT', '本次任务没有对应的待确认步骤')
        current = _handoff_basis(agent, auth_token, data)
        basis = data['handoff_basis']
        receipts = data.setdefault('handoff_receipts', {})
        if any(dependency not in {int(index) for index in receipts}
               for handoff in handoffs for dependency in handoff.get('depends_on', [])
               if any(item['action_index'] == dependency for item in data.get('handoffs', []))):
            raise WorkspaceServiceError('INVALID_INPUT', '前置交接尚未确认，请先完成原有依赖步骤')
        if kind == 'brief':
            if current['confirmation_status'] != 'confirmed' or current['brief_version_id'] != result.get('brief_version_id'):
                raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '确认结果与当前需求版本不一致，请重新读取')
            if current['brief_version_id'] == basis['brief_version_id'] and basis['confirmation_status'] == 'confirmed':
                raise WorkspaceServiceError('INVALID_INPUT', '尚未确认新的需求修订，后续动作继续等待')
            reference = {'brief_version_id': current['brief_version_id']}
        elif kind == 'plan':
            if not current['current_version_id'] or current['current_version_id'] == basis['current_version_id']:
                raise WorkspaceServiceError('INVALID_INPUT', '尚未生成新的正式版本，后续动作继续等待')
            generated = agent.generation.completed_plan_result(auth_token, **_scope(data),
                version_id=current['current_version_id'], completed_after=task['created_at'])
            if (not generated or result.get('plan_id') != generated['plan_id']
                    or result.get('version_id') != generated['version_id']
                    or generated['brief_version_id'] != current['brief_version_id']):
                raise WorkspaceServiceError('INVALID_INPUT', '当前版本不是本次已确认规划的生成结果，后续步骤继续等待')
            reference = {'current_version_id': generated['version_id'], 'plan_id': generated['plan_id'],
                         'plan_sha256': generated['plan_sha256'], 'brief_version_id': generated['brief_version_id'],
                         **({'recovery_attempt_id': generated['recovery_attempt_id']}
                            if generated.get('recovery_attempt_id') else {})}
        else:
            raise WorkspaceServiceError('INVALID_INPUT', '交接类型无效')
        for handoff in handoffs:
            receipts[str(handoff['action_index'])] = {'kind': kind, **reference,
                'project_version': current['project_version'], 'confirmed_by_actor_id': task['actor_id']}
            handoff['completed'] = True
        for handoff in data['handoffs']:
            if str(handoff['action_index']) in receipts:
                handoff['completed'] = True
        response.update(handoff_receipts=deepcopy(receipts), can_resume=True,
            operation_receipt='原入口已确认成功。返回此会话后可按当前项目依据继续剩余步骤。')
        response['handoffs'] = deepcopy(data['handoffs'])
        response['handoff'] = deepcopy(data['handoffs'][0])
        if task['status'] == 'failed':
            raise WorkspaceServiceError('INVALID_TASK_TRANSITION', '原任务已中断，请返回会话读取进展后继续')
        saved = _record(agent, auth_token, task, data, response)
        return {'task_id': task_id, 'agent_session_id': task['agent_session_id'],
            'receipts': saved['response']['handoff_receipts']}


def resume(agent, auth_token, task_id):
    # Reconcile under the existing execution lock before creating any attempt.
    from .workbench_task_recovery import sync_workbench
    sync_workbench(agent, auth_token, task_id)
    with agent.services.store.atomic():
        task, data, response = _load(agent, auth_token, task_id)
        child = _continuation_task(agent, auth_token, task)
        if child:
            return read(agent, auth_token, child['task_id'])
        if task['status'] in {'understanding', 'executing'}:
            return read(agent, auth_token, task_id)
        current_result = read(agent, auth_token, task_id)
        task, data, response = _load(agent, auth_token, task_id)
        external, external_completed = _external_results(agent, auth_token, task, data)
        external_indices = {reference['action_index'] for reference in data.get('material_actions', [])}
        external_indices.update(action['action_index'] for action in data.get('actions', [])
            if action['kind'] == 'create_image' and any(item['action_index'] == action['action_index']
                for item in response.get('image_creation_requests', [])))
        if external_indices - external_completed:
            # Prepared independent groups remain adoptable on this original
            # proposal. A continuation must not recreate unfinished paid work.
            current_result['response']['can_resume'] = False
            return current_result
        retryable = any(action.get('status') in {'failed', 'awaiting_dependency'} for action in data.get('waiting_actions', []))
        if response.get('type') == 'workbench_proposal' and not current_result['response'].get('stale') and not retryable:
            return current_result
        project = agent.services.projects.get_project(auth_token, data['project_id'])
        current = snapshot(agent, auth_token, project, 'formal' if project.get('current_version_id') else 'creative')
        receipts = data.get('handoff_receipts', {})
        if data.get('handoffs') and task['status'] in {'waiting_user', 'completed', 'partially_completed'}:
            # A business confirmation can commit before its callback is saved.
            # Recover only from a different, currently confirmed authority version.
            actual_basis = _handoff_basis(agent, auth_token, data)
            for handoff in data['handoffs']:
                if str(handoff['action_index']) in receipts:
                    continue
                changed = (actual_basis['confirmation_status'] == 'confirmed'
                    and actual_basis['brief_version_id'] != data['handoff_basis']['brief_version_id']) if handoff['kind'] == 'brief' else (
                    actual_basis['current_version_id'] and actual_basis['current_version_id'] != data['handoff_basis']['current_version_id'])
                if changed:
                    result = actual_basis if handoff['kind'] == 'brief' else agent.generation.completed_plan_result(
                        auth_token, **_scope(data), version_id=actual_basis['current_version_id'],
                        completed_after=task['created_at'])
                    if result is None:
                        continue
                    try:
                        record_handoff(agent, auth_token, task_id, project_id=data['project_id'],
                            kind=handoff['kind'], result=result)
                    except WorkspaceServiceError:
                        continue
                    task, data, response = _load(agent, auth_token, task_id)
                    receipts = data.get('handoff_receipts', {})
        actual = _handoff_basis(agent, auth_token, data) if receipts else {}
        completed_steps = {int(index) for index, receipt in receipts.items()
            if all(receipt.get(key) == actual.get(key) for key in ('brief_version_id', 'current_version_id') if key in receipt)}
        completed_steps.update(external_completed)
        handoff_indexes = {item['action_index'] for item in data.get('handoffs', [])}
        pending_handoffs = handoff_indexes - completed_steps
        blocked = set(pending_handoffs)
        for action in data.get('actions', []):
            if set(action['depends_on']) & blocked:
                blocked.add(action['action_index'])
        adopted = set(data.get('applied', {}).get('groups', [])) if not data.get('undone') else set()
        prepared = set(data.get('prepared_action_indices', [action['action_index'] for action in data.get('actions', [])
            if action['action_index'] not in {item['action_index'] for item in data.get('waiting_actions', [])} | handoff_indexes]))
        independent_remaining = any(action['action_index'] not in blocked | completed_steps
            and set(action['target_ids']) - adopted for action in data.get('actions', []))
        if pending_handoffs and not independent_remaining:
            response.update(can_resume=True,
                summary='后续步骤仍等待原入口的有效确认。请核对需求或规划后返回，已准备和已采用的内容均保留。')
            return {'task': task, 'response': response}
        known = {target['id']: target['number'] for target in current['targets']}
        ui = deepcopy(data['request']['context'])
        old_scope = ui['scope']
        if old_scope == 'all':
            ui.update(current_target_id='', selected_target_ids=[])
        elif ((ui.get('current_target_id') and ui['current_target_id'] not in known)
                or not set(ui.get('selected_target_ids', [])) <= known.keys()):
            raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '原先选中的段落或镜头已变化。原要求已保留，请在当前稿选择范围后重新预览。')
        ui.update(stage=current['stage'], expected_project_version=current['project_version'],
            expected_revision=current['revision'])
        ui.pop('expected_progress_revision', None)
        if current['stage'] == 'formal':
            ui['expected_progress_revision'] = current['images']['revision']
        kept = []
        for action in data.get('actions', []):
            if action['action_index'] in completed_steps:
                continue
            prepared_targets = set(data.get('prepared_targets_by_action', {}).get(str(action['action_index']),
                action['target_ids'] if action['action_index'] in prepared else []))
            ids = [identity for identity in action['target_ids'] if identity not in adopted & prepared_targets]
            if not ids and not action['target_ids'] and completed_steps and old_scope == 'all' and action['target_scope'] == 'all':
                ids = list(known)
            if not set(ids) <= known.keys():
                if completed_steps and old_scope == 'all' and action['target_scope'] == 'all':
                    ids = list(known)
                else:
                    raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '原动作的对象已变化，请在当前稿重新选择范围。')
            if not ids and action['kind'] not in {'revise_brief', 'replan'} | _MATERIAL_ACTIONS:
                continue
            kept.append({**action, 'target_ids': ids})
        continuation = {'task_id': task_id, 'completed_group_ids': sorted(adopted),
            'handoff_receipts': deepcopy(receipts)}
        if data.get('decision'):
            decision = deepcopy(data['decision'])
            mapping = {action['action_index']: index for index, action in enumerate(kept)}
            decision['actions'] = [{key: value for key, value in {
                **action, 'target_scope': 'numbers' if action['target_ids'] else 'all',
                'target_numbers': [known[identity] for identity in action['target_ids']],
                'depends_on': [mapping[index] for index in action['depends_on'] if index in mapping],
            }.items() if key in {'kind', 'source_clause', 'target_scope', 'target_numbers', 'fields', 'instruction', 'depends_on'}}
                for action in kept]
            if decision['actions']:
                continuation['decision'] = decision
            elif completed_steps:
                continuation['completed_actions'] = [deepcopy(action) for action in data.get('actions', [])
                    if action['action_index'] in completed_steps]
            else:
                return current_result
        # Saved preview steps can be reused only on the exact unchanged basis.
        if not adopted and _basis(current) == data.get('ready_basis', data['basis']):
            for name in ('text', 'image'):
                child_id = data.get(name + '_task_id')
                retry_images = name == 'image' and any(action['kind'] == 'find_images' for action in data.get('waiting_actions', []))
                status = agent.services.tasks.get_task(auth_token, child_id)['status'] if child_id else ''
                if child_id and (status == 'waiting_user' or retry_images and status in {'failed', 'cancelled'}):
                    continuation[('seed_image_task_id' if retry_images else 'reuse_' + name + '_task_id')] = child_id
        return start(agent, auth_token, task['agent_session_id'], content=data['request']['content'],
            context=ui, operation_key='continue:' + task_id, continuation=continuation)


def read(agent, auth_token, task_id):
    task, data, response = _load(agent, auth_token, task_id)
    response.setdefault('original_instruction', data.get('request', {}).get('content', ''))
    if data.get('material_actions') or any(action['kind'] == 'create_image' for action in data.get('actions', [])):
        external, completed = _external_results(agent, auth_token, task, data)
        response.update(external)
        external_indices = {reference['action_index'] for reference in data.get('material_actions', [])}
        external_indices.update(item['action_index'] for item in response.get('image_creation_requests', []))
        pending = external_indices - completed
        if pending:
            response['can_resume'] = False
        elif data.get('waiting_actions'):
            response['can_resume'] = True
        if external['image_creation_results']:
            generated = sum(value['status'] == 'completed' for value in external['image_creation_results'])
            applied = sum(any(receipt.get('currently_applied') and receipt['status'] in {'reference_saved', 'progress_saved'}
                for receipt in value.get('adoption_receipts', [])) for value in external['image_creation_results'])
            response['summary'] = (f'本次生图已有 {generated} 张生成结果，{applied} 张当前用于目标。'
                + ('原要求的后续步骤可继续。' if not pending and data.get('waiting_actions') else
                   '生成与采用分别保留记录；未采用的结果可打开核对，审核状态沿用原工作区。'))
        if response.get('type') == 'workbench_material_analysis':
            response['summary'] = ('本批分析已结束，实际结果与待核对项已保留。'
                + ('可以继续原要求中的剩余步骤。' if data.get('waiting_actions') else '可在原素材区核对用途。')) if not pending else (
                '本批素材仍在处理；已完成结果保留，可暂停或返回原素材区查看。'
                if response['external_tasks_running'] else '本批素材已暂停或仍需处理，已完成结果保留。请读取原批次后继续。')
            if not pending and not data.get('waiting_actions') and task['status'] == 'waiting_user':
                response['can_resume'] = False
                return _record(agent, auth_token, task, data, response, 'completed')
    if task['status'] == 'waiting_user' and response.get('type') == 'workbench_proposal':
        project = agent.services.projects.get_project(auth_token, data['project_id'])
        current_stage = 'formal' if project.get('current_version_id') else 'creative'
        response['stale'] = _basis(snapshot(agent, auth_token, project, current_stage)) != data.get('ready_basis')
        if response['stale']:
            response['summary'] = '稿件或图片依据已变化，旧建议已保留。请按当前稿重新预览后采用。'
    if task['status'] == 'executing' and data.get('image_task_id'):
        child = agent.script_revision.read_image_batch(auth_token, **_scope(data), task_id=data['image_task_id'])
        response['image_progress'] = child['response'].get('progress')
    continuation = _continuation_task(agent, auth_token, task)
    if continuation:
        response['continuation_task_id'] = continuation['task_id']
    if response.get('type') in {'workbench_failed', 'workbench_cancelled'}:
        response['can_resume'] = True
    if data.get('handoff_receipts') and response.get('type') == 'workbench_applied' and not data.get('applied'):
        response.update(type='read_response' if continuation else 'workbench_handoff', can_resume=not bool(continuation),
            summary='原入口已确认，剩余要求已在后续记录中处理。' if continuation else '原入口已确认。请继续核对原要求中尚未完成的部分。')
    if data.get('continuation_of_task_id'):
        response['continuation_of_task_id'] = data['continuation_of_task_id']
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
        missing = {dependency for group in data['groups'] if group['group_id'] in chosen
            for dependency in group.get('depends_on', []) if dependency not in chosen}
        if missing:
            raise WorkspaceServiceError('WORKBENCH_DEPENDENCY_REQUIRED',
                '所选修改依赖尚未采用的前序组。请一起选择对应组，或取消依赖它的修改；当前稿未改变。')
        _record(agent, auth_token, task, data, response, 'executing')
        text_ids = chosen & {c['target_id'] for c in _changes(data.get('text_subject'), data['before'])}
        if data.get('text_task_id') and data.get('text_subject'):
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
        remaining = [deepcopy(group) for group in data['groups'] if group['group_id'] not in chosen]
        response.update(applied_group_ids=sorted(chosen), remaining_groups=remaining,
            remaining_group_ids=[group['group_id'] for group in remaining], can_resume=bool(remaining or data.get('handoffs') or data.get('waiting_actions')))
        ready_actions, pending_actions = _adopted_creation_actions(data, chosen)
        response['pending_actions'] = [*data.get('waiting_actions', []), *pending_actions]
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
            'applied_group_ids': [], 'remaining_groups': deepcopy(data.get('groups', [])),
            'remaining_group_ids': [group['group_id'] for group in data.get('groups', [])],
            'can_resume': bool(data.get('groups') or data.get('handoffs')),
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
