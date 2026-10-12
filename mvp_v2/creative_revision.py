"""Pre-formal subject adapter for FormalRevisionService and VersionReviewService."""

from copy import deepcopy
import json
import re

from .creative_draft_repository import CreativeDraftRepository
from .creative_generation import CREATIVE_FIELDS
from .creative_generation import (
    creative_lock_path, confirmed_duration_status, _confirmed_output_requirements,
    _output_constraint_mismatches,
)
from .formal_brief_domain import ArtifactPayload
from .formal_script_assistant import analyze_creative_blocks
from .run_state import exclusive_lock, is_lock_held, try_exclusive_lock
from .run_contract import sha256_json
from .version_review_service import VersionReviewService, VersionReviewError
from .workspace_store import WorkspaceServiceError


def effective_brief_facts(raw):
    """Use the selected candidates only; preserve rejected candidates in Brief history."""
    brief = raw.get("brief", {})
    facts = {item["fact_id"]: item for item in brief.get("facts", [])}
    selected, blocked = {}, set()
    for conflict in brief.get("conflicts", []):
        field = conflict["field_path"]
        if conflict.get("resolution_status") == "resolved" and conflict.get("resolved_fact_id") in facts:
            selected[field] = conflict["resolved_fact_id"]
        else:
            blocked.add(field)
    grouped = {}
    for fact in facts.values():
        field = fact["field_path"]
        if (field in blocked or fact.get("adopted") is False
                or selected.get(field, fact["fact_id"]) != fact["fact_id"]
                or fact.get("semantic_metadata", {}).get("support_status") == "needs_review"):
            continue
        grouped.setdefault(field, []).append(fact)
    return [fact for candidates in grouped.values()
            if len({item["value_json"] for item in candidates}) == 1 for fact in candidates]


def effective_brief_values(raw):
    return {item["field_path"]: json.loads(item["value_json"]) for item in effective_brief_facts(raw)}


def _requirement_basis(repo, body, briefs, auth_token):
    basis = body.get('requirement_basis')
    if basis is None:
        # Recover the generation input from the draft's own frozen provenance,
        # not a newer failed generation or the current project Brief.
        refs = body['draft']['shots'][0].get('source_refs', []) if body['draft']['shots'] else []
        source = next((item for item in refs if item.get('input_artifact_ref')), None)
        inputs = body.get('inputs', [])
        if source:
            ref = source['input_artifact_ref']
            inputs = json.loads(repo.artifacts.read(ArtifactPayload(ref, ref.rsplit('/', 1)[-1]))).get('inputs', [])
        version_id = inputs[-1].get('confirmed_brief_version_id') if inputs else None
        basis = {'values': {}}
        if version_id and briefs is not None:
            previous = briefs.read_structured(auth_token, workspace_id=repo.workspace_id,
                project_id=repo.project_id, brief_version_id=version_id)
            basis = {'values': effective_brief_values({'brief': previous})}
    return basis


def _duration_requirement_fields(shots, values):
    """A reviewed or declined proposal does not prove its numeric target was met."""
    constrained, unmet = set(), set()
    for field in ('目标时长', '视频时长'):
        if not values.get(field):
            continue
        requirements = _confirmed_output_requirements({field: values[field]})
        if not any(requirements[key] for key in ('total_seconds', 'per_shot_seconds', 'shot_count')):
            continue
        constrained.add(field)
        if _output_constraint_mismatches([shot['fields'] for shot in shots], requirements):
            unmet.add(field)
    return constrained, unmet


def proposal_duration_status(shots, changes, brief):
    """Project the complete proposed result through the same duration calculation."""
    proposed = {shot['shot_id']: deepcopy(shot['fields']) for shot in shots}
    for change in changes:
        if change['block_id'] in proposed:
            proposed[change['block_id']][change['field']] = change['after']
    return confirmed_duration_status(list(proposed.values()), brief)


def _is_duration_alignment(instruction):
    return "总时长" in instruction and bool(re.search(r"只(?:调整|修改)(?:必要的)?时长", instruction))


