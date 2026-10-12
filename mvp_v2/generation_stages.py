from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import json
from pathlib import Path
import re
from typing import Any, Callable

from openpyxl import load_workbook

from .audit import _required_term_present
from .models import SourceShot, TemplateDecision, parse_positive_seconds_strict, parse_duration_constraints
from .formal_brief_domain import (
    ArtifactPayload,
    ConfirmedBriefInput,
    ConfirmedBriefResolver,
    T02ConfirmedBriefInput,
    adapt_confirmed_brief_for_t02,
)
from .creative_standard import (
    ConfirmedCreativeStandardInput,
    ConfirmedCreativeStandardResolver,
)
from .pipeline_stages import AutoPipelineStageError
from .portable_paths import resolve_run_local_path
from .run_contract import sha256_file, sha256_json
from .run_state import write_json_atomic
from .script_draft import (
    MAX_OUTPUT_TOKENS,
    GROUNDING_VERSION,
    PROMPT_VERSION,
    SCHEMA_VERSION as SCRIPT_DRAFT_SCHEMA_VERSION,
    TEXT_TIMEOUT_SECONDS,
    ScriptDraftError,
    ScriptDraftProviderError,
    TextProvider,
    _planned_business_events,
    requirement_output_evidence,
    create_script_draft,
)
from .stage_contract import StageError, StageErrorCategory, StageStatus
from .stage_runtime import (
    finish_pipeline_stage,
    reused_fork_stage_output,
    start_pipeline_stage,
)
from .script_type_adapter import (
    category_id_for_script_type,
    normalize_script_type,
    resolve_script_type_selection,
    route_contract_for_script_type,
)
from .planning_core import creative_standard_draft_hits, _split_event_text


@dataclass(frozen=True)
class ScriptPlanArtifact:
    path: Path
    briefs: list[dict]
    brief: dict
    constraints: dict
    source_view: str
    script_rows: list[dict]
    detail_rows: list[dict]
    decisions: list[TemplateDecision]
    template_slots: list[SourceShot]
    source_refs: list[dict]
    schema_version: str = "mplus-script-plan/v1"
    plan_schema_version: str = ""
    brief_facts: list[dict] = field(default_factory=list)
    requirements: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    event_graph: dict = field(default_factory=dict)
    duration_budget: dict = field(default_factory=dict)
    shot_plans: list[dict] = field(default_factory=list)
    coverage: list[dict] = field(default_factory=list)
    capacity_report: dict = field(default_factory=dict)
    traceability: list[dict] = field(default_factory=list)
    template_rule_contract: dict = field(default_factory=dict)
    conflicts: list[dict] = field(default_factory=list)
    decision_counts: dict = field(default_factory=dict)
    planning_audit: dict = field(default_factory=dict)
    planning_source_status: dict = field(default_factory=dict)
    confirmed_brief_input: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScriptDraftArtifact:
    path: Path
    plan_path: Path
    briefs: list[dict]
    brief: dict
    constraints: dict
    source_view: str
    quality_profile: str
    text_model: str
    overall_summary: str
    script_rows: list[dict]
    detail_rows: list[dict]
    decisions: list[TemplateDecision]
    template_slots: list[SourceShot]
    source_refs: list[dict]
    pending_confirmations: list[dict]
    usage: dict
    traceability: list[dict] = field(default_factory=list)
    capacity_report: dict = field(default_factory=dict)
    field_decisions: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    decision_counts: dict = field(default_factory=dict)


@dataclass(frozen=True)
class VisualBindingArtifact:
    path: Path
    plan_path: Path
    briefs: list[dict]
    brief: dict
    constraints: dict
    source_view: str
    quality_profile: str
    text_model: str
    overall_summary: str
    script_rows: list[dict]
    detail_rows: list[dict]
    decisions: list[TemplateDecision]
    template_slots: list[SourceShot]
    source_refs: list[dict]
    visual_asset_rows: list[dict]
    bindings: list[dict]
    pending_confirmations: list[dict]
    text_usage: dict
    traceability: list[dict] = field(default_factory=list)
    capacity_report: dict = field(default_factory=dict)
    field_decisions: list[dict] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    decision_counts: dict = field(default_factory=dict)
    image_analysis_rows: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class WorkbookRenderArtifact:
    path: Path
    binding_path: Path


@dataclass(frozen=True)
class MachineAuditArtifact:
    output_path: Path
    audit_path: Path
    audit: dict


class _ConfirmedBriefError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _ResolvedConfirmedBrief:
    brief: dict
    evidence: dict
    fact_provenance: list[dict]


@dataclass(frozen=True)
class _ResolvedCreativeStandard:
    content: dict
    evidence: dict


