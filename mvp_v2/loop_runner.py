from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from .ai_spec import load_ai_spec_shots
from .asset_package import asset_package_source_record, package_script_catalog
from .audit import add_audit_checks, audit_generation, audit_output_workbook_format, audit_rows, write_audit_json
from .client_quality import audit_client_visible_content
from .excel_io import (
    load_creative_variants,
    load_template_slots,
    project_hint_from_brief,
    write_template_output_workbook,
)
from .generator import generate_template_rows, rewrite_template_rows_from_visual_evidence
from .generation_stages import (
    _traceability_with_output,
    read_script_draft,
    read_script_plan,
    read_visual_binding,
    run_machine_audit_stage,
    run_script_draft_stage,
    run_script_plan_stage,
    run_visual_binding_stage,
    run_workbook_render_stage,
)
from .scoring import match_template_slots
from .visual_assets import (
    apply_visual_analysis,
    attach_visual_assets,
    hydrate_visual_asset_identities,
    audit_candidate_visual_coverage,
    audit_visual_evidence,
    rematch_visual_assets,
)
from .visual_function import annotate_visual_function_fields, audit_visual_function_quality
from .image_understanding import audit_image_analysis_provenance
from .models import validate_script_brief
from .planning_core import compile_plan_artifact, creative_standard_draft_hits
from .script_type_adapter import (
    category_id_for_script_type,
    normalize_script_type,
    resolve_script_type_workbook,
)


@dataclass(frozen=True)
class LoopIteration:
    index: int
    output_path: Path
    audit_path: Path
    audit: dict


@dataclass(frozen=True)
class LoopResult:
    exit_code: int
    output_path: Path
    audit_path: Path
    log_path: Path
    iterations: list[LoopIteration]
    passed_iteration: int | None = None
    best_failed_output_path: Path | None = None
    best_failed_audit_path: Path | None = None


def audit_generation_content(
    binding,
    *,
    require_complete_visuals: bool = False,
    image_analysis_rows: list[dict] | None = None,
    visual_analysis: dict | None = None,
    candidate_paths: list[Path] | None = None,
    revision: bool = False,
) -> dict:
    """The shared generation/revision audit; callers add the same file checks."""
    audit = audit_generation(
        binding.brief, binding.template_slots, binding.decisions,
        binding.script_rows, binding.detail_rows,
        visual_asset_rows=binding.visual_asset_rows,
        require_complete_visuals=require_complete_visuals, revision=revision,
        asset_package_evidence=(getattr(binding, 'constraints', {}) or {}).get('formal_asset_package'),
    )
    audit = add_audit_checks(audit, audit_visual_evidence(binding.detail_rows))
    audit = add_audit_checks(audit, audit_visual_function_quality(
        binding.detail_rows, normalize_script_type(binding.brief.get("脚本类型") or "发布会"),
    ))
    audit = _add_visual_retry_constraints(audit, binding.detail_rows)
    audit = add_audit_checks(audit, audit_client_visible_content(
        binding.brief, binding.script_rows, binding.detail_rows,
        overall_summary=binding.overall_summary,
        pending_confirmations=binding.pending_confirmations,
        quality_profile=binding.quality_profile,
    ))
    audit = add_audit_checks(audit, [{
        "audit_domain": "script_document", "检查项": "requirement_output_pending_review",
        "级别": "warning", "状态": "warning",
        "关联镜号": "、".join(str(value) for value in item.get("plan_shot_nos") or []),
        "详情": f"已规划要求‘{item.get('brief_text') or '未提供原文'}’尚不能由当前成稿直接证明",
        "修复建议": "核对需求与当前镜头；确需改写时通过修订提案处理，事实待人工确认",
    } for item in getattr(binding, "traceability", [])
        if item.get("output_status") == "pending_review"
        and item.get("final_status") not in {"optional", "not_applicable", "review_proposed"}])
    analysis_rows = image_analysis_rows if image_analysis_rows is not None else getattr(binding, "image_analysis_rows", [])
    if analysis_rows:
        audit = add_audit_checks(audit, audit_image_analysis_provenance(analysis_rows))
    if visual_analysis and candidate_paths is not None and not analysis_rows:
        audit = add_audit_checks(audit, audit_candidate_visual_coverage(visual_analysis, candidate_paths))
    audit = add_audit_checks(audit, [{
        "audit_domain": "script_document", "检查项": "creative_standard_avoid_expression_hit",
        "级别": "hard", "状态": "fail", "关联镜号": str(hit["shot_no"]),
        "详情": f"镜 {hit['shot_no']} 的{hit['field']}命中已确认创作标准卡禁止表达",
        "修复建议": "按已确认创作标准修正表达后重新保存",
    } for hit in creative_standard_draft_hits(
        binding.constraints, binding.script_rows, extra_texts=[binding.overall_summary],
    )])
    return audit


