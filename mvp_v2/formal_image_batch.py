"""Image-assistant orchestration over canonical retrieval and saved progress."""
from __future__ import annotations

import re
import json
from copy import deepcopy

from .formal_script_assistant import _referenced_shot_ids
from .formal_image_candidates import allocate_diverse_defaults, search_project_candidates
from .image_preview import formal_image_preview_url
from .run_contract import sha256_json
from .identity_service import require_permission
from .run_state import try_exclusive_lock
from .workspace_store import WorkspaceServiceError


def _generation(service):
    generation = getattr(getattr(service, 'default_images', None), 'creative_generation_service', None)
    if generation is None:
        raise WorkspaceServiceError('FORMAL_IMAGE_REVISION_DISABLED', '初稿配图服务尚未接入')
    return generation


def _context(service, auth_token, p, *, projection_task_id=None):
    scope = {key: p[key] for key in ('workspace_id', 'project_id')}
    if p.get('target_kind') == 'creative_block':
        from .creative_generation import repository
        from .creative_images import public_references
        project = service.projects.get_project(auth_token, p['project_id'])
        if project.get('current_version_id'):
            raise WorkspaceServiceError('CREATIVE_FORMAL_VERSION_EXISTS', '当前项目已转入正式制作，请选择正式镜头')
        state = repository(_generation(service), auth_token, **scope).read()
        if state['project_version'] != p['expected_project_version']:
            raise WorkspaceServiceError('PROJECT_VERSION_CONFLICT', '项目已变化，请重新读取当前初稿')
        if state['revision'] != p['expected_creative_revision']:
            raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '初稿已变化，请重新读取当前段落')
        draft = state['body'].get('draft') or {}
        protected = set(state['body'].get('assistant_protected_ids', []))
        rows = [{**row, 'visual': row['fields'].get('画面描述', ''),
                 'structure': row['fields'].get('段落', ''), 'assistant_locked': row['shot_id'] in protected}
                for row in draft.get('shots', [])]
        if projection_task_id:
            from .creative_workbench import text_projection
            changes = text_projection(service.tasks, auth_token, project_id=p['project_id'],
                project_version=p['expected_project_version'], revision=state['revision'],
                task_id=projection_task_id, stage='creative')
            rows = deepcopy(rows)
            by_id = {row['shot_id']: row for row in rows}
            for change in changes:
                row = by_id.get(change['block_id'])
                if not row or row['fields'].get(change['field']) != change['before']:
                    raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '文字建议的原内容已变化')
                row['fields'][change['field']] = change['after']
                row['visual'] = row['fields'].get('画面描述', '')
                row['structure'] = row['fields'].get('段落', '')
        references = public_references(state['body'], **scope, project_version=state['project_version'])
        pending = {ref['block_id']: {'candidate': ref, 'selection_active': True}
                   for ref in references if ref['primary']}
        progress = {'revision': state['revision'], 'script_revision': state['revision'], 'selections': []}
        return rows, {'base_version_id': None, 'shots': []}, progress, pending, {}, {
            'draft_id': draft.get('draft_id'), 'creative_revision': state['revision'],
            'expected_target_fields': {row['shot_id']: sha256_json(row['fields']) for row in rows}}
    current = service.workspace.read_current(auth_token, **scope)
    if current.get('project_version') != p['expected_project_version']:
        raise WorkspaceServiceError('PROJECT_VERSION_CONFLICT', '项目已变化，请重新读取当前稿')
    pictures = service.image_workspace.read_current(auth_token, **scope,
        expected_project_version=p['expected_project_version'])
    progress = service.image_workspace.read_progress(auth_token, **scope,
        expected_project_version=p['expected_project_version'], library_service=getattr(service, 'default_images', None))
    rows = deepcopy(current.get('shots') or [])
    if projection_task_id:
        from .creative_workbench import text_projection
        changes = text_projection(service.tasks, auth_token, project_id=p['project_id'],
            project_version=p['expected_project_version'], revision=current['revision'],
            task_id=projection_task_id, stage='formal')
        by_id = {row['shot_id']: row for row in rows}
        for change in changes:
            if change['block_id'] not in by_id:
                raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '文字建议中的镜头已变化')
            by_id[change['block_id']][change['field']] = change['after']
    return (rows, pictures, progress,
            {s['shot_id']: s for s in progress['selections']}, {s['shot_id']: s for s in pictures['shots']}, {})


def start(service, auth_token, **kwargs):
    # Persist the idempotency lookup and task together before any model work.
    # Concurrent delivery of the same HTTP request must not create two searches.
    with service.projects.store.atomic():
        return _start(service, auth_token, **kwargs)