def run_script_plan_stage(
    state,
    *,
    briefs: list[dict] | None,
    constraints: dict,
    source_view: str,
    builder: Callable,
    confirmed_brief_input: ConfirmedBriefInput | None = None,
    confirmed_brief_resolver: ConfirmedBriefResolver | None = None,
    confirmed_creative_standard_input: ConfirmedCreativeStandardInput | None = None,
    confirmed_creative_standard_resolver: ConfirmedCreativeStandardResolver | None = None,
) -> ScriptPlanArtifact:
    stage_id = "script_plan"
    frozen = reused_fork_stage_output(state, stage_id, "script_plan.json")
    if frozen is not None:
        return read_script_plan(frozen)
    dependency_refs, dependency_hashes = _dependency_evidence(
        state, ("source_catalog",)
    )
    confirmed_requested = any((
        confirmed_brief_input is not None,
        confirmed_brief_resolver is not None,
        confirmed_creative_standard_input is not None,
        confirmed_creative_standard_resolver is not None,
    ))
    confirmed_evidence = None
    if confirmed_requested and confirmed_brief_input is not None:
        try:
            confirmed_evidence = _confirmed_brief_evidence(confirmed_brief_input)
        except _ConfirmedBriefError:
            # Start the stage before reporting the contract block so the
            # manifest records script_plan=blocked and script_draft attempt=0.
            confirmed_evidence = {"invalid_input_type": type(confirmed_brief_input).__name__}
    input_refs = list(dependency_refs)
    if confirmed_evidence is not None and "payload" in confirmed_evidence:
        input_refs.append(confirmed_evidence["payload"]["ref"])
    creative_evidence = None
    if confirmed_requested and confirmed_creative_standard_input is not None:
        try:
            creative_evidence = _confirmed_creative_standard_evidence(
                confirmed_creative_standard_input
            )
        except _ConfirmedBriefError:
            creative_evidence = {
                "invalid_input_type": type(confirmed_creative_standard_input).__name__
            }
    if creative_evidence is not None and "card_payload" in creative_evidence:
        input_refs.append(creative_evidence["card_payload"]["ref"])
    input_sha256 = sha256_json({
        "dependencies": dependency_hashes,
        "briefs": [] if confirmed_requested else (briefs or []),
        "confirmed_brief_input": confirmed_evidence,
        "confirmed_creative_standard_input": creative_evidence,
        "constraints": constraints,
        "source_view": source_view,
    })
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=input_refs,
    )
    reused = _reused_output(state, record)
    if reused is not None:
        return read_script_plan(reused)
    try:
        if confirmed_requested:
            if briefs:
                raise _ConfirmedBriefError(
                    "confirmed_brief_raw_input_rejected",
                    "已确认 Brief 规划不接受调用方传入的裸 Brief",
                )
            resolved_confirmed = _resolve_confirmed_brief(
                confirmed_brief_input, confirmed_brief_resolver,
                project_context=constraints.get("formal_project_context"),
            )
            resolved_standard = _resolve_confirmed_creative_standard(
                confirmed_creative_standard_input,
                confirmed_creative_standard_resolver,
                resolved_confirmed.evidence,
            )
            planning_briefs = [resolved_confirmed.brief]
            planning_constraints = {
                **constraints,
                "creative_standard": resolved_standard.content,
            }
            built = builder(planning_briefs)
        else:
            planning_briefs = list(briefs or [])
            if not planning_briefs:
                raise _ConfirmedBriefError(
                    "brief_missing", "缺少生成镜头计划所需的 Brief"
                )
            resolved_confirmed = None
            resolved_standard = None
            planning_constraints = constraints
            built = builder()
        if not isinstance(built, tuple) or len(built) not in {5, 6}:
            raise ValueError("镜头规划 builder 必须返回5项旧合同或6项 PlanArtifact 合同")
        script_rows, detail_rows, decisions, template_slots, brief = built[:5]
        planning_payload = dict(built[5]) if len(built) == 6 else {}
        if (
            resolved_confirmed is not None
            and sha256_json(brief) != sha256_json(resolved_confirmed.brief)
        ):
            raise _ConfirmedBriefError(
                "confirmed_brief_planning_drift",
                "镜头规划输出的 Brief 与已确认 Brief artifact 不一致",
            )
    except AutoPipelineStageError:
        raise
    except _ConfirmedBriefError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            exc.code,
            str(exc),
        )
    except ValueError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "script_plan_contract_blocked",
            str(exc),
        )
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "script_plan_io_failed",
            f"镜头计划 IO 失败: {exc}",
            retryable=True,
        )
    except Exception as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.EXECUTION,
            "script_plan_execution_failed",
            f"镜头计划执行失败: {exc}",
            retryable=True,
        )
    if not script_rows or not template_slots:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "script_plan_empty",
            "未生成可用镜头计划",
        )
    if (
        len(script_rows) != len(detail_rows)
        or len(script_rows) != len(decisions)
        or len(script_rows) != len(template_slots)
    ):
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "script_plan_count_mismatch",
            "镜头计划、抽调明细、选择决策和投影功能槽数量不一致",
        )
    source_refs = _source_refs(detail_rows)
    if len(source_refs) != len(detail_rows):
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "script_plan_source_trace_incomplete",
            "镜头计划缺少来源文件、来源 Sheet 或来源行",
        )
    planning_audit = dict(planning_payload.get("planning_audit") or {})
    if resolved_standard is not None:
        plan_text_hits = creative_standard_draft_hits(
            planning_constraints,
            script_rows,
        )
        if plan_text_hits:
            findings = list(planning_audit.get("findings") or [])
            findings.extend({
                "code": "creative_standard_avoid_expression_hit",
                "severity": "blocker",
                "message": f"规划业务字段命中避免表达: {hit['value']}",
                **hit,
            } for hit in plan_text_hits)
            planning_audit.update({
                "findings": findings,
                "blocking_codes": list(dict.fromkeys([
                    *list(planning_audit.get("blocking_codes") or []),
                    "creative_standard_avoid_expression_hit",
                ])),
                "status": "blocked",
            })
            consumption = dict(planning_audit.get("creative_standard_consumption") or {})
            consumption["plan_text_hits"] = plan_text_hits
            planning_audit["creative_standard_consumption"] = consumption
    effective_knowledge = planning_constraints.get("effective_knowledge")
    if isinstance(effective_knowledge, dict):
        knowledge_rules = list(effective_knowledge.get("rules") or [])
        planning_audit["knowledge_rule_consumption"] = {
            "snapshot_sha256": str(
                effective_knowledge.get("snapshot_sha256") or ""
            ),
            "rule_count": len(knowledge_rules),
            "rule_ids": [str(item.get("rule_id") or "") for item in knowledge_rules],
            "applied_to": ["planning_input", "script_draft_prompt"],
        }
    planning_status = str(planning_audit.get("status") or "")
    if planning_status == "blocked":
        codes = (
            list(planning_audit.get("blocking_codes") or [])
        )
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "planning_audit_blocked",
            "规划审核未通过: " + "、".join(codes),
        )
    planning_payload = {**planning_payload, "planning_audit": planning_audit}
    output_path = state.run_dir / "script_plan.json"
    payload = {
        "schema_version": (
            "mplus-script-plan/v4"
            if planning_payload.get("plan_schema_version") == "mplus-plan-artifact/v4"
            else "mplus-script-plan/v3"
            if planning_payload.get("plan_schema_version") == "mplus-plan-artifact/v3"
            else "mplus-script-plan/v2" if planning_payload else "mplus-script-plan/v1"
        ),
        "category_id": category_id_for_script_type(brief.get("脚本类型") or "发布会"),
        "script_type": normalize_script_type(brief.get("脚本类型") or "发布会"),
        "route_contract": route_contract_for_script_type(brief.get("脚本类型") or "发布会"),
        "briefs": planning_briefs,
        "brief": brief,
        "constraints": planning_constraints,
        "source_view": source_view,
        "shot_count": len(script_rows),
        "structures": [row.get("结构", "") for row in script_rows],
        "source_refs": source_refs,
        "script_rows": script_rows,
        "detail_rows": detail_rows,
        "decisions": [asdict(item) for item in decisions],
        "template_slots": [asdict(item) for item in template_slots],
        "confirmed_brief_input": (
            resolved_confirmed.evidence if resolved_confirmed is not None else {}
        ),
        "confirmed_brief_provenance": (
            {
                "brief_version_id": resolved_confirmed.evidence["brief_version_id"],
                "payload_sha256": resolved_confirmed.evidence["payload"]["sha256"],
                "facts": resolved_confirmed.fact_provenance,
            }
            if resolved_confirmed is not None else {}
        ),
        "confirmed_creative_standard_provenance": (
            {
                "creative_standard_version_id": resolved_standard.evidence["creative_standard_version_id"],
                "confirmation_id": resolved_standard.evidence["confirmation_id"],
                "brief_version_id": resolved_standard.evidence["brief_version_id"],
                "brief_snapshot_id": resolved_standard.evidence["brief_snapshot_id"],
                "brief_payload": resolved_standard.evidence["brief_payload"],
                "card_payload": resolved_standard.evidence["card_payload"],
            }
            if resolved_standard is not None else {}
        ),
        **planning_payload,
    }
    try:
        write_json_atomic(output_path, payload)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "script_plan_write_failed",
            f"镜头计划写出失败: {exc}",
            retryable=True,
        )
    finish_pipeline_stage(
        state,
        stage_id,
        StageStatus.SUCCEEDED,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return read_script_plan(output_path)


_CONFIRMED_BRIEF_FIELDS = (
    "workspace_id",
    "project_id",
    "brief_version_id",
    "confirmation_id",
    "brief_snapshot_id",
    "payload",
    "confirmation_status",
    "unresolved_conflict_count",
    "confirmed_by_actor_id",
    "confirmed_at",
)


def _resolve_confirmed_brief(
    confirmed_brief_input: ConfirmedBriefInput | None,
    resolver: ConfirmedBriefResolver | None,
    *, project_context: dict | None = None,
) -> _ResolvedConfirmedBrief:
    """Resolve a T01 formal Brief artifact through its authoritative adapter."""
    if confirmed_brief_input is None or resolver is None:
        raise _ConfirmedBriefError(
            "confirmed_brief_dependency_missing",
            "缺少正式 ConfirmedBriefInput Resolver，不能开始镜头规划",
        )
    requested = _confirmed_brief_evidence(confirmed_brief_input)
    try:
        resolved = adapt_confirmed_brief_for_t02(confirmed_brief_input, resolver)
    except Exception as exc:
        raise _ConfirmedBriefError(
            "confirmed_brief_resolution_failed",
            "无法从正式 R01 持久记录解析已确认 Brief",
        ) from exc
    if not isinstance(resolved, T02ConfirmedBriefInput):
        raise _ConfirmedBriefError(
            "confirmed_brief_resolution_invalid",
            "T01 ConfirmedBrief 适配器返回了不支持的对象",
        )
    if _confirmed_brief_evidence(resolved.confirmed) != requested:
        raise _ConfirmedBriefError(
            "confirmed_brief_resolution_drift",
            "T01 ConfirmedBrief 适配记录与请求发生漂移",
        )
    resolved_brief = resolved.brief_payload
    if not isinstance(resolved_brief, dict):
        raise _ConfirmedBriefError(
            "confirmed_brief_resolution_invalid",
            "T01 ConfirmedBrief 适配器必须返回结构化 Brief",
        )
    planning_brief, fact_provenance = _planning_brief_from_confirmed_payload(
        resolved_brief, requested, project_context=project_context,
    )
    if not planning_brief:
        raise _ConfirmedBriefError(
            "confirmed_brief_payload_empty",
            "正式 ConfirmedBrief payload 为空，不能开始镜头规划",
        )
    return _ResolvedConfirmedBrief(
        brief=planning_brief, evidence=requested, fact_provenance=fact_provenance
    )


_FORMAL_CANONICAL_TO_PLANNER_FIELD = {
    # T01 的正式存储字段保持不变。P00 只在这里做显式投影，不能把
    # 外部字段名倒灌为数据库迁移，也不能宽松放行未知 facts。
    "项目名称": "项目名称",
    "项目区域文件名": "项目区域文件名",
    "脚本类型": "脚本类型",
    "品牌或客户": "品牌或客户",
    "行业或活动类别": "行业或活动类别",
    # 兼容 2026-08-19 修复 DOCX 段内换行时已冻结的早期字段名。
    "具体类型": "行业或活动类别",
    "风格调性": "风格调性",
    "必须出现的内容": "必须出现的内容",
    "视频目标": "视频目标",
    "使用场景": "使用场景",
    "特殊需求": "特殊需求",
    "交付要求": "交付要求",
    "目标时长": "视频时长",
    "禁止或不能出现的内容": "禁忌或不能出现的内容",
    "关键事件与流程": "事件流程",
    # 以下三个仅兼容已落库的历史正式 Brief；新 T01 adapter 不会写它们。
    "视频时长": "视频时长",
    "禁忌或不能出现的内容": "禁忌或不能出现的内容",
    "planning_events": "planning_events",
}
_FORMAL_CSTD_REFERENCE_FIELDS = frozenset({"参考方案", "参考图片", "历史脚本"})
_FORMAL_PLANNING_REQUIRED_FIELDS = ("项目名称", "脚本类型", "视频时长")


def _planning_brief_from_confirmed_payload(
    payload: dict, evidence: dict, *, project_context: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Derive planning fields only from T01's already-verified formal facts."""
    if not isinstance(payload.get("facts"), list):
        raise _ConfirmedBriefError(
            "confirmed_brief_needs_clarification", "正式 Brief 缺少可规划的 confirmed facts"
        )
    facts = payload["facts"]
    source_filenames = _formal_source_filenames(payload)
    resolution_by_selected, rejected_candidates = _formal_conflict_selections(
        payload, facts
    )
    brief: dict = {}
    provenance: list[dict] = []
    consumed_planner_fields: set[str] = set()
    for fact in facts:
        if not isinstance(fact, dict):
            raise _ConfirmedBriefError(
                "confirmed_brief_payload_invalid", "正式 Brief facts 必须是对象数组"
            )
        field_path = str(fact.get("field_path") or "").strip()
        if fact.get("semantic_metadata", {}).get("support_status") == "needs_review":
            provenance.append({"fact_id": fact.get("fact_id"), "field_path": field_path,
                "evidence_refs": list(fact.get("evidence_refs", [])), "consumption": "unsupported_candidate_not_adopted"})
            continue
        encoded_value = fact.get("value_json")
        if (
            not field_path
            or fact.get("certainty") != "confirmed"
            or not isinstance(encoded_value, str)
            or not str(fact.get("fact_id") or "").strip()
            or not isinstance(fact.get("evidence_refs"), list)
            or not fact["evidence_refs"]
        ):
            raise _ConfirmedBriefError(
                "confirmed_brief_needs_clarification",
                "正式 Brief 含未知、pending、无证据或不可规划的 fact",
            )
        try:
            value = json.loads(encoded_value)
        except json.JSONDecodeError as exc:
            raise _ConfirmedBriefError(
                "confirmed_brief_payload_invalid", "正式 Brief fact value_json 无法解析"
            ) from exc
        is_cstd_reference = field_path in _FORMAL_CSTD_REFERENCE_FIELDS
        planner_field = _FORMAL_CANONICAL_TO_PLANNER_FIELD.get(field_path)
        if not is_cstd_reference and not planner_field:
            raise _ConfirmedBriefError(
                "confirmed_brief_needs_clarification",
                "正式 Brief 含未知、pending、无证据或不可规划的 fact",
            )
        fact_id = str(fact["fact_id"])
        resolution = resolution_by_selected.get(fact_id)
        if fact_id in rejected_candidates:
            provenance.append({
                "fact_id": fact_id,
                "field_path": field_path,
                "evidence_refs": list(fact["evidence_refs"]),
                "consumption": "conflict_candidate_not_selected",
                "conflict_resolution": rejected_candidates[fact_id],
            })
            continue
        if is_cstd_reference:
            # 标准卡会独立消费参考资料；P00 仅保留其已确认来源，不把它
            # 当成 Brief 事实、事件或生成提示词的一部分。
            provenance_entry = {
                "fact_id": fact_id,
                "field_path": field_path,
                "evidence_refs": list(fact["evidence_refs"]),
                "consumption": "creative_standard_reference_only",
            }
            if resolution:
                provenance_entry["conflict_resolution"] = resolution
            provenance.append(provenance_entry)
            continue
        assert planner_field is not None
        if planner_field in consumed_planner_fields:
            raise _ConfirmedBriefError(
                "confirmed_brief_needs_clarification",
                "正式 Brief 同一规划字段存在未登记冲突的重复事实",
            )
        consumed_planner_fields.add(planner_field)
        brief[planner_field] = value
        provenance_entry = {
            "fact_id": fact_id,
            "field_path": field_path,
            "planner_field": planner_field,
            "evidence_refs": list(fact["evidence_refs"]),
        }
        if fact.get("semantic_metadata"):
            provenance_entry["semantic_metadata"] = dict(fact["semantic_metadata"])
        if planner_field == "视频时长":
            explicit_seconds = parse_positive_seconds_strict(str(value))
            stated = parse_duration_constraints(str(value), infer_total=False)
            totals = stated.get("total", set())
            if explicit_seconds:
                totals = {explicit_seconds}
            implied = parse_duration_constraints(str(value)).get("total", set())
            if len(totals) != 1 or next(iter(totals)) <= 0 or (implied and implied != totals):
                raise _ConfirmedBriefError(
                    "confirmed_brief_needs_clarification",
                    "目标时长需要唯一明确的正整数总秒数，且与镜数、逐镜时长一致。请核对总时长及每镜安排。",
                )
            duration_seconds = next(iter(totals))
            brief[planner_field] = value if explicit_seconds else f"{duration_seconds}s"
            # Keep the confirmed instruction (including explicit shot counts)
            # beside the normalized scalar consumed by duration validators.
            brief["目标时长"] = value
            provenance_entry["duration_projection"] = {
                "source_value": value,
                "total_seconds": duration_seconds,
                "planner_value": brief[planner_field],
            }
        if resolution:
            provenance_entry["conflict_resolution"] = resolution
        if field_path == "关键事件与流程":
            # P00 的动态 N 只以明确的 events 驱动。保留可读的事件流程字段，
            # 同时由同一 confirmed fact 确定性投影为可审计 planning_events。
            events = _formal_planning_events(
                value, fact_id=provenance_entry["fact_id"],
                evidence_refs=provenance_entry["evidence_refs"],
                source_filenames=source_filenames,
            )
            if events:
                if "planning_events" in brief and brief["planning_events"] != events:
                    raise _ConfirmedBriefError(
                        "confirmed_brief_needs_clarification",
                        "正式 Brief 的关键事件与历史 planning_events 不一致",
                    )
                brief["planning_events"] = events
                provenance_entry["event_projection"] = {
                    "event_count": len(events),
                    "fact_status": "confirmed",
                }
        provenance.append(provenance_entry)
    raw_script_type = str(brief.get("脚本类型") or "").strip()
    specific_script_type = str(brief.get("行业或活动类别") or "").strip()

    routing = resolve_script_type_selection(raw_script_type, specific_script_type)
    if routing["status"] == "needs_clarification":
        raise _ConfirmedBriefError(
            "confirmed_brief_needs_clarification",
            "正式 Brief 的脚本类型与具体活动类别不一致",
        )
    if routing["status"] == "unsupported":
        raise _ConfirmedBriefError(
            "SCRIPT_TYPE_UNSUPPORTED", "正式 Brief 的脚本类型和具体活动类别均不受支持"
        )
    resolved_script_type = routing.get("script_type", "")
    if resolved_script_type:
        brief["脚本类型"] = resolved_script_type
        routing_resolution = {
            "raw_script_type": raw_script_type,
            "specific_script_type": specific_script_type,
            "normalized_script_type": resolved_script_type,
            "authority": routing["authority"],
        }
        for entry in provenance:
            if entry.get("planner_field") in {"脚本类型", "行业或活动类别"}:
                entry["script_type_resolution"] = routing_resolution
    if not brief.get("项目名称") and project_context is not None:
        # A project label comes from the authorized project record, not an invented Brief fact.
        if (not isinstance(project_context, dict)
            or project_context.get("project_id") != evidence.get("project_id")
            or project_context.get("workspace_id") != evidence.get("workspace_id")
            or project_context.get("authority") != "projects.name"
            or type(project_context.get("project_version")) is not int
            or not isinstance(project_context.get("name"), str)
            or not project_context["name"].strip()):
            raise _ConfirmedBriefError("confirmed_brief_resolution_drift", "项目名称来源与当前项目不一致")
        brief["项目名称"] = project_context["name"]
        brief["_formal_project_context"] = dict(project_context)
    missing = [field_name for field_name in _FORMAL_PLANNING_REQUIRED_FIELDS if not brief.get(field_name)]
    if missing:
        raise _ConfirmedBriefError(
            "confirmed_brief_needs_clarification",
            "正式 Brief 缺少规划必需字段: " + "、".join(missing),
        )
    brief["_formal_fact_evidence"] = {
        entry["planner_field"]: {
            "fact_id": entry["fact_id"],
            "evidence_refs": list(entry["evidence_refs"]),
            **({"semantic_metadata": entry["semantic_metadata"]} if entry.get("semantic_metadata") else {}),
        }
        for entry in provenance if entry.get("planner_field")
    }
    brief.setdefault("项目ID", evidence["project_id"])
    brief.setdefault("Brief来源文件", evidence["payload"]["ref"])
    brief.setdefault("Brief来源 sheet", "FormalBrief")
    brief.setdefault("Brief来源行", 1)
    brief.setdefault("Brief来源 SHA256", evidence["payload"]["sha256"])
    brief.setdefault("Brief来源证据", evidence["brief_version_id"])
    return brief, provenance


def _formal_conflict_selections(
    payload: dict, facts: list[dict]
) -> tuple[dict[str, dict], dict[str, dict]]:
    conflicts = payload.get("conflicts", [])
    if not isinstance(conflicts, list):
        raise _ConfirmedBriefError(
            "confirmed_brief_payload_invalid", "正式 Brief conflicts 必须是数组"
        )
    fact_by_id = {
        str(fact.get("fact_id") or ""): fact
        for fact in facts if isinstance(fact, dict) and str(fact.get("fact_id") or "")
    }
    if len(fact_by_id) != len(facts):
        raise _ConfirmedBriefError(
            "confirmed_brief_payload_invalid", "正式 Brief fact_id 缺失或重复"
        )
    selected: dict[str, dict] = {}
    rejected: dict[str, dict] = {}
    claimed: set[str] = set()
    for conflict in conflicts:
        if not isinstance(conflict, dict) or conflict.get("resolution_status") != "resolved":
            raise _ConfirmedBriefError(
                "confirmed_brief_conflicts_unresolved", "正式 Brief artifact 仍含未解决冲突"
            )
        candidates = conflict.get("fact_ids")
        selected_id = str(conflict.get("resolved_fact_id") or "")
        if (
            not isinstance(candidates, list)
            or len(candidates) < 2
            or not all(isinstance(item, str) and item for item in candidates)
            or len(set(candidates)) != len(candidates)
            or selected_id not in candidates
            or any(item not in fact_by_id for item in candidates)
            or any(item in claimed for item in candidates)
        ):
            raise _ConfirmedBriefError(
                "confirmed_brief_payload_invalid", "正式 Brief 冲突候选或 resolved_fact_id 无效"
            )
        claimed.update(candidates)
        field_path = str(conflict.get("field_path") or "")
        if not field_path or any(
            str(fact_by_id[item].get("field_path") or "") != field_path
            for item in candidates
        ):
            raise _ConfirmedBriefError(
                "confirmed_brief_payload_invalid", "正式 Brief 冲突字段与候选事实不一致"
            )
        actor_id = str(conflict.get("resolved_by_actor_id") or "")
        resolved_at = str(conflict.get("resolved_at") or "")
        resolution_evidence = conflict.get("resolution_evidence")
        if (
            not actor_id
            or not resolved_at
            or not isinstance(resolution_evidence, dict)
            or set(resolution_evidence) != {"ref", "sha256"}
        ):
            raise _ConfirmedBriefError(
                "confirmed_brief_payload_invalid", "正式 Brief 冲突缺少人工解决证据"
            )
        binding = {
            "conflict_id": str(conflict.get("conflict_id") or ""),
            "resolved_fact_id": selected_id,
            "resolved_by_actor_id": actor_id,
            "resolved_at": resolved_at,
            "resolution_evidence": dict(resolution_evidence),
        }
        if not binding["conflict_id"]:
            raise _ConfirmedBriefError(
                "confirmed_brief_payload_invalid", "正式 Brief conflict_id 不能为空"
            )
        selected[selected_id] = binding
        for candidate in candidates:
            if candidate != selected_id:
                rejected[candidate] = binding
    return selected, rejected


def _formal_planning_events(
    value: object, *, fact_id: str, evidence_refs: list[object],
    source_filenames: dict[str, str] | None = None,
) -> list[dict]:
    """Turn the T01 canonical event-flow fact into P00's only dynamic-N input."""
    if not isinstance(value, str):
        raise _ConfirmedBriefError(
            "confirmed_brief_payload_invalid", "关键事件与流程必须是文本"
        )
    evidence = "；".join(str(item).strip() for item in evidence_refs if str(item).strip())
    titles = _split_event_text(value)
    source = _formal_event_source(evidence_refs, source_filenames or {})
    return [
        {
            "title": title,
            "evidence": evidence,
            "fact_status": "confirmed",
            "source_fact_id": fact_id,
            **({"source": source} if source else {}),
        }
        for title in titles
    ]


def _formal_source_filenames(payload: dict) -> dict[str, str]:
    filenames = {}
    for item in payload.get("source_documents") or []:
        if not isinstance(item, dict):
            continue
        document_id = str(item.get("document_id") or "").strip()
        filename = str(item.get("filename") or "").strip()
        if (
            not document_id
            or not filename
            or filename in {".", ".."}
            or Path(filename).name != filename
            or "\\" in filename
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        ):
            continue
        filenames[document_id] = filename
    return filenames


def _formal_event_source(
    evidence_refs: list[object], source_filenames: dict[str, str]
) -> dict | None:
    for evidence in evidence_refs:
        if not isinstance(evidence, dict):
            continue
        filename = source_filenames.get(str(evidence.get("source_document_id") or ""))
        location = evidence.get("location")
        if not filename or not isinstance(location, dict):
            continue
        source = {"file": filename}
        for key in ("page", "sheet", "row", "column", "slide", "paragraph", "image_index"):
            value = location.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                source[key] = value
        return source
    return None


def _confirmed_brief_evidence(value: object) -> dict:
    """Project the frozen T01 ConfirmedBriefInput contract for plan provenance."""
    if not isinstance(value, ConfirmedBriefInput):
        raise _ConfirmedBriefError(
            "confirmed_brief_input_invalid",
            "ConfirmedBriefInput 必须是 T01 正式不可变对象，不接受 Mapping 或裸 dict",
        )
    schema_version = _confirmed_brief_value(value, "schema_version", required=False)
    if schema_version not in (None, "mplus-confirmed-brief-input/v1"):
        raise _ConfirmedBriefError(
            "confirmed_brief_input_invalid",
            "ConfirmedBriefInput schema_version 不受支持",
        )
    evidence = {
        field_name: _confirmed_brief_value(value, field_name)
        for field_name in _CONFIRMED_BRIEF_FIELDS
    }
    for field_name in (
        "workspace_id",
        "project_id",
        "brief_version_id",
        "confirmation_id",
        "brief_snapshot_id",
        "confirmed_by_actor_id",
        "confirmed_at",
    ):
        if not str(evidence[field_name] or "").strip():
            raise _ConfirmedBriefError(
                "confirmed_brief_input_invalid",
                f"ConfirmedBriefInput 缺少 {field_name}",
            )
    payload = evidence["payload"]
    if not isinstance(payload, ArtifactPayload):
        raise _ConfirmedBriefError(
            "confirmed_brief_input_invalid",
            "ConfirmedBriefInput payload 必须是正式 ArtifactPayload 对象",
        )
    payload_ref = str(payload.ref or "")
    payload_sha256 = str(payload.sha256 or "")
    if (
        not payload_ref.startswith("artifact://")
        or len(payload_sha256) != 64
        or any(character not in "0123456789abcdef" for character in payload_sha256.lower())
    ):
        raise _ConfirmedBriefError(
            "confirmed_brief_input_invalid",
            "ConfirmedBriefInput payload 引用或 SHA256 不合法",
        )
    if evidence["confirmation_status"] != "confirmed":
        raise _ConfirmedBriefError(
            "confirmed_brief_not_confirmed",
            "Brief 尚未确认，不能开始镜头规划",
        )
    conflicts = evidence["unresolved_conflict_count"]
    if isinstance(conflicts, bool) or not isinstance(conflicts, int):
        raise _ConfirmedBriefError(
            "confirmed_brief_input_invalid",
            "unresolved_conflict_count 必须是整数",
        )
    if conflicts != 0:
        raise _ConfirmedBriefError(
            "confirmed_brief_conflicts_unresolved",
            "Brief 仍有未解决冲突，不能开始镜头规划",
        )
    return {
        **{field_name: evidence[field_name] for field_name in _CONFIRMED_BRIEF_FIELDS if field_name != "payload"},
        "payload": {"ref": payload_ref, "sha256": payload_sha256},
    }


def _confirmed_brief_value(
    value: object, field_name: str, *, required: bool = True
) -> Any:
    missing = object()
    result = getattr(value, field_name, missing)
    if result is missing and required:
        raise _ConfirmedBriefError(
            "confirmed_brief_input_invalid",
            f"ConfirmedBriefInput 缺少 {field_name}",
        )
    return None if result is missing else result


def _confirmed_creative_standard_evidence(value: object) -> dict:
    if not isinstance(value, ConfirmedCreativeStandardInput):
        raise _ConfirmedBriefError(
            "creative_standard_input_invalid",
            "ConfirmedCreativeStandardInput 必须是正式不可变对象，不接受 Mapping 或裸 dict",
        )
    mapped = value.as_mapping()
    if mapped.get("schema_version") != "mplus-confirmed-creative-standard-input/v1":
        raise _ConfirmedBriefError("creative_standard_input_invalid", "标准卡 schema_version 不受支持")
    for key in (
        "workspace_id", "project_id", "creative_standard_version_id", "brief_version_id",
        "confirmation_id", "brief_snapshot_id", "confirmed_by_actor_id", "confirmed_at",
    ):
        if not str(mapped.get(key) or "").strip():
            raise _ConfirmedBriefError("creative_standard_input_invalid", f"标准卡缺少 {key}")
    if mapped.get("confirmation_status") != "confirmed":
        raise _ConfirmedBriefError("creative_standard_not_confirmed", "创作标准卡尚未确认")
    for key in ("brief_payload", "card_payload"):
        payload = mapped.get(key)
        if (
            not isinstance(payload, dict)
            or not str(payload.get("ref") or "").startswith("artifact://")
            or len(str(payload.get("sha256") or "")) != 64
        ):
            raise _ConfirmedBriefError("creative_standard_input_invalid", f"标准卡 {key} 无效")
    return mapped


def _resolve_confirmed_creative_standard(
    value: ConfirmedCreativeStandardInput | None,
    resolver: ConfirmedCreativeStandardResolver | None,
    brief_evidence: dict,
) -> _ResolvedCreativeStandard:
    if value is None or resolver is None:
        raise _ConfirmedBriefError(
            "creative_standard_dependency_missing",
            "缺少与已确认 Brief 同版的 ConfirmedCreativeStandardInput Resolver",
        )
    evidence = _confirmed_creative_standard_evidence(value)
    if (
        evidence["workspace_id"], evidence["project_id"], evidence["brief_version_id"],
        evidence["brief_snapshot_id"], evidence["brief_payload"],
    ) != (
        brief_evidence["workspace_id"], brief_evidence["project_id"], brief_evidence["brief_version_id"],
        brief_evidence["brief_snapshot_id"], brief_evidence["payload"],
    ):
        raise _ConfirmedBriefError("creative_standard_binding_drift", "标准卡与当前 Confirmed Brief 绑定不一致")
    try:
        content = resolver.resolve(value)
    except Exception as exc:
        raise _ConfirmedBriefError(
            "creative_standard_resolution_failed", "无法从正式持久记录解析已确认创作标准卡"
        ) from exc
    if not isinstance(content, dict):
        raise _ConfirmedBriefError("creative_standard_resolution_invalid", "标准卡 Resolver 未返回结构化内容")
    return _ResolvedCreativeStandard(content=content, evidence=evidence)


def run_script_draft_stage(
    state,
    *,
    script_plan_path: str | Path,
    quality_profile: str = "draft",
    text_model: str = "MiniMax-M2.7",
    text_command: str = "mmx",
    provider: TextProvider | None = None,
    writer_provider_name: str = "minimax-cli",
    writer_model: str | None = None,
    editor_provider_name: str = "minimax-cli",
    editor_model: str | None = None,
    writer_provider: TextProvider | None = None,
    editor_provider: TextProvider | None = None,
    revision_constraints: dict | None = None,
    parent_draft_path: str | Path | None = None,
    image_manifest_rows: list[dict] | None = None,
    image_analysis_rows: list[dict] | None = None,
) -> ScriptDraftArtifact:
    stage_id = "script_draft"
    frozen = reused_fork_stage_output(state, stage_id, "script_draft.json")
    if frozen is not None:
        return read_script_draft(frozen)
    plan_path = Path(script_plan_path)
    dependency_refs, dependency_hashes = _dependency_evidence(
        state, ("script_plan",), optional_stage_ids=("visual_analysis",)
    )
    parent_draft = Path(parent_draft_path) if parent_draft_path else None
    input_sha256 = sha256_json({
        "dependencies": dependency_hashes,
        "script_plan_sha256": sha256_file(plan_path),
        "quality_profile": quality_profile,
        "text_model": text_model,
        "text_command": text_command,
        "writer_provider": writer_provider_name,
        "writer_model": writer_model or text_model,
        "editor_provider": editor_provider_name,
        "editor_model": editor_model or text_model,
        "prompt_version": PROMPT_VERSION,
        "grounding_version": GROUNDING_VERSION,
        "response_schema_version": SCRIPT_DRAFT_SCHEMA_VERSION,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "text_timeout_seconds": TEXT_TIMEOUT_SECONDS,
        "revision_constraints": revision_constraints or {},
        "parent_draft_sha256": (
            sha256_file(parent_draft)
            if parent_draft is not None and parent_draft.is_file()
            else ""
        ),
        "indexed_visual_context": sha256_json({
            "manifest": image_manifest_rows or [],
            "analysis": image_analysis_rows or [],
        }),
    })
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=dependency_refs,
    )
    reused = _reused_output(state, record)
    if reused is not None:
        return read_script_draft(reused)
    plan = read_script_plan(plan_path)
    if revision_constraints is not None:
        plan = replace(plan, constraints={**plan.constraints, **revision_constraints})
    if plan.confirmed_brief_input and quality_profile != "client-review":
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "confirmed_brief_provider_required",
            "已确认 Brief 只能经 client-review Provider 路由生成 ScriptDraft",
        )
    try:
        drafted = create_script_draft(
            plan,
            quality_profile=quality_profile,
            text_model=text_model,
            text_command=text_command,
            cache_dir=state.run_dir.parent.parent / "text_cache",
            provider=provider,
            writer_provider_name=writer_provider_name,
            writer_model=writer_model,
            editor_provider_name=editor_provider_name,
            editor_model=editor_model,
            writer_provider=writer_provider,
            editor_provider=editor_provider,
            image_manifest_rows=image_manifest_rows,
            image_analysis_rows=image_analysis_rows,
        )
        drafted = _preserve_unaffected_parent_script_text(
            drafted,
            revision_constraints or {},
            parent_draft,
        )
        if quality_profile == "draft":
            _complete_local_draft_business_content(drafted, plan)
        _validate_draft_business_content(drafted, plan)
        if plan.confirmed_brief_input:
            _validate_no_unconfirmed_template_mechanisms(drafted, plan)
            _validate_no_duplicate_shot_content(drafted)
        avoid_hits = creative_standard_draft_hits(
            plan.constraints,
            drafted["script_rows"],
            extra_texts=[str(drafted.get("overall_summary") or "")],
        )
        if avoid_hits:
            raise ScriptDraftError(
                "创作标准卡禁止表达命中: "
                + "、".join(f"镜{hit['shot_no']}:{hit['field']}" for hit in avoid_hits)
            )
    except ScriptDraftProviderError as exc:
        if plan.confirmed_brief_input:
            _abort(
                state,
                stage_id,
                StageStatus.BLOCKED,
                StageErrorCategory.CONTRACT,
                "confirmed_brief_provider_unavailable",
                "已确认 Brief 的 Provider 不可用，未生成伪造 ScriptDraft",
            )
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "script_draft_provider_failed",
            str(exc),
            retryable=True,
        )
    except ScriptDraftError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "script_draft_contract_blocked",
            str(exc),
        )
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "script_draft_io_failed",
            f"文本草稿 IO 失败: {exc}",
            retryable=True,
        )
    output_path = state.run_dir / "script_draft.json"
    payload = {
        "schema_version": "mplus-script-draft/v1",
        "script_plan_ref": str(plan.path.resolve()),
        "briefs": plan.briefs,
        "brief": plan.brief,
        "constraints": plan.constraints,
        "source_view": plan.source_view,
        "quality_profile": drafted["quality_profile"],
        "text_model": drafted["text_model"],
        "writer_provider": drafted.get("writer_provider", writer_provider_name),
        "writer_model": drafted.get("writer_model", writer_model or text_model),
        "editor_provider": drafted.get("editor_provider", editor_provider_name),
        "editor_model": drafted.get("editor_model", editor_model or text_model),
        "overall_summary": drafted["overall_summary"],
        "shot_count": len(drafted["script_rows"]),
        "source_refs": plan.source_refs,
        "script_rows": drafted["script_rows"],
        "detail_rows": plan.detail_rows,
        "decisions": [asdict(item) for item in plan.decisions],
        "template_slots": [asdict(item) for item in plan.template_slots],
        "pending_confirmations": drafted["pending_confirmations"],
        "usage": drafted["usage"],
        "capacity_report": plan.capacity_report,
        "traceability": _traceability_with_output(
            plan.traceability, drafted["script_rows"], plan.shot_plans,
        ),
        "field_decisions": _draft_field_decisions(plan, drafted["script_rows"]),
        "conflicts": plan.conflicts,
        "decision_counts": plan.decision_counts,
    }
    try:
        write_json_atomic(output_path, payload)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "script_draft_write_failed",
            f"文本草稿写出失败: {exc}",
            retryable=True,
        )
    finish_pipeline_stage(
        state,
        stage_id,
        StageStatus.SUCCEEDED,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return read_script_draft(output_path)


def _validate_draft_business_content(drafted: dict, plan: ScriptPlanArtifact) -> None:
    """Reject a draft that is structurally present but unusable to production.

    The provider is allowed to change only writing fields, but neither the
    deterministic ``draft`` route nor a live Provider may pass an empty shell
    to visual binding and workbook rendering.  Source tracing remains locked
    to ScriptPlan; this guard verifies it is present for every generated shot.
    """
    script_rows = list(drafted.get("script_rows") or [])
    if len(script_rows) != len(plan.script_rows):
        raise ScriptDraftError("ScriptDraft 镜头数量与 ScriptPlan 不一致")
    if len(plan.detail_rows) != len(script_rows):
        raise ScriptDraftError("ScriptPlan 来源明细数量与 ScriptDraft 不一致")
    for index, (script_row, detail_row) in enumerate(
        zip(script_rows, plan.detail_rows), start=1
    ):
        shot_no = str(script_row.get("镜号") or "").strip()
        if not shot_no:
            raise ScriptDraftError(f"镜{index}缺少镜号")
        if not str(script_row.get("画面描述") or "").strip():
            raise ScriptDraftError(f"镜{shot_no}画面描述不能为空")
        if not str(script_row.get("声音") or "").strip():
            raise ScriptDraftError(f"镜{shot_no}声音不能为空")
        if parse_positive_seconds_strict(script_row.get("时长(秒)", "")) is None:
            raise ScriptDraftError(f"镜{shot_no}时长必须是正整数秒")
        source_file = str(detail_row.get("采用来源文件") or "").strip()
        source_sheet = str(detail_row.get("采用来源 sheet") or "").strip()
        try:
            source_row = int(detail_row.get("采用来源行") or 0)
        except (TypeError, ValueError):
            source_row = 0
        if not source_file or not source_sheet or source_row <= 0:
            raise ScriptDraftError(f"镜{shot_no}来源文件、Sheet、行不能为空")
        # Content/position completeness is reviewed through output evidence.
        # A missing literal keyword is not a structural generation failure.


def _draft_field_decisions(plan: ScriptPlanArtifact, script_rows: list[dict]) -> list[dict]:
    result = []
    for index, planned in enumerate(plan.shot_plans):
        row = script_rows[index] if index < len(script_rows) else {}
        items = json.loads(json.dumps(planned.get("field_decisions") or [], ensure_ascii=False))
        for item in items:
            item["phase"] = "draft"
            if item.get("field") == "visual_description":
                item.update({
                    "value_status": "resolved", "final_value": row.get("画面描述", ""),
                    "selected_source": "writer_model", "decision_status": "applied",
                    "override_reason_code": "example_only_realized_by_draft",
                    "override_reason": "模型文案仅实现已锁定事件，不继承模板案例事实",
                })
            elif item.get("field") == "sound":
                item.update({
                    "value_status": "resolved", "final_value": row.get("声音", ""),
                    "selected_source": "writer_model", "decision_status": "applied",
                })
        result.append({"shot_no": str(row.get("镜号") or index + 1), "decisions": items})
    return result


def _validate_no_unconfirmed_template_mechanisms(
    drafted: dict, plan: ScriptPlanArtifact
) -> None:
    """Never let a source-library person or mechanism become a project fact."""
    excluded = {
        str(value).strip()
        for shot in plan.shot_plans
        for value in shot.get("excluded_source_mechanisms") or []
        if str(value).strip()
    }
    accepted_by_shot = {
        str(shot.get("shot_no") or ""): (shot.get("creative_block") or {}).get("fields", {})
        for shot in plan.shot_plans if shot.get("creative_block")
    }
    for row in drafted.get("script_rows") or []:
        shot_no = str(row.get("镜号") or "?")
        for field in ("画面描述", "声音", "字幕", "文案", "备注"):
            text = str(row.get(field) or "")
            if field in {"画面描述", "声音"} and text == accepted_by_shot.get(shot_no, {}).get(field):
                # Exact selected expressions retain their provisional wording;
                # they cannot license factual additions in another shot.
                continue
            hits = [value for value in excluded if value in text]
            if hits:
                raise ScriptDraftError(
                    f"镜{shot_no}{field}继承了 Brief 未确认的模板内容: {'、'.join(hits)}"
                )

    confirmed_text = " ".join([
        *(str((getattr(plan, "brief", {}) or {}).get(field) or "")
          for field in ("必须出现的内容", "事件流程")),
        *(
        str(event.get("text") or "")
        for shot in plan.shot_plans
        if not shot.get("generated_structure_only") and not shot.get("creative_block")
        and shot.get("brief_evidence", {}).get("type") != "creative_draft"
        for event in shot.get("events") or []
        ),
    ])
    fact_groups = {
        "艺人亮相": (
            ("艺人", "明星", "嘉宾", "代言人"),
            ("艺人", "明星", "嘉宾", "代言人"),
        ),
        "仪式": (
            ("仪式", "启动", "点亮", "剪彩", "颁奖", "揭幕", "发布"),
            ("仪式", "启动", "点亮", "剪彩", "颁奖", "揭幕"),
        ),
        "表演": (
            ("表演", "演出", "舞蹈", "乐队", "合唱"),
            ("表演", "演出", "舞蹈", "乐队", "合唱"),
        ),
        "发言": (
            ("发言", "致辞", "演讲", "宣讲", "讲话"),
            ("发言", "致辞", "演讲", "宣讲", "讲话"),
        ),
    }
    rows_by_shot = {
        str(row.get("镜号") or ""): row for row in drafted.get("script_rows") or []
    }
    for shot in plan.shot_plans:
        if not shot.get("generated_structure_only"):
            continue
        structure = str(shot.get("structure") or "")
        applicable = []
        if any(term in structure for term in ("艺人", "明星", "嘉宾")):
            applicable.append("艺人亮相")
        if any(term in structure for term in ("仪式", "宣讲", "发布")):
            applicable.extend(("仪式", "表演", "发言"))
        row = rows_by_shot.get(str(shot.get("shot_no") or ""), {})
        visible = " ".join(str(row.get(field) or "") for field in ("画面描述", "声音"))
        for group in applicable:
            confirmation_terms, output_terms = fact_groups[group]
            if any(term in confirmed_text for term in confirmation_terms):
                continue
            hits = [term for term in output_terms if term in visible]
            if hits:
                raise ScriptDraftError(
                    f"镜{shot.get('shot_no') or '?'}由技术结构标签虚构了 Brief 未确认的内容: "
                    + "、".join(hits)
                )


def _validate_no_duplicate_shot_content(drafted: dict) -> None:
    """Exact repeated visual and sound pairs are unusable, not a complete draft."""
    seen: dict[tuple[str, str], str] = {}
    for row in drafted.get("script_rows") or []:
        shot_no = str(row.get("镜号") or "?")
        key = (
            "".join(str(row.get("画面描述") or "").split()),
            "".join(str(row.get("声音") or "").split()),
        )
        previous = seen.get(key)
        if previous is not None:
            raise ScriptDraftError(f"镜{previous}与镜{shot_no}的画面和声音完全重复")
        seen[key] = shot_no


def _traceability_with_output(
    traceability: list[dict], script_rows: list[dict], shot_plans: list[dict],
    *, shot_ids_by_no: dict[str, str] | None = None,
) -> list[dict]:
    scripts = {
        str(row.get("镜号") or index): row
        for index, row in enumerate(script_rows, start=1)
    }
    plans = {
        str(row.get("shot_no") or index): row
        for index, row in enumerate(shot_plans, start=1)
    }
    result = []
    for raw in traceability:
        item = dict(raw)
        item["plan_status"] = str(item.get("plan_status") or item.get("final_status") or "uncovered")
        requirement = str(item.get("brief_text") or "").strip()
        rule = item.get("position_rule") or {}
        position_type = str(rule.get("type") or "none")
        evidence = []
        for shot_no in item.get("plan_shot_nos") or []:
            script = scripts.get(str(shot_no), {})
            plan = plans.get(str(shot_no), {})
            position_verified = (
                position_type == "none"
                or (position_type == "opening" and str(shot_no) == next(iter(scripts), ""))
                or (position_type == "closing" and str(shot_no) == next(reversed(scripts), ""))
                or (position_type == "fixed_shot" and str(shot_no) == str(rule.get("shot_no")))
            )
            content = requirement_output_evidence(
                requirement, script,
                shot_id=str((shot_ids_by_no or {}).get(str(shot_no)) or plan.get("shot_id") or ""),
                event_ids=tuple(plan.get("event_ids") or ()),
                position_verified=position_verified,
            )
            content["requirement_id"] = str(item.get("requirement_id") or "")
            evidence.append({
                "shot_no": str(shot_no),
                "visual_description": str(script.get("画面描述") or ""),
                "sound": str(script.get("声音") or ""),
                "event_ids": list(plan.get("event_ids") or []),
                "verified_fields": [],
                "content_evidence": content,
                "verification_status": "pending_review",
                "verification_basis": (
                    "已定位部分文字；尚未定位：" + "、".join(content["missing_requirement_parts"]) + "；完整画面待人工核对"
                    if content["mentioned"] and content["missing_requirement_parts"]
                    else "已定位文字提及，具体画面与完整要求待人工核对" if content["mentioned"]
                    else "未定位原文匹配；同义画面仍待人工核对，不能据此判完全缺失"
                ),
            })
        item["output_evidence"] = evidence
        verified = any(row["verification_status"] == "text_verified" for row in evidence)
        item["output_status"] = "text_verified" if verified else "pending_review"
        if not evidence and item["plan_status"] in {"optional", "not_applicable", "review_proposed"}:
            item["final_status"] = item["plan_status"]
        else:
            item["final_status"] = "output_verified" if verified else "pending_review"
        result.append(item)
    return result


def _complete_local_draft_business_content(
    drafted: dict, plan: ScriptPlanArtifact
) -> None:
    """Label empty local-draft fields as pending; never manufacture coverage."""
    for index, (script_row, detail_row) in enumerate(zip(
        drafted.get("script_rows") or [], plan.detail_rows
    )):
        event = str(detail_row.get("规划主事件") or "").strip()
        if not event:
            event = str(script_row.get("结构") or "当前流程").strip()
        if not str(script_row.get("画面描述") or "").strip():
            script_row["画面描述"] = f"待完善画面：{event}"
        if not str(script_row.get("声音") or "").strip():
            script_row["声音"] = f"待确认声音：{event}"


def _preserve_unaffected_parent_script_text(
    drafted: dict,
    revision_constraints: dict,
    parent_draft_path: Path | None,
) -> dict:
    feedback = [
        item for item in revision_constraints.get("user_feedback", [])
        if str(item.get("category") or "").casefold() != "image"
    ]
    if not feedback or parent_draft_path is None:
        return drafted
    targeted = {
        str(item.get("shot_no") or "").strip()
        for item in feedback
        if str(item.get("scope") or "").casefold() in {"shot", "single_shot"}
        and str(item.get("shot_no") or "").strip().isdigit()
    }
    if not targeted or any(
        str(item.get("scope") or "").casefold() not in {"shot", "single_shot"}
        for item in feedback
    ):
        return drafted
    if not parent_draft_path.is_file():
        raise ScriptDraftError("定点修改缺少父版本 ScriptDraft，不能安全保留未修改镜头")
    try:
        parent = json.loads(parent_draft_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ScriptDraftError(f"父版本 ScriptDraft 不可读取: {exc}") from exc
    parent_rows = {
        str(row.get("镜号") or ""): dict(row)
        for row in parent.get("script_rows") or []
    }
    revised_rows = []
    preserved_count = 0
    for row in drafted.get("script_rows") or []:
        item = dict(row)
        shot_no = str(item.get("镜号") or "")
        if shot_no not in targeted:
            previous = parent_rows.get(shot_no)
            if previous is None:
                raise ScriptDraftError(f"父版本缺少镜 {shot_no}，不能执行定点文本修改")
            for key in ("画面描述", "声音"):
                item[key] = previous.get(key, item.get(key, ""))
            preserved_count += 1
        revised_rows.append(item)
    parent_pending = list(parent.get("pending_confirmations") or [])
    revised_pending = list(drafted.get("pending_confirmations") or [])
    pending = [
        item for item in parent_pending
        if str(item.get("shot_no") or "") not in targeted
    ] + [
        item for item in revised_pending
        if str(item.get("shot_no") or "") in targeted
    ]
    result = dict(drafted)
    result["overall_summary"] = str(
        parent.get("overall_summary") or drafted.get("overall_summary") or ""
    )
    result["script_rows"] = revised_rows
    result["pending_confirmations"] = pending
    result["usage"] = {
        **dict(drafted.get("usage") or {}),
        "targeted_revision_shots": sorted(targeted, key=int),
        "preserved_parent_text_shot_count": preserved_count,
    }
    return result


def run_visual_binding_stage(
    state,
    *,
    script_plan_path: str | Path | None = None,
    script_draft_path: str | Path | None = None,
    binder: Callable[[ScriptDraftArtifact], tuple[list[dict], list[dict], list[dict]]],
    semantic_inputs: dict | None = None,
    revision_constraints: dict | None = None,
) -> VisualBindingArtifact:
    stage_id = "visual_binding"
    frozen = reused_fork_stage_output(state, stage_id, "visual_binding.json")
    if frozen is not None:
        return read_visual_binding(frozen)
    if script_draft_path is None:
        if script_plan_path is None:
            raise ValueError("script_plan_path 或 script_draft_path 必须提供一个")
        script_draft_path = run_script_draft_stage(
            state,
            script_plan_path=script_plan_path,
        ).path
    draft_path = Path(script_draft_path)
    dependency_refs, dependency_hashes = _dependency_evidence(
        state, ("script_draft",), optional_stage_ids=("visual_analysis",)
    )
    input_sha256 = sha256_json({
        "dependencies": dependency_hashes,
        "script_draft_sha256": sha256_file(draft_path),
        "semantic_inputs": semantic_inputs or {},
    })
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=dependency_refs,
    )
    reused = _reused_output(state, record)
    if reused is not None:
        return read_visual_binding(reused)
    plan = read_script_draft(draft_path)
    if revision_constraints is not None:
        plan = replace(plan, constraints=dict(revision_constraints))
    try:
        script_rows, detail_rows, visual_asset_rows = binder(plan)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "visual_binding_io_failed",
            f"画面绑定 IO 失败: {exc}",
            retryable=True,
        )
    except Exception as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.EXECUTION,
            "visual_binding_execution_failed",
            f"画面绑定执行失败: {exc}",
            retryable=True,
        )
    if len(script_rows) != len(detail_rows):
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "visual_binding_count_mismatch",
            "画面绑定后的镜头与抽调明细数量不一致",
        )
    source_refs = _source_refs(detail_rows)
    if len(source_refs) != len(detail_rows):
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "visual_binding_source_trace_incomplete",
            "画面绑定结果缺少来源文件、来源 Sheet 或来源行",
        )
    bindings = _bindings(
        script_rows,
        detail_rows,
        visual_asset_rows,
        constraints=plan.constraints,
    )
    output_path = state.run_dir / "visual_binding.json"
    payload = {
        "schema_version": "mplus-visual-binding/v1",
        "script_plan_ref": str(plan.plan_path.resolve()),
        "script_draft_ref": str(draft_path.resolve()),
        "briefs": plan.briefs,
        "brief": plan.brief,
        "constraints": plan.constraints,
        "source_view": plan.source_view,
        "quality_profile": plan.quality_profile,
        "text_model": plan.text_model,
        "overall_summary": plan.overall_summary,
        "pending_confirmations": plan.pending_confirmations,
        "text_usage": plan.usage,
        "shot_count": len(script_rows),
        "source_refs": source_refs,
        "script_rows": script_rows,
        "detail_rows": detail_rows,
        "decisions": [asdict(item) for item in plan.decisions],
        "template_slots": [asdict(item) for item in plan.template_slots],
        "visual_asset_rows": visual_asset_rows,
        "image_analysis_rows": list((semantic_inputs or {}).get("image_analysis_rows") or []),
        "bindings": bindings,
        "capacity_report": plan.capacity_report,
        "traceability": plan.traceability,
        "field_decisions": _visual_binding_field_decisions(plan.field_decisions, bindings),
        "conflicts": plan.conflicts,
        "decision_counts": plan.decision_counts,
    }
    try:
        write_json_atomic(output_path, payload)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "visual_binding_write_failed",
            f"画面绑定结果写出失败: {exc}",
            retryable=True,
        )
    finish_pipeline_stage(
        state,
        stage_id,
        StageStatus.SUCCEEDED,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return read_visual_binding(output_path)


def _visual_binding_field_decisions(
    shots: list[dict], bindings: list[dict],
) -> list[dict]:
    result = json.loads(json.dumps(shots, ensure_ascii=False))
    by_shot = {str(item.get("shot_no") or ""): item for item in bindings}
    for shot in result:
        binding = by_shot.get(str(shot.get("shot_no") or ""), {})
        for item in shot.get("decisions") or []:
            item["phase"] = "visual_binding"
            if item.get("field") != "reference_image":
                continue
            value = (
                binding.get("image_id")
                or binding.get("original_path")
                or ""
            )
            item.update({
                "value_status": "resolved" if value else "deferred",
                "final_value": value,
                "selected_source": "project_image_evidence" if value else "deferred",
                "decision_status": "applied" if value else "deferred",
                "override_reason_code": "project_image_selected" if value else "",
                "override_reason": "最终配图来自项目级图片绑定证据" if value else "",
            })
    return result


def run_workbook_render_stage(
    state,
    *,
    visual_binding_path: str | Path,
    renderer: Callable[[VisualBindingArtifact, Path, list[dict]], None],
) -> WorkbookRenderArtifact:
    stage_id = "workbook_render"
    binding_path = Path(visual_binding_path)
    input_sha256 = sha256_file(binding_path)
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=(str(binding_path.resolve()),),
    )
    reused = _reused_output(state, record)
    if reused is not None:
        return WorkbookRenderArtifact(reused, binding_path)
    binding = read_visual_binding(binding_path)
    output_path = state.run_dir / "workbook_render.xlsx"
    try:
        renderer(binding, output_path, [])
        workbook = load_workbook(output_path, read_only=True)
        workbook.close()
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "workbook_render_io_failed",
            f"工作簿写出失败: {exc}",
            retryable=True,
        )
    except Exception as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.EXECUTION,
            "workbook_render_failed",
            f"工作簿生成或重开失败: {exc}",
            retryable=True,
        )
    finish_pipeline_stage(
        state,
        stage_id,
        StageStatus.SUCCEEDED,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return WorkbookRenderArtifact(output_path, binding_path)


def run_machine_audit_stage(
    state,
    *,
    visual_binding_path: str | Path,
    workbook_render_path: str | Path,
    output_path: str | Path,
    audit_path: str | Path,
    auditor: Callable[[VisualBindingArtifact, Path], dict],
    renderer: Callable[[VisualBindingArtifact, Path, list[dict]], None],
) -> MachineAuditArtifact:
    stage_id = "machine_audit"
    binding_path = Path(visual_binding_path)
    workbook_path = Path(workbook_render_path)
    output_path = Path(output_path)
    audit_path = Path(audit_path)
    input_sha256 = sha256_json({
        "visual_binding_sha256": sha256_file(binding_path),
        "workbook_render_sha256": sha256_file(workbook_path),
    })
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=(
            str(binding_path.resolve()),
            str(workbook_path.resolve()),
        ),
    )
    reused = _reused_outputs(state, record)
    if reused is not None:
        final_output, final_audit = reused
        return MachineAuditArtifact(
            final_output,
            final_audit,
            json.loads(final_audit.read_text(encoding="utf-8")),
        )
    binding = read_visual_binding(binding_path)
    try:
        audit = auditor(binding, workbook_path)
        renderer(binding, output_path, list(audit.get("checks", [])))
        workbook = load_workbook(output_path, read_only=True)
        workbook.close()
        write_json_atomic(audit_path, audit)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "machine_audit_io_failed",
            f"机器审计 IO 失败: {exc}",
            retryable=True,
        )
    except Exception as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.EXECUTION,
            "machine_audit_execution_failed",
            f"机器审计执行失败: {exc}",
            retryable=True,
        )
    output_sha256 = sha256_json({
        "workbook": sha256_file(output_path),
        "audit": sha256_file(audit_path),
    })
    refs = (str(output_path.resolve()), str(audit_path.resolve()))
    if audit.get("status") == "fail":
        error = StageError(
            category=StageErrorCategory.CONTRACT,
            code="machine_audit_blocked",
            message=f"机器审计未通过: hard_fail={audit.get('hard_fail_count', 0)}",
            retryable=True,
        )
        finish_pipeline_stage(
            state,
            stage_id,
            StageStatus.BLOCKED,
            output_sha256=output_sha256,
            output_refs=refs,
            error=error,
        )
    else:
        status = (
            StageStatus.SUCCEEDED_WITH_WARNINGS
            if int(audit.get("warning_count") or 0) > 0
            else StageStatus.SUCCEEDED
        )
        finish_pipeline_stage(
            state,
            stage_id,
            status,
            output_sha256=output_sha256,
            output_refs=refs,
        )
    return MachineAuditArtifact(output_path, audit_path, audit)