def run_generation_once(
    briefs: list[dict],
    source_root: str | Path,
    output: str | Path,
    audit_json_path: str | Path | None = None,
    constraints: dict | None = None,
    vision_analysis: dict | None = None,
    rematch_visuals: bool = False,
    image_manifest_rows: list[dict] | None = None,
    image_analysis_rows: list[dict] | None = None,
    source_brand_review_path: str | Path | None = None,
    source_view: str = "human",
    quality_profile: str = "draft",
    text_model: str = "MiniMax-M2.7",
    text_command: str = "mmx",
    writer_provider: str = "minimax-cli",
    writer_model: str | None = None,
    editor_provider: str = "minimax-cli",
    editor_model: str | None = None,
    writer_text_provider=None,
    editor_text_provider=None,
    runtime=None,
    confirmed_brief_input: object | None = None,
    confirmed_brief_resolver=None,
    asset_package: str | Path | None = None,
) -> LoopIteration:
    output_path = Path(output)
    audit_path = Path(audit_json_path) if audit_json_path else _default_audit_path(output_path)
    if runtime is not None:
        return _run_generation_once_staged(
            runtime,
            briefs=briefs,
            source_root=source_root,
            output_path=output_path,
            audit_path=audit_path,
            constraints=constraints or {},
            vision_analysis=vision_analysis,
            rematch_visuals=rematch_visuals,
            image_manifest_rows=image_manifest_rows,
            image_analysis_rows=image_analysis_rows,
            source_brand_review_path=source_brand_review_path,
            source_view=source_view,
            asset_package=asset_package,
            quality_profile=quality_profile,
            text_model=text_model,
            text_command=text_command,
            writer_provider=writer_provider,
            writer_model=writer_model,
            editor_provider=editor_provider,
            editor_model=editor_model,
            writer_text_provider=writer_text_provider,
            editor_text_provider=editor_text_provider,
            confirmed_brief_input=confirmed_brief_input,
            confirmed_brief_resolver=confirmed_brief_resolver,
        )
    if confirmed_brief_input is not None or confirmed_brief_resolver is not None:
        raise ValueError("ConfirmedBriefInput 仅支持带 PipelineRuntime 的正式分阶段生成")
    generated = _generate(
        briefs,
        source_root,
        constraints or {},
        source_brand_review_path,
        source_view,
        legacy_human_compatibility=(source_view == "human"),
        asset_package=asset_package,
    )
    script_rows, detail_rows, decisions, template_slots, brief = generated[:5]
    detail_rows, visual_asset_rows = attach_visual_assets(script_rows, detail_rows)
    detail_rows, visual_asset_rows = hydrate_visual_asset_identities(
        detail_rows,
        visual_asset_rows,
        image_manifest_rows,
    )
    candidate_paths = []
    visual_analysis = vision_analysis or _legacy_analysis_from_image_index(image_manifest_rows, image_analysis_rows)
    if visual_analysis:
        detail_rows, visual_asset_rows = apply_visual_analysis(
            script_rows,
            detail_rows,
            visual_asset_rows,
            visual_analysis,
        )
        if rematch_visuals:
            candidate_paths = _candidate_image_paths(source_root, brief)
            script_rows, detail_rows, visual_asset_rows = rematch_visual_assets(
                script_rows,
                detail_rows,
                visual_asset_rows,
                visual_analysis,
                candidate_paths,
                manifest_rows=image_manifest_rows,
                analysis_rows=image_analysis_rows,
                brief=brief,
                visual_excludes_by_slot=(constraints or {}).get("exclude_visuals_by_slot", {}),
            )
        script_rows, detail_rows, visual_asset_rows = rewrite_template_rows_from_visual_evidence(
            brief,
            script_rows,
            detail_rows,
            visual_asset_rows,
        )
    annotate_visual_function_fields(
        detail_rows,
        visual_asset_rows,
        normalize_script_type(brief.get("脚本类型") or "发布会"),
    )
    planning_payload = generated[5] if len(generated) > 5 else {}
    audit = audit_generation_content(
        SimpleNamespace(
            brief=brief, template_slots=template_slots, decisions=decisions,
            script_rows=script_rows, detail_rows=detail_rows, visual_asset_rows=visual_asset_rows,
            overall_summary="", pending_confirmations=[], quality_profile=quality_profile,
            constraints=constraints or {},
            traceability=_traceability_with_output(
                planning_payload.get("traceability") or [],
                script_rows,
                planning_payload.get("shot_plans") or [],
            ),
        ),
        require_complete_visuals=bool(image_manifest_rows and image_analysis_rows),
        image_analysis_rows=image_analysis_rows,
        visual_analysis=visual_analysis if rematch_visuals else None,
        candidate_paths=candidate_paths,
    )
    script_type = normalize_script_type(brief.get("脚本类型") or "发布会")
    template_workbook_path = (
        Path(asset_package_source_record(
            asset_package, expected_category_id=category_id_for_script_type(script_type),
        )["source_workbook_path"])
        if asset_package else resolve_script_type_workbook(source_root, script_type)
    )
    write_template_output_workbook(
        output_path,
        script_rows,
        detail_rows,
        [],
        template_workbook_path=template_workbook_path,
        visual_asset_rows=visual_asset_rows,
        script_type=script_type,
        duration_seconds=validate_script_brief(brief),
    )
    format_checks = audit_output_workbook_format(
        output_path,
        expected_image_count=_expected_embedded_image_count(script_rows),
        template_workbook_path=template_workbook_path,
        script_type=script_type,
        duration_seconds=validate_script_brief(brief),
    )
    audit = add_audit_checks(audit, format_checks)
    write_template_output_workbook(
        output_path,
        script_rows,
        detail_rows,
        audit_rows(audit),
        template_workbook_path=template_workbook_path,
        visual_asset_rows=visual_asset_rows,
        script_type=script_type,
        duration_seconds=validate_script_brief(brief),
    )
    write_audit_json(audit_path, audit)
    return LoopIteration(index=1, output_path=output_path, audit_path=audit_path, audit=audit)


def run_generation_loop(
    briefs: list[dict],
    source_root: str | Path,
    output: str | Path,
    max_loops: int = 5,
    audit_json_path: str | Path | None = None,
    vision_analysis: dict | None = None,
    rematch_visuals: bool = False,
    image_manifest_rows: list[dict] | None = None,
    image_analysis_rows: list[dict] | None = None,
    source_brand_review_path: str | Path | None = None,
    source_view: str = "human",
    quality_profile: str = "draft",
    text_model: str = "MiniMax-M2.7",
    text_command: str = "mmx",
    writer_provider: str = "minimax-cli",
    writer_model: str | None = None,
    editor_provider: str = "minimax-cli",
    editor_model: str | None = None,
    writer_text_provider=None,
    editor_text_provider=None,
    initial_constraints: dict | None = None,
    runtime=None,
    confirmed_brief_input: object | None = None,
    confirmed_brief_resolver=None,
) -> LoopResult:
    output_path = Path(output)
    final_audit_path = Path(audit_json_path) if audit_json_path else _default_audit_path(output_path)
    loop_log_path = _loop_log_path(output_path)
    max_loops = max(1, int(max_loops or 1))
    constraints: dict = _merge_constraints({}, initial_constraints or {})
    iterations: list[LoopIteration] = []
    iteration_logs: list[dict] = []
    previous_failure_signature = None
    stop_reason = "max_loops"

    for index in range(1, max_loops + 1):
        loop_output = _loop_output_path(output_path, index)
        loop_audit = _loop_audit_path(output_path, index)
        iteration = run_generation_once(
            briefs,
            source_root,
            loop_output,
            loop_audit,
            constraints=constraints,
            vision_analysis=vision_analysis,
            rematch_visuals=rematch_visuals,
            image_manifest_rows=image_manifest_rows,
            image_analysis_rows=image_analysis_rows,
            source_brand_review_path=source_brand_review_path,
            source_view=source_view,
            quality_profile=quality_profile,
            text_model=text_model,
            text_command=text_command,
            writer_provider=writer_provider,
            writer_model=writer_model,
            editor_provider=editor_provider,
            editor_model=editor_model,
            writer_text_provider=writer_text_provider,
            editor_text_provider=editor_text_provider,
            runtime=runtime,
            confirmed_brief_input=confirmed_brief_input,
            confirmed_brief_resolver=confirmed_brief_resolver,
        )
        iteration = LoopIteration(
            index=index,
            output_path=iteration.output_path,
            audit_path=iteration.audit_path,
            audit=iteration.audit,
        )
        iterations.append(iteration)
        iteration_logs.append(_iteration_log(iteration))

        if iteration.audit["status"] == "pass":
            shutil.copyfile(loop_output, output_path)
            shutil.copyfile(loop_audit, final_audit_path)
            _write_loop_log(
                loop_log_path,
                "pass",
                "passed",
                iteration_logs,
                passed_iteration=index,
            )
            return LoopResult(
                exit_code=0,
                output_path=output_path,
                audit_path=final_audit_path,
                log_path=loop_log_path,
                iterations=iterations,
                passed_iteration=index,
            )

        signature = _failure_signature(iteration.audit)
        if previous_failure_signature == signature:
            stop_reason = "repeated_failure_signature"
            break
        previous_failure_signature = signature
        constraints = _merge_constraints(constraints, iteration.audit.get("next_constraints", {}))

    best = _best_iteration(iterations)
    best_output = _best_failed_output_path(output_path)
    best_audit = _best_failed_audit_path(output_path)
    shutil.copyfile(best.output_path, best_output)
    shutil.copyfile(best.audit_path, best_audit)
    _write_loop_log(
        loop_log_path,
        "failed",
        stop_reason,
        iteration_logs,
        best_failed_output_path=best_output,
        best_failed_audit_path=best_audit,
    )
    return LoopResult(
        exit_code=2,
        output_path=output_path,
        audit_path=final_audit_path,
        log_path=loop_log_path,
        iterations=iterations,
        best_failed_output_path=best_output,
        best_failed_audit_path=best_audit,
    )