def _align_confirmed_duration(shots, changes, brief, instruction, selected_block_ids,
                              protected_block_ids, manual, *, duration_target_ids=None, duration_target_seconds=None):
    """Reuse the canonical duration allocator for an explicit whole-draft budget."""
    explicit = duration_target_ids is not None
    if not explicit and not _is_duration_alignment(instruction):
        return changes
    status = confirmed_duration_status([shot['fields'] for shot in shots], brief)
    target = status.get('target_seconds')
    if explicit:
        if duration_target_seconds is not None and target and abs(float(duration_target_seconds) - float(target)) > 0.001:
            raise WorkspaceServiceError('BRIEF_DURATION_CONFLICT', '本次总时长与已确认需求不同，请先在需求页核对并确认新时长')
        target = target or duration_target_seconds
        if not target:
            raise WorkspaceServiceError('DURATION_TARGET_REQUIRED', '请明确全片总时长后再调整分配')
    if not target or (not explicit and any(change['field'] != '时长' for change in changes)):
        return changes
    from .models import parse_positive_seconds_strict
    from .planning_core import _shot_durations
    proposed = {change['block_id']: change['after'] for change in changes if change['field'] == '时长'}
    selected, protected = set(duration_target_ids if explicit else selected_block_ids), set(protected_block_ids)
    if not selected <= set(selected_block_ids):
        raise WorkspaceServiceError('SCRIPT_SCOPE_MISMATCH', '时长调整不能超出本次允许修改的段落')
    preferred, fixed = [], {}
    for index, shot in enumerate(shots):
        identity, original = shot['shot_id'], shot['fields']['时长']
        locked = (identity not in selected or identity in protected
                  or not explicit and manual.get(identity, {}).get('时长') == original)
        seconds = parse_positive_seconds_strict(str(original if locked else proposed.get(identity, original)).removeprefix('建议').strip())
        if seconds is None:
            raise WorkspaceServiceError('INVALID_INPUT', '部分段落时长尚未确定，请先填写这些段落的时长后再按目标调整。')
        preferred.append(seconds)
        if locked:
            fixed[index] = seconds
    try:
        allocated, _ = _shot_durations({'视频时长': str(target)}, [[] for _ in shots], [],
            preferred_durations=preferred, fixed_durations=fixed)
    except ValueError as exc:
        raise WorkspaceServiceError('INVALID_INPUT', str(exc)) from exc
    aligned = [{'block_id': shot['shot_id'], 'field': '时长', 'before': shot['fields']['时长'], 'after': str(seconds)}
            for index, (shot, seconds) in enumerate(zip(shots, allocated))
            if index not in fixed and parse_positive_seconds_strict(str(shot['fields']['时长']).removeprefix('建议').strip()) != seconds]
    return [change for change in changes if change['field'] != '时长'] + aligned


def confirmed_requirement_updates(repo, body, brief, briefs, auth_token):
    """Project confirmed Brief changes onto the existing answer/proposal path."""
    if not body.get('draft') or brief.get('confirmation_status') != 'confirmed':
        return []
    previous = _requirement_basis(repo, body, briefs, auth_token)['values']
    current = effective_brief_values(brief)
    constrained, unmet = _duration_requirement_fields(body['draft']['shots'], current)
    actor_id = repo.read_head()['actor_id']
    return [{'field': field,
             'value': current.get(field, '') if isinstance(current.get(field, ''), str)
                      else json.dumps(current[field], ensure_ascii=False),
             'actor_id': actor_id, 'origin': 'confirmed_brief',
             'answer_id': sha256_json({'origin': 'confirmed_brief', 'basis': previous,
                 'brief_version_id': brief['brief_version_id'], 'field': field,
                 'value': current.get(field)})}
            for field in sorted(set(previous) | set(current))
            if field in unmet or (previous.get(field) != current.get(field) and field not in constrained)]


def outstanding_answers(repo, body, tasks, auth_token, brief=None, briefs=None):
    links = repo.answer_tasks(body)
    settled = set(body.get("incorporated_answer_ids", []))
    for task_id, identities in links.items():
        if body.get("receipts", {}).get("confirmed:" + task_id):
            settled.update(identities)
        else:
            try:
                if tasks.get_task(auth_token, task_id)["status"] == "cancelled":
                    settled.update(identities)
            except WorkspaceServiceError as exc:
                if exc.code != "PERMISSION_DENIED":
                    raise
    latest = {}
    consumed = body.get("consumed_answers", []) if body.get("draft") else []
    for item in consumed + body.get("pending_answers", []):
        latest[item["field"]] = item
    pending_fields = {item['field'] for item in body.get('pending_answers', [])}
    for item in confirmed_requirement_updates(repo, body, brief or {}, briefs, auth_token):
        if item['field'] not in pending_fields:
            latest[item['field']] = item
    authoritative = effective_brief_values(brief or {})
    constrained, unmet = _duration_requirement_fields((body.get('draft') or {}).get('shots', []), authoritative)
    if (brief or {}).get('confirmation_status') != 'confirmed':
        constrained, unmet = set(), set()
    pending = {item["answer_id"] for item in body.get("pending_answers", [])}
    return [item for item in latest.values()
        if (item['field'] not in constrained or item['field'] in unmet or item['answer_id'] in pending)
        and (item["answer_id"] not in settled
             or item.get('origin') == 'confirmed_brief' and item['field'] in unmet)
        and (item.get('origin') == 'confirmed_brief' or item["answer_id"] in pending or item["field"] not in authoritative
             or authoritative[item["field"]] == " ".join(item["value"].strip().splitlines()))], links


def _repo(service, auth_token, workspace_id, project_id):
    service._require_writes("FORMAL_SCRIPT_READ_ONLY", "当前部署只支持读取初稿")
    if service.brief_adapter is None:
        raise WorkspaceServiceError("FORMAL_BRIEF_ADAPTER_UNAVAILABLE", "项目输入服务尚未接入")
    return CreativeDraftRepository(service.projects.store,
        artifact_root=service.brief_adapter.service.artifacts.root.parent,
        auth_token=auth_token, workspace_id=workspace_id, project_id=project_id)


