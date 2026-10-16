"""Persist one frozen material-analysis batch through the existing task authority."""

from copy import deepcopy
from dataclasses import asdict
import json
import os
from subprocess import TimeoutExpired
from uuid import uuid4

from .identity_service import require_permission
from .image_understanding import ProjectImageVisionResult
from .project_image_authority import (
    ProviderImageAnalysisInput, ProviderProjectImageAnalysisCommandService,
)
from .run_contract import sha256_json
from .run_state import try_exclusive_lock
from .workspace_store import WorkspaceServiceError


_DETAIL = 'image_analysis_batch'
_TYPE = 'formal_image_analysis'
_TERMINAL = {'completed', 'partially_completed'}
_PROCESS_OWNER = {'pid': os.getpid(), 'instance_id': uuid4().hex}


def _scope(data):
    return {key: data[key] for key in ('workspace_id', 'project_id')}


def _store(service):
    return service.services.projects.store


def _lock(service, task_id):
    return _store(service).database_path.parent / '.project-image-analysis-actions' / (task_id + '.lock')


def _image_lock(service, data, item):
    return (_store(service).database_path.parent / '.project-image-analysis-actions' /
            ('image-' + data['project_id'] + '-' + item['image_sha256'] + '.lock'))


def _load(service, auth_token, task_id):
    task = service.services.tasks.get_task(auth_token, task_id)
    if task['task_type'] != _TYPE:
        raise WorkspaceServiceError('INVALID_INPUT', '当前任务不是素材分析批次')
    events = service.services.tasks.list_task_events(auth_token, task_id, latest_with_detail=_DETAIL)
    data = next((event['details'][_DETAIL] for event in reversed(events)
                 if event.get('details', {}).get(_DETAIL)), None)
    if not data or data.get('project_id') != task['project_id']:
        raise WorkspaceServiceError('INVALID_INPUT', '素材分析批次记录缺失')
    return task, deepcopy(data)


def _public(task, data):
    items = [{key: deepcopy(item[key]) for key in (
        'image_key', 'status', 'sources', 'analysis_key', 'analysis_status',
        'error_code', 'message', 'reused', 'source_task_id') if key in item}
        for item in data['items']]
    counts = {
        'total': len(items),
        'processed': sum(item['status'] == 'completed' for item in items),
        'pending': sum(item['status'] in {'pending', 'in_flight', 'returned'} for item in items),
        'failed': sum(item['status'] == 'failed' for item in items),
        'unknown': sum(item['status'] == 'unknown' for item in items),
        'needs_review': sum(item['status'] == 'completed' and item.get('analysis_status') != 'pass'
                            for item in items),
    }
    if task['status'] == 'executing':
        summary = ('正在等待当前图片返回，完成回读后暂停后续分析。' if data.get('pause_requested') else
                   '正在分析本次图片；期间新增图片留待下次发起。')
    elif task['status'] == 'waiting_user':
        summary = data.get('pause_message') or '本批分析已暂停，已完成结果保留。'
    else:
        summary = (f"本批已处理 {counts['processed']} / {counts['total']} 张。"
                   + ('仍有未完成或结果未知项，请查看原记录或人工处理。'
                      if counts['failed'] or counts['unknown'] else '自动分析不等于人工核对通过。'))
    if data.get('basis_changed'):
        summary += '当前需求已变化；继续时保留原观察，同一批图片的用途需按当前需求重新核对。'
    return {'type': 'project_image_analysis_batch', 'task_id': task['task_id'],
        **_scope(data), 'status': task['status'], 'summary': summary, 'progress': counts,
        'items': items, 'can_pause': task['status'] == 'executing' and not data.get('pause_requested'),
        'can_resume': task['status'] == 'waiting_user',
        'resume_label': '按当前需求继续' if data.get('basis_changed') else '继续分析',
        'basis_changed': bool(data.get('basis_changed')),
        'original_binding': deepcopy(data.get('original_binding', {})),
        'current_binding': deepcopy(data.get('current_binding') or {
            key: data[key] for key in ('brief_version_id', 'brief_snapshot_id')}),
        'new_pending_count': data.get('new_pending_count', 0),
        'next_href': f"/projects/{data['project_id']}/materials",
        'error_code': data.get('error_code', '')}


def _result(task, data):
    return {'task': task, 'response': _public(task, data)}