def read_script_plan(path: str | Path) -> ScriptPlanArtifact:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ScriptPlanArtifact(
        path=path,
        briefs=list(payload["briefs"]),
        brief=dict(payload["brief"]),
        constraints=dict(payload["constraints"]),
        source_view=str(payload["source_view"]),
        script_rows=list(payload["script_rows"]),
        detail_rows=list(payload["detail_rows"]),
        decisions=[_decision_from_dict(item) for item in payload["decisions"]],
        template_slots=[_source_shot_from_dict(item) for item in payload["template_slots"]],
        source_refs=list(payload["source_refs"]),
        schema_version=str(payload.get("schema_version") or "mplus-script-plan/v1"),
        plan_schema_version=str(payload.get("plan_schema_version") or ""),
        brief_facts=list(payload.get("brief_facts") or []),
        requirements=list(payload.get("requirements") or []),
        events=list(payload.get("events") or []),
        event_graph=dict(payload.get("event_graph") or {}),
        duration_budget=dict(payload.get("duration_budget") or {}),
        shot_plans=list(payload.get("shot_plans") or []),
        coverage=list(payload.get("coverage") or []),
        capacity_report=dict(payload.get("capacity_report") or {}),
        traceability=list(payload.get("traceability") or []),
        template_rule_contract=dict(payload.get("template_rule_contract") or {}),
        conflicts=list(payload.get("conflicts") or []),
        decision_counts=dict(payload.get("decision_counts") or {}),
        planning_audit=dict(payload.get("planning_audit") or {}),
        planning_source_status=dict(payload.get("planning_source_status") or {}),
        confirmed_brief_input=dict(payload.get("confirmed_brief_input") or {}),
    )