def _start(service, auth_token, *, workspace_id, project_id, instruction,
          expected_project_version, idempotency_key, selected_shot_ids=None,
          agent_session_id=None, save_eligible=False, mode='', target_kind='formal_shot',
          selected_block_ids=None, expected_creative_revision=None, projection_task_id=None,
          workbench_parent_task_id=None):
    """Persist scope and basis before any provider-backed candidate search."""
    if not service.writes_enabled or service.image_workspace is None:
        raise WorkspaceServiceError('FORMAL_IMAGE_REVISION_DISABLED', '图片助手暂不可用')
    if target_kind not in {'formal_shot', 'creative_block'}:
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '请选择初稿段落或正式镜头')
    if ((target_kind == 'creative_block' and (selected_shot_ids or type(expected_creative_revision) is not int))
            or (target_kind == 'formal_shot' and (selected_block_ids or expected_creative_revision is not None))):
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '配图目标与当前稿件阶段不一致')
    text = str(instruction or '').strip()
    if not text or len(text) > 4000 or not isinstance(idempotency_key, str) or not idempotency_key:
        raise WorkspaceServiceError('INVALID_INPUT', '请说明本次配图要求')
    project = service.projects.get_project(auth_token, project_id)
    if project['workspace_id'] != workspace_id:
        raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '项目不属于当前工作区')
    request = {'workspace_id': workspace_id, 'project_id': project_id,
        'instruction': instruction, 'expected_project_version': expected_project_version,
        'selected_shot_ids': selected_shot_ids or [], 'save_eligible': save_eligible,
        **({'mode': mode} if mode else {}),
        **({'projection_task_id': projection_task_id} if projection_task_id else {}),
        **({'workbench_parent_task_id': workbench_parent_task_id} if workbench_parent_task_id else {}),
        **({'target_kind': target_kind, 'selected_block_ids': selected_block_ids or [],
            'expected_creative_revision': expected_creative_revision} if target_kind == 'creative_block' else {})}
    fingerprint = sha256_json(request)
    with service.projects.store.connection() as connection:
        actor = require_permission(connection, auth_token, workspace_id=workspace_id,
                                   project_id=project_id, permission='project.update')
        receipt = connection.execute("""SELECT t.task_id,e.details_json FROM tasks t
            JOIN task_events e ON e.task_id=t.task_id WHERE t.project_id=? AND t.actor_id=?
            AND t.task_type='formal_image_candidates'
            AND json_extract(e.details_json,'$.internal_image_batch.idempotency_key')=?
            ORDER BY e.sequence_no DESC LIMIT 1""", (project_id, actor['actor_id'], idempotency_key)).fetchone()
    if receipt:
        details = json.loads(receipt['details_json'])
        if details['internal_image_batch'].get('request_fingerprint') != fingerprint:
            raise WorkspaceServiceError('IDEMPOTENCY_CONFLICT', '同一配图请求不能改为其他范围或要求')
        return {'task': service.tasks.get_task(auth_token, receipt['task_id']), 'response': details['response']}
    p = {**request, 'target_kind': target_kind, 'expected_creative_revision': expected_creative_revision,
         'subject': {'projection_task_id': projection_task_id}}
    rows, pictures, progress, _, _, creative = _context(service, auth_token, p, projection_task_id=projection_task_id)
    if not rows:
        raise WorkspaceServiceError('NOT_FOUND', '请先保存初稿段落' if target_kind == 'creative_block' else '请先将初稿转入正式制作')
    selected = list((selected_block_ids if target_kind == 'creative_block' else selected_shot_ids) or [])
    all_ids = {r['shot_id'] for r in rows}
    if len(set(selected)) != len(selected) or not set(selected).issubset(all_ids):
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '所选目标不属于当前稿')
    explicit = _referenced_shot_ids(text, rows, unit='段' if target_kind == 'creative_block' else '镜')
    if explicit is None:
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '指令中的段号或镜号不属于当前稿')
    half = rows[len(rows)//2:] if '后半段' in text else rows[:(len(rows)+1)//2] if '前半段' in text else []
    sections = [r for r in rows if str(r.get('structure') or '') and str(r['structure']) in text]
    whole = bool(re.search(r'整[份稿片]|全[稿部片]|所有|每[个一]?[镜段]', text))
    targets = explicit or [r['shot_id'] for r in rows if whole] or [r['shot_id'] for r in half or sections]
    if selected and targets and not set(targets).issubset(selected):
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '指令范围超出当前选择，请调整范围')
    targets = targets or selected
    if not targets:
        raise WorkspaceServiceError('IMAGE_SHOT_CLARIFICATION_REQUIRED', '请选择段落或镜头，或说明整稿、前半段、后半段及具体编号')
    mode = mode or ('replace_selected' if re.search(r'换|替|重新配图', text) else 'fill_missing')
    if mode not in {'fill_missing', 'replace_selected'}:
        raise WorkspaceServiceError('INVALID_INPUT', '请选择补齐缺图或重新推荐所选')
    query_match = re.search(r'(?:多一些|更多|换成|换为|替换为|找一些|找几张)(.+)', text)
    p.update(creative, base_version_id=pictures['base_version_id'], expected_revision=progress['revision'],
        expected_script_revision=progress['script_revision'], expected_image_bases=progress.get('image_bases', {}),
        choices={}, request_fingerprint=fingerprint, idempotency_key=idempotency_key, instruction=text,
        mode=mode, proposal_revision=0, target_ids=targets,
        query=query_match.group(1).strip(' 。')[:300] if query_match else '')
    response = {'type': 'image_batch_proposal', 'title': '批量配图建议', 'summary': '正在逐段查找合适参考图，可稍后回来查看。',
        'proposal_digest': sha256_json(p), 'shots': [], 'target_kind': target_kind,
        'mode': mode, 'proposal_revision': 0, 'saved_shot_ids': [], 'gap_shot_ids': [],
        'saved_block_ids': [], 'gap_block_ids': [],
        'affected_shot_ids': [] if creative else list(targets),
        'affected_block_ids': list(targets) if creative else [],
        'script_revision': progress['script_revision'], 'basis_label': '当前已保存初稿' if creative else '当前已保存工作稿',
        'project_id': project_id, 'workspace_id': workspace_id, 'project_version': expected_project_version,
        'subject': deepcopy(p['subject']),
        'progress': {'completed': 0, 'total': len(targets), 'current_target_id': ''}}
    if creative:
        response.update(creative_revision=creative['creative_revision'], draft_id=creative['draft_id'])
    session = service.tasks.get_agent_session(auth_token, agent_session_id) if agent_session_id else service.tasks.create_agent_session(auth_token, project_id=project_id)
    if session['project_id'] != project_id:
        raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '会话不属于当前项目')
    task = service.tasks.create_task(auth_token, agent_session_id=session['agent_session_id'], task_type='formal_image_candidates',
        payload_sha256=sha256_json(p), expected_project_version=expected_project_version)
    if workbench_parent_task_id:
        from .workbench_task_recovery import validate_child_parent
        validate_child_parent(service.tasks, auth_token, task, workbench_parent_task_id)
    task = service.tasks.transition_task(auth_token, task_id=task['task_id'], expected_task_version=task['task_version'],
        status='understanding', details={'user_message': text, 'response': response, 'internal_image_batch': p,
            **({'workbench_parent_task_id': workbench_parent_task_id} if workbench_parent_task_id else {})})
    return {'task': task, 'response': response}