def apply(service, auth_token, *, workspace_id, project_id, expected_project_version,
          expected_revision, expected_draft_revision, action, operation_key, block_id=None, field=None, value=None):
    repo = _repo(service, auth_token, workspace_id, project_id)
    if action not in {"patch", "undo", "redo", "delete"}:
        raise WorkspaceServiceError("INVALID_INPUT", "初稿操作无效")
    if not isinstance(operation_key, str) or not 8 <= len(operation_key) <= 160:
        raise WorkspaceServiceError("INVALID_INPUT", "本次操作标识无效")
    state = repo.read()
    key = sha256_json({"actor_id": state["actor_id"], "operation_key": operation_key})
    request = sha256_json({"action": action, "block_id": block_id, "field": field, "value": value,
        "expected_project_version": expected_project_version, "expected_revision": expected_revision,
        "expected_draft_revision": expected_draft_revision})
    receipt = state["body"]["receipts"].get(key)
    if receipt:
        if receipt["request_sha256"] != request:
            raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "相同操作标识不能用于不同的内容")
        return
    try:
        with repo.transaction(expected_project_version=expected_project_version, expected_revision=expected_revision):
            draft = repo.body["draft"]
            if not draft:
                raise WorkspaceServiceError("CREATIVE_DRAFT_UNAVAILABLE", "请先生成初稿")
            core = VersionReviewService(repo)
            arguments = {"expected_revision": expected_draft_revision, "actor_id": repo.actor_id}
            if action in {"undo", "redo"}:
                if any(item is not None for item in (block_id, field, value)):
                    raise WorkspaceServiceError("INVALID_INPUT", "撤销或恢复不接受额外修改内容")
                getattr(core, action)(draft["draft_id"], **arguments)
                previous = {shot["shot_id"]: shot["fields"] for shot in draft["shots"]}
                for shot in repo.body["draft"]["shots"]:
                    for name, text in shot["fields"].items():
                        if previous.get(shot["shot_id"], {}).get(name) != text:
                            repo.body.setdefault("manual_field_values", {}).setdefault(shot["shot_id"], {})[name] = text
            else:
                target = next((item for item in draft["shots"] if item["shot_id"] == block_id), None)
                if target is None:
                    raise WorkspaceServiceError("INVALID_INPUT", "目标段落已变化，请刷新后选择")
                if action == "patch":
                    if field not in CREATIVE_FIELDS or not isinstance(value, str) or len(value) > 6000:
                        raise WorkspaceServiceError("INVALID_INPUT", "修改字段或内容无效")
                    core.apply_table_patch(draft["draft_id"], **arguments, shot_no=target["shot_no"],
                        field=field, value=value, command_id=operation_key)
                    repo.body.setdefault("manual_field_values", {}).setdefault(block_id, {})[field] = value
                else:
                    if field is not None or value is not None:
                        raise WorkspaceServiceError("INVALID_INPUT", "删除段落不接受额外修改内容")
                    core.delete_shot(draft["draft_id"], **arguments, shot_no=target["shot_no"],
                        origin="table", command_id=operation_key)
            repo.body["receipts"][key] = {"request_sha256": request, "action": action}
    except VersionReviewError as exc:
        code = "DRAFT_REVISION_CONFLICT" if "DRAFT_REVISION_CONFLICT" in str(exc) else "INVALID_INPUT"
        raise WorkspaceServiceError(code, str(exc)) from exc