def read_script_draft(path: str | Path) -> ScriptDraftArtifact:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ScriptDraftArtifact(
        path=path,
        plan_path=Path(payload["script_plan_ref"]),
        briefs=list(payload["briefs"]),
        brief=dict(payload["brief"]),
        constraints=dict(payload["constraints"]),
        source_view=str(payload["source_view"]),
        quality_profile=str(payload["quality_profile"]),
        text_model=str(payload["text_model"]),
        overall_summary=str(payload.get("overall_summary") or ""),
        script_rows=list(payload["script_rows"]),
        detail_rows=list(payload["detail_rows"]),
        decisions=[_decision_from_dict(item) for item in payload["decisions"]],
        template_slots=[_source_shot_from_dict(item) for item in payload["template_slots"]],
        source_refs=list(payload["source_refs"]),
        pending_confirmations=list(payload.get("pending_confirmations") or []),
        usage=dict(payload.get("usage") or {}),
        traceability=list(payload.get("traceability") or []),
        capacity_report=dict(payload.get("capacity_report") or {}),
        field_decisions=list(payload.get("field_decisions") or []),
        conflicts=list(payload.get("conflicts") or []),
        decision_counts=dict(payload.get("decision_counts") or {}),
    )


def read_visual_binding(path: str | Path) -> VisualBindingArtifact:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return VisualBindingArtifact(
        path=path,
        plan_path=Path(payload["script_plan_ref"]),
        briefs=list(payload["briefs"]),
        brief=dict(payload["brief"]),
        constraints=dict(payload["constraints"]),
        source_view=str(payload["source_view"]),
        quality_profile=str(payload.get("quality_profile") or "draft"),
        text_model=str(payload.get("text_model") or ""),
        overall_summary=str(payload.get("overall_summary") or ""),
        script_rows=list(payload["script_rows"]),
        detail_rows=list(payload["detail_rows"]),
        decisions=[_decision_from_dict(item) for item in payload["decisions"]],
        template_slots=[_source_shot_from_dict(item) for item in payload["template_slots"]],
        source_refs=list(payload["source_refs"]),
        visual_asset_rows=list(payload["visual_asset_rows"]),
        image_analysis_rows=list(payload.get("image_analysis_rows") or []),
        bindings=list(payload["bindings"]),
        pending_confirmations=list(payload.get("pending_confirmations") or []),
        text_usage=dict(payload.get("text_usage") or {}),
        traceability=list(payload.get("traceability") or []),
        capacity_report=dict(payload.get("capacity_report") or {}),
        field_decisions=list(payload.get("field_decisions") or []),
        conflicts=list(payload.get("conflicts") or []),
        decision_counts=dict(payload.get("decision_counts") or {}),
    )