def propose(service, auth_token, **kwargs):
    started = start(service, auth_token, **kwargs)
    if started['task']['status'] == 'understanding':
        return execute(service, auth_token, task_id=started['task']['task_id'])
    return started


def _suggestible(candidate):
    return bool(candidate.get('suggestion_eligible') or candidate.get('default_eligible'))


def _creative_project_search(service, auth_token, p, row):
    from .creative_revision import effective_brief_values
    from .formal_image_workspace import formal_image_search_slot
    generation = _generation(service)
    pool = generation.services.images
    scope = {key: p[key] for key in ('workspace_id', 'project_id')}
    brief_id, snapshot_id = pool.current_confirmed_brief_binding(auth_token, **scope,
        expected_project_lock_version=p['expected_project_version'])
    images = pool.resolve_for_delivery(auth_token, **scope, brief_version_id=brief_id,
        brief_snapshot_id=snapshot_id, expected_project_lock_version=p['expected_project_version'])
    current_brief = generation.services.briefs.read_current_formal(auth_token, **scope)
    slot = formal_image_search_slot(row=row['fields'], detail={}, planned={}, fallback_index=int(row['shot_no']))
    if p['query']:
        slot['画面描述'] = '；'.join((str(slot.get('画面描述') or ''), p['query']))
    library = service.default_images
    result = search_project_candidates(slot=slot, brief=effective_brief_values(current_brief), assets=images.assets,
        top_k=None, semantic_policy=getattr(library, 'semantic_policy', None),
        semantic_cache_root=getattr(library, 'semantic_cache_root', None))
    assets = {item.image_id: item for item in images.assets}
    return {**result, 'candidates': [service.image_workspace._employee_asset(assets[c['image_id']], c, result['candidates'])
                                    for c in result.get('candidates', []) if c['image_id'] in assets]}


def _save(service, auth_token, p, selections, operation_key):
    if p.get('target_kind') == 'creative_block':
        from .creative_images import save_references
        return save_references(_generation(service), auth_token,
            **{key: p[key] for key in ('workspace_id', 'project_id', 'expected_project_version')},
            expected_revision=p['expected_creative_revision'], operation_key=operation_key,
            selections=[{key: value for key, value in selection.items() if key in {
                'block_id', 'image_key', 'library_candidate_key', 'content_sha256'}} | {'primary': True}
                for selection in selections], replace_block_ids=[], library_service=service.default_images,
            assistant_protection=True)
    return service.image_workspace.save_progress(auth_token,
        **{key: p[key] for key in ('workspace_id', 'project_id', 'base_version_id', 'expected_project_version', 'expected_revision', 'expected_script_revision')},
        selections=[{key: value for key, value in selection.items() if key not in {'target_id', 'target_kind'}}
                    for selection in selections], idempotency_key=operation_key,
        expected_image_bases=p.get('expected_image_bases'),
        library_service=getattr(service, 'default_images', None), assistant_protection=True)


def execute(service, auth_token, *, task_id):
    # Candidate retrieval belongs to this persisted task, not JobManager.
    service.tasks.get_task(auth_token, task_id)
    with try_exclusive_lock(_execution_lock(service, task_id), run_id=task_id) as acquired:
        if not acquired:
            task, _, response = _load(service, auth_token, task_id)
            return {'task': task, 'response': response}
        return _execute_owned(service, auth_token, task_id=task_id)


def _execution_lock(service, task_id):
    return service.projects.store.database_path.parent / '.image-batch-actions' / (task_id + '.lock')


def sync(service, auth_token, *, task_id):
    task, p, response = _load(service, auth_token, task_id)
    if task['status'] == 'executing':
        with try_exclusive_lock(_execution_lock(service, task_id), run_id=task_id) as acquired:
            if acquired:
                task, p, response = _load(service, auth_token, task_id)
                # Confirmation has its own durable idempotent save receipt;
                # leave that operation to confirm() reconciliation.
                events = service.tasks.list_task_events(auth_token, task_id)
                applying = any(event.get('details', {}).get('image_batch_apply') for event in events)
                if task['status'] == 'executing' and not applying:
                    response.update(status='failed', error_code='IMAGE_RETRIEVAL_INTERRUPTED',
                        summary='配图检索已中断，原选图进度和已查到的候选仍保留。请在当前稿重新推荐；尚未采用任何新建议。')
                    task = service.tasks.transition_task(auth_token, task_id=task_id,
                        expected_task_version=task['task_version'], status='failed',
                        error_code='IMAGE_RETRIEVAL_INTERRUPTED',
                        details={'response': response, 'internal_image_batch': p})
    return {'task': task, 'response': response}