def _record(service, auth_token, task, data, *, status=None, clear_pause=False):
    tasks = service.services.tasks
    with _store(service).atomic():
        latest, persisted = _load(service, auth_token, task['task_id'])
        if not clear_pause and persisted.get('pause_requested'):
            data['pause_requested'] = True
        next_status = status or latest['status']
        response = _public({**latest, 'status': next_status}, data)
        details = {_DETAIL: deepcopy(data), 'response': response}
        if next_status != latest['status']:
            latest = tasks.transition_task(auth_token, task_id=latest['task_id'],
                expected_task_version=latest['task_version'], status=next_status, details=details)
        else:
            latest = tasks.record_task_progress(auth_token, task_id=latest['task_id'],
                expected_task_version=latest['task_version'], details=details,
                advance_version=True, result_annotation=latest['status'] in _TERMINAL)
    return _result(latest, data)


def _current(service, auth_token, data):
    project = service.services.projects.get_project(auth_token, data['project_id'])
    if project['workspace_id'] != data['workspace_id']:
        raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '分析批次不属于当前项目')
    paired = service._resolve_current_paired(auth_token, **_scope(data))
    if (paired.brief.brief_version_id, paired.brief.brief_snapshot_id) != (
            data['brief_version_id'], data['brief_snapshot_id']):
        raise WorkspaceServiceError('IMAGE_ANALYSIS_BRIEF_CHANGED', '需求已变化，原批次结果保留，请核对后继续')
    readiness = service.services.images.resolve_for_planning(auth_token, **_scope(data),
        brief_version_id=data['brief_version_id'], brief_snapshot_id=data['brief_snapshot_id'],
        expected_project_lock_version=project['project_version'])
    return project, readiness


def _observe_binding(service, auth_token, data):
    paired = service._resolve_current_paired(auth_token, **_scope(data))
    binding = {'brief_version_id': paired.brief.brief_version_id,
               'brief_snapshot_id': paired.brief.brief_snapshot_id}
    data['current_binding'] = binding
    data['basis_changed'] = any(binding[key] != data[key] for key in binding)
    return binding


def _identity(item, readiness):
    image = next((image for image in readiness.images if image.image_id == item['image_key']), None)
    if image is None or image.image_sha256 != item['image_sha256']:
        raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '本批图片身份已变化，请核对素材后继续')
    links = [asdict(link) for link in image.source_links]
    if any(link not in links for link in item['source_links']):
        raise WorkspaceServiceError('IMAGE_SOURCE_DRIFT', '本批图片来源已变化，请核对素材后继续')
    return image


def _completed(item, analysis, *, reused=False):
    item.update(status='completed', analysis_key=analysis.analysis_version_id,
        analysis_status=analysis.status, reused=reused)
    item.pop('error_code', None)
    item.pop('message', None)


def _result_unknown(exc):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, (TimeoutError, ConnectionError, TimeoutExpired)):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _provider_failure(exc):
    if _result_unknown(exc):
        return {'status': 'unknown', 'error_code': 'IMAGE_ANALYSIS_RESULT_UNKNOWN',
            'message': '请求已发出但返回结果未知，原请求保留，不会自动重复识别。'}
    diagnostics = getattr(exc, 'diagnostics', None)
    error = diagnostics.get('error', {}) if isinstance(diagnostics, dict) else {}
    code = error.get('code') if isinstance(error, dict) else None
    if code in {'VISION_RESPONSE_TRUNCATED', 'VISION_RESPONSE_PARSE_FAILED',
                'VISION_RESPONSE_VALIDATION_FAILED'}:
        return {'status': 'failed', 'error_code': code,
            'message': '分析服务已返回，但结果格式不完整或不符合要求，未保存为有效分析；原图与已选图保留。'
                '可在原会话发起新一批素材分析，仅重试仍未完成的图片，或到素材页人工核对。'}
    return {'status': 'failed', 'error_code': getattr(exc, 'code', 'FORMAL_VISION_PROVIDER_FAILED'),
        'message': '本图片自动分析未完成；原图与已完成结果保留。'
            '可发起新一批素材分析处理未完成项，或到素材页人工核对。'}