def _run_generation_once_staged(
    runtime,
    *,
    briefs: list[dict],
    source_root: str | Path,
    output_path: Path,
    audit_path: Path,
    constraints: dict,
    vision_analysis: dict | None,
    rematch_visuals: bool,
    image_manifest_rows: list[dict] | None,
    image_analysis_rows: list[dict] | None,
    source_brand_review_path: str | Path | None,
    source_view: str,
    asset_package: str | Path | None,
    quality_profile: str,
    text_model: str,
    text_command: str,
    writer_provider: str,
    writer_model: str | None,
    editor_provider: str,
    editor_model: str | None,
    writer_text_provider,
    editor_text_provider,
    confirmed_brief_input: object | None,
    confirmed_brief_resolver,
) -> LoopIteration:
    planning_constraints = _constraints_for_planning(constraints)
    drafting_constraints = _constraints_for_drafting(constraints)
    image_only_feedback = _is_image_only_feedback(constraints)
    reuse_plan = _can_reuse_stage_output(
        runtime,
        "script_plan",
        "script_plan.json",
        planning_constraints,
    )
    if reuse_plan:
        plan = read_script_plan(runtime.run_dir / "script_plan.json")
    else:
        plan = run_script_plan_stage(
            runtime,
            briefs=briefs,
            constraints=planning_constraints,
            source_view=source_view,
            builder=lambda planning_briefs=None, active_constraints=planning_constraints: _generate(
                planning_briefs if planning_briefs is not None else briefs,
                source_root,
                active_constraints,
                source_brand_review_path,
                source_view,
                asset_package=asset_package,
            ),
            confirmed_brief_input=confirmed_brief_input,
            confirmed_brief_resolver=confirmed_brief_resolver,
        )
    reuse_draft = reuse_plan and _can_reuse_stage_output(
        runtime,
        "script_draft",
        "script_draft.json",
        drafting_constraints,
    )
    if reuse_draft:
        draft = read_script_draft(runtime.run_dir / "script_draft.json")
    else:
        draft = run_script_draft_stage(
            runtime,
            script_plan_path=plan.path,
            quality_profile=quality_profile,
            text_model=text_model,
            text_command=text_command,
            writer_provider_name=writer_provider,
            writer_model=writer_model,
            editor_provider_name=editor_provider,
            editor_model=editor_model,
            writer_provider=writer_text_provider,
            editor_provider=editor_text_provider,
            revision_constraints=drafting_constraints,
            parent_draft_path=(
                runtime.run_dir.parent
                / str(constraints.get("parent_run_id") or "")
                / "script_draft.json"
                if _has_targeted_non_visual_feedback(constraints)
                else None
            ),
            image_manifest_rows=image_manifest_rows,
            image_analysis_rows=image_analysis_rows,
        )
    visual_analysis = vision_analysis or _legacy_analysis_from_image_index(
        image_manifest_rows,
        image_analysis_rows,
    )

    def bind_visuals(reloaded_plan):
        script_rows = reloaded_plan.script_rows
        detail_rows = reloaded_plan.detail_rows
        detail_rows, visual_asset_rows = attach_visual_assets(script_rows, detail_rows)
        detail_rows, visual_asset_rows = hydrate_visual_asset_identities(
            detail_rows,
            visual_asset_rows,
            image_manifest_rows,
        )
        if visual_analysis:
            detail_rows, visual_asset_rows = apply_visual_analysis(
                script_rows,
                detail_rows,
                visual_asset_rows,
                visual_analysis,
            )
            if rematch_visuals:
                script_rows, detail_rows, visual_asset_rows = rematch_visual_assets(
                    script_rows,
                    detail_rows,
                    visual_asset_rows,
                    visual_analysis,
                    _candidate_image_paths(source_root, reloaded_plan.brief),
                    manifest_rows=image_manifest_rows,
                    analysis_rows=image_analysis_rows,
                    brief=reloaded_plan.brief,
                    visual_excludes_by_slot=constraints.get("exclude_visuals_by_slot", {}),
                    visual_preferences_by_slot=_visual_preferences_from_feedback(constraints),
                    forced_visuals_by_slot=_forced_visuals_from_feedback(constraints),
                )
            script_rows, detail_rows, visual_asset_rows = (
                rewrite_template_rows_from_visual_evidence(
                    reloaded_plan.brief,
                    script_rows,
                    detail_rows,
                    visual_asset_rows,
                )
            )
        annotate_visual_function_fields(
            detail_rows,
            visual_asset_rows,
            normalize_script_type(reloaded_plan.brief.get("脚本类型") or "发布会"),
        )
        if image_only_feedback or _has_targeted_non_visual_feedback(constraints):
            script_rows, detail_rows, visual_asset_rows = _preserve_unaffected_parent_visuals(
                runtime,
                constraints,
                script_rows,
                detail_rows,
                visual_asset_rows,
                visual_only=not image_only_feedback,
            )
            detail_rows, visual_asset_rows = hydrate_visual_asset_identities(
                detail_rows,
                visual_asset_rows,
                image_manifest_rows,
            )
            if visual_analysis:
                detail_rows, visual_asset_rows = apply_visual_analysis(
                    script_rows,
                    detail_rows,
                    visual_asset_rows,
                    visual_analysis,
                )
            annotate_visual_function_fields(
                detail_rows,
                visual_asset_rows,
                normalize_script_type(reloaded_plan.brief.get("脚本类型") or "发布会"),
            )
        return script_rows, detail_rows, visual_asset_rows

    binding = run_visual_binding_stage(
        runtime,
        script_draft_path=draft.path,
        binder=bind_visuals,
        semantic_inputs={
            "rematch_visuals": rematch_visuals,
            "vision_analysis": visual_analysis or {},
            "image_manifest_rows": image_manifest_rows or [],
            "image_analysis_rows": image_analysis_rows or [],
            "revision_constraints": constraints,
        },
        revision_constraints=constraints,
    )
    script_type = normalize_script_type(plan.brief.get("脚本类型") or "发布会")
    template_workbook_path = (
        Path(asset_package_source_record(
            asset_package, expected_category_id=category_id_for_script_type(script_type),
        )["source_workbook_path"])
        if asset_package else resolve_script_type_workbook(source_root, script_type)
    )

    def render_workbook(reloaded_binding, target, audit_check_rows):
        write_template_output_workbook(
            target,
            reloaded_binding.script_rows,
            reloaded_binding.detail_rows,
            audit_check_rows,
            template_workbook_path=template_workbook_path,
            visual_asset_rows=reloaded_binding.visual_asset_rows,
            overall_summary=reloaded_binding.overall_summary,
            script_type=script_type,
            duration_seconds=validate_script_brief(reloaded_binding.brief),
        )

    rendered = run_workbook_render_stage(
        runtime,
        visual_binding_path=binding.path,
        renderer=render_workbook,
    )

    def audit_workbook(reloaded_binding, workbook_path):
        audit = audit_generation_content(
            reloaded_binding,
            require_complete_visuals=bool(image_manifest_rows and image_analysis_rows),
            image_analysis_rows=image_analysis_rows,
            visual_analysis=visual_analysis if rematch_visuals else None,
            candidate_paths=_candidate_image_paths(source_root, reloaded_binding.brief),
        )
        return add_audit_checks(
            audit,
            audit_output_workbook_format(
                workbook_path,
                expected_image_count=_expected_embedded_image_count(
                    reloaded_binding.script_rows
                ),
                template_workbook_path=template_workbook_path,
                script_type=script_type,
                duration_seconds=validate_script_brief(reloaded_binding.brief),
            ),
        )

    audited = run_machine_audit_stage(
        runtime,
        visual_binding_path=binding.path,
        workbook_render_path=rendered.path,
        output_path=output_path,
        audit_path=audit_path,
        auditor=audit_workbook,
        renderer=render_workbook,
    )
    return LoopIteration(
        index=1,
        output_path=audited.output_path,
        audit_path=audited.audit_path,
        audit=audited.audit,
    )