def _execute_owned(service, auth_token, *, task_id):
    if not service.writes_enabled or service.image_workspace is None:
        raise WorkspaceServiceError('FORMAL_IMAGE_REVISION_DISABLED', '图片助手暂不可用')
    task, p, response = _load(service, auth_token, task_id)
    if task['status'] != 'understanding':
        return {'task': task, 'response': response}
    task = service.tasks.transition_task(auth_token, task_id=task_id, expected_task_version=task['task_version'],
        status='executing', details={'response': response, 'internal_image_batch': p})
    try:
        return _execute(service, auth_token, task, p, response)
    except Exception as exc:
        task = service.tasks.get_task(auth_token, task_id)
        if task['status'] == 'executing':
            response.update(summary='配图检索未完成，已保留当前进度。请读取最新稿后重试。',
                            error_code=getattr(exc, 'code', 'IMAGE_RETRIEVAL_FAILED'))
            service.tasks.transition_task(auth_token, task_id=task_id, expected_task_version=task['task_version'],
                status='failed', details={'response': response, 'internal_image_batch': p})
        raise


def _execute(service, auth_token, task, p, response):
    rows, pictures, progress, pending, existing, _ = _context(service, auth_token, p,
        projection_task_id=p.get('subject', {}).get('projection_task_id'))
    creative = p['target_kind'] == 'creative_block'
    identity_field = 'block_id' if creative else 'shot_id'
    scope = {key: p[key] for key in ('workspace_id', 'project_id')}
    version = p['expected_project_version']
    library, image = getattr(service, 'default_images', None), service.image_workspace
    public, choices = response['shots'], p['choices']
    for row in rows:
        sid, no = row['shot_id'], str(row['shot_no'])
        if sid not in p['target_ids']:
            continue
        response['progress']['current_target_id'] = sid
        task = service.tasks.record_task_progress(auth_token, task_id=task['task_id'], expected_task_version=task['task_version'],
            details={'response': response, 'internal_image_batch': p})
        old, saved = existing.get(sid, {}), pending.get(sid)
        preserve = bool(saved and saved.get('selection_active', True) and not saved.get('unavailable')) or old.get('confirmed')
        item = {identity_field: sid, 'target_id': sid, 'target_kind': p['target_kind'], 'shot_no': no,
            'visual': row.get('visual', ''), 'before': (saved or {}).get('candidate') or old.get('current_image'),
            'before_selected': bool(preserve), 'candidates': [],
            'retrieval': {'executed': False, 'project_candidates': 0, 'library_inventory': 0,
                          'library_candidates': 0, 'eligible_candidates': 0, 'rejected_count': 0}}
        if row.get('assistant_locked') or (preserve and p['mode'] == 'fill_missing'):
            item.update(state='preserved', reason='当前段落或镜头已保护，未重新检索' if row.get('assistant_locked') else '补缺模式保留已有选择，未重新检索',
                        reason_code='locked' if row.get('assistant_locked') else 'existing_selection')
        else:
            try:
                found = (_creative_project_search(service, auth_token, p, row) if creative else
                    image.search_shot(auth_token, **scope, shot_no=no, query=p['query'],
                        base_version_id=pictures['base_version_id'], expected_project_version=version,
                        **({'proposal_task_id': p['subject']['projection_task_id']} if p['subject'].get('projection_task_id') else {})))
                item['before'] = (saved or {}).get('candidate') or found.get('current_image')
                item['retrieval'].update(executed=True, project_candidates=len(found.get('candidates') or []),
                                        rejected_count=len(found.get('rejected') or []))
                for candidate in found.get('candidates') or []:
                    key = 'candidate_' + sha256_json({identity_field: sid, 'image_key': candidate['image_key']})[:24]
                    href = formal_image_preview_url(candidate, **scope, expected_project_version=version)
                    item['candidates'].append({**candidate, 'candidate_key': key, 'preview_url': href, 'source_label': '项目素材'})
                    choices[key] = {identity_field: sid, 'target_id': sid, 'target_kind': p['target_kind'],
                                    'image_key': candidate['image_key'], 'query': p['query']}
                if library is not None:
                    try:
                        catalog = library.candidates(auth_token, **scope, expected_project_version=version,
                            shot_no=no, query=p['query'], candidate_limit=None, **({'block_id': sid} if creative else {}),
                            **({'proposal_task_id': p['subject']['projection_task_id']} if p['subject'].get('projection_task_id') else {}))
                    except WorkspaceServiceError as exc:
                        if not (exc.code.startswith('ASSET_PACKAGE') or exc.code.startswith('PACKAGE_')):
                            raise
                        catalog = {'shots': [], 'candidate_count': 0}
                        item.update(library_read_error=str(exc), reason_code='read_failed')
                    library_shot = next((s for s in catalog.get('shots', []) if str(s['shot_no']) == no), {})
                    item['retrieval'].update(library_inventory=catalog.get('candidate_count', 0),
                        library_candidates=len(library_shot.get('candidates') or []),
                        rejected_count=item['retrieval']['rejected_count'] + int(library_shot.get('rejected_count') or 0))
                    item['library_gap_reason'] = library_shot.get('gap_reason', '')
                    semantic = library_shot.get('semantic_assessment') or {}
                    item['retrieval']['semantic_status'] = semantic.get('status', 'unavailable')
                    if semantic.get('status') == 'failed':
                        item['semantic_warning'] = semantic.get('message') or '图片语义判断未完成，请重新检索或手动选图。'
                    for candidate in library_shot.get('candidates') or []:
                        key = 'candidate_' + sha256_json({identity_field: sid, 'library': candidate['candidate_key']})[:24]
                        href = formal_image_preview_url(candidate, **scope, expected_project_version=version)
                        item['candidates'].append({**candidate, 'candidate_key': key, 'preview_url': href, 'source_label': '创意库'})
                        choices[key] = {identity_field: sid, 'target_id': sid, 'target_kind': p['target_kind'],
                            'library_candidate_key': candidate['candidate_key'], 'query': p['query']}
                item['retrieval']['eligible_candidates'] = sum(_suggestible(c) for c in item['candidates'])
                item.update(state='ready' if item['candidates'] else 'gap',
                    reason_code='read_failed' if item.get('library_read_error') else 'recommendation' if item['candidates'] else 'no_candidates',
                    selected_candidate_key='', reason=item.get('library_read_error') or ('' if item['candidates'] else
                        item.get('library_gap_reason') or '暂无合适图片，可上传、生成或稍后补图'))
            except (OSError, ValueError) as exc:
                item.update(state='gap', reason_code='read_failed', reason='图片读取失败，请重试检索', read_error=type(exc).__name__)
        public.append(item)
        response['progress']['completed'] = len(public)
        task = service.tasks.record_task_progress(auth_token, task_id=task['task_id'], expected_task_version=task['task_version'],
            details={'response': response, 'internal_image_batch': p})
    ranked, pinned = {}, {}
    by_id = {item['target_id']: item for item in public}
    for row in rows:
        sid, no = row['shot_id'], str(row['shot_no'])
        item = by_id.get(sid)
        if item and item['state'] == 'ready':
            # Eligibility is an allocator projection only; public review/default flags stay unchanged.
            ranked[no] = {'candidates': [{**c, 'image_id': c['candidate_key'], 'default_eligible': _suggestible(c),
                'perceptual_cluster_id': c.get('perceptual_cluster_id') or c.get('content_sha256') or c['candidate_key']}
                for c in item['candidates']]}
        else:
            before = (pending.get(sid) or {}).get('candidate') or existing.get(sid, {}).get('current_image')
            if before:
                identity = before.get('content_sha256') or before.get('image_key') or before.get('candidate_key')
                if identity:
                    ranked[no] = {'candidates': [{**before, 'image_id': identity,
                        'perceptual_cluster_id': before.get('perceptual_cluster_id') or identity}]}
                    pinned[no] = identity
    allocation = allocate_diverse_defaults(ranked, pinned_by_shot=pinned)
    for item in public:
        if item['state'] == 'ready':
            item['selected_candidate_key'] = allocation['selected_by_shot'].get(item['shot_no'], '')
            item['reuse_required'] = item['shot_no'] in allocation['reuse_required_by_shot']
            if item['reuse_required']:
                item['reason'] = '当前相近候选需要重复使用，保存配图版本前请说明复用理由或更换图片。'
            if not item['selected_candidate_key']:
                best = item['candidates'][0]
                matches = [c for c in item['candidates'] if not c.get('missing') and not c.get('conflicts')
                           and (c.get('match_mode') == 'model_semantic' or _suggestible(c))]
                reasons = list(dict.fromkeys(str(r) for r in [*best.get('missing', []), *best.get('conflicts', []), best.get('brand_warning', '')] if r))
                item['retrieval']['content_matched_references'] = len(matches)
                item.update(state='reference' if matches else 'gap', reason_code='reference_only',
                    reason=(f'找到 {len(matches)} 张画面内容匹配的参考图，需要人工选择。' if matches else '找到参考图，尚无同时符合本画面全部要求的候选。')
                        + ('优先候选待核对：' + '；'.join(reasons[:5]) if reasons else '请查看各候选的画面与来源依据。'))
                if item.get('semantic_warning'):
                    item.update(reason_code='semantic_failed', reason=item['semantic_warning'])
            visible = item['candidates'][:8]
            selected = next((c for c in item['candidates'] if c['candidate_key'] == item['selected_candidate_key']), None)
            if selected and selected not in visible:
                visible.append(selected)
            item['candidates'] = visible
        item['included'] = bool(item.get('selected_candidate_key'))
        _exclude_unchanged(item)
        before = item.get('before')
        if before and not before.get('preview_url'):
            item['before'] = {**before, 'preview_url': formal_image_preview_url(before, **scope, expected_project_version=version)}
    visible_keys = {c['candidate_key'] for item in public for c in item['candidates']}
    p['choices'] = {key: value for key, value in choices.items() if key in visible_keys}
    saved_ids = []
    if p['save_eligible']:
        selections = [dict(p['choices'][item['selected_candidate_key']], checked=False, selection_active=True)
                      for item in public if item.get('selected_candidate_key') and item.get('included')]
        if selections:
            saved = _save(service, auth_token, p, selections, 'assistant-image:auto:' + task['task_id'])
            saved_ids = [s[identity_field] for s in selections]
            if creative:
                p.update(expected_project_version=saved['project_version'], expected_revision=saved['revision'],
                         expected_creative_revision=saved['revision'], creative_revision=saved['revision'])
                response.update(project_version=saved['project_version'], creative_revision=saved['revision'])
            else:
                p.update(expected_revision=saved['revision'], expected_script_revision=saved['script_revision'])
    response.update(proposal_digest=sha256_json(p),
        summary='建议尚未应用。可逐项调整，再应用所选图片；采用后仍需核对画面。',
        **{('saved_block_ids' if creative else 'saved_shot_ids'): saved_ids,
           ('gap_block_ids' if creative else 'gap_shot_ids'): [s['target_id'] for s in public if s['state'] == 'gap'],
           ('affected_block_ids' if creative else 'affected_shot_ids'): [s['target_id'] for s in public if s.get('selected_candidate_key')]})
    response['progress']['current_target_id'] = ''
    if p['save_eligible']:
        response['summary'] = f'已暂存 {len(saved_ids)} 处图片选择。请核对画面，再处理剩余缺口。'
    task = service.tasks.transition_task(auth_token, task_id=task['task_id'], expected_task_version=task['task_version'],
        status='completed' if p['save_eligible'] else 'waiting_user', details={'response': response, 'internal_image_batch': p})
    return {'task': task, 'response': response}