def _prior_receipts(service, connection, project_id):
    rows = connection.execute("""SELECT t.task_id, t.actor_id, e.details_json FROM tasks t JOIN task_events e
        ON e.task_id=t.task_id WHERE t.project_id=? AND t.task_type=? AND e.sequence_no=(
            SELECT MAX(e2.sequence_no) FROM task_events e2 WHERE e2.task_id=t.task_id
            AND json_type(e2.details_json, '$.image_analysis_batch')='object')""",
        (project_id, _TYPE)).fetchall()
    pending = {}
    for row in rows:
        batch = json.loads(row['details_json'])[_DETAIL]
        for item in batch['items']:
            if item['status'] in {'in_flight', 'returned', 'unknown'}:
                pending[(item['image_key'], item['image_sha256'])] = {
                    'task_id': row['task_id'], 'actor_id': row['actor_id']}
    return pending


def start(service, auth_token, *, workspace_id, project_id, expected_project_version,
          idempotency_key, agent_session_id=None, workbench_parent_task_id=None):
    if not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 160:
        raise WorkspaceServiceError('INVALID_INPUT', '请保留本次分析请求标识后重试')
    policy = service.vision_provider_policy
    if policy is None or not callable(getattr(policy, 'invoke', None)):
        raise WorkspaceServiceError('FORMAL_VISION_PROVIDER_UNAVAILABLE', '图片自动理解服务尚未配置，可继续人工处理')
    tasks = service.services.tasks
    scope = {'workspace_id': workspace_id, 'project_id': project_id}
    request = {**scope, 'expected_project_version': expected_project_version,
               'agent_session_id': agent_session_id, 'idempotency_key': idempotency_key}
    if workbench_parent_task_id:
        request['workbench_parent_task_id'] = workbench_parent_task_id
    fingerprint = sha256_json(request)
    with _store(service).atomic() as connection:
        actor = require_permission(connection, auth_token, **scope, permission='project.update')
        require_permission(connection, auth_token, **scope, permission='review.record')
        session = tasks.get_agent_session(auth_token, agent_session_id) if agent_session_id else None
        if session and session['project_id'] != project_id:
            raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '会话不属于当前项目')
        if workbench_parent_task_id:
            parent = tasks.get_task(auth_token, workbench_parent_task_id)
            if (not session or parent['project_id'] != project_id or parent['actor_id'] != actor['actor_id']
                    or parent['agent_session_id'] != session['agent_session_id']
                    or parent['task_type'] != 'formal_production_advisor'):
                raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '素材分析与编导任务不属于同一项目会话')
        previous = connection.execute("""SELECT t.task_id,e.details_json FROM tasks t JOIN task_events e
            ON e.task_id=t.task_id WHERE t.project_id=? AND t.actor_id=? AND t.task_type=?
            AND json_extract(e.details_json,'$.image_analysis_batch.idempotency_key')=?
            ORDER BY e.rowid DESC LIMIT 1""", (project_id, actor['actor_id'], _TYPE, idempotency_key)).fetchone()
        if previous:
            old = json.loads(previous['details_json'])[_DETAIL]
            if old['request_fingerprint'] != fingerprint:
                raise WorkspaceServiceError('IDEMPOTENCY_CONFLICT', '同一分析请求不能更改范围或会话')
            return _result(tasks.get_task(auth_token, previous['task_id']), old)
        active = connection.execute("""SELECT task_id FROM tasks WHERE project_id=? AND actor_id=?
            AND task_type=? AND status IN ('received','executing','waiting_user')
            ORDER BY created_at DESC,rowid DESC LIMIT 1""", (project_id, actor['actor_id'], _TYPE)).fetchone()
        if active:
            task, data = _load(service, auth_token, active['task_id'])
            return _result(task, data)
        service._current_plan_state(auth_token, workspace_id, project_id, expected_project_version)
        paired = service._resolve_current_paired(auth_token, **scope)
        readiness = service._synchronize_and_resolve_images(auth_token, **scope, paired=paired,
            expected_project_version=expected_project_version)
        public = {item['image_key']: item for item in service.services.images.employee_readiness(readiness)['images']}
        receipts = _prior_receipts(service, connection, project_id)
        items = []
        for image in readiness.images:
            if image.current_analysis is not None:
                continue
            item = {'image_key': image.image_id, 'image_sha256': image.image_sha256,
                'source_links': [asdict(link) for link in image.source_links],
                'sources': public[image.image_id]['sources'], 'status': 'pending'}
            prior = receipts.get((image.image_id, image.image_sha256))
            if prior:
                item.update(status='unknown',
                    message='原分析请求仍有未回读结果，请读取原批次或人工处理；不会重复发起识别。')
                if prior['actor_id'] == actor['actor_id']:
                    item['source_task_id'] = prior['task_id']
            items.append(item)
        session = session or tasks.create_agent_session(auth_token, project_id=project_id)
        data = {**scope, 'brief_version_id': paired.brief.brief_version_id,
            'brief_snapshot_id': paired.brief.brief_snapshot_id, 'items': items,
            'original_binding': {'brief_version_id': paired.brief.brief_version_id,
                                 'brief_snapshot_id': paired.brief.brief_snapshot_id},
            'binding_history': [],
            'request_fingerprint': fingerprint, 'idempotency_key': idempotency_key,
            'pause_requested': False, 'worker_started': False, 'resume_count': 0,
            'process_owner': deepcopy(_PROCESS_OWNER)}
        task = tasks.create_task(auth_token, agent_session_id=session['agent_session_id'],
            task_type=_TYPE, payload_sha256=sha256_json(data), expected_project_version=expected_project_version)
        task = tasks.transition_task(auth_token, task_id=task['task_id'],
            expected_task_version=task['task_version'], status='executing',
            details={_DETAIL: data, 'user_message': '分析本次待处理图片',
                     **({'workbench_parent_task_id': workbench_parent_task_id} if workbench_parent_task_id else {}),
                     'response': _public({**task, 'status': 'executing'}, data)})
    return _result(task, data)


