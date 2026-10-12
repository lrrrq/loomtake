from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Iterable

from .models import SourceShot
from .planning_core import PLAN_SCHEMA_VERSION, compile_plan_artifact
from .run_contract import sha256_json


STRATEGY_SCHEMA_VERSION = "mplus-creative-strategies/v1"
SELECTION_SCHEMA_VERSION = "mplus-selected-strategy/v1"
STRATEGY_VERSION = 1

_FOCUS_MARKERS = {"重点", "核心", "high", "focus", "primary"}


class CreativeStrategyError(ValueError):
    """A stable rejection that callers can map without parsing Chinese text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def build_creative_strategies(
    brief: dict,
    template_slots: list[SourceShot],
    *,
    source_view: str = "ai-spec",
    strategy_version: int = STRATEGY_VERSION,
) -> dict:
    """Build deterministic, evidence-bound directions without calling a model.

    The returned directions are planning alternatives, not script drafts.  A
    direction only changes the order/emphasis controls accepted by P00 and is
    independently recompiled through the P00 audit before it is selectable.
    """
    if strategy_version != STRATEGY_VERSION:
        raise CreativeStrategyError(
            "STRATEGY_VERSION_UNSUPPORTED",
            f"仅支持策略版本 {STRATEGY_VERSION}",
        )
    baseline = compile_plan_artifact(
        deepcopy(brief),
        list(template_slots),
        source_view=source_view,
    )
    baseline_payload = baseline.to_payload()
    plan_sha256 = sha256_json(baseline_payload)
    events = baseline.events
    direction_specs = [
        {
            "strategy_id": "linear_fact_order",
            "name": "线性事实序",
            "narrative_principle": "严格按 Brief 已确认的事件顺序推进，不做非线性重排。",
            "event_indexes": list(range(len(events))),
            "emphasis_indexes": [],
            "non_linear_reason": "",
            "support_evidence": _event_evidence(events),
            "control_values": {"narrative_mode": "linear_fact_order"},
        }
    ]
    unsupported = []

    focus = _focus_direction(brief, events)
    if focus is None:
        unsupported.append({
            "strategy_id": "focus_first",
            "reason_code": "focus_evidence_missing",
            "message": "Brief 未明确重点事件、重点证据或前置理由，不能生成重点前置方向。",
        })
    else:
        direction_specs.append(focus)

    emotional = _emotional_direction(brief, events)
    if emotional is None:
        unsupported.append({
            "strategy_id": "emotional_progression",
            "reason_code": "emotion_evidence_missing",
            "message": "Brief 未为全部事件提供情绪强度与证据，不能生成情绪递进方向。",
        })
    else:
        direction_specs.append(emotional)

    directions = [
        _compile_direction(
            brief=brief,
            template_slots=template_slots,
            source_view=source_view,
            baseline=baseline,
            plan_sha256=plan_sha256,
            spec=spec,
        )
        for spec in direction_specs[:3]
    ]
    comparison = _comparison_matrix(directions)
    selectable_count = sum(bool(item["selectable"]) for item in directions)
    readiness_reasons = []
    if selectable_count < 2:
        readiness_reasons.append({
            "reason_code": "comparable_directions_insufficient",
            "status": "clarification",
            "message": "至少需要两个有证据且通过 P00 审计的方向，才能进入策略比较与选择。",
        })
    if baseline.audit["status"] == "blocked":
        status = "blocked"
    elif baseline.audit["status"] == "needs_clarification" or readiness_reasons:
        status = "needs_clarification"
    else:
        status = "pass"
    payload = {
        "schema_version": STRATEGY_SCHEMA_VERSION,
        "strategy_version": STRATEGY_VERSION,
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "plan_sha256": plan_sha256,
        "input_sha256": sha256_json({
            "brief": brief,
            "template_slots": [asdict(item) for item in template_slots],
            "source_view": source_view,
            "strategy_version": STRATEGY_VERSION,
        }),
        "status": status,
        "coverage": {
            "generated_direction_count": len(directions),
            "selectable_direction_count": selectable_count,
            "comparison_ready": selectable_count >= 2,
        },
        "baseline": {
            "audit_status": baseline.audit["status"],
            "blocking_codes": list(baseline.audit["blocking_codes"]),
            "clarification_codes": list(baseline.audit["clarification_codes"]),
            "event_count": len(events),
            "planned_shot_count": len(baseline.shots),
            "target_duration_seconds": baseline.duration_budget["target_seconds"],
            "brief_facts_sha256": sha256_json(baseline.facts),
        },
        "directions": directions,
        "unsupported_directions": unsupported,
        "readiness_reasons": readiness_reasons,
        "comparison_matrix": comparison,
        "estimated_model_calls": 0,
        "prohibited_actions": [
            "generate_script",
            "call_model_provider",
            "publish_version",
            "write_current",
        ],
    }
    payload["portfolio_sha256"] = _portfolio_sha256(payload)
    return payload


def select_creative_strategy(
    brief: dict,
    template_slots: list[SourceShot],
    *,
    portfolio: dict,
    plan_sha256: str,
    strategy_id: str,
    expected_strategy_version: int,
    source_view: str = "ai-spec",
) -> dict:
    """Select one direction and return only controlled P00 inputs plus binding.

    The current inputs are recompiled rather than trusting the supplied
    portfolio.  Parameter drift, artifact tampering and failed P00 audits are
    therefore rejected before planning controls leave this service.
    """
    _validate_portfolio_integrity(portfolio)
    if expected_strategy_version != STRATEGY_VERSION:
        raise CreativeStrategyError(
            "STRATEGY_VERSION_CONFLICT",
            "expected_strategy_version 与当前策略版本不一致",
        )
    current = build_creative_strategies(
        brief,
        template_slots,
        source_view=source_view,
        strategy_version=expected_strategy_version,
    )
    if plan_sha256 != current["plan_sha256"] or plan_sha256 != portfolio.get("plan_sha256"):
        raise CreativeStrategyError(
            "PLAN_SHA256_MISMATCH",
            "Brief、模板槽位或基准规划已变化，请重新生成策略方向",
        )
    if portfolio.get("portfolio_sha256") != current["portfolio_sha256"]:
        raise CreativeStrategyError(
            "STRATEGY_ARTIFACT_DRIFT",
            "策略产物与当前输入或策略实现不一致，请重新生成",
        )
    if not current["coverage"]["comparison_ready"]:
        raise CreativeStrategyError(
            "STRATEGY_COMPARISON_NOT_READY",
            "可比较且可选的策略不足两个，请先补充重点或情绪证据",
        )
    direction = next(
        (item for item in current["directions"] if item["strategy_id"] == strategy_id),
        None,
    )
    if direction is None:
        raise CreativeStrategyError(
            "STRATEGY_NOT_FOUND",
            f"未知或无证据支持的策略: {strategy_id}",
        )
    if not direction["selectable"]:
        raise CreativeStrategyError(
            "STRATEGY_NOT_SELECTABLE",
            f"策略 {strategy_id} 尚未通过事实与规划审计",
        )

    controlled_events = deepcopy(direction["planning_events"])
    controlled_constraints = deepcopy(direction["constraints"])
    candidate_brief = deepcopy(brief)
    candidate_brief["planning_events"] = controlled_events
    recompiled = compile_plan_artifact(
        candidate_brief,
        list(template_slots),
        source_view=source_view,
        constraints=controlled_constraints,
    )
    if recompiled.audit["status"] != "pass":
        raise CreativeStrategyError(
            "RECOMPILED_PLAN_REJECTED",
            "所选策略重新编译后未通过 P00 事实、事件、时长或来源审计",
        )
    _ensure_same_facts_and_events(current, recompiled)
    recompiled_plan_sha256 = sha256_json(recompiled.to_payload())
    result = {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "binding": {
            "plan_sha256": plan_sha256,
            "strategy_id": strategy_id,
            "strategy_version": STRATEGY_VERSION,
            "portfolio_sha256": current["portfolio_sha256"],
            "recompiled_plan_sha256": recompiled_plan_sha256,
        },
        "planning_events": controlled_events,
        "constraints": controlled_constraints,
        "recompile_audit": {
            "status": recompiled.audit["status"],
            "blocking_codes": list(recompiled.audit["blocking_codes"]),
            "clarification_codes": list(recompiled.audit["clarification_codes"]),
            "planned_shot_count": len(recompiled.shots),
            "target_duration_seconds": recompiled.duration_budget["target_seconds"],
        },
        "provider_calls": 0,
        "prohibited_actions": ["generate_script", "publish_version", "write_current"],
    }
    result["selection_sha256"] = sha256_json(result)
    return result


def _compile_direction(
    *,
    brief: dict,
    template_slots: list[SourceShot],
    source_view: str,
    baseline,
    plan_sha256: str,
    spec: dict,
) -> dict:
    ordered_events = [baseline.events[index] for index in spec["event_indexes"]]
    changed_order = spec["event_indexes"] != list(range(len(baseline.events)))
    controls = {
        "planned_shot_count": len(baseline.shots),
        "selected_strategy_id": spec["strategy_id"],
        "selected_strategy_version": STRATEGY_VERSION,
        "baseline_plan_sha256": plan_sha256,
        **deepcopy(spec["control_values"]),
    }
    planning_events = _controlled_events(
        ordered_events,
        baseline.events,
        non_linear_reason=spec["non_linear_reason"] if changed_order else "",
    )
    candidate_brief = deepcopy(brief)
    candidate_brief["planning_events"] = planning_events
    compiled = compile_plan_artifact(
        candidate_brief,
        list(template_slots),
        source_view=source_view,
        constraints=controls,
    )
    audit_status = compiled.audit["status"]
    risks = [
        {
            "code": finding["code"],
            "status": finding["severity"],
            "message": finding["message"],
        }
        for finding in compiled.audit["findings"]
    ]
    if changed_order:
        risks.insert(0, {
            "code": "non_linear_reorder",
            "status": "controlled",
            "message": spec["non_linear_reason"],
        })
    emphasis_ids = [baseline.events[index]["event_id"] for index in spec["emphasis_indexes"]]
    will_change = []
    if changed_order:
        will_change.append("event_order")
    if emphasis_ids:
        will_change.append("event_emphasis")
    if not will_change:
        will_change.append("narrative_principle_only")
    return {
        "strategy_id": spec["strategy_id"],
        "strategy_version": STRATEGY_VERSION,
        "name": spec["name"],
        "status": audit_status,
        "selectable": audit_status == "pass",
        "narrative_principle": spec["narrative_principle"],
        "event_order": [
            {
                "position": position,
                "baseline_event_id": event["event_id"],
                "title": event["title"],
            }
            for position, event in enumerate(ordered_events, start=1)
        ],
        "emphasis": [
            {"baseline_event_id": event_id, "title": baseline.events[index]["title"]}
            for event_id, index in zip(emphasis_ids, spec["emphasis_indexes"])
        ],
        "planned_shot_count": len(compiled.shots),
        "duration_budget": deepcopy(compiled.duration_budget),
        "brief_evidence": deepcopy(spec["support_evidence"]),
        "non_linear_reason": spec["non_linear_reason"] if changed_order else "",
        "risks_and_confirmations": risks,
        "baseline_diff": {
            "event_order_changed": changed_order,
            "baseline_event_order": [event["event_id"] for event in baseline.events],
            "proposed_event_order": [event["event_id"] for event in ordered_events],
            "emphasis_event_ids": emphasis_ids,
            "will_change": will_change,
            "will_not_change": [
                "confirmed_brief_facts",
                "event_set",
                "planned_shot_count",
                "target_duration",
                "brief_evidence",
                "functional_source_triples",
                "publication_state",
            ],
        },
        "planning_events": planning_events,
        "constraints": controls,
        "recompile_audit": {
            "status": audit_status,
            "blocking_codes": list(compiled.audit["blocking_codes"]),
            "clarification_codes": list(compiled.audit["clarification_codes"]),
            "warning_codes": list(compiled.audit["warning_codes"]),
        },
        "estimated_model_calls": 0,
    }


def _focus_direction(brief: dict, events: list[dict]) -> dict | None:
    raw = brief.get("planning_events")
    if not isinstance(raw, list) or len(raw) != len(events):
        return None
    declared_focus = str(brief.get("叙事重点") or "").strip()
    candidates = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        emphasis = item.get("emphasis")
        priority = item.get("priority")
        supported = (
            emphasis is True
            or str(emphasis or "").strip().lower() in _FOCUS_MARKERS
            or str(priority or "").strip().lower() in _FOCUS_MARKERS
            or declared_focus == events[index]["title"]
        )
        evidence = str(item.get("emphasis_evidence") or item.get("evidence") or "").strip()
        if supported and evidence:
            candidates.append((index, evidence))
    if not candidates:
        return None
    focus_index, evidence = candidates[0]
    reason = str(
        brief.get("重点前置理由")
        or raw[focus_index].get("emphasis_reason")
        or ""
    ).strip()
    if focus_index > 0 and not reason:
        return None
    order = [focus_index] + [index for index in range(len(events)) if index != focus_index]
    return {
        "strategy_id": "focus_first",
        "name": "重点前置",
        "narrative_principle": "将 Brief 明确标记的核心事件前置，其他事实保持原相对顺序。",
        "event_indexes": order,
        "emphasis_indexes": [focus_index],
        "non_linear_reason": reason,
        "support_evidence": [{
            "baseline_event_id": events[focus_index]["event_id"],
            "type": events[focus_index]["brief_evidence"]["type"],
            "text": evidence,
            "source_ref": deepcopy(events[focus_index]["brief_evidence"]["source_ref"]),
        }],
        "control_values": {
            "narrative_mode": "focus_first",
            "emphasis_event_titles": [events[focus_index]["title"]],
        },
    }


def _emotional_direction(brief: dict, events: list[dict]) -> dict | None:
    raw = brief.get("planning_events")
    if not isinstance(raw, list) or len(raw) != len(events) or not raw:
        return None
    intensities = []
    evidence = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return None
        value = item.get("emotional_intensity")
        if isinstance(value, bool):
            return None
        try:
            intensity = int(value)
        except (TypeError, ValueError):
            return None
        if intensity < 1 or intensity > 5 or str(value).strip() != str(intensity):
            return None
        item_evidence = str(item.get("emotion_evidence") or item.get("evidence") or "").strip()
        if not item_evidence:
            return None
        intensities.append(intensity)
        evidence.append({
            "baseline_event_id": events[index]["event_id"],
            "type": events[index]["brief_evidence"]["type"],
            "text": item_evidence,
            "source_ref": deepcopy(events[index]["brief_evidence"]["source_ref"]),
            "emotional_intensity": intensity,
        })
    order = sorted(range(len(events)), key=lambda index: (intensities[index], index))
    changed_order = order != list(range(len(events)))
    reason = str(brief.get("情绪递进理由") or "").strip()
    if changed_order and not reason:
        return None
    return {
        "strategy_id": "emotional_progression",
        "name": "情绪递进",
        "narrative_principle": "仅依据 Brief 明示的情绪强度由低到高排列事件。",
        "event_indexes": order,
        "emphasis_indexes": [order[-1]],
        "non_linear_reason": reason,
        "support_evidence": evidence,
        "control_values": {
            "narrative_mode": "emotional_progression",
            "emotional_intensity_by_title": {
                events[index]["title"]: intensities[index]
                for index in range(len(events))
            },
        },
    }


def _controlled_events(
    ordered_events: list[dict],
    baseline_events: list[dict],
    *,
    non_linear_reason: str,
) -> list[dict]:
    original_order = {
        event["event_id"]: index
        for index, event in enumerate(baseline_events, start=1)
    }
    changed_order = [event["event_id"] for event in ordered_events] != [
        event["event_id"] for event in baseline_events
    ]
    result = []
    for event in ordered_events:
        controlled = {
            "title": event["title"],
            "evidence": event["brief_evidence"]["text"],
            "fact_status": event["fact_status"],
            "order": original_order[event["event_id"]],
            "shot_units": int(event.get("shot_units") or 1),
            "allow_non_linear": changed_order,
            "non_linear_reason": non_linear_reason if changed_order else "",
        }
        if "duration_seconds" in event:
            controlled["duration_seconds"] = int(event["duration_seconds"])
        result.append(controlled)
    return result


def _event_evidence(events: Iterable[dict]) -> list[dict]:
    return [
        {
            "baseline_event_id": event["event_id"],
            "type": event["brief_evidence"]["type"],
            "text": event["brief_evidence"]["text"],
            "source_ref": deepcopy(event["brief_evidence"]["source_ref"]),
        }
        for event in events
    ]


def _comparison_matrix(directions: list[dict]) -> dict:
    rows = []
    for direction in directions:
        rows.append({
            "strategy_id": direction["strategy_id"],
            "name": direction["name"],
            "selectable": direction["selectable"],
            "event_sequence": [item["title"] for item in direction["event_order"]],
            "emphasis": [item["title"] for item in direction["emphasis"]],
            "planned_shot_count": direction["planned_shot_count"],
            "target_duration_seconds": direction["duration_budget"]["target_seconds"],
            "will_change": list(direction["baseline_diff"]["will_change"]),
            "will_not_change": list(direction["baseline_diff"]["will_not_change"]),
        })
    return {
        "columns": [
            "event_sequence",
            "emphasis",
            "planned_shot_count",
            "target_duration_seconds",
            "will_change",
            "will_not_change",
        ],
        "rows": rows,
        "selection_notice": "选择只改变受控事件顺序/强调；事实、事件集合、N、时长、来源和发布状态不变。",
    }


def _validate_portfolio_integrity(portfolio: dict) -> None:
    if portfolio.get("schema_version") != STRATEGY_SCHEMA_VERSION:
        raise CreativeStrategyError("STRATEGY_ARTIFACT_INVALID", "策略产物版本不受支持")
    if portfolio.get("strategy_version") != STRATEGY_VERSION:
        raise CreativeStrategyError("STRATEGY_VERSION_CONFLICT", "策略产物版本已漂移")
    if portfolio.get("portfolio_sha256") != _portfolio_sha256(portfolio):
        raise CreativeStrategyError("STRATEGY_ARTIFACT_TAMPERED", "策略产物内容已被篡改")


def _portfolio_sha256(payload: dict) -> str:
    canonical = deepcopy(payload)
    canonical.pop("portfolio_sha256", None)
    return sha256_json(canonical)


def _ensure_same_facts_and_events(current: dict, recompiled) -> None:
    expected_event_titles = sorted(
        item["title"]
        for item in current["directions"][0]["event_order"]
    )
    actual_event_titles = sorted(event["title"] for event in recompiled.events)
    if expected_event_titles != actual_event_titles:
        raise CreativeStrategyError(
            "STRATEGY_EVENT_SET_DRIFT",
            "所选策略改变了 Brief 事件集合",
        )
    if sha256_json(recompiled.facts) != current["baseline"]["brief_facts_sha256"]:
        raise CreativeStrategyError(
            "STRATEGY_FACT_SET_DRIFT",
            "所选策略改变了 P00 Brief 事实合同",
        )