def _generate(
    briefs: list[dict],
    source_root: str | Path,
    constraints: dict,
    source_brand_review_path: str | Path | None = None,
    source_view: str = "human",
    *,
    legacy_human_compatibility: bool = False,
    asset_package: str | Path | None = None,
    library_packages: tuple[str | Path, ...] | list[str | Path] = (),
    semantic_policy=None,
    semantic_evidence_path=None,
):
    script_rows = []
    detail_rows = []
    all_decisions = []
    all_projected_slots = []
    brief_plans = []
    first_brief = briefs[0] if briefs else {}
    script_type = normalize_script_type(first_brief.get("脚本类型") or "发布会")
    project_hint = project_hint_from_brief(first_brief)
    if asset_package is not None:
        template_slots, creative_variants, _ = package_script_catalog(
            asset_package,
            expected_category_id=category_id_for_script_type(script_type),
            source_view=source_view,
            library_paths=library_packages,
        )
    elif source_view == "ai-spec":
        workbook_path = resolve_script_type_workbook(source_root, script_type)
        template_slots, creative_variants = load_ai_spec_shots(
            source_root,
            brand_review_path=source_brand_review_path,
            project_hint=project_hint,
            script_type=script_type,
            workbook_path=workbook_path,
        )
    elif source_view == "human":
        workbook_path = resolve_script_type_workbook(source_root, script_type)
        template_slots = load_template_slots(
            source_root,
            project_hint=project_hint,
            script_type=script_type,
            workbook_path=workbook_path,
        )
        creative_variants = load_creative_variants(
            source_root,
            project_hint=project_hint,
            brand_review_path=source_brand_review_path,
            script_type=script_type,
            workbook_path=workbook_path,
        )
    else:
        raise ValueError(f"不支持的 source_view: {source_view}")
    if legacy_human_compatibility:
        for brief in briefs:
            decisions = match_template_slots(
                brief,
                template_slots,
                creative_variants,
                constraints=constraints,
            )
            brief_script_rows, brief_detail_rows = generate_template_rows(brief, decisions)
            script_rows.extend(brief_script_rows)
            detail_rows.extend(brief_detail_rows)
            all_decisions.extend(decisions)
        return script_rows, detail_rows, all_decisions, template_slots, first_brief
    for brief in briefs:
        projection = compile_plan_artifact(
            brief,
            template_slots,
            source_view=source_view,
            constraints=constraints,
        )
        planning_semantics = None
        # Ask for association when deterministic planning would turn a separate
        # requirement into another event. Unambiguous existing bindings need no
        # extra provider call, and trials remain deterministic.
        event_ids = {row['event_id'] for row in projection.events}
        requirement_ids = {row['requirement_id'] for row in projection.requirements} - event_ids
        has_unbound_content = any(event.get('event_id') in requirement_ids
            for shot in projection.shots for event in shot.get('events', []))
        # Capacity conflicts may leave a content requirement outside every
        # allocated shot. It still needs semantic association with an existing
        # event before asking the user to change a valid creative draft.
        content_ids = {row['requirement_id'] for row in projection.requirements
            if row.get('classification') not in {
                'flow_event', 'forbidden', 'style_preference', 'production_requirement'}
            and row.get('origin') != 'review_proposed'}
        has_unbound_content = has_unbound_content or any(
            row.get('requirement_id') in content_ids and row.get('status') in {'uncovered', 'conflict'}
            for row in projection.coverage)
        if semantic_policy is not None and projection.events and has_unbound_content:
            from .scoring import assess_planning_requirements
            planning_semantics = assess_planning_requirements(brief, projection, semantic_policy,
                Path(semantic_evidence_path).with_name('planning_semantics.json') if semantic_evidence_path else None)
            projection = compile_plan_artifact(brief, template_slots, source_view=source_view,
                constraints={**constraints, 'planning_semantics': planning_semantics})
        projected_slots = projection.template_slots
        matching_constraints = constraints
        if semantic_policy is not None:
            from .scoring import assess_template_candidates
            matching_constraints = {**constraints, 'semantic_matching': assess_template_candidates(
                brief, projected_slots, creative_variants, constraints, semantic_policy, semantic_evidence_path)}
        decisions = match_template_slots(
            brief,
            projected_slots,
            creative_variants,
            constraints=matching_constraints,
        )
        decisions, projected_slots = _apply_content_led_structures(
            decisions,
            projection.shots,
            constraints,
        )
        _record_template_structure_downgrades(
            projection.audit, projection.shots,
        )
        projection = replace(projection, template_slots=projected_slots)
        brief_script_rows, brief_detail_rows = generate_template_rows(brief, decisions)
        _apply_plan_evidence(
            brief_script_rows,
            brief_detail_rows,
            projection.shots,
        )
        if constraints.get("creative_draft_binding"):
            from .creative_formal_binding import bind_plan_rows
            bind_plan_rows(brief_script_rows, projection.shots, constraints["creative_draft_binding"])
        script_rows.extend(brief_script_rows)
        detail_rows.extend(brief_detail_rows)
        all_decisions.extend(decisions)
        all_projected_slots.extend(projected_slots)
        brief_plans.append({**projection.to_payload(), **({'semantic_matching': matching_constraints['semantic_matching']}
            if 'semantic_matching' in matching_constraints else {}),
            **({'planning_semantics': planning_semantics} if planning_semantics is not None else {})})
    planning_payload = _merge_brief_plans(brief_plans)
    return (
        script_rows,
        detail_rows,
        all_decisions,
        all_projected_slots,
        first_brief,
        planning_payload,
    )