def propose(service, auth_token, *, workspace_id, project_id, expected_project_version,
            expected_revision, instruction, selected_block_ids, operation_key, parent_task_id=None, input_updates=None,
            answer_ids=None, agent_session_id=None, duration_target_ids=None, duration_target_seconds=None,
            allowed_fields_by_shot=None, workbench_parent_task_id=None):
    repo = _repo(service, auth_token, workspace_id, project_id)
    if (not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 16000
            or not isinstance(operation_key, str) or not 8 <= len(operation_key) <= 160):
        raise WorkspaceServiceError("INVALID_INPUT", "请填写修改要求并使用有效操作标识")
    if not isinstance(selected_block_ids, list) or not selected_block_ids or len(set(selected_block_ids)) != len(selected_block_ids):
        raise WorkspaceServiceError("INVALID_INPUT", "请选择本次允许修改的段落")
    request = sha256_json({"instruction": instruction, "selected_block_ids": selected_block_ids,
        "expected_project_version": expected_project_version, "expected_revision": expected_revision,
        **({'allowed_fields_by_shot': allowed_fields_by_shot} if allowed_fields_by_shot is not None else {}),
        **({'duration_target_ids': duration_target_ids, 'duration_target_seconds': duration_target_seconds}
           if duration_target_ids is not None else {})})
    with try_exclusive_lock(creative_lock_path(service.projects.store, project_id + ":creative-start"), run_id=operation_key) as acquired:
        if not acquired:
            raise WorkspaceServiceError("CREATIVE_GENERATION_ACTIVE", "当前初稿操作仍在启动，请稍后重试")
        state = repo.read()
        key = sha256_json({"actor_id": state["actor_id"], "operation_key": operation_key})
        receipt = state["body"]["receipts"].get(key)
        if receipt:
            if receipt["request_sha256"] != request:
                raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "相同操作标识不能用于不同的内容")
            return {"task_id": receipt["task_id"]}
        draft = state["body"]["draft"]
        if not draft:
            raise WorkspaceServiceError("CREATIVE_DRAFT_UNAVAILABLE", "请先完成一份初稿")
        if not set(selected_block_ids).issubset({shot["shot_id"] for shot in draft["shots"]}):
            raise WorkspaceServiceError("INVALID_INPUT", "所选段落不属于当前初稿")
        if state["revision"] != expected_revision or state["project_version"] != expected_project_version:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "初稿已变化，请保留要求并刷新")
        active_id = state["body"].get("active_task_id")
        if active_id and active_id != parent_task_id and is_lock_held(creative_lock_path(service.projects.store, active_id)):
            raise WorkspaceServiceError("CREATIVE_GENERATION_ACTIVE", "当前操作仍在进行，请等待本次结果")
        current_brief = service.brief_adapter.service.read_current_formal(auth_token, workspace_id=workspace_id, project_id=project_id)
        outstanding, links = outstanding_answers(repo, state["body"], service.tasks, auth_token,
            current_brief, service.brief_adapter.service)
        active_waiting = False
        if active_id and state["body"].get("active_task_kind") == "revision":
            try:
                active_waiting = service.tasks.get_task(auth_token, active_id)["status"] == "waiting_user"
            except WorkspaceServiceError as exc:
                if exc.code != "PERMISSION_DENIED":
                    raise
        if answer_ids is None and outstanding:
            input_updates = [{"field": item["field"], "value": item["value"]} for item in outstanding]
            answer_ids = [item["answer_id"] for item in outstanding]
        if _is_duration_alignment(instruction):
            duration_ids = {item['answer_id'] for item in outstanding if item['field'] in {'目标时长', '视频时长'}}
            answer_ids = [identity for identity in (answer_ids or []) if identity in duration_ids]
            input_updates = [item for item in (input_updates or []) if item['field'] in {'目标时长', '视频时长'}]
        session = (service.tasks.get_agent_session(auth_token, agent_session_id) if agent_session_id
                   else service.tasks.create_agent_session(auth_token, project_id=project_id))
        if session["project_id"] != project_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "修改会话不属于当前项目")
        with service.projects.store.atomic():
            task = service.tasks.create_task(auth_token, agent_session_id=session["agent_session_id"],
                task_type="formal_creative_draft", payload_sha256=sha256_json({"key": key, "request": request}),
                expected_project_version=expected_project_version, reuse_payload=True)
            if workbench_parent_task_id:
                from .workbench_task_recovery import validate_child_parent
                validate_child_parent(service.tasks, auth_token, task, workbench_parent_task_id)
                task = service.tasks.append_task_event(auth_token, task_id=task['task_id'],
                    expected_task_version=task['task_version'], status='understanding',
                    details={'workbench_parent_task_id': workbench_parent_task_id})
        try:
            with repo.transaction(expected_project_version=expected_project_version, expected_revision=expected_revision):
                repo.body["manual_field_values"] = repo.manual_values(state["body"])
                repo.body["manual_fields_initialized"] = True
                repo.body["answer_tasks"] = {**links, task["task_id"]: answer_ids or []}
                repo.body["answer_tasks_initialized"] = True
                for previous_id, previous_answers in links.items():
                    if set(previous_answers) & set(answer_ids or []):
                        repo.body.setdefault("superseded_proposals", {}).setdefault(previous_id, task["task_id"])
                if active_waiting:
                    repo.body.setdefault("superseded_proposals", {}).setdefault(active_id, task["task_id"])
                repo.body["active_task_id"] = task["task_id"]
                repo.body["active_task_kind"] = "revision"
                repo.body["receipts"][key] = {"request_sha256": request, "task_id": task["task_id"]}
            lock = exclusive_lock(creative_lock_path(service.projects.store, task["task_id"]), run_id=task["task_id"])
            lock.__enter__()
        except Exception:
            current = service.tasks.get_task(auth_token, task["task_id"])
            if current["status"] in {"received", "understanding"}:
                service.tasks.append_task_event(auth_token, task_id=task["task_id"],
                    expected_task_version=current["task_version"], status="failed", error_code="CREATIVE_REVISION_START_FAILED")
            raise
    try:
        task = service.tasks.append_task_event(auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="executing")
        state = repo.read()
        frozen = (state["body"].get("generation_basis") or {}).get("context", {})
        current_brief = service.brief_adapter.service.read_current_formal(auth_token,
            workspace_id=workspace_id, project_id=project_id)
        analysis = analyze_creative_blocks(instruction=instruction, blocks=draft["shots"],
            selected_block_ids=selected_block_ids, protected_block_ids=state["body"].get("assistant_protected_ids", []),
            editor_policy=service.assistant_editor_policy, allowed_fields_by_shot=allowed_fields_by_shot,
            context={**{key: frozen[key] for key in ("creative_standard", "effective_knowledge", "template_semantics", "template_rules", "material_bindings") if key in frozen},
                "brief": effective_brief_values(current_brief), "current_script_summary": [shot["fields"] for shot in draft["shots"]],
                "recent_conversation": [{"role": "user", "content": "本次补充：" + json.dumps(input_updates, ensure_ascii=False)}] if input_updates else []})
        if (analysis.get("type") != "rewrite_operations"
                and not ((_is_duration_alignment(instruction) or duration_target_ids is not None)
                         and analysis.get('analysis_code') == 'script_no_changes')):
            service.tasks.append_task_event(auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
                status="completed" if analysis.get("analysis_code") == "script_no_changes" else "failed",
                error_code=None if analysis.get("analysis_code") == "script_no_changes" else "CREATIVE_REVISION_NOT_APPLIED",
                details={"creative_notice": analysis.get("summary", "本次没有可应用的修改")})
            return {"task_id": task["task_id"]}
        current = repo.read()
        if current["revision"] != state["revision"] or current["project_version"] != state["project_version"]:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "分析期间初稿已变化，本次建议未应用")
        field_map = {"structure": "段落", "visual": "画面描述", "sound": "声音", "duration_text": "时长"}
        by_id = {shot["shot_id"]: shot for shot in draft["shots"]}
        manual = {}
        if input_updates:
            answer_fields = {item["answer_id"]: item["field"] for item in state["body"].get("consumed_answers", [])}
            update_fields = {item["field"] for item in input_updates}
            declined_answers = state["body"].get("declined_field_answer_ids", {})
            for block_id, fields in state["body"].get("declined_field_values", {}).items():
                for field, value in fields.items():
                    identities = declined_answers.get(block_id, {}).get(field, [])
                    if not update_fields & {answer_fields.get(identity) for identity in identities}:
                        manual.setdefault(block_id, {})[field] = value
            for previous_id in links:
                try:
                    if service.tasks.get_task(auth_token, previous_id)["status"] != "cancelled":
                        continue
                    if update_fields & {answer_fields.get(identity) for identity in links[previous_id]}:
                        continue
                    for event in service.tasks.list_task_events(auth_token, previous_id):
                        for change in event.get("details", {}).get("creative_subject", {}).get("changes", []):
                            manual.setdefault(change["block_id"], {})[change["field"]] = change["before"]
                except WorkspaceServiceError as exc:
                    if exc.code != "PERMISSION_DENIED":
                        raise
            for block_id, fields in state["body"].get("manual_field_values", {}).items():
                manual.setdefault(block_id, {}).update(fields)
        changes = [{"block_id": item["shot_id"], "field": field_map[item["field"]],
            "before": by_id[item["shot_id"]]["fields"][field_map[item["field"]]], "after": item["value"]}
            for item in analysis.get("operations", [])
            if manual.get(item["shot_id"], {}).get(field_map[item["field"]])
                != by_id[item["shot_id"]]["fields"][field_map[item["field"]]]]
        changes = _align_confirmed_duration(draft['shots'], changes, current_brief, instruction,
            selected_block_ids, state['body'].get('assistant_protected_ids', []),
            {identity: {**manual.get(identity, {}), **fields}
             for identity, fields in {**manual, **repo.manual_values(state['body'])}.items()},
            duration_target_ids=duration_target_ids, duration_target_seconds=duration_target_seconds)
        if not changes:
            service.tasks.append_task_event(auth_token, task_id=task["task_id"],
                expected_task_version=task["task_version"], status="completed",
                details={"creative_notice": "没有可应用的修改；手动编辑与保护内容已保留，补充仍待核对。"})
            return {"task_id": task["task_id"]}
        subject = {"project_id": project_id, "draft_id": draft["draft_id"], "draft_revision": draft["revision"],
            "revision": state["revision"], "project_version": state["project_version"],
            "brief_version_id": current_brief.get('brief_version_id'),
            "selected_block_ids": selected_block_ids, "changes": changes, "answer_ids": answer_ids or []}
        if any(item.get('origin') == 'confirmed_brief' and item['answer_id'] in (answer_ids or [])
               for item in outstanding):
            reviewed_values = effective_brief_values(current_brief)
            if _is_duration_alignment(instruction):
                reviewed_values = {**_requirement_basis(repo, state['body'], service.brief_adapter.service, auth_token)['values'],
                    **{field: value for field, value in reviewed_values.items() if field in {'目标时长', '视频时长'}}}
            subject['confirmed_requirement_basis'] = {'brief_version_id': current_brief['brief_version_id'],
                'values': reviewed_values}
        public = {"digest": sha256_json(subject), "revision": state["revision"], "changes": changes,
            "summary": f"建议修改 {len({item['block_id'] for item in changes})} 段，请核对后应用。",
            "duration_status": proposal_duration_status(draft['shots'], changes, current_brief)}
        service.tasks.append_task_event(auth_token, task_id=task["task_id"],
            expected_task_version=task["task_version"], status="waiting_user",
            details={"creative_proposal": public, "creative_subject": subject})
        return {"task_id": task["task_id"], "proposal": public}
    except Exception as exc:
        current_task = service.tasks.get_task(auth_token, task["task_id"])
        if current_task["status"] == "executing":
            service.tasks.append_task_event(auth_token, task_id=task["task_id"],
                expected_task_version=current_task["task_version"], status="failed",
                error_code=getattr(exc, "code", "CREATIVE_REVISION_INTERRUPTED"))
        raise
    finally:
        lock.__exit__(None, None, None)


