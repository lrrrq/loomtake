"""Frozen creative input adapters for the canonical Formal planning pipeline.

No matching or version logic lives here. The planner assigns event identities;
the adapter checks that each accepted block retains one explicit target.
"""

from copy import deepcopy

from .creative_generation import repository
from .run_contract import sha256_json
from .workspace_store import WorkspaceServiceError


def freeze(service, auth_token, *, workspace_id, project_id, expected_revision):
    state = repository(service, auth_token, workspace_id, project_id).read()
    if state["revision"] != expected_revision:
        raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "初稿已变化，请先保存并重新查看规划")
    draft = state["body"].get("draft")
    if not draft or not draft.get("shots"):
        raise WorkspaceServiceError("CREATIVE_DRAFT_REQUIRED", "请先完成并保存初稿")
    targets = {row['shot_id'] for row in draft['shots']}
    references = [ref for ref in state['body'].get('image_references', []) if ref['block_id'] in targets]
    return {
        "schema": "mplus-creative-formal-binding/v1",
        "workspace_id": workspace_id, "project_id": project_id,
        "draft_id": draft["draft_id"], "draft_revision": draft["revision"],
        "draft_sha256": sha256_json(draft),
        "payload_ref": state["payload_ref"], "payload_sha256": state["payload_sha256"],
        "protected_block_ids": [identity for identity in state["body"].get("assistant_protected_ids", [])
                                if identity in {row["shot_id"] for row in draft["shots"]}],
        "protection_sha256": sha256_json(state["body"].get("assistant_protected_ids", [])),
        "image_references": deepcopy(references),
        "image_references_sha256": sha256_json(references),
        "blocks": [{"block_id": row["shot_id"], "fields": deepcopy(row["fields"]),
                    "source_refs": deepcopy(row.get("source_refs") or [])}
                   for row in draft["shots"]],
    }


def require_current(service, auth_token, *, workspace_id, project_id, binding):
    if not binding:
        return
    if (binding.get("workspace_id"), binding.get("project_id")) != (workspace_id, project_id):
        raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "初稿规划不属于当前项目")
    state = repository(service, auth_token, workspace_id, project_id).read()
    draft = state["body"].get("draft")
    targets = {row['shot_id'] for row in (draft or {}).get('shots', [])}
    references = [ref for ref in state['body'].get('image_references', []) if ref['block_id'] in targets]
    if (not draft or sha256_json(draft) != binding.get("draft_sha256")
            or sha256_json(state["body"].get("assistant_protected_ids", [])) != binding.get("protection_sha256", sha256_json([]))
            or sha256_json(references) != binding.get("image_references_sha256", sha256_json([]))):
        raise WorkspaceServiceError("CREATIVE_PLAN_STALE", "初稿在规划后有修改，请按最新初稿重新生成规划")


def creative_events(binding):
    """Adapt accepted expressions, without presenting them as confirmed facts."""
    events = []
    for index, block in enumerate(binding.get("blocks") or [], start=1):
        fields = block["fields"]
        title = "：".join(filter(None, (fields.get("段落", ""), fields["画面描述"])))
        evidence = {"type": "creative_draft", "block_id": block["block_id"],
            "text": fields["画面描述"],
            "draft_id": binding["draft_id"], "draft_revision": binding["draft_revision"],
            "payload_ref": binding["payload_ref"], "payload_sha256": binding["payload_sha256"]}
        events.append({"event_id": "creative_" + block["block_id"], "order": index,
            "title": title, "text": fields["画面描述"],
            "kind": "flow_event", "primary": True, "required": False, "mandatory": False,
            "fact_status": "creative_expression", "brief_evidence": evidence,
            "evidence_refs": [evidence], "declared_order": index, "allow_non_linear": False,
            "non_linear_reason": "", "shot_units": 1, "technique_overrides": {},
            "creative_section": fields.get("段落", ""),
            "creative_block_id": block["block_id"]})
    return events