def _apply_content_led_structures(
    decisions: list,
    shot_plans: list[dict],
    constraints: dict,
) -> tuple[list, list]:
    feedback_structures = _feedback_structures(constraints)
    updated = []
    slots = []
    count = len(shot_plans)
    for index, (decision, shot) in enumerate(zip(decisions, shot_plans), start=1):
        shot_no = str(shot.get("shot_no") or index)
        event_text = str(shot.get("primary_event") or "")
        if shot.get("field_decisions"):
            structure = str(shot.get("structure") or decision.slot.structure)
        elif shot_no in feedback_structures:
            structure = feedback_structures[shot_no]
            shot["structure_source"] = "confirmed_review"
        elif decision.slot.structure and not _structure_conflicts_with_event(
            decision.slot.structure, event_text, index,
        ):
            structure = decision.slot.structure
            shot["structure_source"] = "template_fallback"
        else:
            structure = _structure_for_event(event_text, index, count)
            shot["structure_source"] = "brief"
        shot["structure"] = structure
        slot = replace(decision.slot, structure=structure)
        updated.append(replace(decision, slot=slot))
        slots.append(slot)
    return updated, slots


def _feedback_structures(constraints: dict) -> dict[str, str]:
    result = {}
    for item in constraints.get("user_feedback", []):
        if str(item.get("category") or "").casefold() != "structure":
            continue
        shot_no = str(item.get("shot_no") or "").strip()
        desired = str(item.get("desired_change") or "")
        match = re.search(r"(?:^|；)结构=([^；]+)", desired)
        if shot_no.isdigit() and match:
            result[shot_no] = match.group(1).strip()
    return result


def _record_template_structure_downgrades(audit: dict, shots: list[dict]) -> None:
    downgraded = [
        shot for shot in shots
        if shot.get("structure_source") == "brief"
        and not shot.get("field_decisions")
    ]
    if not downgraded:
        return
    findings = list(audit.get("findings") or [])
    for shot in downgraded:
        findings.append({
            "code": "template_structure_downgraded",
            "severity": "warning",
            "message": (
                f"镜{shot.get('shot_no')}的模板结构与当前事件职责冲突，"
                "已保留 Brief 事件并局部降级结构标签"
            ),
            "requirement_ref": "",
            "risk": "需人工核对该镜与整体模板结构弧是否连续",
            "cannot_downgrade_reason": "",
            "shot_no": str(shot.get("shot_no") or ""),
        })
    audit["findings"] = findings
    audit["warning_codes"] = list(dict.fromkeys([
        *(audit.get("warning_codes") or []), "template_structure_downgraded",
    ]))


def _structure_for_event(event_text: str, index: int, count: int) -> str:
    if index == 1:
        return "开场建立"
    if index == count:
        return "收束落版"
    if any(value in event_text for value in ("发言", "致辞", "演讲", "战略", "策略")):
        return "核心宣讲"
    if any(value in event_text for value in ("颁奖", "仪式", "启动", "点亮")):
        return "仪式推进"
    if any(value in event_text for value in ("表演", "演出", "歌曲", "合唱")):
        return "节目高潮"
    if any(value in event_text for value in ("互动", "打卡", "参观", "茶歇", "领取", "购物", "挑战")):
        return "互动推进"
    if any(value in event_text for value in ("空镜", "场地", "会场", "外场", "内场")):
        return "空间建立"
    return "流程推进"


def _structure_conflicts_with_event(
    current_structure: str,
    event_text: str,
    index: int,
) -> bool:
    if index > 1 and any(value in current_structure for value in ("开场", "开头", "片头")):
        return True
    person_terms = ("艺人", "明星", "嘉宾", "人物亮相")
    return (
        any(value in current_structure for value in person_terms)
        and not any(value in event_text for value in person_terms)
    )


def _apply_plan_evidence(
    script_rows: list[dict],
    detail_rows: list[dict],
    shot_plans: list[dict],
) -> None:
    for script, detail, shot in zip(script_rows, detail_rows, shot_plans):
        primary_event = str(shot.get("primary_event") or "").strip()
        source = dict(shot.get("functional_source") or {})
        brief_evidence = dict(shot.get("brief_evidence") or {})
        detail.update({
            "规划事件ID": shot.get("primary_event_id", ""),
            "规划主事件": primary_event,
            "规划事件集": "、".join(shot.get("event_ids") or []),
            "规划要求集": "、".join(shot.get("requirement_ids") or []),
            "规划Brief证据": brief_evidence.get("text", ""),
            "规划功能来源文件": source.get("source_file", ""),
            "规划功能来源 sheet": source.get("source_sheet", ""),
            "规划功能来源行": source.get("source_row", ""),
            "功能参考声明": shot.get("functional_reference_notice", ""),
            "叙事阶段": shot.get("narrative_stage", ""),
            "叙事衔接理由": shot.get("transition_reason", ""),
            "结构决策来源": shot.get("structure_source", ""),
            "模板功能来源": "#".join(str(source.get(key) or "") for key in (
                "source_file", "source_sheet", "source_row",
            )),
        })
        duration = shot.get("duration_seconds")
        script["时长(秒)"] = duration
        detail["时长(秒)"] = duration


