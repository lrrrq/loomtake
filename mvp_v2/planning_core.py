from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import re

from .models import SourceShot, parse_positive_seconds_strict
from .script_type_adapter import category_id_for_script_type
from .template_rules import builtin_template_rules, validate_template_rules


PLAN_SCHEMA_VERSION = "mplus-plan-artifact/v4"
_LAUNCH_60S_DURATIONS = (3, 3, 3, 4, 3, 3, 4, 3, 4, 3, 4, 4, 3, 3, 3, 4, 3, 3)
_MAX_SHOT_DURATION_SECONDS = 8
_UNKNOWN_MARKERS = ("待确认", "待定", "未知", "TBD", "TBC", "?")
_SOURCE_MECHANISMS = (
    "剪彩", "艺人致辞", "明星", "高管致辞", "代言人",
    "互动装置", "应援机制", "启动仪式",
)
_SEMANTIC_ANCHOR_TERMS = (
    "ai", "kv", "蝴蝶", "云层", "空镜", "转场", "隔板", "门店", "礼物", "礼品",
    "发言", "致辞", "代表", "启动", "亮灯", "颁奖", "茶歇", "表演",
    "合影", "到场", "互动", "领取", "打卡", "落版", "举杯", "机器狗",
)
_FACT_FIELDS = (
    ("project_name", "项目名称", True),
    ("script_type", "脚本类型", True),
    ("client", "品牌或客户", False),
    ("category", "行业或活动类别", False),
    ("duration", "视频时长", True),
    ("tone", "风格调性", False),
    ("video_goal", "视频目标", False),
    ("usage_scenario", "使用场景", False),
    ("required_content", "必须出现的内容", False),
    ("forbidden_content", "禁忌或不能出现的内容", False),
    ("delivery_requirements", "交付要求", False),
)


@dataclass(frozen=True)
class PlanningProjection:
    template_slots: list[SourceShot]
    facts: list[dict]
    requirements: list[dict]
    events: list[dict]
    event_graph: dict
    duration_budget: dict
    shots: list[dict]
    audit: dict
    source_status: dict
    coverage: list[dict]
    capacity_report: dict
    traceability: list[dict]
    template_rule_contract: dict
    conflicts: list[dict]
    decision_counts: dict
    template_recommendation_summary: dict

    @property
    def plan_schema_version(self) -> str:
        return PLAN_SCHEMA_VERSION

    def to_payload(self) -> dict:
        return {
            "plan_schema_version": PLAN_SCHEMA_VERSION,
            "brief_facts": self.facts,
            "requirements": self.requirements,
            "events": self.events,
            "event_graph": self.event_graph,
            "duration_budget": self.duration_budget,
            "shot_plans": self.shots,
            "coverage": self.coverage,
            "capacity_report": self.capacity_report,
            "traceability": self.traceability,
            "template_rule_contract": self.template_rule_contract,
            "conflicts": self.conflicts,
            "decision_counts": self.decision_counts,
            "template_recommendation_summary": self.template_recommendation_summary,
            "planning_audit": self.audit,
            "planning_source_status": self.source_status,
        }


def compile_plan_artifact(
    brief: dict,
    template_slots: list[SourceShot],
    *,
    source_view: str,
    constraints: dict | None = None,
) -> PlanningProjection:
    """Compile deterministic Brief facts and a content-led N-shot projection.

    This function intentionally does not call a model.  ``planning_events`` is
    an optional adapter input for a future controlled Planner; otherwise the
    current Brief required-content field is the deterministic event source.
    """
    constraints = constraints or {}
    rule_contract = _resolve_template_rule_contract(brief, constraints)
    facts = _brief_facts(brief)
    events, _ = _events_from_brief(brief)
    original_events = events
    if constraints.get("creative_draft_binding"):
        from .creative_formal_binding import creative_events
        events = creative_events(constraints["creative_draft_binding"])
    requirements = _requirements_from_brief(brief, events, constraints)
    original_event_requirements = {}
    if constraints.get("creative_draft_binding"):
        # Confirmed Brief events remain requirements even when accepted creative
        # blocks supply the narrative order. Let the same requirement matcher
        # place them; no separate creative matching algorithm is introduced.
        required_texts = {item["text"] for item in requirements if item.get("mandatory")}
        for event in original_events:
            if event["title"] in required_texts:
                original_event_requirements[event['event_id']] = next(r['requirement_id'] for r in requirements if r.get('mandatory') and r['text'] == event['title'])
                continue
            original_event_requirements[event['event_id']] = 'brief_flow_' + event['event_id']
            requirements.append({"requirement_id": "brief_flow_" + event["event_id"],
                "original_text": event["title"], "text": event["title"],
                "kind": "mandatory_required", "classification": "mandatory_content",
                "mandatory": True, "fact_status": event["fact_status"],
                "position_rule": {"type": "none", "anchor_event_id": ""},
                "origin": "brief", "evidence_refs": event["evidence_refs"]})
    semantic_bindings = _apply_planning_semantics(
        brief, events, requirements, rule_contract, constraints.get('planning_semantics'))
    planning_items, requirement_bindings = _planning_items(events, requirements, semantic_bindings)
    requested_count, count_issue = _requested_shot_count(constraints)
    if requested_count is None and not count_issue:
        requested_count, count_issue = _brief_requested_shot_count(brief)
    target_duration = parse_positive_seconds_strict(brief.get("视频时长", "")) or 0
    image_context = _project_image_context(constraints)
    default_structural_count = _structural_slot_count(rule_contract)
    minimum_structural_count = len(_structural_rules(rule_contract))
    content_shot_count = (
        max(
            sum(int(item.get("shot_units") or 1) for item in planning_items),
            _ceil_div(target_duration, _MAX_SHOT_DURATION_SECONDS),
        )
        if planning_items else len(template_slots)
    )
    if requested_count is None:
        default_quantity = (
            min(default_structural_count, len(image_context))
            if image_context else default_structural_count
        )
        shot_count = max(
            content_shot_count,
            default_quantity if target_duration >= default_quantity
            else minimum_structural_count,
        )
    else:
        shot_count = max(requested_count, minimum_structural_count)
    if planning_items and shot_count <= 0:
        shot_count = 1

    event_groups, conflicts, structural_slot_rules = _assign_events_to_hard_slots(
        planning_items,
        shot_count,
        rule_contract,
        constraints.get("template_conflict_resolutions") or {},
        preserve_total=requested_count is not None,
    )
    shot_count = len(event_groups)
    historical_resolutions = [
        dict(item) for item in (
            (constraints.get("conflict_resolution_binding") or {}).get("resolutions") or []
        )
        if isinstance(item, dict) and item.get("choice") in {"brief_wins", "template_wins"}
    ]
    known_conflict_ids = {str(item.get("conflict_id") or "") for item in conflicts}
    conflicts.extend({
        **item,
        "type": str(item.get("type") or "brief_hard_template_conflict"),
        "status": "resolved",
    } for item in historical_resolutions if str(item.get("conflict_id") or "") not in known_conflict_ids)
    projected_slots = _project_slots(template_slots, event_groups)
    template_recommendation_summary = _template_recommendation_summary(
        template_slots, projected_slots,
    )
    durations, duration_mode = _shot_durations(brief, event_groups, events)
    creative_standard = _creative_standard(constraints)
    shots = _shot_plans(
        brief,
        projected_slots,
        event_groups,
        durations,
        creative_standard=creative_standard,
        image_context=image_context,
        rule_contract=rule_contract,
        conflicts=conflicts,
        structural_slot_rules=structural_slot_rules,
    )
    if constraints.get("creative_draft_binding"):
        for requirement in requirements:
            bound = requirement_bindings.get(requirement["requirement_id"], "")
            if not any(event.get("creative_block_id") and event["event_id"] == bound for event in events):
                continue
            rule = requirement.get("position_rule") or {}
            relation = rule.get("type", "none")
            actual = [int(shot["shot_no"]) for shot in shots if bound in shot["event_ids"]]
            expected = [int(number) for number in _anchored_requirement_shots(requirement, shots)]
            mismatch = bool(actual and relation in {"fixed_shot", "opening", "closing"}
                            and actual != expected)
            if actual and relation in {"before", "after"}:
                anchor = [int(shot["shot_no"]) for shot in shots
                          if rule.get("anchor_event_id") in shot["event_ids"]]
                mismatch = not anchor or (max(actual) >= min(anchor) if relation == "before"
                                           else min(actual) <= max(anchor))
            if mismatch:
                conflicts.append({"conflict_id": "creative_position_" + requirement["requirement_id"],
                    "type": "creative_brief_flow_conflict", "status": "unresolved",
                    "requirement_id": requirement["requirement_id"], "shot_no": str(actual[0]),
                    "evidence_refs": requirement.get("evidence_refs") or [],
                    "message": f"选定初稿顺序与已确认位置要求冲突：{requirement['text']}。请修改初稿或通过资料确认入口调整要求。"})
    if (original_event_requirements
            and any(brief.get(field) for field in ("planning_events", "活动流程", "事件流程", "流程事件"))
            and not all(e.get('allow_non_linear') and e.get('non_linear_reason') for e in original_events)):
        ordered = sorted(original_events, key=lambda e: e.get('declared_order') or e['order'])
        positions = {}
        for event in ordered:
            requirement_id = original_event_requirements[event['event_id']]
            bound = requirement_bindings.get(requirement_id, requirement_id)
            positions[event['event_id']] = [int(s['shot_no']) for s in shots if bound in s['event_ids']]
        for before, after in zip(ordered, ordered[1:]):
            left, right = positions[before['event_id']], positions[after['event_id']]
            if left and right and min(left) > min(right):
                conflicts.append({'conflict_id': 'creative_flow_' + before['event_id'] + '_' + after['event_id'],
                    'type': 'creative_brief_flow_conflict', 'status': 'unresolved',
                    'requirement_id': original_event_requirements[after['event_id']],
                    'shot_no': str(min(right)),
                    'evidence_refs': before['evidence_refs'] + after['evidence_refs'],
                    'message': f"选定初稿顺序与已确认流程冲突：{before['title']}应在{after['title']}之前。请修改初稿或通过资料确认入口调整流程。"})
    conflicted_event_ids = {
        str(item.get("event_id") or "") for item in conflicts
        if item.get("status") == "unresolved"
    }
    conflicted_requirement_ids = {
        str(item.get("requirement_id") or "") for item in conflicts
        if item.get("status") == "unresolved"
    } | {
        requirement_id
        for requirement_id, bound_id in requirement_bindings.items()
        if bound_id in conflicted_event_ids
    }
    coverage = _bind_requirement_coverage(
        requirements,
        shots,
        requirement_bindings=requirement_bindings,
        conflicted_requirement_ids=conflicted_requirement_ids,
        overridden_requirement_ids={
            str(item.get("requirement_id") or "") for item in conflicts
            if item.get("choice") == "template_wins"
        },
    )
    requirement_ids = {str(item.get("requirement_id") or "") for item in requirements}
    for resolution in historical_resolutions:
        requirement_id = str(resolution.get("requirement_id") or "")
        if (
            resolution.get("choice") != "template_wins"
            or not requirement_id
            or requirement_id in requirement_ids
        ):
            continue
        requirement = {
            "requirement_id": requirement_id,
            "original_text": str(
                resolution.get("original_text") or resolution.get("brief_value") or ""
            ),
            "text": str(
                resolution.get("original_text") or resolution.get("brief_value") or ""
            ),
            "kind": "position_constraint",
            "classification": "position_constraint",
            "mandatory": True,
            "fact_status": "confirmed",
            "position_rule": {"type": "none", "anchor_event_id": ""},
            "origin": "confirmed_resolution",
            "decision_source": "template_wins",
            "evidence_refs": list(resolution.get("evidence_refs") or []),
        }
        requirements.append(requirement)
        coverage.append({
            "requirement_id": requirement_id,
            "kind": "position_constraint",
            "status": "overridden_by_resolution",
            "shot_nos": [],
            "evidence_refs": list(resolution.get("evidence_refs") or []),
        })
        requirement_ids.add(requirement_id)
    capacity_report = _capacity_report(
        target_duration=target_duration,
        requested_count=requested_count,
        planning_items=planning_items,
        shot_count=len(shots),
        structural_slot_count=shot_count,
        structural_minimum_count=minimum_structural_count,
    )
    capacity_report.update({
        "available_project_images": len(image_context),
        "hard_slot_quantity_mode": (
            "explicit_shot_count"
            if requested_count is not None else
            "project_image_capacity"
            if image_context and len(image_context) < default_structural_count else
            "template_default"
        ),
    })
    if any(duration <= 0 for duration in durations):
        capacity_report.update({
            "status": "needs_clarification",
            "choices": ["增加目标时长", "调整明确镜头时长", "由用户确认压缩结构弧"],
        })
    traceability = _traceability(requirements, coverage, shots)
    decision_counts = _decision_counts(coverage, shots, conflicts)
    event_graph = {
        "graph_type": "ordered_event_flow",
        "nodes": [
            *[event["event_id"] for event in events],
            *[
                item["requirement_id"] for item in requirements
                if item["position_rule"]["type"] != "none"
            ],
        ],
        "edges": [
            {
                "from_event_id": events[index]["event_id"],
                "to_event_id": events[index + 1]["event_id"],
                "relation": "before",
                "reason": "选定初稿顺序" if original_event_requirements else "Brief事件顺序",
            }
            for index in range(max(0, len(events) - 1))
        ] + [
            edge for requirement in requirements
            for edge in _position_edges(requirement, events)
        ],
    }
    if original_event_requirements:
        event_graph['confirmed_brief_events'] = original_events
        event_graph['brief_event_requirement_bindings'] = original_event_requirements
    explicit_event_seconds = (
        sum(int(event["duration_seconds"]) for event in events)
        if duration_mode == "explicit_event_budget"
        else None
    )
    duration_budget = {
        "target_seconds": target_duration,
        "planned_seconds": sum(durations),
        "explicit_event_seconds": explicit_event_seconds,
        "max_shot_seconds": _MAX_SHOT_DURATION_SECONDS,
        "allocation_mode": duration_mode,
        "shot_count": len(shots),
        "event_count": len(events),
        "required_shot_units": capacity_report["required_shot_units"],
    }
    source_status = {
        "source_view": source_view,
        "ai_spec_ready": source_view == "ai-spec",
        "status": "ready" if source_view == "ai-spec" else "legacy_human_compatibility",
    }
    audit = _audit_plan(
        brief=brief,
        facts=facts,
        events=events,
        shots=shots,
        coverage=coverage,
        projected_slots=projected_slots,
        duration_budget=duration_budget,
        source_status=source_status,
        count_issue=count_issue,
        requirements=requirements,
        capacity_report=capacity_report,
        conflicts=conflicts,
        decision_counts=decision_counts,
        rule_contract=rule_contract,
    )
    _apply_creative_standard_audit(
        audit, creative_standard, shots, requirements, coverage,
    )
    _normalize_audit_findings(audit)
    return PlanningProjection(
        template_slots=projected_slots,
        facts=facts,
        requirements=requirements,
        events=events,
        event_graph=event_graph,
        duration_budget=duration_budget,
        shots=shots,
        audit=audit,
        source_status=source_status,
        coverage=coverage,
        capacity_report=capacity_report,
        traceability=traceability,
        template_rule_contract=rule_contract,
        conflicts=conflicts,
        decision_counts=decision_counts,
        template_recommendation_summary=template_recommendation_summary,
    )