def bind_plan_rows(script_rows, shot_plans, binding):
    """Publish explicit block-to-shot mappings after canonical event placement."""
    if not binding:
        return
    previous = -1
    for block in binding["blocks"]:
        event_id = "creative_" + block["block_id"]
        targets = [i for i, shot in enumerate(shot_plans) if event_id in shot.get("event_ids", [])]
        if len(targets) != 1 or targets[0] <= previous:
            raise WorkspaceServiceError("CREATIVE_PLAN_MAPPING_REQUIRED", "当前镜数无法独立容纳已选初稿，请调整镜数或初稿后重新规划")
        index = targets[0]
        if shot_plans[index].get("creative_block"):
            raise WorkspaceServiceError("CREATIVE_PLAN_MAPPING_REQUIRED", "当前规划合并了多个初稿段落，请增加镜数或调整初稿后重新规划")
        previous = index
        # The explicit mapping is created here, never inferred from a filename
        # or reused display number after a later version revision.
        mapped = {"block_id": block["block_id"], "fields": deepcopy(block["fields"]),
            "draft_id": binding["draft_id"], "draft_revision": binding["draft_revision"],
            "draft_sha256": binding["draft_sha256"],
            "source_refs": deepcopy(block["source_refs"])}
        mapped["assistant_protected"] = block["block_id"] in binding.get("protected_block_ids", [])
        mapped["image_references"] = [deepcopy(ref) for ref in binding.get("image_references", [])
                                      if ref["block_id"] == block["block_id"]]
        shot_plans[index]["creative_block"] = mapped
        script_rows[index].update({"画面描述": block["fields"]["画面描述"],
                                   "声音": block["fields"]["声音"]})
        section = str(block["fields"].get("段落") or "").strip()
        if section:
            script_rows[index]["备注"] = "；".join(filter(None, (
                str(script_rows[index].get("备注") or "").strip(), "初稿段落：" + section,
            )))


def protected_text(source):
    return {str(shot["shot_no"]): shot["accepted_creative_fields"]
            for shot in source.get("shots") or [] if shot.get("accepted_creative_fields")}


def validate_preserved_text(payload, source):
    from .script_draft import ScriptDraftError
    protected = protected_text(source)
    for shot in payload.get("shots") or []:
        fields = protected.get(str(shot.get("shot_no")))
        if fields and any(shot.get(key) != fields[key] for key in fields):
            raise ScriptDraftError("正式转换修改了已接受的初稿文字；请保留初稿后重新生成，或先在初稿中明确修改")