def _merge_brief_plans(brief_plans: list[dict]) -> dict:
    if not brief_plans:
        return {}
    if len(brief_plans) == 1:
        return dict(brief_plans[0])
    statuses = [
        str((plan.get("planning_audit") or {}).get("status") or "")
        for plan in brief_plans
    ]
    merged_status = (
        "blocked"
        if "blocked" in statuses
        else "needs_clarification"
        if "needs_clarification" in statuses
        else "pass"
    )
    merged_facts = []
    merged_events = []
    merged_shots = []
    merged_requirements = []
    merged_coverage = []
    merged_traceability = []
    merged_findings = []
    for brief_index, plan in enumerate(brief_plans, start=1):
        namespace = f"brief_{brief_index:03d}"
        event_ids = {
            str(event.get("event_id") or ""): (
                f"{namespace}:{event.get('event_id')}"
            )
            for event in plan.get("events") or []
        }
        requirement_ids = {
            str(item.get("requirement_id") or ""): (
                f"{namespace}:{item.get('requirement_id')}"
            )
            for item in plan.get("requirements") or []
        }
        for fact in plan.get("brief_facts") or []:
            merged_facts.append({
                **fact,
                "fact_id": f"{namespace}:{fact.get('fact_id')}",
                "brief_index": brief_index,
            })
        for event in plan.get("events") or []:
            merged_events.append({
                **event,
                "event_id": event_ids.get(str(event.get("event_id") or ""), ""),
                "brief_index": brief_index,
            })
        for shot in plan.get("shot_plans") or []:
            merged_shots.append({
                **shot,
                "primary_event_id": event_ids.get(
                    str(shot.get("primary_event_id") or ""), ""
                ),
                "event_ids": [
                    event_ids.get(str(event_id), "")
                    for event_id in shot.get("event_ids") or []
                ],
                "requirement_ids": [
                    requirement_ids.get(str(requirement_id), "")
                    for requirement_id in shot.get("requirement_ids") or []
                ],
                "brief_index": brief_index,
            })
        for requirement in plan.get("requirements") or []:
            merged_requirements.append({
                **requirement,
                "requirement_id": requirement_ids.get(
                    str(requirement.get("requirement_id") or ""), "",
                ),
                "brief_index": brief_index,
            })
        for item in plan.get("coverage") or []:
            merged_coverage.append({
                **item,
                "requirement_id": requirement_ids.get(
                    str(item.get("requirement_id") or ""), "",
                ),
                "brief_index": brief_index,
            })
        for item in plan.get("traceability") or []:
            merged_traceability.append({
                **item,
                "requirement_id": requirement_ids.get(
                    str(item.get("requirement_id") or ""), "",
                ),
                "brief_index": brief_index,
            })
        for finding in (plan.get("planning_audit") or {}).get("findings") or []:
            requirement_ref = str(finding.get("requirement_ref") or "")
            merged_findings.append({
                **finding,
                "requirement_ref": requirement_ids.get(
                    requirement_ref, requirement_ref,
                ),
                "brief_index": brief_index,
            })
    return {
        "plan_schema_version": "mplus-plan-artifact/v4",
        "brief_plans": brief_plans,
        "brief_facts": merged_facts,
        "events": merged_events,
        "event_graph": {"graph_type": "per_brief", "brief_count": len(brief_plans)},
        "duration_budget": {
            "target_seconds": sum(
                int((plan.get("duration_budget") or {}).get("target_seconds") or 0)
                for plan in brief_plans
            ),
            "planned_seconds": sum(
                int((plan.get("duration_budget") or {}).get("planned_seconds") or 0)
                for plan in brief_plans
            ),
            "shot_count": sum(
                int((plan.get("duration_budget") or {}).get("shot_count") or 0)
                for plan in brief_plans
            ),
        },
        "shot_plans": merged_shots,
        "requirements": merged_requirements,
        "coverage": merged_coverage,
        "traceability": merged_traceability,
        "template_rule_contract": dict(brief_plans[0].get("template_rule_contract") or {}),
        "conflicts": [
            {**item, "brief_index": brief_index}
            for brief_index, plan in enumerate(brief_plans, start=1)
            for item in plan.get("conflicts") or []
        ],
        "decision_counts": {
            key: sum(
                int((plan.get("decision_counts") or {}).get(key) or 0)
                for plan in brief_plans
            )
            for key in (
                "requirements_uncovered", "hard_template_downgrades",
                "unresolved_conflicts", "unexplained_overrides",
            )
        },
        "capacity_report": {
            "status": (
                "needs_clarification"
                if any(
                    (plan.get("capacity_report") or {}).get("status") == "needs_clarification"
                    for plan in brief_plans
                ) else "ready"
            ),
            "target_seconds": sum(
                int((plan.get("capacity_report") or {}).get("target_seconds") or 0)
                for plan in brief_plans
            ),
            "required_shot_units": sum(
                int((plan.get("capacity_report") or {}).get("required_shot_units") or 0)
                for plan in brief_plans
            ),
            "mandatory_shot_units": sum(
                int((plan.get("capacity_report") or {}).get("mandatory_shot_units") or 0)
                for plan in brief_plans
            ),
            "requested_shot_count": None,
            "planned_shot_count": len(merged_shots),
            "choices": list(dict.fromkeys(
                choice
                for plan in brief_plans
                for choice in (plan.get("capacity_report") or {}).get("choices") or []
            )),
        },
        "planning_audit": {
            "status": merged_status,
            "blocking_codes": list(dict.fromkeys(
                code
                for plan in brief_plans
                for code in (plan.get("planning_audit") or {}).get("blocking_codes") or []
            )),
            "clarification_codes": list(dict.fromkeys(
                code
                for plan in brief_plans
                for code in (plan.get("planning_audit") or {}).get("clarification_codes") or []
            )),
            "warning_codes": list(dict.fromkeys(
                code
                for plan in brief_plans
                for code in (plan.get("planning_audit") or {}).get("warning_codes") or []
            )),
            "findings": merged_findings,
        },
        "planning_source_status": {
            "source_view": (brief_plans[0].get("planning_source_status") or {}).get("source_view", ""),
            "ai_spec_ready": all(
                bool((plan.get("planning_source_status") or {}).get("ai_spec_ready"))
                for plan in brief_plans
            ),
            "status": (
                "ready"
                if all(
                    bool((plan.get("planning_source_status") or {}).get("ai_spec_ready"))
                    for plan in brief_plans
                )
                else "blocked"
            ),
        },
    }


def _merge_constraints(current: dict, incoming: dict) -> dict:
    merged = {
        "exclude_by_slot": {
            key: list(value) for key, value in current.get("exclude_by_slot", {}).items()
        },
        "force_keep_slots": list(current.get("force_keep_slots", [])),
        "global_excludes": list(current.get("global_excludes", [])),
        "exclude_visuals_by_slot": {
            key: list(value) for key, value in current.get("exclude_visuals_by_slot", {}).items()
        },
        "user_feedback": list(current.get("user_feedback", [])),
        "parent_run_id": str(current.get("parent_run_id") or ""),
        "feedback_id": str(current.get("feedback_id") or ""),
    }
    if incoming.get("force_template_order"):
        merged["force_template_order"] = True
    for key, values in incoming.get("exclude_by_slot", {}).items():
        merged["exclude_by_slot"].setdefault(str(key), [])
        for value in values:
            _append_unique(merged["exclude_by_slot"][str(key)], value)
    for value in incoming.get("force_keep_slots", []):
        _append_unique(merged["force_keep_slots"], value)
    for value in incoming.get("global_excludes", []):
        _append_unique(merged["global_excludes"], value)
    for key, values in incoming.get("exclude_visuals_by_slot", {}).items():
        merged["exclude_visuals_by_slot"].setdefault(str(key), [])
        for value in values:
            _append_unique(merged["exclude_visuals_by_slot"][str(key)], value)
    for value in incoming.get("user_feedback", []):
        _append_unique(merged["user_feedback"], value)
    for key in ("parent_run_id", "feedback_id"):
        if incoming.get(key):
            merged[key] = str(incoming[key])
    return merged