def _template_recommendation_summary(
    available_slots: list[SourceShot], selected_slots: list[SourceShot],
) -> dict:
    """Expose the whole safe template library separately from N-shot projection."""
    keys = [
        (slot.source_file, slot.source_sheet, int(slot.source_row))
        for slot in available_slots
    ]
    adopted: dict[tuple[str, str, int], list[str]] = {key: [] for key in keys}
    for shot_index, slot in enumerate(selected_slots, start=1):
        key = (slot.source_file, slot.source_sheet, int(slot.source_row))
        adopted.setdefault(key, []).append(str(shot_index))
    records = []
    for index, slot in enumerate(available_slots, start=1):
        key = (slot.source_file, slot.source_sheet, int(slot.source_row))
        shot_nos = adopted.get(key, [])
        records.append({
            "recommendation_key": f"template-{index:03d}",
            "source": {
                "file": Path(slot.source_file).name,
                "sheet": slot.source_sheet,
                "row": int(slot.source_row),
            },
            "shot_function": slot.shot_function or slot.structure or "模板镜头功能",
            "adopted_shot_nos": shot_nos,
            "adoption_count": len(shot_nos),
        })
    selected_count = sum(bool(item["adoption_count"]) for item in records)
    return {
        "available_count": len(records),
        "selected_distinct_count": selected_count,
        "reuse_count": max(0, len(selected_slots) - selected_count),
        "records": records,
    }


def _brief_facts(brief: dict) -> list[dict]:
    source_ref = _brief_source_ref(brief)
    evidence_type = "file_brief" if source_ref else "inline_brief"
    facts = []
    for fact_id, field_name, required in _FACT_FIELDS:
        value = str(brief.get(field_name) or "").strip()
        facts.append({
            "fact_id": fact_id,
            "field": field_name,
            "value": value,
            "required": required,
            "status": "confirmed" if value else "missing",
            "evidence_type": evidence_type,
            "source_ref": source_ref,
        })
    return facts


def _events_from_brief(brief: dict) -> tuple[list[dict], bool]:
    raw_events = brief.get("planning_events")
    explicit = isinstance(raw_events, list) and bool(raw_events)
    if explicit:
        candidates = raw_events
    else:
        flow = next(
            (
                brief.get(key)
                for key in ("活动流程", "事件流程", "流程事件")
                if brief.get(key)
            ),
            "",
        )
        candidates = (_split_event_text(flow) if flow else [
            event for clause in brief_content_clauses(brief)
            if clause["classification"] == "mandatory_content"
            for event in _split_event_text(clause["text"])])
    source_ref = _brief_source_ref(brief)
    default_evidence = str(
        brief.get("Brief来源证据")
        or brief.get("必须出现的内容")
        or ""
    ).strip()
    events = []
    for index, item in enumerate(candidates, start=1):
        if isinstance(item, dict):
            title = _clean_event_title(item.get("title") or item.get("event") or "")
            evidence = str(item.get("evidence") or default_evidence).strip()
            business_source = item.get("source")
            source_fact_id = str(item.get("source_fact_id") or "").strip()
            requested_duration = _positive_int(item.get("duration_seconds"))
            technique_overrides = {
                key: item[key]
                for key in ("effect", "camera_type", "shot_size", "camera_move", "sound")
                if str(item.get(key) or "").strip()
            }
            shot_units = _positive_int(item.get("shot_units")) or 1
            declared_status = str(item.get("fact_status") or "").strip()
            declared_order = _positive_int(item.get("order"))
            allow_non_linear = bool(item.get("allow_non_linear"))
            non_linear_reason = str(item.get("non_linear_reason") or "").strip()
        else:
            title = _clean_event_title(item)
            evidence = default_evidence or str(item).strip()
            requested_duration = None
            shot_units = _event_shot_units(title)
            declared_status = ""
            declared_order = None
            allow_non_linear = False
            non_linear_reason = ""
            business_source = None
            technique_overrides = {}
            source_fact_id = ""
        if not title:
            continue
        pending = declared_status == "pending_confirmation" or any(
            marker in f"{title} {evidence}" for marker in _UNKNOWN_MARKERS
        )
        brief_evidence = {
            "type": "file_brief" if source_ref else "inline_brief",
            "text": evidence,
            "source_ref": source_ref,
        }
        if isinstance(business_source, dict) and business_source.get("file"):
            brief_evidence["source"] = dict(business_source)
        if source_fact_id:
            brief_evidence["fact_id"] = source_fact_id
        event = {
            "event_id": f"event_{index:03d}",
            "order": index,
            "title": title,
            "text": title,
            "kind": "flow_event",
            "primary": True,
            "required": False,
            "mandatory": False,
            "fact_status": "pending_confirmation" if pending else "confirmed",
            "brief_evidence": brief_evidence,
            "evidence_refs": [brief_evidence],
            "declared_order": declared_order,
            "allow_non_linear": allow_non_linear,
            "non_linear_reason": non_linear_reason,
            "shot_units": shot_units,
            "technique_overrides": technique_overrides,
        }
        if requested_duration is not None:
            event["duration_seconds"] = requested_duration
        events.append(event)
    # Re-number after empty candidates are discarded so graph references remain dense.
    for index, event in enumerate(events, start=1):
        event["event_id"] = f"event_{index:03d}"
        event["order"] = index
    return events, explicit


def _requirements_from_brief(
    brief: dict, events: list[dict], constraints: dict,
) -> list[dict]:
    """Normalize Brief and confirmed review clauses into one v3 contract."""
    source_ref = _brief_source_ref(brief)
    default_ref = {
        "type": "file_brief" if source_ref else "inline_brief",
        "source_ref": source_ref,
    }
    formal_evidence = dict(brief.get("_formal_fact_evidence") or {})
    requirements = []
    fields = (
        ("mandatory_required", "必须出现的内容", "mandatory_required", "mandatory_content", True),
        ("mandatory_forbidden", "禁止或不能出现的内容", "mandatory_forbidden", "forbidden", True),
    )
    for prefix, field, kind, classification, mandatory in fields:
        clauses = [
            (clause, text)
            for clause in brief_content_clauses(brief, field)
            for text in _split_covered_action_clauses(clause["text"], events)
        ]
        for index, (clause, text) in enumerate(clauses, start=1):
            source_text = clause["source_text"]
            clause_class = clause["classification"]
            requirements.append({
                "requirement_id": f"{prefix}_{index:03d}",
                "original_text": source_text,
                "text": text,
                "source_text": source_text,
                "kind": kind,
                "classification": clause_class,
                "mandatory": mandatory,
                "fact_status": "confirmed",
                "position_rule": _position_rule(text, events),
                "origin": "brief",
                "evidence_refs": [{
                    **default_ref,
                    "field": field,
                    "text": source_text,
                    "clause_text": text,
                    **(
                        {"fact_id": formal_evidence[field]["fact_id"]}
                        if isinstance(formal_evidence.get(field), dict)
                        and formal_evidence[field].get("fact_id") else {}
                    ),
                }],
            })
    special_index = 0
    special_semantics = (formal_evidence.get("特殊需求") or {}).get("semantic_metadata", {})
    confirmed_classes = {item["text"]: item["classification"] for item in special_semantics.get("clauses", [])}
    for source_text in _split_special_requirements(brief.get("特殊需求")):
        for text in _split_special_requirement_clauses(source_text):
            special_index += 1
            classification = confirmed_classes.get(text) or _special_classification(text)
            requirements.append({
                "requirement_id": f"special_{special_index:03d}",
                "original_text": text,
                "text": text,
                "source_text": source_text,
                "kind": classification,
                "classification": classification,
                "classification_source": "confirmed_brief" if text in confirmed_classes else "legacy_interpretation",
                "mandatory": True,
                "fact_status": "confirmed",
                "position_rule": _position_rule(text, events),
                "origin": "brief",
                "evidence_refs": [{
                    **default_ref,
                    "field": "特殊需求",
                    "text": source_text,
                    "clause_text": text,
                    **(
                        {"fact_id": formal_evidence["特殊需求"]["fact_id"]}
                        if isinstance(formal_evidence.get("特殊需求"), dict)
                        and formal_evidence["特殊需求"].get("fact_id") else {}
                    ),
                }],
            })
    for index, text in enumerate(_split_special_requirements(brief.get("交付要求")), start=1):
        requirements.append({
            "requirement_id": f"delivery_{index:03d}",
            "original_text": text,
            "text": text,
            "kind": "production_requirement",
            "classification": "production_requirement",
            "mandatory": True,
            "fact_status": "confirmed",
            "position_rule": {"type": "none", "anchor_event_id": ""},
            "origin": "brief",
            "evidence_refs": [{
                **default_ref,
                "field": "交付要求",
                "text": text,
                **(
                    {"fact_id": formal_evidence["交付要求"]["fact_id"]}
                    if isinstance(formal_evidence.get("交付要求"), dict)
                    and formal_evidence["交付要求"].get("fact_id") else {}
                ),
            }],
        })
    for index, text in enumerate(_split_requirement_text(brief.get("风格调性")), start=1):
        requirements.append({
            "requirement_id": f"style_{index:03d}",
            "original_text": text,
            "text": text,
            "kind": "style_preference",
            "classification": "style_preference",
            "mandatory": False,
            "fact_status": "confirmed",
            "position_rule": {"type": "none", "anchor_event_id": ""},
            "origin": "brief",
            "evidence_refs": [{**default_ref, "field": "风格调性", "text": text}],
        })
    for event in events:
        requirements.append({
            "requirement_id": str(event["event_id"]),
            "original_text": str(event["title"]),
            "text": str(event["title"]),
            "kind": "flow_event",
            "classification": "flow_event",
            "mandatory": False,
            "fact_status": str(event.get("fact_status") or "confirmed"),
            "position_rule": {"type": "none", "anchor_event_id": ""},
            "origin": "creative_draft" if event.get("creative_block_id") else "brief",
            "evidence_refs": list(event.get("evidence_refs") or []),
        })
    for status, key, origin in (
        ("pending_confirmation", "review_requirement_proposals", "review_proposed"),
        ("confirmed", "confirmed_review_requirements", "confirmed_review"),
    ):
        for index, raw in enumerate(constraints.get(key) or [], start=1):
            if not isinstance(raw, dict):
                continue
            text = str(raw.get("text") or raw.get("comment") or "").strip()
            if not text:
                continue
            requirement_id = str(raw.get("requirement_id") or f"review_{index:03d}")
            shot_no = str(raw.get("shot_no") or "").strip()
            classification = str(raw.get("classification") or "review_preference")
            requirements.append({
                "requirement_id": requirement_id,
                "original_text": text,
                "text": text,
                "kind": classification,
                "classification": classification,
                "mandatory": status == "confirmed",
                "fact_status": status,
                "position_rule": (
                    {"type": "fixed_shot", "anchor_event_id": "", "shot_no": shot_no}
                    if shot_no.isdigit()
                    else _position_rule(text, events)
                ),
                "origin": origin,
                "decision_source": str(raw.get("decision_source") or origin),
                "evidence_refs": list(raw.get("evidence_refs") or []),
            })
    return requirements


_CONTENT_EXCLUSION = re.compile(
    r"^(?:(?:请|务必)?(?:不要|不得|禁止|不能|不可|避免|不允许)(?:写入|写|展示|出现|呈现|拍摄|包含|使用)?"
    r"|不(?:写入|写|展示|出现|呈现|拍摄|包含|使用))\s*(.+)$"
)


def brief_content_clauses(brief: dict, field: str = "必须出现的内容") -> list[dict]:
    """Interpret content obligations once for planning, extraction and final audit.

    A field label cannot turn a stated exclusion or a declared information
    boundary into a positive event. Preserve the exact clause and its source.
    """
    value = brief.get(field)
    forbidden_field = field in {"禁止或不能出现的内容", "禁忌或不能出现的内容"}
    if not value and forbidden_field:
        value = brief.get("禁忌或不能出现的内容") or brief.get("禁止或不能出现的内容")
    result = []
    clauses = []
    for source in _split_quoted_clauses(value, ",，;；\n"):
        # A list joined with the enumeration comma shares its leading polarity.
        # Splitting it first turns “不写型号、性能” into a demand for performance.
        clauses.extend([source] if forbidden_field or _CONTENT_EXCLUSION.fullmatch(source.rstrip("。！!"))
            else _split_quoted_clauses(source, "、"))
    for text in clauses:
        exclusion = _CONTENT_EXCLUSION.fullmatch(text.rstrip("。！!"))
        classification = ("forbidden" if forbidden_field or exclusion else
            "production_requirement" if _fact_handling_instruction(text) else "mandatory_content")
        result.append({"text": text, "source_text": text, "classification": classification,
            "forbidden_text": exclusion.group(1) if exclusion else text if forbidden_field else ""})
    return result


def _split_special_requirements(value: object) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[;；\n]+", str(value or ""))
        if item.strip() and item.strip() not in {"无", "无特别要求", "暂无"}
    ]


def _split_special_requirement_clauses(value: str) -> list[str]:
    return _split_quoted_clauses(value, "，,。")


def _split_quoted_clauses(value: object, separators: str) -> list[str]:
    """Keep literal copy and parenthesized shot detail intact while splitting prose."""
    pairs = {"“": "”", "‘": "’", '"': '"', "（": "）", "(": ")", "[": "]", "【": "】"}
    stack, current, result = [], [], []
    literal_copy = False
    for character in str(value or ""):
        if stack and character == stack[-1]:
            stack.pop()
        elif character in pairs:
            stack.append(pairs[character])
            literal_copy = False
        if character in ":：" and not stack and re.search(
            r"(?:字幕|文案)(?:内容|文字)?$", "".join(current).strip(),
        ):
            literal_copy = True
        if character in separators and not stack and not (
            literal_copy and character in "、,，"
        ):
            part = "".join(current).strip()
            if part and part not in {"无", "无特别要求", "暂无"}:
                result.append(part)
            current = []
            literal_copy = False
        else:
            current.append(character)
    part = "".join(current).strip()
    if part and part not in {"无", "无特别要求", "暂无"}:
        result.append(part)
    return result


def _is_duration_annotation(text: str) -> bool:
    return bool(re.fullmatch(
        r"(?:(?:每(?:个)?镜(?:头)?(?:时长)?|全片|整片|成片|总(?:时长)?|时长|片长)"
        r"\s*(?:为|是|约|[:：])?\s*)?[+-]?\d+(?:\.\d+)?\s*(?:秒|s|sec|seconds?)",
        str(text).strip(), re.IGNORECASE,
    ))


def _special_classification(text: str) -> str:
    if _is_duration_annotation(text):
        return "production_requirement"
    if re.search(
        r"(?:参考图|分镜图|配图|图片).{0,15}(?:仅说明|仅供参考|只作参考|各自生成|逐镜生成|不复用)"
        r"|(?:先)?(?:核对|检查|复核).{0,15}(?:产品身份|镜头差异|水印)"
        r"|不(?:要)?复用.{0,12}(?:同一|同一张).{0,5}(?:图|画面)",
        text,
    ):
        return "production_requirement"
    if re.search(r"(?<![第\d])\d+\s*(?:个\s*)?镜(?:头)?", text) or re.search(
        r"(?:无|不要|不需|不用|不加|没有)旁白|旁白.{0,6}(?:无|不要)|(?:少|简短|精简)字幕|字幕.{0,6}(?:少|精简)", text,
    ):
        return "production_requirement"
    if _fact_handling_instruction(text):
        return "production_requirement"
    if re.search(r"(?:后期|叠加|屏幕|画面).{0,8}(?:字幕|文字)|字幕.{0,8}(?:后期|叠加)|旁白", text):
        return "production_requirement"
    if re.search(r"声音|环境声|音效|同期声|音乐|配乐|BGM|自然光|光线|灯光(?!秀)", text, re.IGNORECASE):
        return "style_preference"
    if re.search(r"(?:声音|现场声|环境声|音效|同期声)(?:要求)?$", text):
        return "production_requirement"
    if re.search(r"(?:预览|采纳|采用|撤销|恢复|保存|修改|重写|调整).{0,20}(?:初稿|草稿|段落|修改建议|保护范围)", text):
        return "production_requirement"
    if re.search(r"(?:保留|保持).{0,20}(?:段落|[0-9一二三四五六七八九十]+段).{0,4}(?:顺序|原文|保护)", text):
        return "production_requirement"
    if _position_rule(text, [])["type"] != "none":
        return "position_constraint"
    if any(term in text for term in ("画面", "空镜", "配图", "视觉", "AI", "KV", "转场")):
        return "visual_requirement"
    if re.search(r"音乐|歌曲|BGM|配乐|伴奏|木吉他|风格|调性", text, re.IGNORECASE):
        return "style_preference"
    if re.search(r"交付|Excel|PDF|字段|格式|导出|下载", text, re.IGNORECASE):
        return "production_requirement"
    if re.fullmatch(r"保留(?:旁白|字幕)+(?:字段)?", text):
        return "production_requirement"
    return "mandatory_content"