def inherit_images(service, auth_token, *, workspace_id, project_id, version_id):
    """Replay only the selected first-draft references through canonical progress.

    Progress starts at revision zero. Any later human selection conflicts instead
    of being overwritten. Completed progress requests provide durable replay.
    """
    from .creative_images import load_result
    from .generation_stages import read_script_plan, read_visual_binding
    from .version_delivery import resolve_version_shot_ids
    revision = service.creative_revision
    if revision is None or revision.image_workspace is None:
        raise WorkspaceServiceError("IMAGE_WORKSPACE_UNAVAILABLE", "参考图片已保留，请接入配图工作区后继续")
    image = revision.image_workspace
    project, context = image._current_context(auth_token, workspace_id, project_id, None)
    if context is None or context.version_id != version_id:
        raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "正式版本已变化，请在当前稿重新核对图片")
    plan = read_script_plan(service.services.artifacts.resolve_stage_artifact(
        context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256).path)
    binding = plan.constraints.get("creative_draft_binding") or {}
    all_refs = binding.get("image_references", [])
    refs = [ref for ref in all_refs if ref.get('primary', True)]
    if len({ref['block_id'] for ref in refs}) != len(refs):
        raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '有段落指定了多张当前采用图，请回初稿核对')
    protected = binding.get("protected_block_ids", [])
    protection_key = "creative-protection:" + binding.get("payload_sha256", "")
    with service.services.projects.store.connection() as connection:
        version = connection.execute("SELECT parent_version_id FROM project_versions WHERE version_id=?", (version_id,)).fetchone()
        if version["parent_version_id"] is not None:
            raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "请在当前稿按镜头重新选择原初稿图片")
        actor_id = repository(service, auth_token, workspace_id, project_id).read()["actor_id"]
        receipt = connection.execute("SELECT response_json FROM formal_image_progress_requests WHERE actor_id=? AND project_id=? AND idempotency_key=?",
            (actor_id, project_id, "creative-images:" + binding.get("payload_sha256", ""))).fetchone()
        protection_receipt = connection.execute(
            "SELECT 1 FROM formal_script_assistant_lock_requests WHERE project_id=? AND idempotency_key=? LIMIT 1",
            (project_id, protection_key)).fetchone()
    if not refs and not protected:
        return {"status": "not_required", "count": 0}
    visual = read_visual_binding(service.services.artifacts.resolve_stage_artifact(
        context.run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256).path)
    identities = resolve_version_shot_ids(service.services.artifacts, context, visual.script_rows)
    mappings = {}
    for shot in plan.shot_plans:
        block = shot.get("creative_block") or {}
        if block.get("block_id"):
            mappings.setdefault(block["block_id"], []).append(identities.get(str(shot["shot_no"])))
    if protected and not protection_receipt:
        if not getattr(revision, 'workspace', None):
            raise WorkspaceServiceError('FORMAL_SCRIPT_UNAVAILABLE', '初稿保护已保留，请接入脚本工作区后继续')
        if any(len(mappings.get(block, [])) != 1 or not mappings[block][0] for block in protected):
            raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '初稿保护范围未能明确对应正式镜头，请重新核对规划')
        existing = revision.workspace.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)
        revision.workspace.set_assistant_locks(auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=context.project_lock_version,
            locked_shot_ids=sorted(set(existing.get('assistant_locked_shot_ids', [])) | {mappings[block][0] for block in protected}),
            idempotency_key=protection_key)
    if receipt:
        import json
        restored = json.loads(receipt['response_json'])
        return {'status': 'progress_saved', 'count': len(refs), 'progress_revision': restored['revision'], 'protected_count': len(protected)}
    if not refs:
        return {'status': 'not_required', 'count': 0, 'protected_count': len(protected)}
    # Every correspondence must be explicit before adopting any material.
    if any(len(mappings.get(ref["block_id"], [])) != 1 or not mappings[ref["block_id"]][0] for ref in refs):
        raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "有参考图不能明确对应正式镜头，请重新选择")
    brief_id, snapshot_id = image._brief_binding(context, plan.confirmed_brief_input)
    selections = []
    library_service = getattr(revision, 'default_images', None)
    library_keys = [ref['candidate_key'] for ref in refs if ref.get('kind') == 'library']
    if library_keys and library_service is None:
        raise WorkspaceServiceError('ASSET_PACKAGE_UNAVAILABLE', '初稿参考图已保留，请恢复创意库后继续接入')
    library_refs = library_service.progress_references(auth_token, workspace_id=workspace_id, project_id=project_id,
        expected_project_version=context.project_lock_version, candidate_keys=library_keys) if library_keys else {}
    project_refs = image._progress_project_references(auth_token, context) if any(ref.get('kind') == 'project' for ref in refs) else {}
    for ref in refs:
        if ref.get('kind') in {'library', 'project'}:
            if ref['kind'] == 'library':
                current = library_refs.get(ref['candidate_key'])
                if not current or not image._same_progress_identity(ref, current):
                    raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '初稿图库来源已变化，冻结参考图保留，请重新核对采用图')
                selection = {'library_candidate_key': ref['candidate_key']}
            else:
                current = project_refs.get(ref['image_key'])
                if not current or current['content_sha256'] != ref['content_sha256'] or current['sources'] != ref['sources']:
                    raise WorkspaceServiceError('IMAGE_ARTIFACT_DRIFT', '初稿项目图片身份已变化，参考图保留，请重新核对')
                selection = {'image_key': ref['image_key']}
            selections.append({'shot_id': mappings[ref['block_id']][0], 'checked': False,
                               'selection_active': True, **selection})
            continue
        task, _, metadata, _ = load_result(service, auth_token, workspace_id=workspace_id,
            project_id=project_id, task_id=ref["task_id"])
        if task["result_ref"] != ref["result_ref"] or metadata["image"] != ref["image"]:
            raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "初稿参考图与冻结的正式来源不一致")
        image_id = service.services.images.adopt_generated_image(auth_token, workspace_id=workspace_id,
            project_id=project_id, brief_version_id=brief_id, brief_snapshot_id=snapshot_id,
            expected_project_lock_version=context.project_lock_version, task_id=ref["task_id"],
            result_ref=task["result_ref"], result_sha256=sha256_json(metadata), metadata=metadata)
        selections.append({"shot_id": mappings[ref["block_id"]][0], "image_key": image_id,
                           "checked": False, "selection_active": True})
    result = image.save_progress(auth_token, workspace_id=workspace_id, project_id=project_id,
        base_version_id=version_id, expected_project_version=context.project_lock_version,
        expected_revision=0, expected_script_revision=0, selections=selections,
        idempotency_key="creative-images:" + binding["payload_sha256"],
        **({'library_service': library_service} if library_keys else {}))
    return {"status": "progress_saved", "count": len(selections), "progress_revision": result["revision"],
            "message": "初稿参考图已对应到正式镜头，需完成图片分析与人工核对。"}