def confirm(service, auth_token, *, workspace_id, project_id, task_id, proposal_digest, retained_block_ids=None):
    repo = _repo(service, auth_token, workspace_id, project_id)
    task = service.tasks.get_task(auth_token, task_id)
    if task["project_id"] != project_id or task["task_type"] != "formal_creative_draft":
        raise WorkspaceServiceError("PERMISSION_DENIED", "修改建议不属于当前项目")
    with try_exclusive_lock(creative_lock_path(service.projects.store, task_id), run_id=task_id) as acquired:
        if not acquired:
            raise WorkspaceServiceError("CREATIVE_TASK_ACTIVE", "这次修改正在保存，请稍后重新读取结果")
        task = service.tasks.get_task(auth_token, task_id)
        state = repo.read()
        subject = None
        for event in service.tasks.list_task_events(auth_token, task_id):
            subject = event.get("details", {}).get("creative_subject") or subject
        if not subject or sha256_json(subject) != proposal_digest:
            raise WorkspaceServiceError("CREATIVE_PROPOSAL_INVALID", "修改建议与本次确认不一致")
        if retained_block_ids is not None and (not isinstance(retained_block_ids, list)
                or any(not isinstance(identity, str) for identity in retained_block_ids)):
            raise WorkspaceServiceError("INVALID_INPUT", "请选择本次建议中的完整段落组")
        proposed = {change["block_id"] for change in subject["changes"]}
        retained = proposed if retained_block_ids is None else set(retained_block_ids)
        if (not retained <= proposed or retained_block_ids is not None
                and len(retained) != len(retained_block_ids)):
            raise WorkspaceServiceError("INVALID_INPUT", "请选择本次建议中的完整段落组")
        fingerprint = sha256_json({"proposal_digest": proposal_digest, "retained_block_ids": sorted(retained)})
        terminal_status = "completed" if retained else "cancelled"
        result_notice = "修改已应用到初稿。" if retained else "本次修改建议未采用，当前初稿已保留。"
        receipt = state["body"]["receipts"].get("confirmed:" + task_id)
        if receipt:
            matches = (receipt == proposal_digest and retained == proposed) if isinstance(receipt, str) else receipt.get("request_sha256") == fingerprint
            if not matches:
                raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "这次建议已按另一组选择应用，请读取已保存的初稿")
            if task["status"] == terminal_status:
                return
            service.tasks.append_task_event(auth_token, task_id=task_id, expected_task_version=task["task_version"],
                status=terminal_status, result_ref=state["payload_ref"], details={"creative_notice": result_notice})
            return
        if task["status"] != "waiting_user":
            raise WorkspaceServiceError("CREATIVE_PROPOSAL_UNAVAILABLE", "本次建议已结束，请重新读取")
        try:
            with repo.transaction(expected_project_version=subject["project_version"], expected_revision=subject["revision"]):
                if repo.body["active_task_id"] != task_id or repo.body["draft"]["draft_id"] != subject["draft_id"]:
                    raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "当前初稿或修改建议已变化")
                by_id = {shot["shot_id"]: shot for shot in repo.body["draft"]["shots"]}
                protected = set(repo.body.get("assistant_protected_ids", []))
                operations = []
                for change in subject["changes"]:
                    if change["block_id"] not in retained:
                        repo.body.setdefault("declined_field_values", {}).setdefault(change["block_id"], {})[change["field"]] = change["before"]
                        repo.body.setdefault("declined_field_answer_ids", {}).setdefault(change["block_id"], {})[change["field"]] = subject.get("answer_ids", [])
                        continue
                    shot = by_id.get(change["block_id"])
                    if (not shot or change["block_id"] not in subject["selected_block_ids"] or change["block_id"] in protected
                            or shot["fields"].get(change["field"]) != change["before"]):
                        raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "目标段落已变化，请重新预览")
                    operations.append({"shot_no": shot["shot_no"], "field": change["field"], "value": change["after"]})
                if operations:
                    VersionReviewService(repo).apply_agent_change_set(subject["draft_id"], expected_revision=subject["draft_revision"],
                        actor_id=repo.actor_id, operations=operations, command_id=task_id)
                    repo.body["last_applied_revision_task_id"] = task_id
                repo.body["receipts"]["confirmed:" + task_id] = {"request_sha256": fingerprint,
                    "proposal_digest": proposal_digest, "retained_block_ids": sorted(retained)}
                if 'confirmed_requirement_basis' in subject:
                    basis = deepcopy(subject['confirmed_requirement_basis'])
                    _, unmet = _duration_requirement_fields(repo.body['draft']['shots'], basis['values'])
                    previous = _requirement_basis(repo, state['body'], service.brief_adapter.service, auth_token)['values']
                    for field in unmet:
                        if field in previous:
                            basis['values'][field] = previous[field]
                        else:
                            basis['values'].pop(field, None)
                    repo.body['requirement_basis'] = basis
            saved = repo.read()
            service.tasks.append_task_event(auth_token, task_id=task_id, expected_task_version=task["task_version"],
                status=terminal_status, result_ref=saved["payload_ref"], details={"creative_notice": result_notice})
        except VersionReviewError as exc:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", str(exc)) from exc