def _fact_handling_instruction(text: str) -> bool:
    """Recognize an explicit instruction about uncertainty, not an unknown event."""
    if re.fullmatch(
        r"(?:对|将)?(?:未确认|未确定|未知)的?(?:事实|信息|内容)"
        r"(?:应|必须)?(?:保留|标注|标记|注明)(?:为)?(?:待确认|待定|未知)(?:状态)?[。！!]?",
        text.strip(),
    ):
        return True
    return re.fullmatch(
        r"[^，,；;。\n]{0,30}(?:名称|名字|型号|参数|性能|规格|数值|身份|具体信息)"
        r"(?:目前|当前|暂时|尚)?(?:未确定|未确认|未定|未知|待确认|待定)[。！!]?",
        text.strip(),
    ) is not None


def _brief_requested_shot_count(brief: dict) -> tuple[int | None, str]:
    """Read only explicit total-shot declarations from confirmed Brief fields."""
    fields = ("交付要求", "特殊需求", "视频目标", "使用场景", "目标时长", "视频时长", "关键事件与流程", "事件流程")
    declarations: list[tuple[int, str]] = []
    approximate = False
    total_pattern = re.compile(
        r"(?:共|总计|总数(?:为)?|总镜数(?:为)?|输出|交付|制作|拍摄|成片(?:输出)?|全片|按|安排(?:为)?|采用)"
        r"\s*(?:(?:约|大约)\s*)?(\d+)\s*(?:个\s*)?镜"
        r"(?:头|脚本|素材|画面|片段)?\s*(?:左右|上下)?"
    )
    approximate_pattern = re.compile(
        r"(?<!第)(?:约|大约)\s*\d+\s*(?:个\s*)?镜(?:头|脚本|素材|画面|片段)?"
        r"|(?<!第)\d+\s*(?:个\s*)?镜(?:头|脚本|素材|画面|片段)?\s*(?:左右|上下)"
    )
    for field in fields:
        text = str(brief.get(field) or "")
        approximate = approximate or bool(approximate_pattern.search(text))
        for clause in _split_quoted_clauses(text, "，,；;\n。"):
            bare = re.fullmatch(r"\s*(\d+)\s*(?:个\s*)?镜(?:头)?\s*(?:安排|成片)?\s*", clause)
            if bare:
                declarations.append((int(bare.group(1)), field))
            sequence = re.match(r"^\s*(\d+)\s*镜(?:头)?\s*(?:顺序|流程)\s*[:：]", clause)
            if sequence:
                declarations.append((int(sequence.group(1)), field))
        for match in total_pattern.finditer(text):
            declarations.append((int(match.group(1)), field))
            if match.group(0).strip().endswith(("左右", "上下")) or re.search(
                r"(?:共|总计|总数|总镜数|输出|交付|制作|拍摄|成片|全片)\s*(?:约|大约)",
                match.group(0),
            ):
                approximate = True
    if approximate:
        return None, "approximate_brief_shot_count"
    counts = {count for count, _field in declarations}
    if len(counts) > 1:
        return None, "conflicting_brief_shot_counts"
    if not counts:
        return None, ""
    count = next(iter(counts))
    return (count, "") if count > 0 else (0, "invalid_brief_shot_count")


def _position_rule(text: str, events: list[dict]) -> dict:
    normalized = _normalize_focus(text)
    rule = {"type": "none", "anchor_event_id": ""}
    if any(marker in normalized for marker in ("开头", "开场", "片头")):
        rule["type"] = "opening"
        return rule
    if any(marker in normalized for marker in ("结尾", "片尾", "收尾", "最后落版")):
        rule["type"] = "closing"
        return rule
    fixed = re.search(r"第\s*(\d+)\s*镜", str(text or ""))
    if fixed:
        return {"type": "fixed_shot", "anchor_event_id": "", "shot_no": fixed.group(1)}
    for suffix, relation in (("之前", "before"), ("之后", "after")):
        if suffix not in str(text or ""):
            continue
        anchor_text = str(text).split(suffix, 1)[0].strip("在于需应当，,。；; ")
        anchor_id = _best_event_id(anchor_text, events)
        return {
            "type": relation,
            "anchor_event_id": anchor_id,
            "anchor_text": anchor_text,
        }
    return rule


def _best_event_id(text: str, events: list[dict]) -> str:
    ranked = [
        (_semantic_match_score(text, str(event.get("title") or "")), -index, str(event["event_id"]))
        for index, event in enumerate(events)
    ]
    if not ranked:
        return ""
    score, _index, event_id = max(ranked)
    return event_id if score > 0 else ""


def _semantic_match_score(left: str, right: str) -> int:
    first = _semantic_text(left)
    second = _semantic_text(right)
    if not first or not second:
        return 0
    negative = re.compile(r"不要|不得|禁止|不能|不可|无需|不需要|不展示|不出现|避免")
    if bool(negative.search(first)) != bool(negative.search(second)):
        return 0
    if first in second or second in first:
        return 10_000
    if _short_action_match(first, second):
        return 10_000
    first_pairs = {first[index:index + 2] for index in range(max(0, len(first) - 1))}
    second_pairs = {second[index:index + 2] for index in range(max(0, len(second) - 1))}
    return 20 * len(first_pairs & second_pairs)


def _semantic_text(value: object) -> str:
    text = _normalize_focus(value)
    for equivalent, canonical in (("来宾", "嘉宾"), ("步入会场", "入场"), ("进入会场", "入场"),
                                   ("合照", "合影")):
        text = text.replace(equivalent, canonical)
    for term in ("必须", "需要", "应当", "应该", "完整呈现", "现场", "画面", "镜头"):
        text = text.replace(term, "")
    return text


def _short_action_match(requirement: str, expression: str) -> bool:
    """Keep complete short action phrases while allowing an object modifier."""
    requirement = re.sub(r"^(?:将|把)", "", requirement)
    if not re.fullmatch(r"[\u4e00-\u9fff]{4,8}", requirement) or len(requirement) % 2:
        return False
    if re.search(r"不|无|禁止|未|别", requirement):
        return False
    chunks = [requirement[index:index + 2] for index in range(0, len(requirement), 2)]
    patterns = {
        "备米": r"备米|(?:准备|备)(?:好|好的|的)?(?:大米|米)|淘(?:好|好的)?米|(?:将|把)(?:大米|米粒|米)(?:缓缓|慢慢)?(?:倒|放)入|(?:大米|米粒)(?:被)?倒入",
        "入锅": r"(?:放|倒)?入(?:内)?锅|放(?:进|到)(?:内)?锅",
        "开盖": r"开(?:上|锅)?盖|(?:打开|掀开|揭开)(?:上|锅)?盖|(?:上|锅)?盖(?:缓缓|慢慢|逐渐|已)?(?:开启|打开|掀开)",
        "成饭": r"成饭|米饭(?:已|已然)?(?:饱满|蓬松|颗粒分明)?(?:成形|成型|煮好|煮熟|蒸熟)|(?:煮好|煮熟|蒸熟)(?:的)?米饭|熟米饭",
    }
    return all(any(not re.search(r"(?:不(?:要|得|需)?|禁止|未|别)(?:将|把)?$", expression[max(0, match.start() - 5):match.start()])
                   for match in re.finditer(patterns.get(chunk, re.escape(chunk[0]) + r"(?:内(?:部)?|的)?" + re.escape(chunk[1])), expression))
               for chunk in chunks)


def _positive_phrase_occurs(phrase: str, expression: str) -> bool:
    return bool(phrase) and any(
        not re.search(r"(?:不(?:要|得|需)?|禁止|未|别)$", expression[max(0, match.start() - 3):match.start()])
        for match in re.finditer(re.escape(phrase), expression)
    )


def _event_primary_text(event: dict) -> str:
    section = str(event.get("creative_section") or event.get("title") or "")
    section = re.sub(r"^\s*镜\s*\d+\s*[:：]", "", section)
    return re.split(r"[。！？\n：:]", section, maxsplit=1)[0].strip()


def _event_requirement_score(text: str, event: dict, *, preserved_flow: bool = False) -> int:
    normalize = _flow_semantic_text if preserved_flow else _semantic_text
    primary = normalize(_event_primary_text(event))
    needle = normalize(text)
    score = _semantic_match_score(needle, normalize(event.get("title")))
    if _semantic_requirement_matches_event(needle, primary):
        score += 20_000
    return score


def _split_covered_action_clauses(text: str, events: list[dict]) -> list[str]:
    # Only split a conjunction when each side names a distinct existing theme.
    # Quoted copy and ambiguous new actions retain the original requirement.
    parts = _split_quoted_clauses(text, "和与")
    if len(parts) < 2:
        return [text]
    targets = []
    for part in parts:
        matches = [event for event in events if _semantic_requirement_matches_event(
            part, _event_primary_text(event))]
        if len(matches) != 1:
            return [text]
        targets.append(str(matches[0]["event_id"]))
    return parts if len(set(targets)) == len(parts) else [text]


def planning_semantic_input(brief, events, requirements, rule_contract):
    """One frozen input contract for the existing planner's semantic bindings."""
    return {'brief': brief,
        'events': [{key: event.get(key) for key in ('event_id', 'title', 'fact_status', 'evidence_refs')}
                   for event in events],
        'requirements': [{key: item.get(key) for key in ('requirement_id', 'text', 'classification', 'position_rule', 'evidence_refs')}
            for item in requirements if item.get('classification') not in {
                'flow_event', 'forbidden', 'style_preference', 'production_requirement'}
            and item.get('origin') != 'review_proposed'],
        'structural_rules': [{key: rule.get(key) for key in ('rule_id', 'value', 'rationale', 'allowed_event_roles')}
                             for rule in _structural_rules(rule_contract)]}