def _pause_error(service, auth_token, task, data, exc):
    data.update(pause_requested=True, error_code=getattr(exc, 'code', 'IMAGE_ANALYSIS_WRITE_PENDING'),
        pause_message='本批已暂停，已返回的结果和成功项保留。' + (
            exc.message if isinstance(exc, WorkspaceServiceError) else '结果尚未写入，请继续原批次回读后处理。'))
    try:
        _observe_binding(service, auth_token, data)
    except WorkspaceServiceError:
        pass
    return _record(service, auth_token, task, data, status='waiting_user')


def _recover(service, auth_token, task, data):
    project, readiness = _current(service, auth_token, data)
    for item in data['items']:
        image = _identity(item, readiness)
        if image.current_analysis is not None and item['status'] != 'completed':
            _completed(item, image.current_analysis, reused=True)
        elif item['status'] == 'in_flight':
            item.update(status='unknown', message='请求已发出，但没有可确认的返回结果；原请求保留，不会自动重复识别。')
    frozen = {item['image_key'] for item in data['items']}
    data['new_pending_count'] = sum(image.current_analysis is None and image.image_id not in frozen
                                     for image in readiness.images)
    return project, readiness


def read(service, auth_token, *, workspace_id, project_id, task_id=None):
    if task_id is None:
        with _store(service).connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id,
                project_id=project_id, permission='project.read')
            row = connection.execute("""SELECT task_id FROM tasks WHERE project_id=? AND actor_id=?
                AND task_type=? ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (project_id, actor['actor_id'], _TYPE)).fetchone()
        if row is None:
            return {'task': None, 'response': None}
        task_id = row['task_id']
    task, data = _load(service, auth_token, task_id)
    if _scope(data) != {'workspace_id': workspace_id, 'project_id': project_id}:
        raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '分析批次不属于当前项目')
    if task['status'] == 'executing' and (data.get('worker_started') or data.get('process_owner') != _PROCESS_OWNER):
        with try_exclusive_lock(_lock(service, task_id), run_id=task_id) as acquired:
            if acquired:
                task, data = _load(service, auth_token, task_id)
                if task['status'] == 'executing' and (data.get('worker_started') or data.get('process_owner') != _PROCESS_OWNER):
                    try:
                        _recover(service, auth_token, task, data)
                    except WorkspaceServiceError as exc:
                        return _pause_error(service, auth_token, task, data, exc)
                    data.update(pause_requested=True,
                        pause_message='执行已中断，成功项及返回结果保留；继续时仅处理未完成项。'
                            if data.get('worker_started') else '执行尚未开始，请继续原批次；本次图片集合已保留。')
                    return _record(service, auth_token, task, data, status='waiting_user')
    try:
        _observe_binding(service, auth_token, data)
        _, readiness = _current(service, auth_token, data)
        frozen = {item['image_key'] for item in data['items']}
        data['new_pending_count'] = sum(image.current_analysis is None and image.image_id not in frozen
                                         for image in readiness.images)
    except WorkspaceServiceError:
        pass  # Historical results stay readable after the current Brief changes.
    return _result(task, data)


def pause(service, auth_token, *, task_id):
    with _store(service).atomic():
        task, data = _load(service, auth_token, task_id)
        if task['status'] != 'executing':
            return _result(task, data)
        data['pause_requested'] = True
        result = _record(service, auth_token, task, data)
    with try_exclusive_lock(_lock(service, task_id), run_id=task_id) as acquired:
        if acquired:
            task, data = _load(service, auth_token, task_id)
            if task['status'] == 'executing':
                try:
                    _recover(service, auth_token, task, data)
                except WorkspaceServiceError as exc:
                    return _pause_error(service, auth_token, task, data, exc)
                return _record(service, auth_token, task, data, status='waiting_user')
    return result


def resume(service, auth_token, *, task_id):
    task, data = _load(service, auth_token, task_id)
    read(service, auth_token, **_scope(data), task_id=task_id)
    with _store(service).atomic():
        task, data = _load(service, auth_token, task_id)
        if task['status'] != 'waiting_user':
            return _result(task, data)
        try:
            binding = _observe_binding(service, auth_token, data)
            if data['basis_changed']:
                old = {key: data[key] for key in binding}
                data['binding_history'].append({'from': old, 'to': binding,
                    'resume_count': data['resume_count'] + 1})
                data.update(binding)
                # Reuse the existing observation under the new Brief. A prior
                # human decision belongs to its original requirement binding.
                for item in data['items']:
                    if item['status'] == 'completed':
                        item.update(status='returned' if item.get('receipt') else 'pending')
                        item['previous_analysis_key'] = item.pop('analysis_key', None)
                        item.pop('analysis_status', None)
                data['basis_changed'] = False
            _recover(service, auth_token, task, data)
        except WorkspaceServiceError as exc:
            return _pause_error(service, auth_token, task, data, exc)
        data.update(pause_requested=False, worker_started=False, resume_count=data['resume_count'] + 1,
            process_owner=deepcopy(_PROCESS_OWNER))
        data.pop('pause_message', None)
        data.pop('error_code', None)
        return _record(service, auth_token, task, data, status='executing', clear_pause=True)


def _commit(service, auth_token, task, data, item, project):
    receipt = item['receipt']
    result = receipt['result']
    values = {key: (tuple(value) if key in {'tags', 'subjects', 'evidence', 'quality_flags', 'negative_flags'} else value)
              for key, value in result.items()}
    analysis = ProviderProjectImageAnalysisCommandService(service.services.images).record_returned_analysis(
        auth_token, **_scope(data), expected_project_lock_version=project['project_version'],
        submission=ProviderImageAnalysisInput(image_id=item['image_key'], image_sha256=item['image_sha256'],
            brief_version_id=data['brief_version_id'], brief_snapshot_id=data['brief_snapshot_id'],
            provider=receipt['provider'], model=receipt['model'], prompt_version=receipt['prompt_version'], **values),
        observation_brief_version_id=receipt['brief_version_id'],
        observation_brief_snapshot_id=receipt['brief_snapshot_id'], source_task_id=task['task_id'])
    _completed(item, analysis)


def _execute_item(service, auth_token, task, data, index):
    item = data['items'][index]
    with try_exclusive_lock(_image_lock(service, data, item), run_id=task['task_id']) as acquired:
        if not acquired:
            data.update(pause_requested=True,
                pause_message='这张图片正在其他批次处理中。已完成结果保留，请稍后继续本批；不会重复识别同一图片。')
            return _record(service, auth_token, task, data, status='waiting_user')
        task, data = _load(service, auth_token, task['task_id'])
        item = data['items'][index]
        if data.get('pause_requested'):
            return _record(service, auth_token, task, data, status='waiting_user')
        try:
            project, readiness = _current(service, auth_token, data)
            image = _identity(item, readiness)
            if image.current_analysis is not None:
                _completed(item, image.current_analysis, reused=True)
                _record(service, auth_token, task, data)
                return None
            if item['status'] in {'pending', 'unknown'}:
                with _store(service).connection() as connection:
                    require_permission(connection, auth_token, **_scope(data), permission='review.record')
                reuse = getattr(service.services.images, 'reuse_analysis_for_current_brief', None)
                if not callable(reuse):
                    raise WorkspaceServiceError('IMAGE_ANALYSIS_REUSE_UNAVAILABLE', '原观察复用入口尚未接通，已停止重复识别')
                analysis = reuse(auth_token, **_scope(data), image_id=item['image_key'],
                    image_sha256=item['image_sha256'], brief_version_id=data['brief_version_id'],
                    brief_snapshot_id=data['brief_snapshot_id'], expected_project_lock_version=project['project_version'])
                if analysis is not None:
                    _completed(item, analysis, reused=True)
                    _record(service, auth_token, task, data)
                    return None
                if item['status'] == 'unknown':
                    return None
                with _store(service).connection() as connection:
                    prior = _prior_receipts(service, connection, data['project_id']).get(
                        (item['image_key'], item['image_sha256']))
                if prior and prior['task_id'] != task['task_id']:
                    item.update(status='unknown', message='此图片已有未回读的分析请求；请读取原批次或人工处理，不会重复识别。')
                    if prior['actor_id'] == task['actor_id']:
                        item['source_task_id'] = prior['task_id']
                    _record(service, auth_token, task, data)
                    return None
                pending = service.services.images.resolve_pending_for_provider(auth_token, **_scope(data),
                    brief_version_id=data['brief_version_id'], brief_snapshot_id=data['brief_snapshot_id'],
                    expected_project_lock_version=project['project_version'])
                asset = next((asset for asset in pending.assets if asset.image_id == item['image_key']
                              and asset.image_sha256 == item['image_sha256']), None)
                if asset is None:
                    raise WorkspaceServiceError('IMAGE_ANALYSIS_INPUT_CHANGED', '本图片已有新的处理结果，请继续原批次回读')
                item['status'] = 'in_flight'
                _record(service, auth_token, task, data)
                if data.get('pause_requested'):
                    item['status'] = 'pending'
                    return _record(service, auth_token, task, data, status='waiting_user')
                # Hold the image-content lock, but never a database transaction,
                # across the request and durable receipt.
                try:
                    result = service.vision_provider_policy.invoke(asset.controlled_path)
                    if not isinstance(result, ProjectImageVisionResult):
                        raise TypeError('图片 Provider 必须返回 ProjectImageVisionResult')
                except Exception as exc:
                    item.update(_provider_failure(exc))
                    _record(service, auth_token, task, data)
                    return None
                item.update(status='returned', receipt={'result': asdict(result),
                    'provider': service.vision_provider_policy.provider,
                    'model': service.vision_provider_policy.model,
                    'prompt_version': service.vision_provider_policy.prompt_version,
                    'brief_version_id': data['brief_version_id'], 'brief_snapshot_id': data['brief_snapshot_id']})
                _record(service, auth_token, task, data)
            # A human confirmation may have arrived during the Provider call.
            project, readiness = _current(service, auth_token, data)
            image = _identity(item, readiness)
            if image.current_analysis is not None:
                _completed(item, image.current_analysis, reused=True)
            else:
                _commit(service, auth_token, task, data, item, project)
            _record(service, auth_token, task, data)
            return None
        except Exception as exc:
            return _pause_error(service, auth_token, task, data, exc)


def execute(service, auth_token, *, task_id):
    _load(service, auth_token, task_id)
    with try_exclusive_lock(_lock(service, task_id), run_id=task_id) as acquired:
        if not acquired:
            return _result(*_load(service, auth_token, task_id))
        task, data = _load(service, auth_token, task_id)
        if task['status'] != 'executing':
            return _result(task, data)
        try:
            _recover(service, auth_token, task, data)
        except WorkspaceServiceError as exc:
            return _pause_error(service, auth_token, task, data, exc)
        data.update(worker_started=True, process_owner=deepcopy(_PROCESS_OWNER))
        _record(service, auth_token, task, data)
        for index in range(len(data['items'])):
            task, data = _load(service, auth_token, task_id)
            if data.get('pause_requested'):
                return _record(service, auth_token, task, data, status='waiting_user')
            if data['items'][index]['status'] not in {'pending', 'returned', 'unknown'}:
                continue
            stopped = _execute_item(service, auth_token, task, data, index)
            if stopped is not None:
                return stopped
        task, data = _load(service, auth_token, task_id)
        incomplete = any(item['status'] != 'completed' for item in data['items'])
        return _record(service, auth_token, task, data,
            status='partially_completed' if incomplete else 'completed')