def _exclude_unchanged(shot):
    selected = next((c for c in shot['candidates'] if c['candidate_key'] == shot.get('selected_candidate_key')), None)
    before = shot.get('before') or {}
    if (shot.get('before_selected') and selected and before.get('content_sha256')
            and before['content_sha256'] == selected.get('content_sha256')):
        shot['included'] = False
        shot['reason'] = '与已有选择相同，无需重复应用；可逐镜选择其他图片。'


def _load(service, auth_token, task_id):
    task = service.tasks.get_task(auth_token, task_id)
    events = service.tasks.list_task_events(auth_token, task_id=task_id)
    event = next((e for e in reversed(events) if (e.get('details') or {}).get('internal_image_batch')), None)
    if event is None:
        raise WorkspaceServiceError('INVALID_INPUT', '当前任务不是配图方案')
    return task, deepcopy(event['details']['internal_image_batch']), deepcopy(event['details']['response'])


def read(service, auth_token, *, workspace_id, project_id, task_id=None):
    project = service.projects.get_project(auth_token, project_id)
    if project['workspace_id'] != workspace_id:
        raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '项目不属于当前工作区')
    if not task_id:
        with service.projects.store.connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id,
                project_id=project_id, permission='project.read')
            row = connection.execute("""SELECT task_id FROM tasks WHERE project_id=? AND actor_id=?
                AND task_type='formal_image_candidates'
                AND EXISTS (SELECT 1 FROM task_events e WHERE e.task_id=tasks.task_id
                    AND json_extract(e.details_json,'$.internal_image_batch') IS NOT NULL)
                ORDER BY updated_at DESC, task_id DESC LIMIT 1""", (project_id, actor['actor_id'])).fetchone()
        if not row:
            return {'task': None, 'response': None}
        task_id = row['task_id']
    task, parameters, response = _load(service, auth_token, task_id)
    if task['project_id'] != project_id:
        raise WorkspaceServiceError('CROSS_PROJECT_DENIED', '方案不属于当前项目')
    if task['status'] == 'executing':
        sync(service, auth_token, task_id=task_id)
        task, parameters, response = _load(service, auth_token, task_id)
    response['status'] = task['status']
    response['basis_changed'] = (
        (response.get('project_version') if task['status'] == 'completed' else parameters['expected_project_version']) != project['project_version']
        or parameters['base_version_id'] != project['current_version_id']
    )
    if parameters.get('target_kind') == 'creative_block':
        from .creative_generation import repository
        state = repository(_generation(service), auth_token, workspace_id, project_id).read()
        response['basis_changed'] = response['basis_changed'] or (
            state['revision'] != (response.get('creative_revision') if task['status'] == 'completed' else parameters['expected_creative_revision'])
            or (state['body'].get('draft') or {}).get('draft_id') != parameters.get('draft_id'))
    elif task['status'] not in {'completed', 'cancelled'} and not response['basis_changed']:
        current = service.workspace.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)
        progress = service.image_workspace.read_progress(auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=project['project_version'], library_service=getattr(service, 'default_images', None))
        image_bases = progress.get('image_bases', {})
        original_bases = parameters.get('expected_image_bases', {})
        picture_changed = current['revision'] != parameters['expected_script_revision'] and any(
            not original_bases.get(identity) or original_bases.get(identity) != image_bases.get(identity)
            for identity in parameters['target_ids'])
        response['basis_changed'] = picture_changed or progress['revision'] != parameters['expected_revision']
    if task['status'] == 'cancelled':
        response['summary'] = '已取消建议，原选图进度保持不变。'
    return {'task': task, 'response': response}