def protect(service, auth_token, *, workspace_id, project_id, expected_project_version,
            expected_revision, protected_block_ids, operation_key):
    repo = _repo(service, auth_token, workspace_id, project_id)
    if not isinstance(protected_block_ids, list) or len(set(protected_block_ids)) != len(protected_block_ids):
        raise WorkspaceServiceError("INVALID_INPUT", "保护范围无效")
    state = repo.read()
    key = sha256_json({"actor_id": state["actor_id"], "operation_key": operation_key})
    request = sha256_json({"protected_block_ids": protected_block_ids,
        "project_version": expected_project_version, "revision": expected_revision})
    if state["body"]["receipts"].get(key):
        if state["body"]["receipts"][key]["request_sha256"] != request:
            raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "保护操作标识与内容不一致")
        return
    with repo.transaction(expected_project_version=expected_project_version, expected_revision=expected_revision):
        draft = repo.body["draft"]
        if not draft or not set(protected_block_ids).issubset({shot["shot_id"] for shot in draft["shots"]}):
            raise WorkspaceServiceError("INVALID_INPUT", "保护范围包含未知段落")
        repo.body["assistant_protected_ids"] = sorted(protected_block_ids)
        repo.body["receipts"][key] = {"request_sha256": request}


def answer(service, auth_token, *, workspace_id, project_id, expected_project_version,
           expected_revision, field, value, operation_key):
    repo = _repo(service, auth_token, workspace_id, project_id)
    if (field not in {"脚本类型", "目标时长", "风格调性", "视频目标"}
            or not isinstance(value, str) or not value.strip() or len(value) > 4000
            or not isinstance(operation_key, str) or not 8 <= len(operation_key) <= 160):
        raise WorkspaceServiceError("INVALID_INPUT", "补充字段或内容无效")
    state = repo.read()
    key = sha256_json({"actor_id": state["actor_id"], "operation_key": operation_key})
    request = sha256_json({"field": field, "value": value,
        "project_version": expected_project_version, "revision": expected_revision})
    receipt = state["body"]["receipts"].get(key)
    if receipt:
        if receipt["request_sha256"] != request:
            raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "补充操作标识与内容不一致")
        return
    with repo.transaction(expected_project_version=expected_project_version, expected_revision=expected_revision,
                          advance_project_version=False):
        queue = [item for item in repo.body.get("pending_answers", []) if item["field"] != field]
        queue.append({"answer_id": key, "field": field, "value": value,
            "actor_id": repo.actor_id, "request_sha256": request})
        repo.body["pending_answers"] = queue
        repo.body["input_notice"] = "补充内容已保存，将准备相关段落的修改建议。"
        repo.body["receipts"][key] = {"request_sha256": request}
    latest = repo.read()
    active = latest["body"].get("active_task_id")
    if not active or not is_lock_held(creative_lock_path(service.projects.store, active)):
        while consume_answers(service, auth_token, workspace_id=workspace_id, project_id=project_id):
            pass