def _source_shot_from_dict(payload: dict) -> SourceShot:
    values = dict(payload)
    values["missing_metadata"] = tuple(values.get("missing_metadata") or ())
    return SourceShot(**values)


def _decision_from_dict(payload: dict) -> TemplateDecision:
    values = dict(payload)
    values["slot"] = _source_shot_from_dict(values["slot"])
    values["variant"] = (
        _source_shot_from_dict(values["variant"])
        if values.get("variant") is not None
        else None
    )
    for key in ("hit_terms", "missing_terms", "conflict_terms"):
        values[key] = tuple(values.get(key) or ())
    return TemplateDecision(**values)


def _source_refs(detail_rows: list[dict]) -> list[dict]:
    refs = []
    for row in detail_rows:
        source_file = row.get("采用来源文件")
        source_sheet = row.get("采用来源 sheet")
        source_row = row.get("采用来源行")
        if not source_file or not source_sheet or not source_row:
            continue
        refs.append({
            "source_file": source_file,
            "source_sheet": source_sheet,
            "source_row": source_row,
            **({"category_id": row['采用来源类别'], "package_id": row.get('采用来源包', ''),
                "package_version": row.get('采用来源包版本', 0), "asset_id": row.get('采用来源资产ID', '')}
               if row.get('采用来源类别') else {}),
        })
    return refs