def validate_planning_semantics(inputs, events, assessment):
    """Validate model identities and hard constraints before the planner consumes them."""
    required = {row['requirement_id']: row for row in inputs['requirements']}
    event_map = {row['event_id']: row for row in events}
    rules = {row['rule_id']: row for row in inputs['structural_rules']}
    bindings, structures = assessment.get('bindings'), assessment.get('structures')
    if (not isinstance(bindings, list) or len(bindings) != len(required)
            or not isinstance(structures, list) or len(structures) != len(event_map)):
        raise ValueError('incomplete semantic bindings')
    negative = re.compile(r"不要|不得|禁止|不能|不可|无需|不需要|不展示|不出现|避免")
    seen = set()
    for row in bindings:
        if (not isinstance(row, dict) or row.get('requirement_id') not in required
                or row['requirement_id'] in seen
                or row.get('relation') not in {'same_action', 'attribute', 'separate_action', 'unresolved'}
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            raise ValueError('invalid requirement identity')
        if row['relation'] in {'same_action', 'attribute'}:
            if row.get('event_id') not in event_map:
                raise ValueError('invalid event identity')
            text = str(required[row['requirement_id']].get('text') or '')
            event_text = str(event_map[row['event_id']].get('title') or '')
            if bool(negative.search(text)) != bool(negative.search(event_text)):
                raise ValueError('opposed requirement polarity')
        elif row.get('event_id') != '':
            raise ValueError('unbound action requires an empty event identity')
        seen.add(row['requirement_id'])
    seen = set()
    order = {}
    concrete_roles = {'speech', 'ceremony', 'performance', 'talent_arrival', 'talent_interaction'}
    for row in structures:
        if (not isinstance(row, dict) or row.get('event_id') not in event_map
                or row['event_id'] in seen or row.get('rule_id') not in rules
                or not isinstance(row.get('reason'), str) or not row['reason'].strip()):
            raise ValueError('invalid structural identity')
        role = _event_role(event_map[row['event_id']])
        if role in concrete_roles and role not in (rules[row['rule_id']].get('allowed_event_roles') or []):
            raise ValueError('concrete event violates structural rule')
        order[row['event_id']] = list(rules).index(row['rule_id'])
        seen.add(row['event_id'])
    positions = [order[event_id] for event_id in event_map]
    if positions != sorted(positions):
        raise ValueError('semantic structures reverse declared event order')


def _apply_planning_semantics(brief, events, requirements, rule_contract, assessment):
    from .run_contract import sha256_json
    if not isinstance(assessment, dict) or assessment.get('status') != 'ready':
        return {}
    expected = planning_semantic_input(brief, events, requirements, rule_contract)
    if assessment.get('input_sha256') != sha256_json(expected):
        return {}
    try:
        validate_planning_semantics(expected, events, assessment)
    except (TypeError, ValueError, KeyError):
        return {}
    eligible = {row['requirement_id'] for row in expected['requirements']}
    event_ids = {event['event_id'] for event in events}
    bindings = {row['requirement_id']: row for row in assessment.get('bindings', [])
        if row.get('requirement_id') in eligible and row.get('event_id') in event_ids
        and row.get('relation') in {'same_action', 'attribute'} and row.get('reason')}
    for requirement in requirements:
        binding = bindings.get(requirement['requirement_id'])
        if binding:
            requirement['semantic_binding'] = dict(binding)
    rules = {rule['rule_id'] for rule in expected['structural_rules']}
    assigned = {row['event_id']: row for row in assessment.get('structures', [])
        if row.get('event_id') in event_ids and row.get('rule_id') in rules and row.get('reason')}
    for event in events:
        if event['event_id'] in assigned:
            event['structural_assignment'] = dict(assigned[event['event_id']])
    return bindings


def _planning_items(
    events: list[dict], requirements: list[dict], semantic_bindings: dict | None = None,
) -> tuple[list[dict], dict[str, str]]:
    items = [dict(event) for event in events]
    bindings: dict[str, str] = {}
    opening: list[dict] = []
    closing: list[dict] = []
    positioned: list[dict] = []
    additions: list[dict] = []
    occupied_mandatory_events: set[str] = set()
    for requirement in requirements:
        requirement_id = str(requirement["requirement_id"])
        classification = str(requirement.get("classification") or "")
        if classification == "flow_event":
            bindings[requirement_id] = requirement_id
            continue
        if requirement.get("origin") == "review_proposed":
            continue
        if classification in {"forbidden", "style_preference", "production_requirement"}:
            continue
        text = str(requirement.get("text") or "")
        preserved_flow = requirement_id.startswith("brief_flow_")
        matching_text = _flow_semantic_text(text) if preserved_flow else text
        position = dict(requirement.get("position_rule") or {})
        relation = str(position.get("type") or "none")
        # Literal captions describe a field at an existing position, not a new action.
        if events and re.search(r"字幕|文案|(?:结尾|片尾|开头|片头).{0,4}(?:写|文字)", text) and relation in {"opening", "closing"}:
            event = events[0] if relation == "opening" else events[-1]
            bindings[requirement_id] = str(event["event_id"])
            continue
        if relation == "fixed_shot" and requirement.get("origin") == "confirmed_review":
            bindings[requirement_id] = ""
            continue
        semantic = (semantic_bindings or {}).get(requirement_id)
        ranked = [
            (_event_requirement_score(matching_text, event, preserved_flow=preserved_flow), -index, event)
            for index, event in enumerate(events)
        ]
        if semantic:
            ranked = [(30_000, 0, event) for event in events if event['event_id'] == semantic['event_id']]
        if ranked:
            score, _index, event = max(ranked, key=lambda row: (row[0], row[1]))
            event_id = str(event["event_id"])
            semantic_match = bool(semantic) or _semantic_requirement_matches_event(
                text, str(event.get("title") or ""),
            )
            if preserved_flow:
                event_text = _flow_semantic_text(event.get("title"))
                semantic_match = semantic_match or _semantic_requirement_matches_event(matching_text, event_text)
                flow_anchors = {term for term in ("入口", "签名", "陈列", "产品", "布置", "主持", "开场", "来宾", "体验")
                                if term in matching_text and term in event_text}
                semantic_match = semantic_match or len(flow_anchors) >= 2
            event_available = (
                not requirement.get("mandatory")
                or event_id not in occupied_mandatory_events
                or preserved_flow
                or bool(semantic)
                or _shared_visual_attribute(text, str(event.get("title") or ""))
            )
            if score >= 40 and semantic_match and event_available:
                bindings[requirement_id] = event_id
                if requirement.get("mandatory"):
                    occupied_mandatory_events.add(event_id)
                if relation == "none":
                    continue
                matched = None
                matched_container = None
                for container in (items, opening, positioned, additions, closing):
                    matched = next(
                        (item for item in container if item.get("event_id") == event_id),
                        None,
                    )
                    if matched is not None:
                        matched_container = container
                        break
                if matched is not None:
                    matched_container.remove(matched)
                    matched["requirement_ids"] = list(dict.fromkeys([
                        *(matched.get("requirement_ids") or []), requirement_id,
                    ]))
                    matched["position_rule"] = position
                    matched["position_evidence_refs"] = list(
                        requirement.get("evidence_refs") or []
                    )
                    matched["mandatory"] = bool(requirement.get("mandatory"))
                    matched["origin"] = str(requirement.get("origin") or "brief")
                    if relation == "opening":
                        opening.append(matched)
                    elif relation == "closing":
                        closing.append(matched)
                    else:
                        positioned.append(matched)
                    continue
        item = _requirement_planning_item(requirement)
        bindings[requirement_id] = requirement_id
        if relation == "opening":
            opening.append(item)
        elif relation == "closing":
            closing.append(item)
        elif relation in {"before", "after", "fixed_shot"}:
            positioned.append(item)
        else:
            additions.append(item)
    items = [*opening, *items]
    for item in positioned:
        rule = dict(item.get("position_rule") or {})
        if rule.get("type") == "fixed_shot":
            index = max(0, min(len(items), int(rule.get("shot_no") or 1) - 1))
        else:
            anchor = str(rule.get("anchor_event_id") or "")
            index = next(
                (offset for offset, value in enumerate(items) if value.get("event_id") == anchor),
                len(items),
            )
            if rule.get("type") == "after" and index < len(items):
                index += 1
        items.insert(index, item)
    insert_at = max(len(opening), len(items) - 1) if items else 0
    items[insert_at:insert_at] = additions
    items.extend(closing)
    return items, bindings


def _semantic_requirement_matches_event(requirement_text: str, event_text: str) -> bool:
    requirement = _semantic_text(requirement_text)
    event = _semantic_text(event_text)
    if not requirement or not event:
        return False
    if _positive_phrase_occurs(requirement, event) or _positive_phrase_occurs(event, requirement):
        return True
    if _short_action_match(requirement, event):
        return True
    quoted = re.findall(r'[“\"]([^”\"]+)[”\"]', requirement_text)
    if quoted and all(_semantic_text(value) in event for value in quoted):
        return True
    if _shared_visual_attribute(requirement_text, event_text):
        return True
    shared = {
        term for term in _SEMANTIC_ANCHOR_TERMS
        if term in requirement and term in event
    }
    return len(shared) >= 2


def _flow_semantic_text(value: object) -> str:
    # These aliases retain the entrance and registration action independently.
    return (_semantic_text(value).replace("门口", "入口").replace("签到", "签名")
            .replace("主持人", "主持").replace("试用", "体验").replace("与", "和"))


def _shared_visual_attribute(requirement_text: str, event_text: str) -> bool:
    """Only share an already expressed object attribute, never an independent action."""
    if re.search(r"代表|发言|致辞|主持|签到(?!台)|体验|合影|启动|颁奖", requirement_text):
        return False
    literal = re.sub(r"^(?:并)?(?:保留|展示|显示|呈现)?", "", requirement_text.strip())
    literal = re.sub(r"字幕$", "", literal)
    if not re.search(r"放入|入锅|开盖|盛饭|上桌|操作|按键|拿起|走入|到场|入场", literal):
        parts = _split_quoted_clauses(literal, "和与")
        if parts and all(len(_semantic_text(part)) >= 2 and _positive_phrase_occurs(_semantic_text(part), _semantic_text(event_text))
                         for part in parts):
            return True
    if (re.search(r"杯|盖|桌|台|板|背景|品牌|字样|标识|材质|颜色", requirement_text)
            and _semantic_text(requirement_text) in _semantic_text(event_text)):
        return True
    terms = re.findall(r"(?:米白|深灰|黑|白|红|蓝|绿|黄|银|金|同)色|杯盖|杯身|材质|标识|logo", requirement_text, re.IGNORECASE)
    literal = re.search(r"(?:写|印有|字样为)[：:\s]*[“\"]?([^，,。；;”\"]+)", requirement_text)
    if literal:
        terms.append(literal.group(1).removesuffix("字样"))
    return bool(terms) and all(_semantic_text(term) in _semantic_text(event_text) for term in terms)


def _requirement_planning_item(requirement: dict) -> dict:
    evidence_refs = list(requirement.get("evidence_refs") or [])
    evidence = evidence_refs[0] if evidence_refs else {}
    return {
        "event_id": str(requirement["requirement_id"]),
        "order": 0,
        "title": str(requirement.get("text") or ""),
        "text": str(requirement.get("text") or ""),
        "kind": str(requirement.get("classification") or "mandatory_content"),
        "primary": True,
        "required": True,
        "mandatory": True,
        "fact_status": "confirmed",
        "brief_evidence": {
            "type": str(evidence.get("type") or "file_brief"),
            "text": str(requirement.get("original_text") or ""),
            "source_ref": dict(evidence.get("source_ref") or {}),
            **({"fact_id": evidence["fact_id"]} if evidence.get("fact_id") else {}),
        },
        "evidence_refs": evidence_refs,
        "declared_order": None,
        "allow_non_linear": False,
        "non_linear_reason": "",
        "shot_units": 1,
        "requirement_ids": [str(requirement["requirement_id"])],
        "position_rule": dict(requirement.get("position_rule") or {}),
        "origin": str(requirement.get("origin") or "brief"),
    }


def _bind_requirement_coverage(
    requirements: list[dict],
    shots: list[dict],
    *,
    requirement_bindings: dict[str, str],
    conflicted_requirement_ids: set[str] | None = None,
    overridden_requirement_ids: set[str] | None = None,
) -> list[dict]:
    """Bind every clause to authoritative plan slots without inventing facts."""
    coverage = []
    conflicted_requirement_ids = conflicted_requirement_ids or set()
    overridden_requirement_ids = overridden_requirement_ids or set()
    all_shot_nos = [str(shot["shot_no"]) for shot in shots]
    for requirement in requirements:
        text = str(requirement.get("text") or "")
        kind = str(requirement.get("kind") or "")
        classification = str(requirement.get("classification") or kind)
        pending = (
            requirement.get("fact_status") not in {"confirmed", "creative_expression"}
            or not text
            or (requirement.get("origin") != "creative_draft"
                and any(marker in text for marker in _UNKNOWN_MARKERS)
                and not (classification == "production_requirement" and _fact_handling_instruction(text)))
        )
        if requirement.get("origin") == "review_proposed":
            shot_nos = []
        elif classification == "flow_event":
            shot_nos = [
                str(shot["shot_no"])
                for shot in shots
                if requirement["requirement_id"] in shot.get("event_ids", [])
            ]
        elif classification in {"forbidden", "style_preference"}:
            shot_nos = all_shot_nos
        elif classification == "production_requirement":
            # Delivery clauses are Brief facts consumed by the delivery contract,
            # not shot content that must be bound to an event.
            shot_nos = []
        else:
            bound_id = requirement_bindings.get(str(requirement["requirement_id"]))
            shot_nos = [
                str(shot["shot_no"])
                for shot in shots
                if bound_id and bound_id in shot.get("event_ids", [])
            ]
            anchored = _anchored_requirement_shots(requirement, shots)
            creative_binding = any(bound_id in shot.get("event_ids", [])
                                   and shot.get("brief_evidence", {}).get("type") == "creative_draft"
                                   for shot in shots)
            if not creative_binding:
                shot_nos = anchored or shot_nos
        status = (
            "overridden_by_resolution"
            if str(requirement["requirement_id"]) in overridden_requirement_ids
            else
            "needs_fact"
            if pending
            else "conflict" if str(requirement["requirement_id"]) in conflicted_requirement_ids
            else "covered" if shot_nos or classification == "production_requirement" else "uncovered"
        )
        item = {
            "requirement_id": str(requirement["requirement_id"]),
            "kind": classification,
            "status": status,
            "shot_nos": shot_nos,
            "evidence_refs": list(requirement.get("evidence_refs") or []),
        }
        coverage.append(item)
        for shot in shots:
            if str(shot["shot_no"]) in shot_nos:
                shot.setdefault("requirement_ids", []).append(item["requirement_id"])
    return coverage


def _decision_counts(
    coverage: list[dict], shots: list[dict], conflicts: list[dict],
) -> dict:
    decisions = [
        decision
        for shot in shots
        for decision in shot.get("field_decisions") or []
    ]
    unexplained = 0
    hard_downgrades = 0
    for item in decisions:
        if item.get("decision_status") not in {"overridden", "downgraded"}:
            continue
        candidates = list(item.get("candidates") or [])
        if item.get("decision_status") == "downgraded" and any(
            candidate.get("rule_level") == "structural_required"
            for candidate in candidates
        ):
            hard_downgrades += 1
        has_rule = any(candidate.get("rule_id") for candidate in candidates)
        if not (
            str(item.get("override_reason_code") or "").strip()
            and str(item.get("override_reason") or "").strip()
            and list(item.get("evidence_refs") or [])
            and has_rule
        ):
            unexplained += 1
    return {
        "requirements_uncovered": sum(
            item.get("status") in {"partial", "uncovered", "overridden_by_resolution"}
            for item in coverage
        ),
        "hard_template_downgrades": hard_downgrades,
        "unresolved_conflicts": sum(item.get("status") == "unresolved" for item in conflicts),
        "unexplained_overrides": unexplained,
    }


def _capacity_report(
    *, target_duration: int, requested_count: int | None,
    planning_items: list[dict], shot_count: int, structural_slot_count: int = 0,
    structural_minimum_count: int = 0,
) -> dict:
    content_units = sum(int(item.get("shot_units") or 1) for item in planning_items)
    required_units = max(content_units, structural_slot_count)
    duration_shortage = bool(target_duration and target_duration < shot_count)
    mandatory_units = sum(
        int(item.get("shot_units") or 1)
        for item in planning_items
        if item.get("mandatory")
    )
    requested_shortage = (
        requested_count is not None
        and requested_count < max(
            content_units if any(item.get("creative_block_id") for item in planning_items) else mandatory_units,
            structural_minimum_count,
        )
    )
    final_count_mismatch = requested_count is not None and shot_count != requested_count
    needs_choice = duration_shortage or requested_shortage or final_count_mismatch
    mismatch_reason = ""
    if final_count_mismatch:
        mismatch_reason = f"Brief明确要求{requested_count}镜，但当前规划为{shot_count}镜"
    return {
        "status": "needs_clarification" if needs_choice else "ready",
        "target_seconds": target_duration,
        "required_shot_units": required_units,
        "mandatory_shot_units": mandatory_units,
        "structural_slot_count": structural_slot_count,
        "structural_minimum_count": structural_minimum_count,
        "requested_shot_count": requested_count,
        "planned_shot_count": shot_count,
        "final_shot_count_matches": not final_count_mismatch,
        "mismatch_reason": mismatch_reason,
        "choices": (
            ["重新规划为 Brief 明确镜数", "增加目标时长", "由用户明确取舍非强制流程"]
            if needs_choice else []
        ),
    }


def _traceability(
    requirements: list[dict], coverage: list[dict], shots: list[dict],
) -> list[dict]:
    coverage_by_id = {str(item["requirement_id"]): item for item in coverage}
    shots_by_no = {str(shot["shot_no"]): shot for shot in shots}
    rows = []
    for requirement in requirements:
        requirement_id = str(requirement["requirement_id"])
        item = coverage_by_id.get(requirement_id, {})
        shot_nos = list(item.get("shot_nos") or [])
        origin = str(requirement.get("origin") or "brief")
        final_status = (
            "review_proposed"
            if origin == "review_proposed"
            else "review_confirmed"
            if origin == "confirmed_review" and item.get("status") == "covered"
            else str(item.get("status") or "uncovered")
        )
        rows.append({
            "requirement_id": requirement_id,
            "brief_text": str(requirement.get("original_text") or requirement.get("text") or ""),
            "classification": str(requirement.get("classification") or requirement.get("kind") or ""),
            "position_rule": dict(requirement.get("position_rule") or {}),
            "plan_shot_nos": shot_nos,
            "output_evidence": [],
            "template_source": [
                dict(shots_by_no[shot_no].get("template_source") or {})
                for shot_no in shot_nos if shot_no in shots_by_no
            ],
            "review_findings": (
                [
                    {
                        "requirement_id": requirement_id,
                        "text": str(requirement.get("original_text") or ""),
                        "status": final_status,
                        "evidence_refs": list(requirement.get("evidence_refs") or []),
                    }
                ]
                if origin in {"review_proposed", "confirmed_review"}
                else []
            ),
            "final_status": final_status,
            "decision_source": str(requirement.get("decision_source") or origin),
        })
    return rows


def _position_edges(requirement: dict, events: list[dict]) -> list[dict]:
    rule = dict(requirement.get("position_rule") or {})
    relation = str(rule.get("type") or "none")
    requirement_id = str(requirement.get("requirement_id") or "")
    if not requirement_id or not events:
        return []
    if relation == "opening":
        return [{
            "from_event_id": requirement_id,
            "to_event_id": str(events[0]["event_id"]),
            "relation": "before",
            "reason": "Brief开头位置约束",
        }]
    if relation == "closing":
        return [{
            "from_event_id": str(events[-1]["event_id"]),
            "to_event_id": requirement_id,
            "relation": "before",
            "reason": "Brief结尾位置约束",
        }]
    anchor = str(rule.get("anchor_event_id") or "")
    if relation in {"before", "after"} and anchor:
        return [{
            "from_event_id": requirement_id if relation == "before" else anchor,
            "to_event_id": anchor if relation == "before" else requirement_id,
            "relation": "before",
            "reason": "Brief相对位置约束",
        }]
    return []


def _coverage_matches(text: str, shot: dict) -> bool:
    needle = _normalize_focus(text)
    haystack = _normalize_focus(" ".join((
        str(shot.get("primary_event") or ""),
        str(shot.get("image_query") or ""),
        str(shot.get("inheritable_function") or ""),
    )))
    return bool(needle and needle in haystack)


def _anchored_requirement_shots(requirement: dict, shots: list[dict]) -> list[str]:
    if not shots:
        return []
    rule = dict(requirement.get("position_rule") or {})
    if rule.get("type") == "fixed_shot":
        shot_no = str(rule.get("shot_no") or "")
        return [shot_no] if any(str(shot.get("shot_no")) == shot_no for shot in shots) else []
    text = str(requirement.get("text") or "")
    normalized = _normalize_focus(text)
    if (
        any(marker in normalized for marker in ("开头", "开场", "片头"))
        or "落到现场" in normalized and "转场" in normalized
    ):
        return [str(shots[0]["shot_no"])]
    if any(marker in normalized for marker in ("结尾", "片尾", "收尾", "最后落版")):
        return [str(shots[-1]["shot_no"])]
    return []


def _requested_shot_count(constraints: dict) -> tuple[int | None, str]:
    if "planned_shot_count" not in constraints:
        return None, ""
    value = _positive_int(constraints.get("planned_shot_count"))
    if value is None:
        return 0, "invalid_planned_shot_count"
    return value, ""


def _resolve_template_rule_contract(brief: dict, constraints: dict) -> dict:
    supplied = constraints.get("template_rule_contract")
    if supplied is not None:
        if not isinstance(supplied, dict) or not isinstance(supplied.get("payload"), dict):
            raise ValueError("template_rule_contract 规划约束无效")
        payload = validate_template_rules(dict(supplied["payload"]))
        category_id = category_id_for_script_type(str(brief.get("脚本类型") or "发布会"))
        if payload.get("category_id") != category_id:
            raise ValueError("模板规则类别与 Brief 脚本类型不一致")
        return {
            "status": str(supplied.get("status") or "ready"),
            "source": str(supplied.get("source") or "package"),
            "path": str(supplied.get("path") or ""),
            "sha256": str(supplied.get("sha256") or ""),
            "warning_codes": list(supplied.get("warning_codes") or []),
            "payload": payload,
        }
    category_id = category_id_for_script_type(str(brief.get("脚本类型") or "发布会"))
    payload = builtin_template_rules(category_id)
    from .run_contract import sha256_json
    return {
        "status": "legacy_default",
        "source": "builtin_versioned_default",
        "path": "",
        "sha256": sha256_json(payload),
        "warning_codes": ["template_rules_legacy_default"],
        "payload": payload,
    }


def _structural_rules(rule_contract: dict) -> list[dict]:
    return [
        dict(rule)
        for rule in rule_contract["payload"]["rules"]
        if rule.get("level") == "structural_required"
        and rule.get("field") == "structure"
    ]


def _structural_slot_count(rule_contract: dict) -> int:
    return max(
        int(rule["selector"]["slot_end"])
        for rule in _structural_rules(rule_contract)
    )


def _rule_for_slot(rule_contract: dict, slot_no: int, field: str) -> dict:
    return _rule_for_scaled_slot(rule_contract, slot_no, field, None)


def _rule_for_scaled_slot(
    rule_contract: dict, slot_no: int, field: str, total_slots: int | None,
) -> dict:
    default_count = _structural_slot_count(rule_contract)
    effective_slot = slot_no
    if total_slots and total_slots != default_count:
        effective_slot = (
            1 if total_slots <= 1 else
            1 + round((slot_no - 1) * (default_count - 1) / (total_slots - 1))
        )
    return next(
        (
            dict(rule) for rule in rule_contract["payload"]["rules"]
            if rule.get("field") == field
            and int(rule["selector"]["slot_start"]) <= effective_slot
            <= int(rule["selector"]["slot_end"])
        ),
        {},
    )


def _rule_for_structural_arc(
    rule_contract: dict, structural_rule: dict, field: str,
) -> dict:
    selector = structural_rule.get("selector") or {}
    return next(
        (
            dict(rule) for rule in rule_contract["payload"]["rules"]
            if rule.get("field") == field
            and rule.get("selector") == selector
        ),
        {},
    )


def _event_role(event: dict) -> str:
    text = str(event.get("title") or event.get("text") or "").casefold()
    if any(term in text for term in (
        "落版", "收尾", "收束", "片尾", "logo", "结束", "全场大合影",
    )):
        return "closing"
    talent = any(term in text for term in ("明星", "艺人", "嘉宾", "代言人"))
    if talent and any(term in text for term in ("互动", "合影", "交流", "应援")):
        return "talent_interaction"
    if talent and any(term in text for term in ("亮相", "到场", "抵达", "入场")):
        return "talent_arrival"
    if any(term in text for term in ("巡游", "参观", "游览", "探店")):
        return "tour"
    if any(term in text for term in ("发言", "致辞", "演讲", "宣讲", "讲话", "趋势", "布局", "方案")):
        return "speech"
    if re.search(r"仪式(?!感)", text) or any(term in text for term in ("启动", "剪彩", "颁奖", "揭幕", "点亮", "发布")):
        return "ceremony"
    if any(term in text for term in ("表演", "演出", "演绎", "乐队", "合唱", "舞蹈", "歌曲", "音乐", "狂欢", "竞技", "比赛")):
        return "performance"
    if any(term in text for term in ("空镜", "氛围", "环境", "外景", "云层", "蝴蝶", "转场")):
        return "atmosphere"
    if "产品" in text and any(term in text for term in ("演示", "展示", "体验", "操作")):
        return "interaction"
    if any(term in text for term in ("互动", "体验", "打卡", "茶歇", "交流", "合影", "领取", "领周边", "应援")):
        return "interaction"
    if any(term in text for term in ("空间", "场地", "会场", "门店", "展区", "舞台")):
        return "space"
    if any(term in text for term in ("签到", "入场", "到场", "抵达", "迎宾")):
        return "arrival"
    if any(term in text for term in ("人气", "观众", "欢呼", "代表", "宾客")):
        return "crowd"
    if any(term in text for term in ("品牌", "产品", "kv", "主视觉")):
        return "brand"
    return "interaction"


def _generic_structure_event(slot_no: int, rule: dict, arc_ordinal: int) -> dict:
    structure = str(rule.get("value") or "结构建立")
    if any(term in structure for term in ("艺人", "明星", "嘉宾")):
        event_label = "叙事焦点"
    elif any(term in structure for term in (
        "仪式", "宣讲", "发布", "颁奖", "表演", "演出", "狂欢", "竞技", "应援",
    )):
        event_label = "核心节奏"
    else:
        event_label = structure
    roles = list(rule.get("allowed_event_roles") or [])
    role = roles[0] if roles else "atmosphere"
    phases = (
        "广角建立", "动线推进", "人物入画", "中景承接",
        "细节补充", "情绪反应", "局部切换", "纵深延展",
        "节奏回收", "转场衔接", "焦点提炼", "落点完成",
    )
    phase = (
        phases[max(arc_ordinal, 1) - 1]
        if arc_ordinal <= len(phases)
        else f"节奏延展{arc_ordinal - len(phases)}"
    )
    title = f"{event_label}·{phase}"
    return {
        "event_id": f"template_slot_{slot_no:03d}",
        "order": 0,
        "title": title,
        "text": title,
        "kind": "template_structure",
        "primary": True,
        "required": False,
        "mandatory": False,
        "fact_status": "structural_placeholder",
        "brief_evidence": {
            "type": "template_rule",
            "text": str(rule.get("rationale") or ""),
            "source_ref": {"rule_id": str(rule.get("rule_id") or "")},
        },
        "evidence_refs": [{"type": "template_rule", "rule_id": rule.get("rule_id")}],
        "shot_units": 1,
        "event_role": role,
        "generated_structure_event": True,
    }


def _adaptive_structural_slot_rules(
    events: list[dict],
    shot_count: int,
    rule_contract: dict,
    *,
    preserve_total: bool = False,
) -> tuple[list[dict], dict[str, str]]:
    """Keep default hard-slot quantities, expanding only arcs with excess demand."""
    structural_rules = _structural_rules(rule_contract)
    base_slots = [
        _rule_for_scaled_slot(rule_contract, index, "structure", shot_count)
        for index in range(1, shot_count + 1)
    ]
    base_counts = {
        str(rule.get("rule_id") or ""): sum(
            str(slot.get("rule_id") or "") == str(rule.get("rule_id") or "")
            for slot in base_slots
        )
        for rule in structural_rules
    }
    demand_counts = {str(rule.get("rule_id") or ""): 0 for rule in structural_rules}
    assigned_rule_ids: dict[str, str] = {}
    arc_cursor = 0
    for event in events:
        role = _event_role(event)
        event["event_role"] = role
        semantic_rule = (event.get('structural_assignment') or {}).get('rule_id')
        compatible = [
            index for index, rule in enumerate(structural_rules)
            if (rule.get('rule_id') == semantic_rule if semantic_rule
                else role in (rule.get("allowed_event_roles") or []))
        ]
        if not compatible:
            continue
        position_type = str((event.get("position_rule") or {}).get("type") or "none")
        if position_type == "opening":
            selected = 0
        elif position_type == "closing":
            selected = len(structural_rules) - 1
        else:
            forward = [index for index in compatible if index >= arc_cursor]
            ordered = [*forward, *(index for index in compatible if index < arc_cursor)]
            selected = next((
                index for index in ordered
                if demand_counts[str(structural_rules[index].get("rule_id") or "")]
                < base_counts[str(structural_rules[index].get("rule_id") or "")]
            ), ordered[0])
            arc_cursor = selected
        selected_rule_id = str(structural_rules[selected].get("rule_id") or "")
        assigned_rule_ids[str(event.get("event_id") or "")] = selected_rule_id
        demand_counts[selected_rule_id] += 1

    fact_bearing_roles = {
        "talent_arrival", "talent_interaction", "ceremony", "performance", "speech",
    }
    quantities = {
        str(rule.get("rule_id") or ""): max(
            base_counts[str(rule.get("rule_id") or "")],
            demand_counts[str(rule.get("rule_id") or "")],
        )
        for rule in structural_rules
    }
    unsupported_quantity = 0
    for rule in structural_rules:
        rule_id = str(rule.get("rule_id") or "")
        allowed_roles = set(rule.get("allowed_event_roles") or [])
        if (
            allowed_roles
            and allowed_roles <= fact_bearing_roles
            and demand_counts[rule_id] == 0
        ):
            unsupported_quantity += quantities[rule_id]
            quantities[rule_id] = 0
    if unsupported_quantity:
        supported = [
            rule for rule in structural_rules
            if quantities[str(rule.get("rule_id") or "")] > 0
        ]
        fallback = max(
            supported,
            key=lambda rule: (
                demand_counts[str(rule.get("rule_id") or "")],
                -structural_rules.index(rule),
            ),
        )
        quantities[str(fallback.get("rule_id") or "")] += unsupported_quantity
    if preserve_total and sum(quantities.values()) > shot_count:
        overflow = sum(quantities.values()) - shot_count
        reducible = sorted(
            structural_rules,
            key=lambda rule: (
                demand_counts[str(rule.get("rule_id") or "")],
                -quantities[str(rule.get("rule_id") or "")],
                structural_rules.index(rule),
            ),
        )
        for rule in reducible:
            rule_id = str(rule.get("rule_id") or "")
            removable = max(0, quantities[rule_id] - demand_counts[rule_id])
            removed = min(overflow, removable)
            quantities[rule_id] -= removed
            overflow -= removed
            if overflow == 0:
                break
    expanded = []
    for rule in structural_rules:
        rule_id = str(rule.get("rule_id") or "")
        expanded.extend(dict(rule) for _ in range(quantities[rule_id]))
    return expanded, assigned_rule_ids


def _assign_events_to_hard_slots(
    events: list[dict],
    shot_count: int,
    rule_contract: dict,
    raw_resolutions: object,
    *,
    preserve_total: bool = False,
) -> tuple[list[list[dict]], list[dict], list[dict]]:
    rules, assigned_rule_ids = _adaptive_structural_slot_rules(
        events, shot_count, rule_contract, preserve_total=preserve_total,
    )
    shot_count = len(rules)
    groups: list[list[dict]] = [[] for _ in range(shot_count)]
    resolutions = dict(raw_resolutions) if isinstance(raw_resolutions, dict) else {}
    conflicts = []
    creative = sorted((event for event in events if event.get("creative_block_id")),
                      key=lambda event: event["declared_order"])
    creative_positions = {
        event["event_id"]: (index * (shot_count - 1) // (len(creative) - 1) if len(creative) > 1 else 0)
        for index, event in enumerate(creative)
    }
    if creative:
        anchors = {0: 0}
        if len(creative) > 1:
            anchors[len(creative) - 1] = shot_count - 1
        for index, event in enumerate(creative):
            rule = event.get("position_rule") or {}
            if rule.get("type") == "opening":
                anchors[index] = 0
            elif rule.get("type") == "closing":
                anchors[index] = shot_count - 1
            elif rule.get("type") == "fixed_shot" and str(rule.get("shot_no", "")).isdigit():
                anchors[index] = int(rule["shot_no"]) - 1
        ordered_anchors = sorted(anchors.items())
        if (all(0 <= position < shot_count for _, position in ordered_anchors)
                and all(right_pos - left_pos >= right - left
                        for (left, left_pos), (right, right_pos) in zip(ordered_anchors, ordered_anchors[1:]))):
            for index, position in ordered_anchors:
                creative_positions[creative[index]["event_id"]] = position
            for (left, left_pos), (right, right_pos) in zip(ordered_anchors, ordered_anchors[1:]):
                for index in range(left + 1, right):
                    creative_positions[creative[index]["event_id"]] = left_pos + (index - left) * (right_pos - left_pos) // (right - left)
    # Accepted expressions already carry an explicit narrative order. Place
    # them through the same slot allocator before supplementary requirements;
    # keyword roles in long visual prose must not silently reorder paragraphs.
    # Template fields remain intact. A role mismatch is shown for review and
    # does not manufacture a confirmed Brief fact or a template override.
    ordered_events = [*creative, *(event for event in events if not event.get("creative_block_id"))]
    for event in ordered_events:
        role = _event_role(event)
        event["event_role"] = role
        if event["event_id"] in creative_positions:
            index = creative_positions[event["event_id"]]
            if role not in (rules[index].get("allowed_event_roles") or []):
                event["creative_order_note"] = "按已选初稿保留段落顺序；请核对当前画面与模板结构的适配。"
            groups[index].append(event)
            continue
        rule = dict(event.get("position_rule") or {})
        position_type = str(rule.get("type") or "none")
        if position_type == "opening":
            candidates = [0]
        elif position_type == "closing":
            candidates = [shot_count - 1]
        elif position_type == "fixed_shot" and str(rule.get("shot_no") or "").isdigit():
            candidates = [int(rule["shot_no"]) - 1]
        else:
            assigned_rule_id = assigned_rule_ids.get(str(event.get("event_id") or ""), "")
            candidates = [
                index for index, hard_rule in enumerate(rules)
                if str(hard_rule.get("rule_id") or "") == assigned_rule_id
            ]
        compatible = next((
            index for index in candidates
            if 0 <= index < shot_count
            and (role in (rules[index].get("allowed_event_roles") or [])
                 or (event.get('structural_assignment') or {}).get('rule_id') == rules[index].get('rule_id'))
            and not groups[index]
        ), None)
        if compatible is None and position_type in {"opening", "closing", "fixed_shot"}:
            slot_index = min(max(candidates[0] if candidates else 0, 0), shot_count - 1)
            hard_rule = rules[slot_index]
            conflict_id = (
                "conflict_" + re.sub(
                    r"[^a-zA-Z0-9_]+", "_",
                    f"{event.get('event_id')}_{hard_rule.get('rule_id')}",
                ).strip("_")
            )
            choice = str(resolutions.get(conflict_id) or "")
            conflict = {
                "conflict_id": conflict_id,
                "type": "brief_hard_template_conflict",
                "status": "resolved" if choice in {"brief_wins", "template_wins"} else "unresolved",
                "choice": choice,
                "requirement_id": str((event.get("requirement_ids") or [event.get("event_id")])[0]),
                "event_id": str(event.get("event_id") or ""),
                "shot_no": str(slot_index + 1),
                "field": "structure",
                "brief_value": str(event.get("title") or ""),
                "template_value": str(hard_rule.get("value") or ""),
                "rule_id": str(hard_rule.get("rule_id") or ""),
                "rule_level": "structural_required",
                "rule_sha256": str(rule_contract.get("sha256") or ""),
                "evidence_refs": list(
                    event.get("position_evidence_refs") or event.get("evidence_refs") or []
                ),
            }
            conflicts.append(conflict)
            if choice == "brief_wins":
                groups[slot_index].append(event)
            continue
        if compatible is None:
            slot_index = next(
                (index for index in candidates if 0 <= index < shot_count and not groups[index]),
                next((index for index, group in enumerate(groups) if not group), 0),
            )
            hard_rule = rules[slot_index]
            conflict_id = (
                "conflict_" + re.sub(
                    r"[^a-zA-Z0-9_]+", "_",
                    f"{event.get('event_id')}_{hard_rule.get('rule_id')}_capacity",
                ).strip("_")
            )
            choice = str(resolutions.get(conflict_id) or "")
            conflicts.append({
                "conflict_id": conflict_id,
                "type": "template_role_capacity_conflict",
                "status": "resolved" if choice in {"brief_wins", "template_wins"} else "unresolved",
                "choice": choice,
                "requirement_id": str((event.get("requirement_ids") or [event.get("event_id")])[0]),
                "event_id": str(event.get("event_id") or ""),
                "shot_no": str(slot_index + 1),
                "field": "structure",
                "brief_value": str(event.get("title") or ""),
                "template_value": str(hard_rule.get("value") or ""),
                "rule_id": str(hard_rule.get("rule_id") or ""),
                "rule_level": "structural_required",
                "rule_sha256": str(rule_contract.get("sha256") or ""),
                "evidence_refs": list(
                    event.get("position_evidence_refs") or event.get("evidence_refs") or []
                ),
            })
            if choice == "brief_wins":
                groups[slot_index].append(event)
            continue
        groups[compatible].append(event)
    arc_ordinals: dict[str, int] = {}
    for index, group in enumerate(groups):
        rule_id = str(rules[index].get("rule_id") or "")
        arc_ordinals[rule_id] = arc_ordinals.get(rule_id, 0) + 1
        if not group:
            group.append(_generic_structure_event(
                index + 1, rules[index], arc_ordinals[rule_id],
            ))
    return groups, conflicts, rules


def _assign_events(events: list[dict], shot_count: int) -> list[list[dict]]:
    if shot_count <= 0:
        return []
    if not events:
        return [[] for _ in range(shot_count)]
    expanded = [
        event
        for event in events
        for _ in range(int(event.get("shot_units") or 1))
    ]
    if len(expanded) == shot_count:
        return [[event] for event in expanded]
    groups = [[] for _ in range(shot_count)]
    if len(events) <= shot_count:
        for shot_index in range(shot_count):
            event_index = min(
                len(events) - 1,
                (shot_index * len(events)) // shot_count,
            )
            groups[shot_index].append(events[event_index])
        return groups
    for event_index, event in enumerate(events):
        shot_index = min(shot_count - 1, (event_index * shot_count) // len(events))
        groups[shot_index].append(event)
    return groups


def _project_slots(
    template_slots: list[SourceShot],
    event_groups: list[list[dict]],
) -> list[SourceShot]:
    if not event_groups or not template_slots:
        return []
    projected = []
    count = len(event_groups)
    for index, event_group in enumerate(event_groups, start=1):
        event_text = " ".join(event["title"] for event in event_group)
        source = max(
            enumerate(template_slots),
            key=lambda item: (
                _source_match_score(event_text, item[1]),
                -item[0],
            ),
        )[1]
        arc_slot = template_slots[min(
            len(template_slots) - 1,
            (
                ((index - 1) * (len(template_slots) - 1)) // (count - 1)
                if count > 1 else 0
            ),
        )]
        projected.append(replace(
            source,
            slot_index=index,
            shot_no=str(index),
            structure=arc_slot.structure,
            reference_image_path=(
                arc_slot.reference_image_path or source.reference_image_path
            ),
            reference_image_evidence=(
                arc_slot.reference_image_evidence
                or source.reference_image_evidence
            ),
            effect_reference_path=(
                arc_slot.effect_reference_path or source.effect_reference_path
            ),
            effect_reference_evidence=(
                arc_slot.effect_reference_evidence
                or source.effect_reference_evidence
            ),
            structure_source_file=arc_slot.source_file,
            structure_source_sheet=arc_slot.source_sheet,
            structure_source_row=arc_slot.source_row,
        ))
    return projected


def _source_match_score(event_text: str, slot: SourceShot) -> int:
    source_text = slot.searchable_text()
    score = 100 if event_text and event_text in source_text else 0
    score += sum(
        2 for char in set(event_text) if char.strip() and char in source_text
    )
    keyword_groups = (
        (("入场", "签到", "邀请函"), ("入场", "签到", "入口")),
        (("空间", "场地", "会场"), ("空间", "全景", "会场", "舞台")),
        (("嘉宾", "艺人", "亮相"), ("嘉宾", "艺人", "人物", "亮相")),
        (("产品", "互动", "体验"), ("产品", "互动", "体验", "细节")),
        (("空镜", "外场", "内场"), ("空镜", "全景", "外场", "内场")),
        (("茶歇",), ("茶歇", "交流", "互动")),
        (("发言", "致辞", "演讲"), ("发言", "致辞", "演讲", "宣讲")),
        (("听取", "方案", "趋势", "布局"), ("演讲", "战略", "大屏", "宣讲", "发言", "信息", "口号")),
        (("颁奖", "仪式"), ("颁奖", "仪式", "奖杯", "启动")),
        (("落版", "LOGO", "logo", "收束"), ("落版", "LOGO", "logo", "收束", "主视觉")),
    )
    for event_terms, source_terms in keyword_groups:
        if any(term in event_text for term in event_terms):
            score += 10 * sum(term in source_text for term in source_terms)
    return score


def _shot_durations(
    brief: dict,
    event_groups: list[list[dict]],
    events: list[dict],
    *,
    preferred_durations: list[int] | None = None,
    fixed_durations: dict[int, int] | None = None,
) -> tuple[list[int], str]:
    target = parse_positive_seconds_strict(brief.get("视频时长", "")) or 0
    if preferred_durations is not None:
        count = len(event_groups)
        fixed = fixed_durations or {}
        if (len(preferred_durations) != count or any(value <= 0 for value in preferred_durations)
                or any(index < 0 or index >= count or value <= 0 for index, value in fixed.items())):
            raise ValueError("时长分配输入无效")
        editable = [index for index in range(count) if index not in fixed]
        remaining = target - sum(fixed.values())
        if remaining < len(editable) or not editable and remaining != 0:
            raise ValueError("目标时长不足以保留已固定时长并为其余每段分配至少1秒；请调整目标或解除相关时长保护")
        durations = [fixed.get(index, 1) for index in range(count)]
        # Preserve the proposed pacing while making the total exact. Reserve
        # one second per editable block, then apportion the remaining budget.
        budget = remaining - len(editable)
        weights = [max(0, preferred_durations[index] - 1) for index in editable]
        if editable and not sum(weights):
            weights = [1] * len(editable)
        total_weight = sum(weights)
        remainders = []
        for index, weight in zip(editable, weights):
            whole, remainder = divmod(budget * weight, total_weight)
            durations[index] += whole
            remainders.append((remainder, index))
        for _, index in sorted(remainders, key=lambda item: (-item[0], item[1]))[:target - sum(durations)]:
            durations[index] += 1
        return durations, "reviewed_pacing_target_budget"
    explicit = bool(events) and all(
        _positive_int(event.get("duration_seconds")) is not None for event in events
    )
    direct_event_budget = (
        explicit
        and all(len(group) == 1 for group in event_groups)
        and {group[0]["event_id"] for group in event_groups} == {
            event["event_id"] for event in events
        }
    )
    if direct_event_budget:
        positions_by_event: dict[str, list[int]] = {}
        for index, group in enumerate(event_groups):
            positions_by_event.setdefault(group[0]["event_id"], []).append(index)
        durations = [0 for _ in event_groups]
        for event in events:
            positions = positions_by_event[event["event_id"]]
            base, remainder = divmod(int(event["duration_seconds"]), len(positions))
            for offset, position in enumerate(positions):
                durations[position] = base + (1 if offset < remainder else 0)
        return durations, "explicit_event_budget"
    count = len(event_groups)
    if count <= 0:
        return [], "empty"
    try:
        from .script_type_adapter import metadata_for_script_type
        launch_budget = metadata_for_script_type(brief.get("脚本类型", "")).pacing_profile == "launch_60s"
    except ValueError:
        launch_budget = False
    if target == 60 and count == 18 and launch_budget:
        defaults = list(_LAUNCH_60S_DURATIONS)
    else:
        base, remainder = divmod(target, count)
        defaults = [base + (1 if index < remainder else 0) for index in range(count)]
    fixed = {
        index: int(event["duration_seconds"])
        for index, group in enumerate(event_groups)
        for event in group
        if not event.get("generated_structure_event") and "duration_seconds" in event
    }
    if fixed and sum(fixed.values()) < target and len(fixed) < count:
        remaining_indices = [index for index in range(count) if index not in fixed]
        remaining = target - sum(fixed.values())
        base, remainder = divmod(remaining, len(remaining_indices))
        durations = [0] * count
        for index, value in fixed.items():
            durations[index] = value
        for offset, index in enumerate(remaining_indices):
            durations[index] = base + (1 if offset < remainder else 0)
        return durations, "brief_technique_override_budget"
    if target == 60 and count == 18 and launch_budget:
        return defaults, "approved_launch_60s_budget"
    return defaults, "deterministic_even_budget"


def _shot_plans(
    brief: dict,
    slots: list[SourceShot],
    event_groups: list[list[dict]],
    durations: list[int],
    *,
    creative_standard: dict,
    image_context: list[dict],
    rule_contract: dict,
    conflicts: list[dict],
    structural_slot_rules: list[dict],
) -> list[dict]:
    shots = []
    count = len(event_groups)
    for index, (slot, event_group) in enumerate(zip(slots, event_groups), start=1):
        primary = event_group[0] if event_group else None
        previous_title = (
            event_groups[index - 2][0]["title"]
            if index > 1 and event_groups[index - 2]
            else ""
        )
        next_title = (
            event_groups[index][0]["title"]
            if index < count and event_groups[index]
            else ""
        )
        if index == 1:
            narrative_stage = "opening"
        elif index == count:
            narrative_stage = "closing"
        else:
            narrative_stage = "development"
        source_ref = {
            "source_file": slot.source_file,
            "source_sheet": slot.source_sheet,
            "source_row": slot.source_row,
            "structure_source_file": slot.structure_source_file or slot.source_file,
            "structure_source_sheet": slot.structure_source_sheet or slot.source_sheet,
            "structure_source_row": slot.structure_source_row or slot.source_row,
        }
        brief_text = " ".join(
            str(brief.get(field) or "")
            for field in ("项目名称", "脚本类型", "品牌或客户", "行业或活动类别", "视频目标", "使用场景", "必须出现的内容", "禁忌或不能出现的内容")
        )
        source_text = slot.searchable_text()
        excluded_mechanisms = [
            mechanism
            for mechanism in _SOURCE_MECHANISMS
            if mechanism in source_text and mechanism not in brief_text
        ]
        tone = [item["value"] for item in creative_standard["tone_keywords"]]
        references = [item["value"] for item in creative_standard["references"]]
        image_terms = [
            str(value)
            for image in image_context
            for value in (
                image["caption"], *image["tags"], *image["subjects"], image["scene"],
                image["purpose"], image["quality"], image["brand"],
            )
        ]
        shot = {
            "shot_no": str(index),
            "narrative_stage": narrative_stage,
            "primary_event_id": primary["event_id"] if primary else "",
            "primary_event": primary["title"] if primary else "",
            "event_ids": [event["event_id"] for event in event_group],
            "events": [
                {
                    "event_id": event["event_id"],
                    "text": event.get("text") or event["title"],
                    "role": "primary" if event is primary else "secondary",
                }
                for event in event_group
            ],
            "requirement_ids": list(dict.fromkeys(
                requirement_id
                for event in event_group
                for requirement_id in event.get("requirement_ids") or []
            )),
            "structure": slot.structure,
            "structure_source": "template_fallback",
            "generated_structure_only": all(
                event.get("generated_structure_event") for event in event_group
            ),
            "position_rule": dict(primary.get("position_rule") or {}) if primary else {},
            **({"creative_order_note": primary["creative_order_note"]}
               if primary and primary.get("creative_order_note") else {}),
            **({'structural_assignment': dict(primary['structural_assignment'])}
               if primary and primary.get('structural_assignment') else {}),
            "template_source": source_ref,
            "duration_seconds": durations[index - 1] if index <= len(durations) else 0,
            "brief_evidence": (
                dict(primary["brief_evidence"])
                if primary
                else {"type": "inline_brief", "text": "", "source_ref": {}}
            ),
            "brief_source": _brief_source_ref(brief),
            "functional_source": source_ref,
            "functional_reference_notice": "仅作镜头功能参考，非本项目事实或素材",
            "image_query": " ".join(filter(None, (
                str(brief.get("脚本类型") or ""),
                primary["title"] if primary else "",
                slot.shot_function,
                slot.visual_subject,
                *tone,
                *image_terms,
            ))),
            "creative_standard_tone_keywords": tone,
            "creative_standard_references": references,
            "inheritable_function": slot.shot_function or slot.structure,
            "prohibited_inheritance": "不得继承未被当前Brief确认的案例人物、仪式、互动机制和活动事实",
            "excluded_source_mechanisms": excluded_mechanisms,
            "transition_reason": _transition_reason(previous_title, primary, next_title),
        }
        shot["field_decisions"] = _field_decisions_for_shot(
            shot,
            slot,
            event_group,
            rule_contract,
            conflicts,
            total_shots=count,
            structural_rule=structural_slot_rules[index - 1],
        )
        if image_context:
            shot["project_image_candidates"] = image_context
            shot["project_image_candidate_notice"] = "仅作候选图片业务上下文，不覆盖 Brief 事实；Journey6 决定逐镜最终图片"
        shots.append(shot)
    return shots


def _field_decisions_for_shot(
    shot: dict,
    slot: SourceShot,
    event_group: list[dict],
    rule_contract: dict,
    conflicts: list[dict],
    total_shots: int,
    structural_rule: dict,
) -> list[dict]:
    shot_no = int(shot["shot_no"])
    conflict = next(
        (item for item in conflicts if str(item.get("shot_no")) == str(shot_no)),
        None,
    )
    source_evidence = [{
        "type": "template_source",
        "source_file": slot.source_file,
        "source_sheet": slot.source_sheet,
        "source_row": slot.source_row,
    }]
    brief_evidence = [
        evidence
        for event in event_group
        if not event.get("generated_structure_event")
        for evidence in event.get("evidence_refs") or []
    ]

    def decision(
        field: str,
        final_value: object,
        *,
        selected_source: str,
        status: str = "applied",
        value_status: str = "resolved",
        reason_code: str = "",
        reason: str = "",
        candidates: list[dict] | None = None,
        conflict_id: str = "",
        evidence_refs: list[dict] | None = None,
    ) -> dict:
        return {
            "field": field,
            "phase": "plan",
            "value_status": value_status,
            "final_value": final_value,
            "candidates": candidates or [],
            "selected_source": selected_source,
            "decision_status": status,
            "override_reason_code": reason_code,
            "override_reason": reason,
            "evidence_refs": list(evidence_refs or []),
            "conflict_id": conflict_id,
        }

    event_value = [
        {"event_id": item["event_id"], "text": item["title"], "event_role": item.get("event_role", "")}
        for item in event_group
    ]
    decisions = [decision(
        "events", event_value,
        selected_source=(
            "template_structural_placeholder"
            if all(item.get("generated_structure_event") for item in event_group)
            else "brief"
        ),
        candidates=[{
            "source": "brief" if brief_evidence else "template_rule",
            "value": event_value,
            "rule_id": "",
            "rule_level": "",
            "evidence_refs": brief_evidence or [{"type": "template_rule"}],
        }],
        evidence_refs=brief_evidence or [{"type": "template_rule"}],
    )]
    position_value = dict(shot.get("position_rule") or {})
    decisions.append(decision(
        "position_rule", position_value,
        selected_source="brief" if position_value else "none",
        candidates=[{
            "source": "brief", "value": position_value, "rule_id": "",
            "rule_level": "", "evidence_refs": brief_evidence,
        }] if position_value else [],
        evidence_refs=brief_evidence,
    ))
    structure_rule = dict(structural_rule)
    structure_candidate = {
        "source": "template_rule",
        "value": structure_rule.get("value") or slot.structure,
        "rule_id": structure_rule.get("rule_id") or "",
        "rule_level": "structural_required",
        "evidence_refs": source_evidence,
    }
    if conflict and conflict.get("status") == "unresolved":
        shot["structure_source"] = "conflict"
        decisions.append(decision(
            "structure", "",
            selected_source="",
            status="conflict",
            value_status="conflict",
            candidates=[structure_candidate, {
                "source": "brief", "value": conflict["brief_value"], "rule_id": "",
                "rule_level": "", "evidence_refs": brief_evidence,
            }],
            conflict_id=str(conflict["conflict_id"]),
            evidence_refs=[*source_evidence, *brief_evidence],
        ))
    elif conflict and conflict.get("choice") == "brief_wins":
        decisions.append(decision(
            "structure", str(conflict["brief_value"]),
            selected_source="brief",
            status="downgraded",
            reason_code="confirmed_brief_precedence_over_structural_required",
            reason="用户确认 Brief 位置要求优先，项目级降级硬模板结构",
            candidates=[structure_candidate, {
                "source": "brief", "value": conflict["brief_value"], "rule_id": "",
                "rule_level": "", "evidence_refs": brief_evidence,
            }],
            conflict_id=str(conflict["conflict_id"]),
            evidence_refs=[*source_evidence, *brief_evidence],
        ))
        shot["structure"] = str(conflict["brief_value"])
        shot["structure_source"] = "brief_confirmed_override"
    else:
        decisions.append(decision(
            "structure", str(structure_rule.get("value") or slot.structure),
            selected_source="template_rule",
            candidates=[structure_candidate],
            evidence_refs=source_evidence,
        ))
        shot["structure"] = str(structure_rule.get("value") or slot.structure)
        shot["structure_source"] = "structural_required"

    field_values = {
        "duration_seconds": shot.get("duration_seconds"),
        "effect": slot.effect,
        "camera_type": slot.camera_type,
        "shot_size": slot.shot_size,
        "camera_move": slot.camera_move,
        "sound": slot.sound,
        "visual_description": slot.visual_desc,
        "reference_image": slot.reference_image_path,
    }
    for field, value in field_values.items():
        rule = _rule_for_structural_arc(rule_contract, structural_rule, field)
        level = str(rule.get("level") or "")
        is_example = level == "example_only"
        brief_override = next(
            (
                event.get("duration_seconds")
                if field == "duration_seconds" else
                (event.get("technique_overrides") or {}).get(field)
                for event in event_group
                if (
                    field == "duration_seconds" and event.get("duration_seconds") is not None
                    or field != "duration_seconds"
                    and (event.get("technique_overrides") or {}).get(field) is not None
                )
            ),
            None,
        )
        overridden = brief_override is not None and level == "technique_default"
        decisions.append(decision(
            field,
            "" if is_example else brief_override if overridden else value,
            selected_source=(
                "deferred" if is_example else "brief" if overridden else "template_rule"
            ),
            status="deferred" if is_example else "overridden" if overridden else "applied",
            value_status="deferred" if is_example else "resolved",
            reason_code=(
                "example_only_not_project_fact" if is_example
                else "brief_precedence_over_technique_default" if overridden else ""
            ),
            reason=(
                "案例字段仅作表达参考，等待草稿或项目图片证据" if is_example
                else "Brief 明确技术值覆盖模板默认值" if overridden else ""
            ),
            candidates=[{
                "source": "template_rule",
                "value": value,
                "rule_id": rule.get("rule_id") or "",
                "rule_level": level,
                "evidence_refs": source_evidence,
            }, *([{
                "source": "brief", "value": brief_override, "rule_id": "",
                "rule_level": "", "evidence_refs": brief_evidence,
            }] if overridden else [])],
            evidence_refs=[*source_evidence, *brief_evidence] if overridden else source_evidence,
        ))
    return decisions


def _creative_standard(constraints: dict) -> dict:
    value = constraints.get("creative_standard") if isinstance(constraints, dict) else None
    if value is None:
        return {name: [] for name in ("tone_keywords", "expression_focus", "avoid_expressions", "references")}
    if not isinstance(value, dict) or set(value) != {"tone_keywords", "expression_focus", "avoid_expressions", "references"}:
        raise ValueError("creative_standard 规划约束不完整")
    result = {}
    for name, entries in value.items():
        if not isinstance(entries, list):
            raise ValueError(f"creative_standard.{name} 必须是数组")
        normalized = []
        for entry in entries:
            if not isinstance(entry, dict) or not all(entry.get(key) for key in ("value", "fact_id", "evidence_refs")):
                raise ValueError(f"creative_standard.{name} 条目缺少来源")
            normalized.append({
                "value": str(entry["value"]).strip(),
                "fact_id": str(entry["fact_id"]),
                "evidence_refs": list(entry["evidence_refs"]),
            })
        result[name] = normalized
    return result


def _project_image_context(constraints: dict) -> list[dict]:
    """Keep the seven verified image business fields available to P00, not as facts."""
    value = constraints.get("project_image_context") if isinstance(constraints, dict) else None
    if value is None:
        return []
    if not isinstance(value, dict) or not isinstance(value.get("images"), list):
        raise ValueError("project_image_context 必须来自正式图片规划证据")
    result = []
    for item in value["images"]:
        if not isinstance(item, dict) or not isinstance(item.get("business_summary"), dict):
            raise ValueError("project_image_context 图片摘要无效")
        summary = item["business_summary"]
        if set(summary) != {"caption", "tags", "subjects", "scene", "purpose", "quality", "brand", "candidate_only"}:
            raise ValueError("project_image_context 必须包含完整七类业务字段")
        if summary["candidate_only"] is not True or not all(isinstance(summary[key], str) and summary[key].strip() for key in ("caption", "scene", "purpose", "quality", "brand")):
            raise ValueError("project_image_context 图片摘要无效")
        if not all(isinstance(summary[key], list) and summary[key] and all(isinstance(entry, str) and entry.strip() for entry in summary[key]) for key in ("tags", "subjects")):
            raise ValueError("project_image_context 图片列表摘要无效")
        result.append({
            key: list(summary[key]) if key in {"tags", "subjects"} else summary[key]
            for key in ("caption", "tags", "subjects", "scene", "purpose", "quality", "brand")
        })
    return result


def _apply_creative_standard_audit(
    audit: dict, standard: dict, shots: list[dict],
    requirements: list[dict], coverage: list[dict],
) -> None:
    if not any(standard.values()):
        return
    findings = list(audit.get("findings") or [])
    focus_bindings = []
    requirements_by_id = {
        str(item.get("requirement_id") or ""): item for item in requirements
    }
    for entry in standard["expression_focus"]:
        for focus in _focus_terms(entry["value"]):
            value = _normalize_focus(focus)
            matching = {
                shot["shot_no"] for shot in shots
                if value and value in _normalize_focus(shot.get("primary_event"))
            }
            matching.update(
                shot_no
                for item in coverage
                for shot_no in item.get("shot_nos") or []
                if _semantic_match_score(
                    focus,
                    str(requirements_by_id.get(
                        str(item.get("requirement_id") or ""), {}
                    ).get("text") or ""),
                ) >= 40
            )
            matching = sorted(matching, key=lambda item: int(item))
            if not matching:
                findings.append({
                    "code": "creative_standard_focus_unbound",
                    "severity": "warning",
                    "message": f"表达重点未绑定到任何事件或镜头: {focus}",
                    "fact_id": entry["fact_id"],
                    "evidence_refs": entry["evidence_refs"],
                })
                focus_bindings.append({
                    "value": focus, "fact_id": entry["fact_id"],
                    "evidence_refs": entry["evidence_refs"], "shot_nos": [],
                    "usage": "writer_constraint_unbound",
                })
            else:
                focus_bindings.append({
                    "value": focus, "fact_id": entry["fact_id"],
                    "evidence_refs": entry["evidence_refs"], "shot_nos": matching,
                })
                for shot in shots:
                    if shot["shot_no"] in matching:
                        shot.setdefault("creative_standard_expression_focus", []).append(focus)
                        shot["image_query"] = " ".join((shot["image_query"], focus)).strip()
    avoid_hits = creative_standard_text_hits(
        standard["avoid_expressions"],
        shots,
        fields=("primary_event", "image_query"),
    )
    for hit in avoid_hits:
        findings.append({
            "code": "creative_standard_avoid_expression_hit",
            "severity": "blocker",
            "message": f"规划命中避免表达: {hit['value']}",
            **hit,
        })
    audit["findings"] = findings
    audit["blocking_codes"] = list(dict.fromkeys(
        item["code"] for item in findings if item.get("severity") == "blocker"
    ))
    audit["clarification_codes"] = list(dict.fromkeys(
        item["code"] for item in findings if item.get("severity") == "clarification"
    ))
    audit["warning_codes"] = list(dict.fromkeys(
        item["code"] for item in findings if item.get("severity") == "warning"
    ))
    audit["status"] = "blocked" if audit["blocking_codes"] else "needs_clarification" if audit["clarification_codes"] else "pass"
    audit["creative_standard_consumption"] = {
        "tone_keywords": [
            {"value": item["value"], "fact_id": item["fact_id"], "evidence_refs": item["evidence_refs"], "usage": "planner_and_writer_constraint"}
            for item in standard["tone_keywords"]
        ],
        "expression_focus": focus_bindings,
        "avoid_expressions": [
            {"value": item["value"], "fact_id": item["fact_id"], "evidence_refs": item["evidence_refs"], "usage": "plan_and_draft_deterministic_audit"}
            for item in standard["avoid_expressions"]
        ],
        "references": [
            {"value": item["value"], "fact_id": item["fact_id"], "evidence_refs": item["evidence_refs"], "usage": "style_context_not_brief_fact"}
            for item in standard["references"]
        ],
        "avoid_hits": avoid_hits,
    }


def _normalize_audit_findings(audit: dict) -> None:
    """Keep one public finding shape across planner and standard-card audits."""
    normalized = []
    for raw in list(audit.get("findings") or []):
        item = dict(raw)
        item.setdefault("requirement_ref", str(item.get("fact_id") or ""))
        item.setdefault("risk", str(item.get("message") or ""))
        item.setdefault(
            "cannot_downgrade_reason",
            "该问题会破坏已确认输入、来源绑定或机器合同"
            if item.get("severity") == "blocker" else "",
        )
        normalized.append(item)
    audit["findings"] = normalized


def creative_standard_draft_hits(
    constraints: dict,
    script_rows: list[dict],
    *,
    extra_texts: list[str] | None = None,
) -> list[dict]:
    standard = _creative_standard(constraints)
    shots = [
        {"shot_no": str(row.get("镜号") or index), "text": row}
        for index, row in enumerate(script_rows, start=1)
    ]
    hits = creative_standard_text_hits(
        standard["avoid_expressions"],
        shots,
        fields=("画面描述", "声音", "字幕", "文案"),
    )
    return [
        *hits,
        *creative_standard_text_hits(
            standard["avoid_expressions"],
            [{"shot_no": "overall", "text": {"文案": value}} for value in extra_texts or []],
            fields=("文案",),
        ),
    ]


def creative_standard_text_hits(
    entries: list[dict],
    rows: list[dict],
    *,
    fields: tuple[str, ...],
) -> list[dict]:
    hits = []
    for entry in entries:
        expression = _avoid_expression(entry["value"])
        if not expression:
            continue
        if not _usable_expression(expression):
            continue
        for item in rows:
            text = dict(item.get("text") or item)
            for field in fields:
                value = str(text.get(field) or "")
                if _expression_matches(expression, value):
                    hits.append({
                        "value": entry["value"], "fact_id": entry["fact_id"],
                        "evidence_refs": entry["evidence_refs"], "shot_no": item["shot_no"], "field": field,
                    })
    return hits


def _avoid_expression(value: str) -> str:
    return re.sub(r"^(?:不得|禁止|避免|不(?:要|得|可)?出现)", "", str(value).strip())


def _usable_expression(expression: str) -> bool:
    value = _avoid_expression(expression)
    return len(value) >= 2


def _expression_matches(expression: str, value: str) -> bool:
    """ASCII requires whole-token matches; CJK stays an explicit contiguous phrase."""
    needle = _avoid_expression(expression)
    if not needle or not value:
        return False
    if needle.isascii():
        tokens = re.findall(r"[A-Za-z0-9]+", needle.casefold())
        if not tokens:
            return False
        haystack = re.findall(r"[A-Za-z0-9]+", value.casefold())
        return any(
            haystack[index:index + len(tokens)] == tokens
            for index in range(max(0, len(haystack) - len(tokens) + 1))
        )
    normalized_needle = re.sub(r"[\s，。；、,.!?！？:：\-_/]+", "", needle).casefold()
    normalized_value = re.sub(r"[\s，。；、,.!?！？:：\-_/]+", "", value).casefold()
    return len(normalized_needle) >= 2 and normalized_needle in normalized_value


def _focus_terms(value: str) -> list[str]:
    """Only split an explicitly delimited card field; never infer synonyms."""
    terms = [item.strip() for item in re.split(r"[、，,；;\n]+", str(value or "")) if item.strip()]
    return terms or [str(value).strip()]


def _normalize_focus(value: object) -> str:
    return re.sub(r"[\s，。；、,.!?！？:：\-_/]+", "", str(value or "")).casefold()


def _transition_reason(previous_title: str, primary: dict | None, next_title: str) -> str:
    current = primary["title"] if primary else ""
    if previous_title and next_title:
        return f"承接{previous_title}，完成{current}后转入{next_title}"
    if next_title:
        return f"以{current}建立开场，后续转入{next_title}"
    if previous_title:
        return f"承接{previous_title}，以{current}完成收束"
    return f"独立完成{current}" if current else "缺少可连接事件"


def _audit_plan(
    *,
    brief: dict,
    facts: list[dict],
    events: list[dict],
    shots: list[dict],
    coverage: list[dict],
    projected_slots: list[SourceShot],
    duration_budget: dict,
    source_status: dict,
    count_issue: str,
    requirements: list[dict],
    capacity_report: dict,
    conflicts: list[dict],
    decision_counts: dict,
    rule_contract: dict,
) -> dict:
    findings = []

    def finding(
        code: str,
        severity: str,
        message: str,
        *,
        requirement_ref: str = "",
        risk: str = "",
        cannot_downgrade_reason: str = "",
        **extra,
    ) -> None:
        findings.append({
            "code": code,
            "severity": severity,
            "message": message,
            "requirement_ref": requirement_ref,
            "risk": risk or message,
            "cannot_downgrade_reason": (
                cannot_downgrade_reason if severity == "blocker" else ""
            ),
            **extra,
        })

    for code in rule_contract.get("warning_codes") or []:
        finding(
            str(code),
            "warning",
            "当前素材包未内置规则文件，已使用同类别版本化默认规则",
            risk="正式验收前应发布带规则 SHA 的新素材包版本",
        )
    for conflict in conflicts:
        if conflict.get('type') == 'creative_brief_flow_conflict':
            finding('creative_brief_flow_conflict', 'clarification', conflict['message'],
                requirement_ref=conflict['requirement_id'], conflict_id=conflict['conflict_id'],
                risk='自动改动任一顺序都会改变已选初稿或已确认流程')
            continue
        if conflict.get("status") == "unresolved":
            finding(
                "hard_template_conflict_unresolved",
                "clarification",
                (f"镜{conflict.get('shot_no')}的内容类型超出模板结构承载范围"
                 if conflict.get("type") == "template_role_capacity_conflict" else
                 f"镜{conflict.get('shot_no')}的 Brief 位置要求与硬模板结构不兼容"),
                requirement_ref=str(conflict.get("requirement_id") or ""),
                risk="自动选择任一侧都会改变已确认 Brief 或模板硬结构",
                conflict_id=str(conflict.get("conflict_id") or ""),
                shot_no=str(conflict.get("shot_no") or ""),
            )
        elif conflict.get("choice") == "brief_wins":
            finding(
                "hard_template_downgraded_by_confirmation",
                "warning",
                f"镜{conflict.get('shot_no')}已按用户确认让 Brief 覆盖硬模板",
                requirement_ref=str(conflict.get("requirement_id") or ""),
                risk="该项目的结构弧已发生有依据的局部降级",
                conflict_id=str(conflict.get("conflict_id") or ""),
            )
        elif conflict.get("choice") == "template_wins":
            finding(
                "brief_position_overridden_by_confirmation",
                "warning",
                f"镜{conflict.get('shot_no')}已按用户确认保留硬模板并派生 Brief 决定",
                requirement_ref=str(conflict.get("requirement_id") or ""),
                risk="原 Brief 证据保留，但该位置要求不进入本次规划",
                conflict_id=str(conflict.get("conflict_id") or ""),
            )
    if decision_counts.get("unexplained_overrides"):
        finding(
            "field_override_unexplained",
            "blocker",
            "存在缺少原因、证据或规则 ID 的字段覆盖",
            cannot_downgrade_reason="无法解释的字段覆盖会破坏生产追踪与审计可信度",
        )
    for shot in shots:
        for decision in shot.get("field_decisions") or []:
            if (
                decision.get("selected_source") == "template_rule"
                and decision.get("value_status") == "resolved"
                and any(
                    candidate.get("rule_level") == "example_only"
                    for candidate in decision.get("candidates") or []
                )
            ):
                finding(
                    "example_only_promoted_to_project_fact",
                    "blocker",
                    f"镜{shot.get('shot_no')}{decision.get('field')}将模板案例当成了项目事实",
                    risk="可能把其他项目的人物、画面或品牌信息带入当前产物",
                    cannot_downgrade_reason="example_only 合同明确禁止作为项目事实",
                    shot_no=str(shot.get("shot_no") or ""),
                    field=str(decision.get("field") or ""),
                )

    missing_facts = [fact["field"] for fact in facts if fact["required"] and fact["status"] == "missing"]
    if missing_facts:
        finding(
            "fact_missing",
            "clarification",
            "Brief缺少必需事实: " + "、".join(missing_facts),
            fields=missing_facts,
        )
    if any(fact["fact_id"] == "required_content" and fact["status"] == "missing" for fact in facts):
        finding("required_content_unspecified", "warning", "未声明额外必现内容；如有必须呈现的事实，请补充后重新规划")
    creative_events = [event["event_id"] for event in events if event["fact_status"] == "creative_expression"]
    if creative_events:
        finding("creative_expression_requires_review", "warning",
            "已接受初稿表达按原文保留，仍需人工核对内容与事实；本次规划不创建事实确认或质量通过证据",
            event_ids=creative_events)
    for shot in shots:
        if shot.get("creative_order_note"):
            finding("creative_order_template_review", "warning", shot["creative_order_note"],
                    shot_no=shot["shot_no"],
                    risk="初稿画面是已选创作表达，模板结构仍为制作参考，须在规划确认时核对")
    pending_events = [event["event_id"] for event in events if event["fact_status"] not in {"confirmed", "creative_expression"}]
    if pending_events:
        finding(
            "fact_missing",
            "clarification",
            "事件含待确认事实，不得由模型补齐",
            event_ids=pending_events,
        )
    if not events:
        finding("event_missing", "clarification", "Brief未提供可执行事件")
    declared_orders = [event.get("declared_order") for event in events]
    if any(value is not None for value in declared_orders):
        complete_order = all(value is not None for value in declared_orders)
        monotonic_order = complete_order and declared_orders == sorted(set(declared_orders))
        if not monotonic_order:
            permitted = all(event.get("allow_non_linear") for event in events)
            reasons_complete = permitted and all(
                str(event.get("non_linear_reason") or "").strip() for event in events
            )
            if not reasons_complete:
                finding(
                    "event_order_inconsistent",
                    "blocker",
                    "事件顺序重复、缺失或倒置，且没有完整的非线性叙事理由",
                )
    overloaded = [shot for shot in shots if len(shot["event_ids"]) > 1]
    for shot in overloaded:
        finding(
            "event_overload",
            "warning",
            f"镜{shot['shot_no']}压缩承载{len(shot['event_ids'])}个流程事件",
            shot_no=shot["shot_no"],
            event_ids=shot["event_ids"],
            risk="单镜信息密度较高，需人工核对节奏与可读性",
        )
    requirements_by_id = {
        str(item["requirement_id"]): item for item in requirements
    }
    resolved_or_unresolved_conflict_requirement_ids = {
        str(item.get("requirement_id") or "")
        for item in conflicts
    }
    for item in coverage:
        requirement = requirements_by_id.get(str(item["requirement_id"]), {})
        if requirement.get("origin") == "review_proposed":
            finding(
                "review_preference_confirmation_required",
                "clarification",
                "复核新增偏好尚未由用户确认，不得覆盖原 Brief",
                requirement_ref=item["requirement_id"],
                risk="未确认复核意见若直接执行会改变原需求",
            )
            continue
        if item["status"] == "needs_fact":
            finding(
                "requirement_needs_fact",
                "clarification",
                "强制条款包含待确认事实" if item["kind"].startswith("mandatory_") else "流程事件包含待确认事实",
                requirement_ref=item["requirement_id"],
                risk="事实未确认时生成可能引入虚构内容",
            )
        elif (
            requirement.get("mandatory")
            and item["status"] == "uncovered"
            and item.get("kind") != "production_requirement"
        ):
            finding(
                "mandatory_requirement_uncovered",
                "blocker",
                "强制条款未绑定到任何规划镜头",
                requirement_ref=item["requirement_id"],
                risk="确认生成后可能遗漏客户明确要求",
                cannot_downgrade_reason="强制条款缺失属于可验证的生产合同违约",
            )
        rule = dict(requirement.get("position_rule") or {})
        expected = (
            [str(shots[0]["shot_no"])] if shots and rule.get("type") == "opening"
            else [str(shots[-1]["shot_no"])] if shots and rule.get("type") == "closing"
            else [str(rule.get("shot_no"))] if rule.get("type") == "fixed_shot"
            else []
        )
        if (
            expected
            and item.get("shot_nos") != expected
            and str(item.get("requirement_id") or "")
            not in resolved_or_unresolved_conflict_requirement_ids
        ):
            finding(
                "position_contract_violated",
                "blocker",
                "明确位置要求未绑定到指定镜头",
                requirement_ref=item["requirement_id"],
                risk="成片叙事位置与客户明确要求不一致",
                cannot_downgrade_reason="明确开头、结尾或固定镜号属于可验证的位置合同",
                expected_shot_nos=expected,
                actual_shot_nos=list(item.get("shot_nos") or []),
            )
    if count_issue:
        count_messages = {
            "invalid_planned_shot_count": ("blocker", "planned_shot_count必须是正整数"),
            "invalid_brief_shot_count": ("blocker", "Brief明确镜数必须是正整数"),
            "conflicting_brief_shot_counts": ("clarification", "Brief中出现互相矛盾的总镜数表达，请确认唯一目标镜数"),
            "approximate_brief_shot_count": ("clarification", "Brief只给出近似镜数，请确认唯一目标镜数"),
        }
        severity, message = count_messages.get(
            count_issue, ("blocker", "镜数约束无效")
        )
        finding(count_issue, severity, message)
    if len(projected_slots) != len(shots):
        finding(
            "shot_count_inconsistent",
            "blocker",
            "投影功能槽与镜头规划数量不一致",
        )
    if capacity_report.get("status") == "needs_clarification":
        finding(
            "capacity_requires_choice",
            "clarification",
            "目标时长不足以容纳全部独立流程和强制内容，需要用户选择处理方式",
            risk="继续生成会压缩或覆盖已确认内容",
            capacity_report=capacity_report,
        )
    elif any(shot["duration_seconds"] <= 0 for shot in shots):
        finding("duration_budget_insufficient", "blocker", "总时长不足以为每镜分配正整数秒")
    over_capacity = [
        shot["shot_no"]
        for shot in shots
        if shot["duration_seconds"] > duration_budget["max_shot_seconds"]
    ]
    if over_capacity:
        finding(
            "shot_duration_over_capacity",
            "blocker",
            "单镜规划时长超过"
            f"{duration_budget['max_shot_seconds']}秒容量: " + "、".join(over_capacity),
            shot_nos=over_capacity,
        )
    explicit_seconds = duration_budget.get("explicit_event_seconds")
    if (
        duration_budget["planned_seconds"] != duration_budget["target_seconds"]
        or explicit_seconds is not None
        and explicit_seconds != duration_budget["target_seconds"]
    ):
        finding(
            "duration_budget_mismatch",
            "blocker",
            f"规划{duration_budget['planned_seconds']}秒，"
            f"事件声明{explicit_seconds if explicit_seconds is not None else '未指定'}秒，"
            f"Brief目标{duration_budget['target_seconds']}秒",
        )
    for shot in shots:
        source = shot["functional_source"]
        if not all((source.get("source_file"), source.get("source_sheet"), source.get("source_row"))):
            finding(
                "functional_source_incomplete",
                "blocker",
                f"镜{shot['shot_no']}缺少功能来源三元组",
                shot_no=shot["shot_no"],
            )
        if not str(shot["brief_evidence"].get("text") or "").strip():
            finding(
                "brief_evidence_missing",
                "clarification",
                f"镜{shot['shot_no']}缺少Brief证据",
                shot_no=shot["shot_no"],
            )
        if shot.get("excluded_source_mechanisms"):
            finding(
                "source_mechanism_excluded",
                "warning",
                f"镜{shot['shot_no']}已排除Brief未确认的案例机制: "
                + "、".join(shot["excluded_source_mechanisms"]),
                shot_no=shot["shot_no"],
                mechanisms=shot["excluded_source_mechanisms"],
            )
    for left_index, left in enumerate(shots):
        for right in shots[left_index + 1:]:
            if any(
                str(event.get("event_id") or "").startswith("template_slot_")
                for event in [*(left.get("events") or []), *(right.get("events") or [])]
            ):
                continue
            if left.get("primary_event_id") == right.get("primary_event_id"):
                continue
            if _semantic_match_score(left.get("primary_event", ""), right.get("primary_event", "")) < 40:
                continue
            finding(
                "semantic_event_duplicate",
                "warning",
                f"镜{left['shot_no']}与镜{right['shot_no']}存在非预期语义重复",
                risk="重复内容可能挤占其他流程或造成叙事反复",
                shot_nos=[left["shot_no"], right["shot_no"]],
            )
    if source_status["source_view"] != "ai-spec":
        finding(
            "legacy_human_source_view",
            "blocker",
            "生产规划必须使用已同步且全部ready的AI_规范表；human仅保留旧流程兼容",
        )

    blocking_codes = list(dict.fromkeys(
        item["code"] for item in findings if item["severity"] == "blocker"
    ))
    clarification_codes = list(dict.fromkeys(
        item["code"] for item in findings if item["severity"] == "clarification"
    ))
    warning_codes = list(dict.fromkeys(
        item["code"] for item in findings if item["severity"] == "warning"
    ))
    status = "blocked" if blocking_codes else "needs_clarification" if clarification_codes else "pass"
    return {
        "status": status,
        "blocking_codes": blocking_codes,
        "clarification_codes": clarification_codes,
        "warning_codes": warning_codes,
        "findings": findings,
        "summary": {
            "fact_count": len(facts),
            "event_count": len(events),
            "shot_count": len(shots),
            "target_duration_seconds": duration_budget["target_seconds"],
            "planned_duration_seconds": duration_budget["planned_seconds"],
        },
    }


def _brief_source_ref(brief: dict) -> dict:
    source_file = str(brief.get("Brief来源文件") or "").strip()
    source_sheet = str(brief.get("Brief来源 sheet") or "").strip()
    source_row = brief.get("Brief来源行")
    source_sha256 = str(brief.get("Brief来源 SHA256") or "").strip()
    if not all((source_file, source_sheet, source_row, source_sha256)):
        return {}
    return {
        "source_file": source_file,
        "source_sheet": source_sheet,
        "source_row": source_row,
        "source_sha256": source_sha256,
    }


def _split_event_text(value) -> list[str]:
    return [
        _clean_event_title(part)
        for part in _split_quoted_clauses(value, "、,，;；\n→⇒。")
        if _clean_event_title(part) and not _is_duration_annotation(part)
    ]


def _split_requirement_text(value) -> list[str]:
    return _split_quoted_clauses(value, "、,，;；\n")


def _event_shot_units(title: str) -> int:
    text = str(title or "")
    if any(term in text for term in ("logo", "LOGO", "标识", "落版", "片尾", "END", "KV")):
        return 1
    if any(term in text for term in (
        "空间", "场地", "会场", "展台", "产品", "互动", "体验",
        "竞技", "比赛", "对抗", "表演", "致辞", "演讲", "颁奖", "仪式",
    )):
        return 3
    return 2


def _clean_event_title(value) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^\s*\d+\s*镜(?:头)?\s*(?:顺序|流程)\s*[:：]\s*", "", text)
    return re.sub(r"^\s*(?:\d+[.\u3001)\uff09]|[\uff08(]\d+[\uff09)])\s*", "", text).strip()


def _positive_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 and str(value).strip() == str(result) else None


def _ceil_div(value: int, divisor: int) -> int:
    if value <= 0 or divisor <= 0:
        return 0
    return (value + divisor - 1) // divisor