def revise(service, auth_token, *, task_id, proposal_digest, updates):
    if not service.writes_enabled or service.image_workspace is None:
        raise WorkspaceServiceError('FORMAL_IMAGE_REVISION_DISABLED', '图片助手暂不可用')
    task, p, response = _load(service, auth_token, task_id)
    if sha256_json(p) != proposal_digest:
        raise WorkspaceServiceError('PROPOSAL_DIGEST_MISMATCH', '另一处已调整本建议，请读取最新建议后继续')
    if task['status'] != 'waiting_user':
        raise WorkspaceServiceError('INVALID_TASK_TRANSITION', '本建议已应用或取消，请重新生成建议')
    creative = p.get('target_kind') == 'creative_block'
    identity_field = 'block_id' if creative else 'shot_id'
    by_id = {shot[identity_field]: shot for shot in response['shots']}
    if (not updates or any(not isinstance(u, dict) or not u.get(identity_field) for u in updates)
            or len({u[identity_field] for u in updates}) != len(updates)):
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '请选择不同镜头进行调整')
    for update in updates:
        sid = update[identity_field]
        shot = by_id.get(sid)
        if not shot or shot.get('reason_code') == 'locked':
            raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '该镜头不在可调整范围内')
        key = update.get('candidate_key')
        image_key, library_key = update.get('image_key'), update.get('library_candidate_key')
        if image_key or library_key:
            if image_key and library_key or key is not None:
                raise WorkspaceServiceError('IMAGE_CANDIDATE_INVALID', '每镜只能选择一个图片来源')
            scope = {k: p[k] for k in ('workspace_id', 'project_id', 'expected_project_version')}
            if library_key:
                library = getattr(service, 'default_images', None)
                if library is None:
                    raise WorkspaceServiceError('ASSET_PACKAGE_UNAVAILABLE', '图库暂时无法读取')
                reference = library.progress_reference(auth_token, **scope, candidate_key=library_key)
            else:
                if creative:
                    generation = _generation(service)
                    pool = generation.services.images
                    brief_id, snapshot_id = pool.current_confirmed_brief_binding(auth_token,
                        workspace_id=p['workspace_id'], project_id=p['project_id'],
                        expected_project_lock_version=p['expected_project_version'])
                    readiness = pool.resolve_for_planning(auth_token, workspace_id=p['workspace_id'], project_id=p['project_id'],
                        brief_version_id=brief_id, brief_snapshot_id=snapshot_id,
                        expected_project_lock_version=p['expected_project_version'])
                    public = next((item for item in pool.employee_readiness(readiness)['images'] if item['image_key'] == image_key), None)
                    reference = {'candidate': public} if public else None
                else:
                    _, context = service.image_workspace._current_context(auth_token,
                        scope['workspace_id'], scope['project_id'], scope['expected_project_version'])
                    reference = service.image_workspace._progress_project_references(auth_token, context).get(image_key)
                if not reference:
                    raise WorkspaceServiceError('IMAGE_CANDIDATE_INVALID', '图片不属于当前项目的可用素材')
            identity = {identity_field: sid, **({'image_key': image_key} if image_key else {'library_candidate_key': library_key})}
            key = 'candidate_' + sha256_json(identity)[:24]
            p['choices'][key] = {**identity, 'target_id': sid,
                'target_kind': p.get('target_kind', 'formal_shot'), 'query': update.get('query') or ''}
            candidate = {**reference['candidate'], 'candidate_key': key,
                         'source_label': '手选项目素材' if image_key else '手选创意库',
                         'default_eligible': False, 'match_basis': '用户逐镜选择，应用后仍需看图核对'}
            shot['candidates'] = [c for c in shot['candidates'] if c['candidate_key'] != key] + [candidate]
        if key is not None:
            if key and (key not in p['choices'] or p['choices'][key][identity_field] != sid):
                raise WorkspaceServiceError('IMAGE_CANDIDATE_INVALID', '图片不属于本镜建议')
            shot['selected_candidate_key'] = key
            shot['included'] = bool(key)
        if update.get('included') is not None:
            shot['included'] = bool(update['included'] and shot.get('selected_candidate_key'))
        if update.get('prepare_generation') is not None:
            shot['prepare_generation'] = bool(update['prepare_generation'])
        _exclude_unchanged(shot)
    p['proposal_revision'] = int(p.get('proposal_revision') or 0) + 1
    # Choices are part of the proposal contract and survive refresh without touching image progress.
    p['selection_updates'] = {s[identity_field]: {'candidate_key': s.get('selected_candidate_key', ''),
                                           'included': s.get('included', False)} for s in response['shots']}
    response.update(proposal_revision=p['proposal_revision'], proposal_digest=sha256_json(p))
    task = service.tasks.record_task_progress(auth_token, task_id=task_id,
        expected_task_version=task['task_version'], advance_version=True,
        details={'response': response, 'internal_image_batch': p})
    return {'task': task, 'response': response}