def _bindings(
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    *,
    constraints: dict | None = None,
) -> list[dict]:
    assets_by_slot = {}
    for asset in visual_asset_rows:
        slot = str(asset.get("slot_index") or "")
        if slot and slot not in assets_by_slot:
            assets_by_slot[slot] = asset
    text_only_slots = {
        str(item.get("shot_no") or "").strip()
        for item in (constraints or {}).get("user_feedback", [])
        if str(item.get("category") or "").casefold() == "image"
        and bool(item.get("approve_text_only"))
    }
    bindings = []
    for index, (script, detail) in enumerate(zip(script_rows, detail_rows), start=1):
        slot = str(detail.get("slot_index") or index)
        asset = assets_by_slot.get(slot, {})
        image_id = asset.get("image_id") or detail.get("image_id", "")
        original_path = asset.get("原图路径") or detail.get("原图路径", "")
        has_visual = bool(str(image_id or "").strip() and str(original_path or "").strip())
        text_basis = bool(
            str(script.get("画面描述") or "").strip()
            and str(detail.get("采用来源文件") or "").strip()
            and str(detail.get("采用来源 sheet") or "").strip()
            and str(detail.get("采用来源行") or "").strip()
        )
        resolution_status = (
            "resolved"
            if has_visual
            else "text_only_approved"
            if slot in text_only_slots
            else "visual_resolution_required"
        )
        bindings.append({
            "slot_index": detail.get("slot_index") or index,
            "shot_no": script.get("镜号", ""),
            "image_id": image_id,
            "original_path": original_path,
            "decision": asset.get("配图判断") or detail.get("配图判断", ""),
            "resolution_status": resolution_status,
            "document_basis_available": text_basis,
            "recommended_action": (
                "已绑定安全候选"
                if resolution_status == "resolved"
                else "已由人工批准仅使用文字依据，成片阶段待补素材"
                if resolution_status == "text_only_approved"
                else "重新检索安全候选、手工选图或补充本地图片"
            ),
            "basis": (
                asset.get("绑定说明")
                or detail.get("图片绑定说明")
                or asset.get("视觉内容摘要")
                or detail.get("视觉回写依据")
                or ""
            ),
        })
    return bindings