def _forced_visuals_from_feedback(constraints: dict) -> dict[str, dict]:
    forced = {}
    for item in constraints.get("user_feedback", []):
        if str(item.get("category") or "").casefold() != "image":
            continue
        shot_no = str(item.get("shot_no") or "").strip()
        image_id = str(item.get("selected_image_id") or "").strip()
        path = str(item.get("selected_image_path") or item.get("supplemental_image_path") or "").strip()
        if shot_no.isdigit() and (image_id or path):
            forced[shot_no] = {"image_id": image_id, "path": path}
    return forced


def _constraints_for_planning(constraints: dict) -> dict:
    envelope_keys = {"feedback_id", "parent_run_id", "restart_stage", "schema_version"}
    result = {
        key: value
        for key, value in constraints.items()
        if key not in envelope_keys
        and key not in {"user_feedback", "exclude_visuals_by_slot"}
    }
    user_feedback = [
        item for item in constraints.get("user_feedback", [])
        if str(item.get("category") or "").casefold() in {"brand", "fact", "structure"}
    ]
    if user_feedback:
        result["user_feedback"] = user_feedback
    return result


def _constraints_for_drafting(constraints: dict) -> dict:
    envelope_keys = {"feedback_id", "parent_run_id", "restart_stage", "schema_version"}
    result = {
        key: value
        for key, value in constraints.items()
        if key not in envelope_keys
        and key not in {"user_feedback", "exclude_visuals_by_slot"}
    }
    user_feedback = [
        item for item in constraints.get("user_feedback", [])
        if str(item.get("category") or "").casefold() != "image"
    ]
    if user_feedback:
        result["user_feedback"] = user_feedback
    return result


def _can_reuse_upstream_for_image_feedback(runtime, constraints: dict) -> bool:
    if not _is_image_only_feedback(constraints):
        return False
    return _can_reuse_stage_output(
        runtime,
        "script_plan",
        "script_plan.json",
        _constraints_for_planning(constraints),
    ) and _can_reuse_stage_output(
        runtime,
        "script_draft",
        "script_draft.json",
        _constraints_for_drafting(constraints),
    )