def cancel(service, auth_token, *, task_id, proposal_digest):
    if not service.writes_enabled:
        raise WorkspaceServiceError('FORMAL_IMAGE_REVISION_DISABLED', '图片助手暂不可用')
    task, p, _ = _load(service, auth_token, task_id)
    if sha256_json(p) != proposal_digest:
        raise WorkspaceServiceError('PROPOSAL_DIGEST_MISMATCH', '建议已变化，请读取后再取消')
    if task['status'] != 'cancelled':
        if task['status'] != 'waiting_user':
            raise WorkspaceServiceError('INVALID_TASK_TRANSITION', '本建议已应用，不能取消')
        task = service.tasks.transition_task(auth_token, task_id=task_id,
            expected_task_version=task['task_version'], status='cancelled',
            details={'response': {'type': 'image_batch_cancelled', 'summary': '已取消建议，原选图进度保持不变。'}})
    return {'task': task, 'response': {'type': 'image_batch_cancelled', 'summary': '已取消建议，原选图进度保持不变。'}}


def confirm(service, auth_token, *, task_id, proposal_digest, selected_candidate_keys=None):
    if not service.writes_enabled or service.image_workspace is None:
        raise WorkspaceServiceError('FORMAL_IMAGE_REVISION_DISABLED', '图片助手暂不可用')
    task, p, response = _load(service, auth_token, task_id)
    if p.get('subject', {}).get('projection_task_id'):
        raise WorkspaceServiceError('IMAGE_PROJECTION_REQUIRES_TEXT', '请在图文建议中一起采用文字和依赖的图片')
    if task['status'] not in {'waiting_user', 'executing', 'completed'}:
        raise WorkspaceServiceError('INVALID_TASK_TRANSITION', '本建议已取消，不能应用')
    if sha256_json(p) != proposal_digest:
        raise WorkspaceServiceError('PROPOSAL_DIGEST_MISMATCH', '配图方案已变化')
    keys = selected_candidate_keys if selected_candidate_keys is not None else [s['selected_candidate_key'] for s in response['shots'] if s.get('selected_candidate_key') and s.get('included', True)]
    if not keys or len(keys) != len(set(keys)) or any(k not in p['choices'] for k in keys):
        raise WorkspaceServiceError('IMAGE_CANDIDATE_INVALID', '请选择当前方案中的图片')
    selections = [dict(p['choices'][k], checked=False, selection_active=True) for k in keys]
    creative = p.get('target_kind') == 'creative_block'
    identity_field = 'block_id' if creative else 'shot_id'
    if len({s[identity_field] for s in selections}) != len(selections):
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '每镜只能采用一张图片')
    if task['status'] == 'waiting_user':
        task = service.tasks.transition_task(auth_token, task_id=task_id,
            expected_task_version=task['task_version'], status='executing',
            details={'image_batch_apply': {'proposal_digest': proposal_digest, 'candidate_keys': keys}})
    elif task['status'] == 'executing':
        events = service.tasks.list_task_events(auth_token, task_id=task_id)
        attempt = next((e['details']['image_batch_apply'] for e in reversed(events) if e.get('details', {}).get('image_batch_apply')), {})
        if attempt != {'proposal_digest': proposal_digest, 'candidate_keys': keys}:
            raise WorkspaceServiceError('IDEMPOTENCY_CONFLICT', '本建议正在应用其他选择，请先读取原请求结果')
    try:
        progress = _save(service, auth_token, p, selections, 'assistant-image:' + task_id)
    except WorkspaceServiceError:
        if task['status'] == 'executing':
            service.tasks.transition_task(auth_token, task_id=task_id, expected_task_version=task['task_version'],
                status='waiting_user', details={'response': response, 'internal_image_batch': p})
        raise
    result = {'type': 'image_batch_saved', 'target_kind': p.get('target_kind', 'formal_shot'),
        'summary': '已保存段落图片参考，请继续核对画面。' if creative else '已应用所选镜头，请核对图片后保存配图版本。',
        'progress': progress}
    if task['status'] == 'executing':
        saved_field, gap_field = ('saved_block_ids', 'gap_block_ids') if creative else ('saved_shot_ids', 'gap_shot_ids')
        response.update(summary=result['summary'], status='completed',
            **{saved_field: [selection[identity_field] for selection in selections]})
        response[gap_field] = [sid for sid in response[gap_field] if sid not in response[saved_field]]
        if creative:
            response.update(project_version=progress['project_version'], creative_revision=progress['revision'])
        for shot in response['shots']:
            if shot[identity_field] in response[saved_field]:
                shot.update(state='saved', reason='')
        service.tasks.transition_task(auth_token, task_id=task_id, expected_task_version=task['task_version'],
            status='completed', details={'response': response, 'internal_image_batch': p, 'applied_candidate_keys': keys})
    return result