def _dependency_evidence(
    state,
    stage_ids: tuple[str, ...],
    *,
    optional_stage_ids: tuple[str, ...] = (),
) -> tuple[list[str], dict]:
    manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
    refs = []
    hashes = {}
    for stage_id in stage_ids:
        record = manifest["stages"][stage_id]
        refs.extend(record.get("output_refs") or [])
        hashes[stage_id] = record.get("output_sha256")
    for stage_id in optional_stage_ids:
        record = manifest["stages"][stage_id]
        if record.get("status") not in {
            StageStatus.SUCCEEDED.value,
            StageStatus.SUCCEEDED_WITH_WARNINGS.value,
        }:
            continue
        refs.extend(record.get("output_refs") or [])
        hashes[stage_id] = record.get("output_sha256")
    return refs, hashes


def _reused_output(state, record: dict) -> Path | None:
    if record.get("status") not in {
        StageStatus.SUCCEEDED.value,
        StageStatus.SUCCEEDED_WITH_WARNINGS.value,
    }:
        return None
    refs = record.get("output_refs") or []
    if not refs:
        raise RuntimeError(f"{record.get('stage_id')} 已成功但缺少 output_refs")
    return resolve_run_local_path(state.run_dir, refs[0])


def _reused_outputs(state, record: dict) -> tuple[Path, Path] | None:
    if record.get("status") not in {
        StageStatus.SUCCEEDED.value,
        StageStatus.SUCCEEDED_WITH_WARNINGS.value,
    }:
        return None
    refs = record.get("output_refs") or []
    if len(refs) < 2:
        raise RuntimeError(f"{record.get('stage_id')} 已成功但缺少 output_refs")
    return (
        resolve_run_local_path(state.run_dir, refs[0]),
        resolve_run_local_path(state.run_dir, refs[1]),
    )


def _abort(
    state,
    stage_id: str,
    status: StageStatus,
    category: StageErrorCategory,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> None:
    error = StageError(
        category=category,
        code=code,
        message=message,
        retryable=retryable,
    )
    finish_pipeline_stage(state, stage_id, status, error=error)
    raise AutoPipelineStageError(stage_id, status.value, error)