def consume_answers(service, auth_token, *, workspace_id, project_id, parent_task_id=None, force_preview=False):
    repo = _repo(service, auth_token, workspace_id, project_id)
    # The same input operation cannot be consumed twice by a poll and a worker.
    with try_exclusive_lock(creative_lock_path(service.projects.store, project_id + ":creative-inputs"), run_id=project_id) as acquired:
        if not acquired:
            return False
        state = repo.read()
        queue = state["body"].get("pending_answers", [])
        outstanding, links = outstanding_answers(repo, state["body"], service.tasks, auth_token,
            service.brief_adapter.service.read_current_formal(auth_token, workspace_id=workspace_id, project_id=project_id),
            service.brief_adapter.service)
        active = state["body"].get("active_task_id")
        if active and active != parent_task_id and is_lock_held(creative_lock_path(service.projects.store, active)):
            return False
        if not queue:
            # Before the first draft, saving facts completes the input operation.
            # There is no revision proposal to apply, so consumed answers must not
            # be requeued indefinitely while answer() waits for this loop to end.
            if not outstanding or not state["body"].get("draft"):
                return False
            if active and set(item["answer_id"] for item in outstanding) <= set(links.get(active, [])):
                task = service.tasks.get_task(auth_token, active)
                if task["status"] == "completed" and not force_preview:
                    return False
                subjects = [event.get("details", {}).get("creative_subject")
                    for event in service.tasks.list_task_events(auth_token, active)]
                subject = next((item for item in reversed(subjects) if item), None)
                if task["status"] == "waiting_user" and subject and subject["revision"] == state["revision"] and not force_preview:
                    return False
            queue = outstanding
        # Facts are attributed to the person who entered them, never another collaborator.
        queue = [item for item in queue if item["actor_id"] == state["actor_id"]]
        if not queue:
            return False
        consumed = []
        try:
            imported = {item["answer_id"]: item for item in state["body"].get("consumed_answers", [])}
            for item in queue:
                before = service.brief_adapter.service.read_current_formal(auth_token, workspace_id=workspace_id, project_id=project_id)
                if item.get('origin') == 'confirmed_brief':
                    # The requirement workspace has already imported, resolved
                    # and confirmed these facts. Preview must not reimport them.
                    consumed.append({**item, 'brief_version_id': before.get('brief_version_id')})
                    continue
                effective_values = effective_brief_values(before)
                if item["answer_id"] not in imported:
                    effective_values[item["field"]] = " ".join(item["value"].strip().splitlines())
                    current = repo.read()
                    service.brief_adapter.import_inline_answer(auth_token, workspace_id=workspace_id,
                        project_id=project_id, field=item["field"], value=item["value"],
                        expected_project_version=current["project_version"])
                raw = service.brief_adapter.service.read_current_formal(auth_token, workspace_id=workspace_id, project_id=project_id)
                # Reimport rebuilds conflicts for all sources. Preserve earlier
                # explicit field answers too, using the same exact-fact rule.
                while True:
                    brief = raw.get("brief", {})
                    choices = {fact["fact_id"]: fact for fact in brief.get("facts", [])}
                    chosen = None
                    for conflict in brief.get("conflicts", []):
                        field = conflict.get("field_path")
                        if conflict.get("resolution_status") == "resolved" or field not in effective_values:
                            continue
                        matching = [fact_id for fact_id in conflict.get("fact_ids", [])
                            if json.loads(choices[fact_id]["value_json"]) == effective_values[field]]
                        if len(matching) == 1:
                            chosen = (conflict["conflict_id"], matching[0])
                            break
                    if not chosen:
                        break
                    project = service.projects.get_project(auth_token, project_id)
                    service.brief_adapter.resolve_conflict(auth_token, workspace_id=workspace_id, project_id=project_id,
                        brief_version_id=raw["brief_version_id"], conflict_id=chosen[0], selected_fact_id=chosen[1],
                        expected_project_version=project["project_version"])
                    raw = service.brief_adapter.service.read_current_formal(auth_token, workspace_id=workspace_id, project_id=project_id)
                consumed.append({**item, "brief_version_id": raw.get("brief_version_id"),
                    "input_payload": raw.get("brief", {}).get("payload")})
            current = repo.read()
            draft = current["body"]["draft"]
            consumed_ids = {item["answer_id"] for item in consumed}
            # Finish input bookkeeping before freezing the proposal revision.
            # A later metadata-only revision would otherwise make it stale.
            with repo.transaction(expected_project_version=current["project_version"], expected_revision=current["revision"], advance_project_version=False):
                repo.body["pending_answers"] = [item for item in repo.body.get("pending_answers", []) if item["answer_id"] not in consumed_ids]
                previous = {item['answer_id'] for item in repo.body.get('consumed_answers', [])}
                repo.body.setdefault("consumed_answers", []).extend(item for item in consumed if item['answer_id'] not in previous)
                if draft and all(item.get('origin') == 'confirmed_brief' for item in consumed):
                    repo.body["input_notice"] = "需求已确认，当前初稿保留；请核对相关修改建议。"
                else:
                    repo.body["input_notice"] = "补充资料已保存，当前初稿保留；请核对相关修改建议。正式规划前仍需确认资料。" if draft else "补充资料已保存，可以继续起稿。"
            if draft:
                current = repo.read()
                updates, _ = outstanding_answers(repo, current["body"], service.tasks, auth_token,
                    service.brief_adapter.service.read_current_formal(auth_token, workspace_id=workspace_id, project_id=project_id),
                    service.brief_adapter.service)
                instruction = "依据所有尚未落实的有效补充资料，基于当前初稿重建修改建议；只更新受这些信息影响的内容，保留手动编辑、其他有效内容和仍未知的待补项。"
                if updates and all(item['field'] in {'目标时长', '视频时长'} for item in updates):
                    instruction = ("按已确认目标调整逐段时长，使全部段落的总时长满足需求；只调整必要的时长字段，"
                        "不要修改画面描述与声音；不要修改段落名称和顺序；保留手动编辑与已保护段落。")
                propose(service, auth_token, workspace_id=workspace_id, project_id=project_id,
                    expected_project_version=current["project_version"], expected_revision=current["revision"],
                    instruction=instruction,
                    selected_block_ids=[shot["shot_id"] for shot in draft["shots"]],
                    operation_key="input-update-" + sha256_json({"answers": [item["answer_id"] for item in queue], "revision": current["revision"]}),
                    parent_task_id=parent_task_id, input_updates=[{"field": item["field"], "value": item["value"]} for item in updates],
                    answer_ids=[item["answer_id"] for item in updates])
                updated = repo.read()
                task = service.tasks.get_task(auth_token, updated["body"]["active_task_id"])
                if task["status"] not in {"waiting_user", "completed"}:
                    raise WorkspaceServiceError("CREATIVE_INPUT_UPDATE_PENDING", "补充资料已保存，修改建议尚未完成，可稍后继续。")
            return True
        except Exception as exc:
            latest = repo.read()
            with repo.transaction(expected_project_version=latest["project_version"], expected_revision=latest["revision"], advance_project_version=False):
                pending_fields = {item['field'] for item in repo.body.get('pending_answers', [])}
                repo.body.setdefault('pending_answers', []).extend(item for item in queue if item['field'] not in pending_fields)
                repo.body["input_notice"] = "补充内容已保留，修改建议尚未准备完成。" + (exc.message if isinstance(exc, WorkspaceServiceError) else "请稍后继续处理。")
            raise