def confirm_composed(service, auth_token, *, task_id, proposal_digest, selected_candidate_keys,
                     parent_task_id, retained_target_ids):
    """Rebase only a server-owned, atomically applied parent proposal."""
    parent = service.tasks.get_task(auth_token, parent_task_id)
    events = service.tasks.list_task_events(auth_token, parent_task_id)
    workbench = next((e['details']['workbench'] for e in reversed(events)
                      if e.get('details', {}).get('workbench')), None)
    task, p, response = _load(service, auth_token, task_id)
    if (not workbench or workbench.get('image_task_id') != task_id or parent['status'] != 'executing'
            or parent['project_id'] != task['project_id'] or parent['actor_id'] != task['actor_id']
            or sha256_json(p) != proposal_digest or task['status'] != 'waiting_user'):
        raise WorkspaceServiceError('IMAGE_PROJECTION_INVALID', '图文建议的关联或依据已变化，请重新预览')
    keys = selected_candidate_keys
    if not keys or len(keys) != len(set(keys)) or any(key not in p['choices'] for key in keys):
        raise WorkspaceServiceError('IMAGE_CANDIDATE_INVALID', '请选择本次建议中的图片')
    selections = [dict(p['choices'][key], checked=False, selection_active=True) for key in keys]
    identity_field = 'block_id' if p.get('target_kind') == 'creative_block' else 'shot_id'
    if (len({s[identity_field] for s in selections}) != len(selections)
            or not {s[identity_field] for s in selections} <= set(retained_target_ids)):
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '图片选择超出本次采用的段落组')
    project = service.projects.get_project(auth_token, p['project_id'])
    rebased = {**p, 'expected_project_version': project['project_version']}
    scope = {key: p[key] for key in ('workspace_id', 'project_id')}
    if identity_field == 'block_id':
        from .creative_generation import repository
        state = repository(_generation(service), auth_token, **scope).read()
        rebased.update(expected_creative_revision=state['revision'], expected_revision=state['revision'])
        current = {row['shot_id']: row['fields'] for row in state['body']['draft']['shots']}
    else:
        draft = service.workspace.read_current(auth_token, **scope)
        progress = service.image_workspace.read_progress(auth_token, **scope,
            expected_project_version=project['project_version'], library_service=service.default_images)
        rebased.update(expected_revision=progress['revision'], expected_script_revision=draft['revision'],
                       expected_image_bases=progress.get('image_bases', {}))
        current = {row['shot_id']: row for row in draft['shots']}
    before = {row['id']: deepcopy(row['fields']) for row in workbench['before']['targets']}
    for group in workbench['groups']:
        if group['group_id'] in retained_target_ids:
            for change in group['changes']:
                before[group['target_id']][change['field']] = change['after']
    for selection in selections:
        identity = selection[identity_field]
        if identity not in current or any(current[identity].get(k) != v for k, v in before[identity].items()):
            raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '采用后的文字与配图建议依据不一致，未保存图文修改')
    saved = _save(service, auth_token, rebased, selections, 'workbench-image:' + parent_task_id)
    task = service.tasks.transition_task(auth_token, task_id=task_id, expected_task_version=task['task_version'],
        status='executing', details={'workbench_parent': parent_task_id})
    response.update(status='completed', summary='已随文字一起保存配图建议；仍待人工核对。')
    service.tasks.transition_task(auth_token, task_id=task_id, expected_task_version=task['task_version'],
        status='completed', details={'response': response, 'internal_image_batch': p,
            'applied_candidate_keys': keys, 'workbench_parent': parent_task_id})
    return saved