def _can_reuse_stage_output(
    runtime,
    stage_id: str,
    filename: str,
    semantic_constraints: dict,
) -> bool:
    if any(
        _constraint_value_has_effect(value)
        for value in semantic_constraints.values()
    ):
        return False
    try:
        manifest = json.loads(runtime.manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    record = dict((manifest.get("stages") or {}).get(stage_id) or {})
    return (
        record.get("status") in {"succeeded", "succeeded_with_warnings"}
        and (runtime.run_dir / filename).is_file()
    )


def _is_image_only_feedback(constraints: dict) -> bool:
    feedback = list(constraints.get("user_feedback") or [])
    return bool(feedback) and all(
        str(item.get("category") or "").casefold() == "image"
        for item in feedback
    )


def _has_targeted_non_visual_feedback(constraints: dict) -> bool:
    feedback = list(constraints.get("user_feedback") or [])
    return bool(feedback) and any(
        str(item.get("category") or "").casefold() != "image"
        for item in feedback
    ) and all(
        str(item.get("category") or "").casefold() == "image"
        or (
            str(item.get("category") or "").casefold() in {"style", "detail", "copy"}
            and str(item.get("scope") or "") == "shot"
            and str(item.get("shot_no") or "").strip().isdigit()
        )
        for item in feedback
    )


def _constraint_value_has_effect(value) -> bool:
    if isinstance(value, dict):
        return any(_constraint_value_has_effect(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return any(_constraint_value_has_effect(item) for item in value)
    return bool(value)


def _preserve_unaffected_parent_visuals(
    runtime,
    constraints: dict,
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    *,
    visual_only: bool = False,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Targeted revisions may change requested shots, never unrelated visuals."""
    target_slots = {
        str(item.get("shot_no") or "").strip()
        for item in constraints.get("user_feedback", [])
        if str(item.get("category") or "").casefold() == "image"
        and str(item.get("shot_no") or "").strip().isdigit()
    }
    text_target_slots = {
        str(item.get("shot_no") or "").strip()
        for item in constraints.get("user_feedback", [])
        if str(item.get("category") or "").casefold() != "image"
        and str(item.get("scope") or "").casefold() in {"shot", "single_shot"}
        and str(item.get("shot_no") or "").strip().isdigit()
    }
    parent_run_id = str(constraints.get("parent_run_id") or "").strip()
    if (not visual_only and not target_slots) or not parent_run_id:
        return script_rows, detail_rows, visual_asset_rows
    parent_path = runtime.run_dir.parent / parent_run_id / "visual_binding.json"
    if not parent_path.is_file():
        return script_rows, detail_rows, visual_asset_rows
    parent = read_visual_binding(parent_path)
    parent_rows = {
        str(detail.get("slot_index") or script.get("镜号") or index): (script, detail)
        for index, (script, detail) in enumerate(
            zip(parent.script_rows, parent.detail_rows),
            start=1,
        )
    }
    for index, detail in enumerate(detail_rows):
        slot = str(detail.get("slot_index") or script_rows[index].get("镜号") or index + 1)
        parent_row = parent_rows.get(slot)
        if parent_row:
            if visual_only:
                if slot not in target_slots:
                    script_rows[index]["画面示意"] = parent_row[0].get("画面示意", "")
                    for key in _VISUAL_DETAIL_FIELDS:
                        if key in parent_row[1]:
                            detail_rows[index][key] = parent_row[1][key]
                        else:
                            detail_rows[index].pop(key, None)
                if slot not in text_target_slots:
                    for key in ("画面描述", "声音"):
                        script_rows[index][key] = parent_row[0].get(
                            key, script_rows[index].get(key, "")
                        )
            elif slot not in target_slots:
                script_rows[index] = dict(parent_row[0])
                detail_rows[index] = dict(parent_row[1])

    current_assets = {
        str(asset.get("slot_index") or ""): asset
        for asset in visual_asset_rows
        if str(asset.get("slot_index") or "")
    }
    parent_assets = {
        str(asset.get("slot_index") or ""): asset
        for asset in parent.visual_asset_rows
        if str(asset.get("slot_index") or "")
    }
    ordered_assets = []
    for index, detail in enumerate(detail_rows, start=1):
        slot = str(detail.get("slot_index") or index)
        asset = (
            current_assets.get(slot)
            if slot in target_slots
            else parent_assets.get(slot) or current_assets.get(slot)
        )
        if asset is not None:
            ordered_assets.append(dict(asset))
    return script_rows, detail_rows, ordered_assets


_VISUAL_DETAIL_FIELDS = {
    "image_id", "media_id", "content_sha256", "原图路径", "图片绑定方式", "图片绑定说明", "图片证据类型",
    "功能适配判断", "品牌中性确认依据", "视觉内容摘要", "视觉功能标签",
    "视觉匹配分", "视觉回写依据", "视觉标签", "视觉读取状态", "视觉质量指标",
    "视觉资产ID", "视觉适配查询", "视觉适配模式", "输出图片来源", "配图判断",
    "配图置信度", "绑定说明", "ocr_text", "brand_names", "negative_flags",
    "命中视觉要素", "缺失视觉要素", "冲突视觉要素",
}


def _visual_preferences_from_feedback(constraints: dict) -> dict[str, str]:
    preferences: dict[str, list[str]] = {}
    for item in constraints.get("user_feedback", []):
        if str(item.get("category") or "").casefold() != "image":
            continue
        shot_no = str(item.get("shot_no") or "").strip()
        if not shot_no.isdigit():
            continue
        text = "；".join(
            value
            for value in (
                str(item.get("comment") or "").strip(),
                str(item.get("desired_change") or "").strip(),
            )
            if value
        )
        if text:
            preferences.setdefault(shot_no, []).append(text)
    return {
        shot_no: "；".join(dict.fromkeys(values))
        for shot_no, values in preferences.items()
    }


def _add_visual_retry_constraints(audit: dict, detail_rows: list[dict]) -> dict:
    next_constraints = dict(audit.get("next_constraints", {}))
    excluded = {
        str(key): list(values)
        for key, values in next_constraints.get("exclude_visuals_by_slot", {}).items()
    }
    for index, detail in enumerate(detail_rows, start=1):
        if str(detail.get("配图判断") or "") != "不匹配":
            continue
        slot = str(detail.get("slot_index") or index)
        tokens = [
            part.strip()
            for value in (detail.get("image_id"), detail.get("原图路径"))
            for part in str(value or "").split("；")
            if part.strip()
        ]
        if not tokens:
            continue
        excluded.setdefault(slot, [])
        for token in tokens:
            _append_unique(excluded[slot], token)
    next_constraints["exclude_visuals_by_slot"] = excluded
    return {**audit, "next_constraints": next_constraints}


def _append_unique(items: list, value) -> None:
    if value not in items:
        items.append(value)


def _failure_signature(audit: dict) -> tuple:
    hard_items = tuple(
        (
            check.get("检查项", ""),
            check.get("关联镜号", ""),
            check.get("详情", ""),
        )
        for check in audit.get("checks", [])
        if check.get("级别") == "hard"
    )
    constraints = json.dumps(audit.get("next_constraints", {}), ensure_ascii=False, sort_keys=True)
    return audit.get("hard_fail_count", 0), hard_items, constraints


def _iteration_log(iteration: LoopIteration) -> dict:
    audit = iteration.audit
    return {
        "index": iteration.index,
        "phase": "generate-audit-diagnose",
        "output_path": str(iteration.output_path),
        "audit_path": str(iteration.audit_path),
        "status": audit.get("status", ""),
        "hard_fail_count": audit.get("hard_fail_count", 0),
        "warning_count": audit.get("warning_count", 0),
        "score": audit.get("score", 0),
        "blocking_items": audit.get("blocking_items", []),
        "next_constraints": audit.get("next_constraints", {}),
        "diagnosis": _diagnose_audit(audit),
    }


def _diagnose_audit(audit: dict) -> dict:
    hard_items = []
    warning_items = []
    for check in audit.get("checks", []):
        item = {
            "check": check.get("检查项", ""),
            "slot": check.get("关联镜号", ""),
            "detail": check.get("详情", ""),
            "fix": check.get("修复建议", ""),
        }
        if check.get("状态") != "fail":
            continue
        if check.get("级别") == "hard":
            hard_items.append(item)
        elif check.get("级别") == "warning":
            warning_items.append(item)
    return {
        "hard_items": hard_items,
        "warning_items": warning_items,
        "next_constraints": audit.get("next_constraints", {}),
    }


def _write_loop_log(
    path: Path,
    status: str,
    stop_reason: str,
    iterations: list[dict],
    passed_iteration: int | None = None,
    best_failed_output_path: Path | None = None,
    best_failed_audit_path: Path | None = None,
) -> None:
    payload = {
        "status": status,
        "stop_reason": stop_reason,
        "passed_iteration": passed_iteration,
        "iterations": iterations,
    }
    if best_failed_output_path:
        payload["best_failed_output_path"] = str(best_failed_output_path)
    if best_failed_audit_path:
        payload["best_failed_audit_path"] = str(best_failed_audit_path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _best_iteration(iterations: list[LoopIteration]) -> LoopIteration:
    return sorted(
        iterations,
        key=lambda item: (
            item.audit.get("hard_fail_count", 0),
            -item.audit.get("score", 0),
        ),
    )[0]


def _default_audit_path(output_path: Path) -> Path:
    return Path(str(output_path) + ".audit.json")


def _loop_log_path(output_path: Path) -> Path:
    return Path(str(output_path) + ".loop.json")


def _loop_output_path(output_path: Path, index: int) -> Path:
    return output_path.with_name(f"{output_path.stem}.loop{index}{output_path.suffix}")


def _loop_audit_path(output_path: Path, index: int) -> Path:
    return output_path.with_name(f"{output_path.stem}.loop{index}.audit.json")


def _best_failed_output_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.best_failed{output_path.suffix}")


def _best_failed_audit_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.best_failed.audit.json")


def _expected_embedded_image_count(script_rows: list[dict]) -> int:
    count = 0
    for row in script_rows:
        for column in ("画面示意", "效果示意"):
            if _has_existing_local_file(row.get(column, "")):
                count += 1
    return count


def _has_existing_local_file(value: str) -> bool:
    for part in str(value or "").split(";"):
        path = Path(part.strip())
        if path.exists() and path.is_file():
            return True
    return False


def _legacy_analysis_from_image_index(
    image_manifest_rows: list[dict] | None,
    image_analysis_rows: list[dict] | None,
) -> dict | None:
    if not image_manifest_rows or not image_analysis_rows:
        return None
    assets = {str(row.get("image_id") or ""): row for row in image_manifest_rows}
    result = {}
    for item in image_analysis_rows:
        asset = assets.get(str(item.get("image_id") or ""))
        if not asset:
            continue
        path = Path(str(asset.get("原图路径") or ""))
        analysis = {
            "summary": item.get("caption", ""),
            "caption": item.get("caption", ""),
            "tags": item.get("tags", []),
            "ocr_text": item.get("ocr_text", ""),
            "brand_names": item.get("brand_names", []),
            "negative_flags": item.get("negative_flags", []),
            "source": item.get("source", ""),
        }
        for key in (str(path), path.name, path.stem, str(item.get("image_id") or "")):
            if key:
                result[key] = analysis
    return result


def _candidate_image_paths(source_root: str | Path, brief: dict) -> list[Path]:
    root = Path(source_root)
    launch_dir = root / "发布会类"
    if not launch_dir.exists():
        return []
    hint = _normalize_name(project_hint_from_brief(brief))
    directories = []
    for path in sorted(launch_dir.iterdir()):
        if path.is_dir() and _normalize_name(path.name) in hint:
            directories.append(path)
    if not directories:
        directories = [path for path in sorted(launch_dir.iterdir()) if path.is_dir()]
    images = []
    for directory in directories:
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                images.append(path)
    return images


def _normalize_name(value: str) -> str:
    return "".join(str(value or "").lower().split())
