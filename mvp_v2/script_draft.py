from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable

from .run_contract import sha256_json
from .run_state import write_json_atomic
from .text_providers import (
    TextProviderError,
    TextProviderResponse,
    _redact_diagnostics,
    _text_sha256,
    _transport_evidence,
    build_text_provider,
    minimax_cli_provider,
)
from .script_type_adapter import category_id_for_script_type, normalize_script_type
from .image_retrieval import search_images_for_slot
from .audit import (
    _is_formal_evidence_ref,
    _planned_event_allows_slot_term,
    _required_term_present,
)
from .template_logic import slot_logic_for_source


SCHEMA_VERSION = "mplus-script-draft-response/v1"
PROMPT_VERSION = "mplus-client-review-prompt/v13"
GROUNDING_VERSION = "mplus-required-grounding/v7"
MAX_OUTPUT_TOKENS = 65536
TEXT_TIMEOUT_SECONDS = 600
MAX_PENDING_CONFIRMATIONS = 6
LOW_VALUE_PENDING_TERMS = (
    "字体", "样式", "尺寸", "分辨率", "相机型号", "设备型号", "数量", "人数",
    "时长", "布局", "位置", "进度", "开放时间", "预估", "站位", "取景范围",
)
INTERNAL_PHRASES = (
    "Brief要求",
    "AI生成",
    "AI合成",
    "AI制作",
    "AI画面",
    "候选方案",
    "候选镜头",
    "镜头明确呈现",
)


class ScriptDraftError(ValueError):
    pass


class ScriptDraftProviderError(ScriptDraftError):
    pass


TextProvider = Callable[[str, str, int], str]


def create_script_draft(
    plan,
    *,
    quality_profile: str,
    text_model: str,
    text_command: str,
    cache_dir: str | Path,
    provider: TextProvider | None = None,
    writer_provider: TextProvider | None = None,
    editor_provider: TextProvider | None = None,
    writer_provider_name: str = "minimax-cli",
    editor_provider_name: str = "minimax-cli",
    writer_model: str | None = None,
    editor_model: str | None = None,
    image_manifest_rows: list[dict] | None = None,
    image_analysis_rows: list[dict] | None = None,
) -> dict:
    if quality_profile not in {"draft", "client-review"}:
        raise ScriptDraftError(f"未知质量模式: {quality_profile}")
    source = _model_source(
        plan,
        image_manifest_rows=image_manifest_rows,
        image_analysis_rows=image_analysis_rows,
    )
    logic_coverage = _asset_logic_coverage(source)
    if quality_profile == "draft":
        return {
            "schema_version": SCHEMA_VERSION,
            "quality_profile": quality_profile,
            "text_model": text_model,
            "overall_summary": "",
            "script_rows": [dict(row) for row in plan.script_rows],
            "pending_confirmations": [],
            "usage": {
                "live_call_count": 0,
                "cache_hit_count": 0,
                "writer_cache_hit": False,
                "editor_cache_hit": False,
                **logic_coverage,
            },
        }

    _validate_asset_logic_source(source)
    writer_model = str(writer_model or text_model)
    editor_model = str(editor_model or text_model)
    shared_provider = provider
    try:
        writer_invoke = writer_provider or shared_provider or _provider_for_name(
            writer_provider_name, text_command
        )
        editor_invoke = editor_provider or shared_provider or _provider_for_name(
            editor_provider_name, text_command
        )
    except TextProviderError as exc:
        raise ScriptDraftProviderError(str(exc)) from exc
    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    writer_prompt = _writer_prompt(source)
    writer, writer_hit, writer_cache_path = _cached_call(
        cache_root,
        pass_name="writer",
        model=writer_model,
        prompt=writer_prompt,
        provider=writer_invoke,
        provider_name=writer_provider_name,
    )
    writer_payload, writer_json_repair_count = _process_model_response(
        writer, source, plan, cache_path=writer_cache_path, cache_hit=writer_hit,
        pass_name="writer", model=writer_model, provider_name=writer_provider_name,
        prompt=writer_prompt, final=False,
    )
    if writer_json_repair_count:
        _record_json_repair(
            cache_root,
            pass_name="writer",
            model=writer_model,
            provider_name=writer_provider_name,
            cache_path=writer_cache_path,
            repair_count=writer_json_repair_count,
        )

    editor_prompt = _editor_prompt(source, writer_payload)
    editor, editor_hit, editor_cache_path = _cached_call(
        cache_root,
        pass_name="editor",
        model=editor_model,
        prompt=editor_prompt,
        provider=editor_invoke,
        provider_name=editor_provider_name,
    )
    editor_payload, editor_json_repair_count = _process_model_response(
        editor, source, plan, cache_path=editor_cache_path, cache_hit=editor_hit,
        pass_name="editor", model=editor_model, provider_name=editor_provider_name,
        prompt=editor_prompt, final=True,
    )
    if editor_json_repair_count:
        _record_json_repair(
            cache_root,
            pass_name="editor",
            model=editor_model,
            provider_name=editor_provider_name,
            cache_path=editor_cache_path,
            repair_count=editor_json_repair_count,
        )
    rows = _apply_response(plan.script_rows, editor_payload)
    return {
        "schema_version": SCHEMA_VERSION,
        "quality_profile": quality_profile,
        "text_model": editor_model,
        "writer_provider": writer_provider_name,
        "writer_model": writer_model,
        "editor_provider": editor_provider_name,
        "editor_model": editor_model,
        "overall_summary": str(editor_payload.get("overall_summary") or "").strip(),
        "script_rows": rows,
        "pending_confirmations": _pending_confirmations(editor_payload),
        "usage": {
            "live_call_count": int(not writer_hit) + int(not editor_hit),
            "cache_hit_count": int(writer_hit) + int(editor_hit),
            "writer_cache_hit": writer_hit,
            "editor_cache_hit": editor_hit,
            "writer_json_repair_count": writer_json_repair_count,
            "editor_json_repair_count": editor_json_repair_count,
            "local_json_repair_count": (
                writer_json_repair_count + editor_json_repair_count
            ),
            "writer_provider": writer_provider_name,
            "writer_model": writer_model,
            "editor_provider": editor_provider_name,
            "editor_model": editor_model,
            **logic_coverage,
        },
    }


def _model_source(
    plan,
    *,
    image_manifest_rows: list[dict] | None = None,
    image_analysis_rows: list[dict] | None = None,
) -> dict:
    brief_keys = (
        "项目名称",
        "脚本类型",
        "品牌或客户",
        "行业或活动类别",
        "视频时长",
        "风格调性",
        "视频目标",
        "使用场景",
        "必须出现的内容",
        "禁忌或不能出现的内容",
        "特殊需求",
        "备注",
    )
    script_type = normalize_script_type(plan.brief.get("脚本类型") or "发布会")
    rows = []
    plans_by_shot = {
        str(item.get("shot_no") or ""): item
        for item in getattr(plan, "shot_plans", [])
    }
    structure_arc = []
    for shot_index, (script, detail, source_ref) in enumerate(zip(
        plan.script_rows,
        plan.detail_rows,
        plan.source_refs,
    ), start=1):
        planned = (
            plan.shot_plans[shot_index - 1]
            if shot_index <= len(getattr(plan, "shot_plans", []))
            else plans_by_shot.get(str(script.get("镜号") or ""), {})
        )
        generated_structure_only = bool(
            planned and planned.get("generated_structure_only")
        )
        structure = (
            "技术结构推进"
            if generated_structure_only
            else str(script.get("结构") or "")
        )
        if structure and (not structure_arc or structure_arc[-1] != structure):
            structure_arc.append(structure)
        slot_logic = (
            slot_logic_for_source(detail, len(plan.detail_rows))
            if script_type == "发布会"
            else None
        )
        required_content = _required_brief_content(detail)
        if generated_structure_only:
            structure_label = str(planned.get("primary_event") or "").strip()
            required_content = [term for term in required_content if term != structure_label]
        shot = {
            "shot_no": str(script.get("镜号") or ""),
            "structure": structure,
            "function": str(
                "中性构图与节奏推进"
                if generated_structure_only else detail.get("槽位功能")
                or detail.get("镜头功能")
                or detail.get("视觉功能")
                or script.get("结构")
                or ""
            ),
            "duration_seconds": script.get("时长(秒)", ""),
            "effect": str(script.get("效果") or ""),
            "camera_type": str(script.get("类型") or ""),
            "shot_size": str(script.get("景别") or ""),
            "camera_move": str(script.get("运镜") or ""),
            "basis": str(
                detail.get("原始画面描述")
                or detail.get("采用画面描述")
                or script.get("画面描述")
                or ""
            ),
            "sound_basis": str(script.get("声音") or ""),
            "editing_logic": {
                "intent": (
                    "仅作为技术结构槽位，不引入 Brief 外事实"
                    if generated_structure_only
                    else str(detail.get("槽位选择原因") or "")
                ),
                "allowed_materials": (
                    "中性环境、构图和节奏动作"
                    if generated_structure_only
                    else str(detail.get("允许素材") or "")
                ),
                "blocked_materials": str(detail.get("禁止素材") or ""),
                "forbidden_terms": "、".join(slot_logic.forbidden_terms) if slot_logic else "",
                "recommended_shot_sizes": str(detail.get("推荐景别") or ""),
                "recommended_camera_moves": str(detail.get("推荐运镜") or ""),
                "recommended_sounds": (
                    "环境声或节奏音"
                    if generated_structure_only
                    else str(detail.get("推荐声音") or "")
                ),
                "rhythm": str(detail.get("节奏说明") or ""),
                "required_visual_evidence": str(detail.get("必须视觉证据") or ""),
                "repeat_policy": str(detail.get("重复素材规则") or ""),
            },
            "source_evidence": str(detail.get("采用来源证据") or ""),
            "required_brief_content": required_content,
            "source": f"{source_ref['source_sheet']}!{source_ref['source_row']}",
        }
        decisions = getattr(plan, 'decisions', ())
        if shot_index <= len(decisions):
            decision = decisions[shot_index - 1]
            source = decision.variant if decision.variant and decision.mode in {'REPLACE', 'ADAPT'} else decision.slot
            shot['source_reference'] = {key: getattr(source, key) for key in (
                'generic_desc', 'visual_desc', 'brand_judgement_original', 'source_brand',
                'source_file', 'source_sheet', 'source_row', 'source_package_id', 'source_package_version', 'source_category_id')}
        if planned:
            if planned.get("creative_block"):
                accepted = planned["creative_block"]["fields"]
                shot["accepted_creative_fields"] = {
                    "visual_description": accepted["画面描述"], "sound": accepted["声音"],
                    "voiceover": "", "subtitle": "",
                }
            planned_events = [
                {
                    "event_id": str(item.get("event_id") or ""),
                    "text": str(item.get("text") or ""),
                    "role": str(item.get("role") or "secondary"),
                }
                for item in planned.get("events") or []
            ]
            shot["planning_contract"] = {
                "narrative_stage": str(planned.get("narrative_stage") or ""),
                "primary_event": str(planned.get("primary_event") or ""),
                "events": planned_events,
                "requirement_ids": list(planned.get("requirement_ids") or []),
                "structure_source": str(planned.get("structure_source") or ""),
                "generated_structure_only": bool(
                    planned.get("generated_structure_only")
                ),
                "position_rule": dict(planned.get("position_rule") or {}),
                "brief_evidence": {
                    "type": "confirmed_brief",
                    "text": str(planned.get("primary_event") or ""),
                },
                "functional_source": (
                    {"usage": "technical_structure_only"}
                    if generated_structure_only
                    else dict(planned.get("functional_source") or {})
                ),
                "functional_reference_notice": str(
                    planned.get("functional_reference_notice") or ""
                ),
                "excluded_source_mechanisms": list(
                    planned.get("excluded_source_mechanisms") or []
                ),
                "prohibited_inheritance": str(
                    planned.get("prohibited_inheritance") or ""
                ),
                "transition_reason": str(planned.get("transition_reason") or ""),
                "locked": True,
            }
            if planned.get("field_decisions"):
                primary_event = str(planned.get("primary_event") or structure or "结构建立")
                shot["basis"] = (
                    f"第{shot['shot_no']}镜，{primary_event}；"
                    "使用中性环境、构图或节奏动作"
                    if planned.get("generated_structure_only")
                    else f"第{shot['shot_no']}镜，{structure}：{primary_event}"
                )
        if image_manifest_rows and image_analysis_rows:
            query_slot = dict(detail)
            query_slot.update(script)
            result = search_images_for_slot(
                query_slot,
                plan.brief,
                image_manifest_rows,
                image_analysis_rows,
                top_k=3,
            )
            shot["indexed_visual_candidates"] = [
                {
                    "image_id": str(item.get("image_id") or ""),
                    "score": int(item.get("score") or 0),
                    "summary": str(item.get("summary") or ""),
                    "tags": list(item.get("tags") or []),
                    "source": "#".join(str(item.get(key) or "") for key in (
                        "来源文件", "来源 sheet", "来源行",
                    )),
                    "judgement": str(item.get("judgement") or ""),
                }
                for item in result.get("candidates") or []
            ]
        rows.append(shot)
    if not any(row.get("planning_contract") for row in rows):
        _route_required_content_to_indexed_visuals(rows)
    return {
        "category_id": category_id_for_script_type(script_type),
        "script_type": script_type,
        "shot_count": len(rows),
        "required_shot_nos": [row["shot_no"] for row in rows],
        "brief": {
            key: plan.brief.get(key, "")
            for key in brief_keys
            if plan.brief.get(key)
        },
        "confirmed_required_brief_content": _confirmed_formal_required_terms(plan.brief),
        "requirements": _model_safe_contract(
            list(getattr(plan, "requirements", []) or [])
        ),
        "coverage": _model_safe_contract(
            list(getattr(plan, "coverage", []) or [])
        ),
        "source_file": plan.source_refs[0]["source_file"] if plan.source_refs else "",
        "editing_logic_contract": {
            "source_kind": "approved_asset_library",
            "script_type": script_type,
            "structure_arc": structure_arc,
            "locked_fields": [
                "shot_no",
                "structure",
                "duration_seconds",
                "effect",
                "camera_type",
                "shot_size",
                "camera_move",
                "source",
            ],
        },
        "creative_standard": _writer_creative_standard(
            plan.constraints.get("creative_standard")
        ),
        "project_image_context": _writer_image_context(
            plan.constraints.get("project_image_context")
        ),
        **_writer_semantic_context(frozen_creative_context(
            plan.constraints, getattr(plan, 'template_rule_contract', None))),
        "shots": rows,
        "revision_constraints": list(plan.constraints.get("user_feedback") or []),
    }


def _model_safe_contract(values: list[dict]) -> list[dict]:
    """Keep business evidence coordinates while excluding internal refs and hashes."""
    result = []
    for raw in values:
        item = {key: value for key, value in dict(raw).items() if key != "evidence_refs"}
        evidence_refs = []
        for evidence in raw.get("evidence_refs") or []:
            value = dict(evidence or {})
            source_raw = dict(value.get("source") or value.get("source_ref") or {})
            source = {
                key: source_raw[key]
                for key in ("page", "sheet", "row", "column", "slide", "paragraph", "image_index")
                if key in source_raw
            }
            source_file = str(source_raw.get("file") or source_raw.get("source_file") or "")
            if source_file and not source_file.startswith("artifact://"):
                source["file"] = Path(source_file).name
            safe = {
                key: content
                for key, content in {
                    "type": value.get("type"),
                    "field": value.get("field"),
                    "source": source or None,
                }.items()
                if content not in (None, "", {})
            }
            evidence_refs.append(safe)
        item["evidence_refs"] = evidence_refs
        result.append(item)
    return result


def _writer_image_context(value: object) -> dict:
    """Project the immutable planning evidence to business-only model context."""
    if value is None:
        return {"candidate_only": True, "images": []}
    if not isinstance(value, dict) or not isinstance(value.get("images"), list):
        raise ScriptDraftError("项目图片上下文不是受控规划证据")
    images = []
    for item in value["images"]:
        if not isinstance(item, dict) or not isinstance(item.get("business_summary"), dict):
            raise ScriptDraftError("项目图片业务摘要无效")
        summary = item["business_summary"]
        fields = ("caption", "tags", "subjects", "scene", "purpose", "quality", "brand")
        if (
            summary.get("candidate_only") is not True
            or set(summary) != {*fields, "candidate_only"}
            or not all(isinstance(summary[name], str) and summary[name].strip() for name in ("caption", "scene", "purpose", "quality", "brand"))
            or not all(isinstance(summary[name], list) and summary[name] and all(isinstance(entry, str) and entry.strip() for entry in summary[name]) for name in ("tags", "subjects"))
        ):
            raise ScriptDraftError("项目图片业务摘要无效")
        images.append({
            name: list(summary[name]) if name in {"tags", "subjects"} else summary[name]
            for name in fields
        })
    return {"candidate_only": True, "images": images}


def _writer_creative_standard(value: object) -> dict:
    if value is None:
        return {name: [] for name in ("tone_keywords", "expression_focus", "avoid_expressions", "references")}
    if not isinstance(value, dict):
        raise ScriptDraftError("创作标准卡上下文无效")
    result = {}
    for name in ("tone_keywords", "expression_focus", "avoid_expressions", "references"):
        entries = value.get(name)
        if not isinstance(entries, list) or not all(isinstance(entry, dict) and isinstance(entry.get("value"), str) and entry["value"].strip() for entry in entries):
            raise ScriptDraftError("创作标准卡上下文无效")
        result[name] = [entry["value"].strip() for entry in entries]
    if set(value) != set(result):
        raise ScriptDraftError("创作标准卡上下文字段无效")
    return result


def _writer_semantic_context(value):
    """Keep full semantic text; audit identities stay in the frozen artifact."""
    if isinstance(value, dict):
        return {key: _writer_semantic_context(item) for key, item in value.items()
                if 'sha256' not in key.casefold() and key not in {
                    'workspace_id', 'project_id', 'artifact_ref', 'payload_ref',
                    'evidence_refs', 'local_path', 'snapshot_ref'}}
    if isinstance(value, list):
        return [_writer_semantic_context(item) for item in value]
    if isinstance(value, str) and value.startswith('artifact://'):
        return ''
    return value


def frozen_creative_context(constraints: dict, template_rule_contract: dict | None = None) -> dict:
    """Common full-text frozen basis for initial writing and later edits."""
    from copy import deepcopy
    package = constraints.get("formal_asset_package") or {}
    return {
        "effective_knowledge": _writer_effective_knowledge(constraints.get("effective_knowledge")),
        "template_semantics": deepcopy(package.get("source_semantics") or {"status": "not_recorded"}),
        "template_rules": deepcopy((template_rule_contract or constraints.get("template_rule_contract") or {}).get("payload", {})),
        "material_bindings": deepcopy(package.get("library_bindings", [])),
    }


def _writer_effective_knowledge(value: object) -> dict:
    snapshot = dict(value) if isinstance(value, dict) else {}
    rules = []
    for raw in snapshot.get("rules") or []:
        if not isinstance(raw, dict):
            continue
        statement = str(raw.get("statement") or "").strip()
        scope = str(raw.get("scope") or "").strip()
        if statement and scope:
            rules.append({**raw, "scope": scope, "statement": statement})
    return {**snapshot, "rules": rules}


def _required_brief_content(detail: dict) -> list[str]:
    text = str(detail.get("缺口说明") or "")
    return list(dict.fromkeys(
        value
        for value in (
            str(detail.get("规划主事件") or "").strip(),
            *(
                match.strip()
                for match in re.findall(r"Brief规划内容：([^;；,，]+)", text)
                if match.strip()
            ),
        )
        if value
    ))


def _confirmed_formal_required_terms(brief: dict) -> list[str]:
    formal = brief.get("_formal_fact_evidence")
    required = formal.get("必须出现的内容") if isinstance(formal, dict) else None
    refs = required.get("evidence_refs") if isinstance(required, dict) else None
    if (
        not isinstance(required, dict)
        or not str(required.get("fact_id") or "").strip()
        or not isinstance(refs, list)
        or not refs
        or not all(_is_formal_evidence_ref(ref) for ref in refs)
    ):
        return []
    return [
        term.strip()
        for term in re.split(r"[、,，;；\n]+", str(brief.get("必须出现的内容") or ""))
        if term.strip()
    ]


def _route_required_content_to_indexed_visuals(rows: list[dict]) -> None:
    assignments = [
        (index, term)
        for index, row in enumerate(rows)
        for term in row.get("required_brief_content") or []
    ]
    for current_index, term in assignments:
        current = rows[current_index]
        current_source_evidence = str(current.get("source_evidence") or "")
        if (
            _required_term_present(term, current_source_evidence)
            or _shot_visuals_support(term, current)
        ):
            continue
        ranked = []
        for index, row in enumerate(rows):
            source_evidence = str(row.get("source_evidence") or "")
            source_score = 200 if _required_term_present(term, source_evidence) else 0
            visual_score = _shot_visual_support_score(term, row)
            if source_score <= 0 and visual_score <= 0:
                continue
            context = " ".join(str(row.get(key) or "") for key in (
                "structure", "function", "basis", "source_evidence",
            ))
            context_score = sum(char in context for char in set(term) if char.strip())
            if any(keyword in term for keyword in ("比赛", "对抗", "对战", "竞技")):
                context_score += 20 * int("竞技" in context or "比赛" in context)
            ranked.append((source_score + visual_score + context_score, -index, index))
        if not ranked:
            continue
        target_index = max(ranked)[2]
        if target_index == current_index:
            continue
        current["required_brief_content"] = [
            value for value in current.get("required_brief_content") or []
            if value != term
        ]
        target = rows[target_index]
        target["required_brief_content"] = list(dict.fromkeys([
            *(target.get("required_brief_content") or []),
            term,
        ]))


def _shot_visuals_support(term: str, shot: dict) -> bool:
    return _shot_visual_support_score(term, shot) > 0


def _shot_visual_support_score(term: str, shot: dict) -> int:
    scores = [
        int(candidate.get("score") or 0)
        for candidate in shot.get("indexed_visual_candidates") or []
        if _required_term_present(
            term,
            " ".join([
                str(candidate.get("summary") or ""),
                " ".join(str(tag) for tag in candidate.get("tags") or []),
            ]),
        )
    ]
    return max(scores, default=0)


def _ground_required_assignments(payload: dict, source: dict) -> dict:
    # Planning/asset evidence is not a description of the generated frame.
    # Preserve the model text; traceability reports the gap for review instead.
    return deepcopy(payload)


def _planned_business_events(contract: dict) -> list[dict]:
    """Return Brief events; template structure labels are not project facts."""
    events = list(contract.get("events") or [])
    if not events and contract.get("primary_event"):
        events = [{"text": contract["primary_event"], "role": "primary"}]
    if contract.get("generated_structure_only"):
        events = [
            event for event in events
            if str(event.get("role") or "secondary") != "primary"
            and not str(event.get("event_id") or "").startswith("template_slot_")
        ]
    return events


def _ground_primary_events(payload: dict, source: dict) -> dict:
    """Enforce existing forbidden-content rules without inventing missing scenes."""
    normalized = dict(payload)
    source_by_shot = {
        str(shot.get("shot_no") or ""): shot
        for shot in source.get("shots") or []
    }
    shots = []
    for item in payload.get("shots") or []:
        row = dict(item)
        source_shot = source_by_shot.get(str(row.get("shot_no") or ""), {})
        contract = source_shot.get("planning_contract") or {}
        description = str(row.get("visual_description") or "").strip().rstrip("。；")
        events = _planned_business_events(contract)
        event_text = " ".join(
            str(event.get("text") or "").strip()
            for event in events
            if str(event.get("text") or "").strip()
        )
        blocked_terms = [
            term.strip()
            for term in re.split(
                r"[、,，;；/]+",
                str((source_shot.get("editing_logic") or {}).get("blocked_materials") or ""),
            )
            if term.strip()
        ]
        forbidden_terms = [
            term.strip()
            for term in re.split(
                r"[、,，;；/]+",
                str((source_shot.get("editing_logic") or {}).get("forbidden_terms") or ""),
            )
            if term.strip()
        ]
        unauthorized_blocked_hits = [
            marker
            for term in blocked_terms
            if (
                (marker := _blocked_material_hit(term, description))
                and marker not in event_text
            )
        ]
        unauthorized_forbidden_hits = [
            term
            for term in forbidden_terms
            if term in description
            and not _planned_event_allows_slot_term(term, event_text)
        ]
        if unauthorized_blocked_hits or unauthorized_forbidden_hits:
            authoritative = [
                _required_content_clause(str(event.get("text") or "").strip())
                for event in events
                if str(event.get("text") or "").strip()
            ]
            replacement = "；".join(dict.fromkeys(authoritative)).strip()
            if not replacement or any(
                (marker := _blocked_material_hit(term, replacement)) and marker not in event_text
                for term in blocked_terms
            ) or any(
                term in replacement and not _planned_event_allows_slot_term(term, event_text)
                for term in forbidden_terms
            ):
                raise ScriptDraftError(
                    f"GROUNDING_CONFLICT: 镜 {row.get('shot_no')} 画面描述命中禁用内容，"
                    "没有非空且合法的已确认事件可用于纠偏；保留原响应供核对"
                )
            description = replacement
        sound = str(row.get("sound") or "").strip()
        sound_conflicts = [
            term
            for term in forbidden_terms
            if term in sound
            and not _planned_event_allows_slot_term(term, event_text)
        ]
        if sound_conflicts:
            sound_basis = str(source_shot.get("sound_basis") or "").strip()
            if not sound_basis or any(
                term in sound_basis and not _planned_event_allows_slot_term(term, event_text)
                for term in forbidden_terms
            ):
                raise ScriptDraftError(
                    f"GROUNDING_CONFLICT: 镜 {row.get('shot_no')} 声音命中禁用内容，"
                    "没有非空且合法的声音依据可用于纠偏；保留原响应供核对"
                )
            row["sound"] = sound_basis
        row["visual_description"] = description + "。" if description else ""
        shots.append(row)
    normalized["shots"] = shots
    return normalized


def _event_text_present(event_text: str, description: str) -> bool:
    if _required_term_present(event_text, description):
        return True
    normalized = re.sub(r"\s+", "", str(event_text or ""))
    normalized = re.sub(r"^\d+[.、]", "", normalized)
    normalized = re.sub(r"[（(][^）)]*[）)]$", "", normalized)
    return bool(normalized and normalized in re.sub(r"\s+", "", description))


def _blocked_material_hit(blocked_term: str, description: str) -> str:
    if blocked_term in description:
        return blocked_term
    for marker in (
        "主持人", "观众", "演讲者", "茶歇", "合影", "外场", "签到", "颁奖", "品牌落版",
    ):
        if marker in blocked_term and marker in description:
            return marker
    return ""


def _normalize_client_placeholders(payload: dict) -> dict:
    normalized = dict(payload)
    shots = []
    for item in payload.get("shots") or []:
        row = dict(item)
        row["visual_description"] = str(
            row.get("visual_description") or ""
        ).replace("核心内容", "演讲要点")
        shots.append(row)
    normalized["shots"] = shots
    return normalized


def _is_opening_requirement(term: str) -> bool:
    normalized = re.sub(r"\s+", "", str(term or ""))
    return (
        any(marker in normalized for marker in ("开头", "开场", "片头"))
        or "落到现场" in normalized and "转场" in normalized
    )


def _ground_special_requirements(payload: dict, source: dict) -> dict:
    brief = source.get("brief") if isinstance(source, dict) else None
    special = str(brief.get("特殊需求") or "") if isinstance(brief, dict) else ""
    if not re.search(r"音乐|歌曲|BGM", special, re.IGNORECASE):
        return payload
    cue = "明星歌曲BGM" if re.search(r"明星|艺人", special) else "指定音乐BGM"
    normalized = dict(payload)
    shots = []
    for item in payload.get("shots") or []:
        row = dict(item)
        sound = str(row.get("sound") or "")
        ambient = re.sub(r"背景音乐|音乐|BGM", "", sound, flags=re.IGNORECASE)
        ambient = ambient.strip(" +/、，,.;；。")
        if ambient.endswith("声"):
            ambient = ambient[:-1]
        ambient = ambient[:4]
        row["sound"] = f"{cue}+{ambient}" if ambient else cue
        row["pending_confirmations"] = list(row.get("pending_confirmations") or [])
        shots.append(row)
    pending_count = sum(len(row["pending_confirmations"]) for row in shots)
    has_rights_confirmation = any(
        "授权" in str(value)
        for row in shots for value in row["pending_confirmations"]
    )
    if not has_rights_confirmation and pending_count < MAX_PENDING_CONFIRMATIONS:
        target = next((row for row in shots if not row["pending_confirmations"]), None)
        if target is not None:
            target["pending_confirmations"] = ["具体歌曲及授权待确认"]
    normalized["shots"] = shots
    return normalized


def _required_content_clause(term: str) -> str:
    # Used only by the existing forbidden-content fallback and legacy local
    # draft path. A confirmed requirement may be repeated, never expanded into
    # invented choreography or treated as evidence of execution.
    return str(term or "").strip()


def requirement_output_evidence(
    requirement: str, script: dict, *, shot_id: str = "",
    event_ids: tuple[str, ...] = (), aliases: tuple[str, ...] = (),
    position_verified: bool = True,
) -> dict:
    """Collect literal, locatable evidence, never a semantic or human verdict.

    Aliases must come from an explicit event/source mapping supplied by the
    authority, not a detector-generated synonym or similarity score. Empty
    stable identity stays empty; display numbers are not identity substitutes.
    All spans are offsets into the original field (Python Unicode indices).
    """
    fields = ("画面描述", "声音", "旁白", "字幕", "文案")
    # Normalize only explicit source delimiters. Do not invent synonyms or
    # split Chinese words (for example, the 与 in 参与 is not a conjunction).
    parts = tuple(dict.fromkeys(part.strip() for part in re.split(r"[：:、]", requirement) if part.strip()))
    if len(parts) < 2 or any(len(part) < 2 for part in parts):
        parts = (requirement,) if requirement else ()
    terms = tuple(dict.fromkeys(t for t in (requirement, *aliases, *parts) if t))
    fragments = []
    for field in fields:
        original = str(script.get(field) or "")
        for match in re.finditer(r"[^。；;\n]+", original):
            fragment = match.group(0)
            hits = [term for term in terms if term in fragment]
            if not hits:
                continue
            negative = any(word in fragment for word in (
                "不安排", "不包含", "取消", "未确认", "待确认", "待补", "禁止",
                "未到场", "未出现", "没有", "未展示", "不展示",
            ))
            generic = (
                fragment.strip(" ，,：:") in terms
                or any(word in fragment for word in (
                    "完整呈现", "可执行动作", "聚焦", "需要体现", "要求体现",
                ))
            )
            fragments.append({
                "field": field, "text": fragment,
                "start": match.start(), "end": match.end(),
                "matched_terms": hits, "negated_or_uncertain": negative,
                "generic_restatement": generic,
            })
    return {
        "shot_id": str(shot_id), "event_ids": list(event_ids),
        "script_sha256": sha256_json(script),
        "evidence_version": "mplus-output-fragments/v2",
        "evidence_source": "literal_original_fields",
        "requirement_parts": list(parts),
        "missing_requirement_parts": [part for part in parts
            if not any(part in fragment["matched_terms"] for fragment in fragments)],
        "position_verified": bool(position_verified),
        "mentioned": bool(fragments), "fragments": fragments,
        # A literal match, including an explicitly mapped synonym, cannot
        # establish all actors/actions/locations or approve a compound request.
        "verification_status": "pending_review",
    }


def _asset_logic_coverage(source: dict) -> dict:
    shots = list(source.get("shots") or [])
    critical = (
        "structure", "function", "basis", "camera_type", "shot_size", "camera_move", "source",
    )
    complete = sum(
        all(str(shot.get(field) or "").strip() for field in critical)
        for shot in shots
    )
    return {
        "asset_logic_shot_count": len(shots),
        "asset_logic_complete_count": complete,
        "asset_logic_coverage": round(complete / len(shots), 6) if shots else 0.0,
    }


def _validate_asset_logic_source(source: dict) -> None:
    coverage = _asset_logic_coverage(source)
    if not coverage["asset_logic_shot_count"]:
        raise ScriptDraftError("已审核资产库未提供可用镜头逻辑")
    if coverage["asset_logic_coverage"] != 1.0:
        missing = []
        for shot in source["shots"]:
            absent = [
                field
                for field in (
                    "structure", "function", "basis", "camera_type", "shot_size", "camera_move", "source",
                )
                if not str(shot.get(field) or "").strip()
            ]
            if absent:
                missing.append(f"镜{shot.get('shot_no') or '?'}:{','.join(absent)}")
        raise ScriptDraftError("已审核资产库剪辑逻辑不完整: " + "；".join(missing))


def _creative_preservation_prompt(source: dict) -> str:
    if not any(shot.get("accepted_creative_fields") for shot in source.get("shots") or []):
        return ""
    return ("这是已接受初稿的正式转换。凡含 accepted_creative_fields 的镜头，必须逐字符复制其中全部字段；"
            "这些字段优先于下述润色、长度、压缩和规范化建议。仅无此标记的新增镜头允许写作。"
            "初稿表达不等于客户事实已确认；不得增加事实或自行替换原句。")


def _writer_prompt(source: dict) -> str:
    return (
        _creative_preservation_prompt(source)
        +
        f"直接输出 JSON，不要解释或 Markdown。当前脚本类型是{source['script_type']}，"
        f"shots 必须且只能输出{source['shot_count']}项，shot_no 必须按{source['required_shot_nos']}逐项一一对应，不得漏项、合并、增项或改号；"
        "依据 Brief 和每条已审核 basis，"
        "为当前项目写可拍分镜。不得猜测 Brief 外的主题名、产品系列、人物身份、数字。"
        "source_reference.generic_desc是通用目的，visual_desc是来源案例；承接通用目的，案例品牌与案例事实不转成项目事实。"
        "template_semantics、template_rules和effective_knowledge中的条件、否定与例外须完整理解，原文约数或示例不自动成为硬要求。"
        "来源案例中的城市、场馆、产品、设备、机器人、车型、酒类等具体主体，"
        "若 Brief 未确认，必须删除或改成当前项目已确认的通用功能表达。"
        "Brief 的每项必须内容必须在一个职责相符的镜头中自然写明；"
        "每镜 planning_contract.primary_event 和 planning_contract.events 是已确认的流程主线，每项都必须在原镜完成，不得被强制细节覆盖或删除；"
        "每镜 visual_description 必须以本镜 primary_event 为唯一内容主体；不得写入 editing_logic.blocked_materials 或 editing_logic.forbidden_terms 的任一词或同义表达；"
        "按 narrative_stage 从 opening 连续推进到 closing，开场要素只在片头建立，不得在中段重启开场或在结尾回到未开始状态；"
        "重复出现的流程事件必须有明确的首次建立与后续收束差异，不得复制同一动作或人物到场。"
        "即使相邻镜头属于同一结构弧或结构占位，也必须依据各自 basis、function、机位、景别和运镜写成不同的可拍动作；"
        "任意两镜的 visual_description 与 sound 不得同时完全相同。"
        "planning_contract.generated_structure_only=true 表示该镜只是技术结构槽：structure 仅是模板结构标签，不是项目事实；"
        "不得因标签中的艺人、仪式、表演、发言等字样虚构人物或流程，只能使用 Brief 已确认事实或中性环境、构图和节奏动作。"
        "revision_constraints 是人工复核明确提出的修改，必须按 scope、shot_no、category、comment 和 desired_change 执行；未列入反馈的镜头不得无故改写。"
        "brief.特殊需求是全片制作约束：音乐或声音要求只能落在 sound，不得写入 visual_description；"
        "每镜 required_brief_content 是规划器依据已审核来源指定的必选任务，必须在该镜完成，不得擅自挪到其他镜头；"
        "indexed_visual_candidates 是已识别的创意库功能参考，可用来具体化本镜动作，但不得把其中的源品牌、城市、人名或数字当作当前项目事实；"
        "不得用‘镜头明确呈现’等内部标签代替真实画面，也不得把一个复合要求拆散后冒充完成。"
        "必须遵守 editing_logic_contract 和每镜的结构、时长、效果、机位、景别、运镜、"
        "节奏、允许/禁止素材、视觉证据及重复规则；不得让新画面描述与这些剪辑约束冲突。"
        "creative_standard.tone_keywords 是整体表达约束，必须贯彻；expression_focus 必须保留其已绑定镜头的重点；"
        "avoid_expressions 中的表达不得出现在画面、声音、字幕或文案；references 仅是风格上下文，不得当成 Brief 事实。"
        "project_image_context.images 中的 caption、tags、subjects、scene、purpose、quality、brand 是已核验的候选画面业务上下文，"
        "可用于具体化画面，但不覆盖 Brief 事实，也不代表已决定逐镜最终用图。"
        "effective_knowledge.rules 是已审核生效的表达与制作规则，必须执行；它们是约束而不是项目事实。"
        "缺失事实写‘待确认’并列入 pending_confirmations。"
        "每镜 visual_description 25-55个汉字，sound 必须是非空字符串、12个汉字内；"
        "未提供现场录音时，可明确写‘声音设计建议：延续底乐’或‘声音设计建议：留白’，不得空缺或宣称已录到现场声；"
        "voiceover 与 subtitle 必须输出字符串，只在本镜确有旁白或字幕时填写，否则输出空字符串；overall_summary 80字内。Schema:"
        + _schema_text()
        + "\nDATA:"
        + json.dumps(source, ensure_ascii=False, sort_keys=True)
    )


def _editor_prompt(source: dict, writer_payload: dict) -> str:
    return (
        _creative_preservation_prompt(source)
        +
        f"直接输出 JSON，不要解释或 Markdown。当前脚本类型是{source['script_type']}，"
        f"shots 必须且只能保留{source['shot_count']}项，shot_no 必须按{source['required_shot_nos']}逐项一一对应，不得漏项、合并、增项或改号；"
        "把 DRAFT 编辑成客户前终审文案："
        "删除其他品牌、模板词、内部过程语言、重复高潮和不可拍表达；"
        "客户画面不得出现‘AI生成’、‘AI合成’、‘AI制作’、‘AI画面’等内部制作方式；"
        "删除 Brief 未提供的来源城市、场馆和行业专属主体，不得把来源案例事实当作当前项目事实；"
        "逐项核对 Brief 必须内容已在职责相符的单镜中形成可拍动作，不得保留内部完成标记；"
        "不得删除或覆盖任何镜头的 planning_contract.primary_event 及 planning_contract.events；"
        "若 DRAFT 的镜头内容错位，必须按本镜 primary_event 直接重写，不得追加另一流程敷衍；不得写入 editing_logic.blocked_materials 或 editing_logic.forbidden_terms 的任一词或同义表达；"
        "全片必须从 opening 连续进入 development 并在 closing 收束，不得在中段重启开场，不得重复人物到场或同一互动动作。"
        "终审前逐镜比较 visual_description 和 sound：即使是同一结构弧或结构占位，也必须根据各镜 basis、function、机位、景别和运镜保留不同的可拍动作；"
        "任意两镜的画面与声音不得同时完全相同。"
        "planning_contract.generated_structure_only=true 的镜头不得从 structure 标签推导项目事实；"
        "如 Brief 未确认艺人、仪式、表演或发言，必须删除草稿中因模板结构标签产生的这些人物和流程，改为中性环境、构图或节奏动作。"
        "逐条复核 revision_constraints，必须完成 comment 和 desired_change；未被反馈指向的内容保持不变。"
        "brief.特殊需求中的音乐或声音约束必须保留在每镜 sound，不得转写到画面；"
        "不得删除、拆散或转移每镜 required_brief_content 指定的任务；"
        "不得增加 Brief 外事实，不得改变或违背 DATA 中的剪辑逻辑合同。"
        "必须继续遵守 creative_standard：调性关键词贯穿表达、已绑定表达重点不得移除、避免表达不得写入，参考资料不得提升为事实。"
        "project_image_context 仅为候选图片业务上下文：可使用七类字段润色画面，不能覆盖 Brief 事实或宣称其是逐镜最终选图。"
        "effective_knowledge.rules 是已审核生效的表达与制作规则，必须继续执行，不得当成项目事实。"
        "DATA 未出现的年份、日期、数量、参数等数字禁止写入画面、声音或整体说明。"
        "待确认只保留会改变脚本内容且无法从 Brief 或 basis 得出的客户事实；"
        "不得把字体、尺寸、机型、数量、时长、布局、拍摄参数等执行细节逐镜列为待确认。"
        f"全片 pending_confirmations 最多{MAX_PENDING_CONFIRMATIONS}项，每镜最多1项；"
        "每镜保持25-55个汉字，sound 必须是非空字符串、12字内；缺少录音依据时明确写声音设计建议或留白，不得删除声音字段；voiceover 与 subtitle 必须保留为字符串，"
        "没有旁白或字幕的镜头输出空字符串。Schema:"
        + _schema_text()
        + "\nDATA:"
        + json.dumps(source, ensure_ascii=False, sort_keys=True)
        + "\nDRAFT:"
        + json.dumps(writer_payload, ensure_ascii=False, sort_keys=True)
    )


def _schema_text() -> str:
    return (
        '{"schema_version":"mplus-script-draft-response/v1",'
        '"overall_summary":"项目专属整体说明",'
        '"shots":[{"shot_no":"1","visual_description":"画面",'
        '"sound":"声音",'
        '"voiceover":"旁白或空字符串","subtitle":"字幕或空字符串",'
        '"pending_confirmations":["待确认事项"]}]}'
    )


def _cached_call(
    cache_dir: Path,
    *,
    pass_name: str,
    model: str,
    prompt: str,
    provider: TextProvider,
    provider_name: str = "custom",
) -> tuple[str, bool, Path]:
    key = sha256_json({
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "pass_name": pass_name,
        "model": model,
        "provider": provider_name,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "prompt": prompt,
    })
    path = cache_dir / f"{key}.json"
    ledger_path = cache_dir / "text_call_ledger.jsonl"
    prompt_sha256 = _text_sha256(prompt)
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
        response = TextProviderResponse(str(payload["response"]), diagnostics=payload.get("provider_diagnostics"))
        _append_call_ledger(ledger_path, {
            "event": "cache_hit",
            "cache_key": key,
            "pass_name": pass_name,
            "model": model,
            "provider": provider_name,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_sha256,
            "response_sha256": payload.get("response_sha256"),
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            **_ledger_transport_parameters(_transport_diagnostics(response)),
        })
        return response, True, path
    try:
        response = provider(prompt, model, MAX_OUTPUT_TOKENS)
    except Exception as exc:
        transport = _transport_diagnostics(exc)
        diagnostic_path = _write_model_diagnostics(cache_dir, key, {
            "pass_name": pass_name, "status": "failed", "failure_stage": "provider",
            "invocation": {"scope": "provider_callable_request", "provider": provider_name, "model": model,
                "max_tokens_requested": MAX_OUTPUT_TOKENS, "prompt": prompt, "prompt_sha256": prompt_sha256},
            "transport": transport,
            "raw_response": (transport.get("response") or {}).get("raw_response", ""),
            "stages": [{"stage": "provider", "status": "failed"}],
            "error": {"type": type(exc).__name__, "message": str(exc)},
        })
        _append_call_ledger(ledger_path, {
            "event": "live_call_failed",
            "cache_key": key,
            "pass_name": pass_name,
            "model": model,
            "provider": provider_name,
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_sha256,
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            **_ledger_transport_parameters(transport),
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "diagnostics_file": diagnostic_path.name,
        })
        if isinstance(exc, TextProviderError):
            error = ScriptDraftProviderError(str(exc))
            error.diagnostics = transport
            raise error from exc
        raise
    response_sha256 = sha256_json(response)
    write_json_atomic(path, _redact_diagnostics({
        "schema_version": "mplus-text-cache/v1",
        "cache_key": key,
        "pass_name": pass_name,
        "model": model,
        "provider": provider_name,
        "response": response,
        "response_sha256": response_sha256,
        "provider_diagnostics": _transport_diagnostics(response),
    }))
    _append_call_ledger(ledger_path, {
        "event": "live_call_succeeded",
        "cache_key": key,
        "pass_name": pass_name,
        "model": model,
        "provider": provider_name,
        "prompt_version": PROMPT_VERSION,
        "prompt_sha256": prompt_sha256,
        "response_sha256": response_sha256,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        **_ledger_transport_parameters(_transport_diagnostics(response)),
    })
    return response, False, path


def _append_call_ledger(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _redact_diagnostics(dict(payload))
    record["timestamp"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _transport_diagnostics(value) -> dict:
    return _transport_evidence(value)


def _ledger_transport_parameters(transport: dict) -> dict:
    return {"max_output_tokens_scope": "provider_callable_request",
        "transport_provider": transport.get("provider", "unavailable"),
        "transport_request": transport.get("request", {})}


def _write_model_diagnostics(cache_dir: Path, cache_key: str, record: dict) -> Path:
    directory = cache_dir / "diagnostics"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{cache_key}-{uuid.uuid4().hex}.json"
    safe = _redact_diagnostics({
        "schema_version": "mplus-model-diagnostics/v1", "cache_key": cache_key,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), **record,
    })
    safe["response_redacted"] = safe.get("raw_response") != record.get("raw_response")
    safe["stored_response_sha256"] = _text_sha256(safe.get("raw_response", ""))
    write_json_atomic(path, safe)
    path.chmod(0o600)
    return path


def _process_model_response(raw: str, source: dict, plan, *, cache_path: Path, cache_hit: bool,
        pass_name: str, model: str, provider_name: str, prompt: str, final: bool) -> tuple[dict, int]:
    record = {
        "pass_name": pass_name, "cache_hit": cache_hit, "raw_response": str(raw),
        "raw_response_sha256": _text_sha256(str(raw)),
        "invocation": {"scope": "provider_callable_request", "provider": provider_name, "model": model, "max_tokens_requested": MAX_OUTPUT_TOKENS,
            "prompt": prompt, "prompt_sha256": _text_sha256(prompt)},
        "transport": _transport_diagnostics(raw), "stages": [],
    }
    stage = {"stage": "parse", "input": str(raw)}
    repair_count = 0
    try:
        payload, repair_count = _parse_response_with_diagnostics(raw)
        stage.update(status="succeeded", output=deepcopy(payload), repair_count=repair_count)
        record["stages"].append(stage)
        from .creative_formal_binding import protected_text, validate_preserved_text
        accepted_fields = protected_text(source)
        if accepted_fields:
            stage = {"stage": "bind_accepted_creative_fields", "input": deepcopy(payload)}
            # Formal conversion compiles accepted expressions from their frozen
            # block-to-shot mapping. Model output is authoritative only for the
            # fields that have not already been accepted by the user.
            for shot in payload.get("shots") or []:
                fields = accepted_fields.get(str(shot.get("shot_no")))
                if fields:
                    shot.update(fields)
            stage.update(status="succeeded", output=deepcopy(payload))
            record["stages"].append(stage)
        validate_preserved_text(payload, source)
        operations = [("normalize_schema", _normalize_response_schema)]
        if final:
            operations.append(("normalize_pending_confirmations", _normalize_pending_confirmations))
        operations.extend([
            ("ground_primary_events", lambda value: _ground_primary_events(value, source)),
            ("ground_required_assignments", lambda value: _ground_required_assignments(value, source)),
            ("normalize_client_placeholders", _normalize_client_placeholders),
            ("ground_special_requirements", lambda value: _ground_special_requirements(value, source)),
        ])
        for name, operation in operations:
            stage = {"stage": name, "input": deepcopy(payload)}
            payload = operation(payload)
            # Automatic punctuation/grounding may enrich only newly written
            # content. Accepted fields remain bound to the frozen input.
            for shot in payload.get("shots") or []:
                fields = accepted_fields.get(str(shot.get("shot_no")))
                if fields:
                    shot.update(fields)
            stage.update(status="succeeded", output=deepcopy(payload))
            record["stages"].append(stage)
        validate_preserved_text(payload, source)
        stage = {"stage": "validation", "input": deepcopy(payload)}
        _validate_response(payload, plan, final=final)
        stage["status"] = "succeeded"
        record["stages"].append(stage)
    except Exception as exc:
        stage["status"] = "failed"
        record["stages"].append(stage)
        record.update(status="failed", failure_stage=stage["stage"], error={"type": type(exc).__name__, "message": str(exc)})
        evidence = _write_model_diagnostics(cache_path.parent, cache_path.stem, record)
        # Preserve failure evidence before invalidating even an old bad cache.
        # A retry may reuse successful writer output, never the failed response.
        cache_path.unlink(missing_ok=True)
        _append_call_ledger(cache_path.parent / "text_call_ledger.jsonl", {
            "event": "response_processing_failed", "cache_key": cache_path.stem, "pass_name": pass_name,
            "failure_stage": stage["stage"], "error_type": type(exc).__name__, "diagnostics_file": evidence.name,
        })
        raise
    record["status"] = "succeeded"
    _write_model_diagnostics(cache_path.parent, cache_path.stem, record)
    return payload, repair_count


def _record_json_repair(
    cache_dir: Path,
    *,
    pass_name: str,
    model: str,
    provider_name: str,
    cache_path: Path,
    repair_count: int,
) -> None:
    _append_call_ledger(cache_dir / "text_call_ledger.jsonl", {
        "event": "response_json_repaired",
        "cache_key": cache_path.stem,
        "pass_name": pass_name,
        "model": model,
        "provider": provider_name,
        "prompt_version": PROMPT_VERSION,
        "repair_count": repair_count,
        "repair_method": "conservative_schema_json_syntax",
    })


def _mmx_provider(command: str) -> TextProvider:
    try:
        provider = minimax_cli_provider(command)
    except TextProviderError as exc:
        raise ScriptDraftProviderError(str(exc)) from exc

    def invoke(prompt: str, model: str, max_tokens: int) -> str:
        try:
            return provider(prompt, model, max_tokens)
        except TextProviderError as exc:
            error = ScriptDraftProviderError(str(exc))
            error.diagnostics = exc.diagnostics
            raise error from exc
    return invoke


def _provider_for_name(provider_name: str, command: str) -> TextProvider:
    if str(provider_name).strip().casefold() in {"minimax", "minimax-cli", "mmx"}:
        return _mmx_provider(command)
    return build_text_provider(provider_name, command=command)


def _is_transient_provider_failure(message: str) -> bool:
    normalized = message.lower()
    return any(marker in normalized for marker in (
        "429",
        "529",
        "overloaded",
        "rate limit",
        "timed out",
        "timeout",
        "temporarily unavailable",
    ))


def _extract_text(raw_output: str) -> str:
    raw_output = raw_output.strip()
    if not raw_output:
        raise ScriptDraftError("MiniMax 文本调用返回空内容")
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return raw_output
    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, list):
        joined = "".join(
            str(block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        if joined:
            return joined
    candidates = [
        content,
        payload.get("text") if isinstance(payload, dict) else None,
        payload.get("response") if isinstance(payload, dict) else None,
    ]
    if isinstance(payload, dict):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, dict):
                message = choice.get("message")
                candidates.append(message.get("content") if isinstance(message, dict) else None)
                candidates.append(choice.get("text"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    if isinstance(payload, dict):
        block_types = [
            str(block.get("type") or "unknown")
            for block in content or []
            if isinstance(block, dict)
        ] if isinstance(content, list) else []
        stop_reason = payload.get("stop_reason") or payload.get("finish_reason") or "unknown"
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        output_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or "unknown"
        raise ScriptDraftError(
            "MiniMax JSON 返回中找不到文本内容: "
            f"stop_reason={stop_reason}, content_types={block_types}, "
            f"output_tokens={output_tokens}"
        )
    raise ScriptDraftError("MiniMax JSON 返回中找不到文本内容")


def _parse_response(raw: str) -> dict:
    payload, _ = _parse_response_with_diagnostics(raw)
    return payload


def _parse_response_with_diagnostics(raw: str) -> tuple[dict, int]:
    text = raw.strip()
    if text.startswith("```"):
        raise ScriptDraftError("模型返回不得包含 Markdown 代码块")
    repair_count = 0
    while True:
        try:
            payload = json.loads(text)
            break
        except json.JSONDecodeError as exc:
            repaired = (
                _repair_schema_missing_comma(text, exc)
                or _repair_unescaped_schema_value_quote(text, exc)
            )
            if repaired is None or repair_count >= 7:
                raise ScriptDraftError(f"模型返回不是合法 JSON: {exc}") from exc
            text = repaired
            repair_count += 1
    if not isinstance(payload, dict):
        raise ScriptDraftError("模型返回 JSON 必须为对象")
    return payload, repair_count


def _repair_schema_missing_comma(
    text: str,
    error: json.JSONDecodeError,
) -> str | None:
    """Only repair an unambiguous missing comma before a known schema key.

    Content, field names and values are never rewritten. The repaired object still
    has to pass the complete ScriptDraft schema and locked-field validation.
    """
    if error.msg != "Expecting ',' delimiter":
        return None
    position = error.pos
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text):
        return None
    previous = position - 1
    while previous >= 0 and text[previous].isspace():
        previous -= 1
    if previous < 0:
        return None
    if text[position] == "{" and text[previous] == "}":
        return text[:position] + "," + text[position:]
    if text[position] != '"':
        return None
    known_keys = (
        "schema_version",
        "overall_summary",
        "shots",
        "shot_no",
        "visual_description",
        "sound",
        "pending_confirmations",
    )
    if not any(
        text.startswith(f'"{key}"', position)
        for key in known_keys
    ):
        return None
    key_end = text.find('"', position + 1)
    if key_end < 0:
        return None
    colon = key_end + 1
    while colon < len(text) and text[colon].isspace():
        colon += 1
    if colon >= len(text) or text[colon] != ":":
        return None
    if text[previous] not in {'"', ']', '}'}:
        return None
    return text[:position] + "," + text[position:]


def _repair_unescaped_schema_value_quote(
    text: str,
    error: json.JSONDecodeError,
) -> str | None:
    """Escape an unambiguous quote inside a known free-text field.

    Some providers emit JSON such as ``"visual_description":"Midea"智美万象"主题"``.
    The JSON decoder then stops immediately after the first inner quote.  We only
    repair that quote when the surrounding value belongs to a known free-text
    schema field and another closing quote still exists.  Schema and locked-field
    validation continue to run after parsing.
    """
    if error.msg != "Expecting ',' delimiter":
        return None
    position = error.pos
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text) or text[position] in ",:{}[]\"":
        return None
    previous = position - 1
    while previous >= 0 and text[previous].isspace():
        previous -= 1
    if previous < 0 or text[previous] != '"' or _quote_is_escaped(text, previous):
        return None
    opening = _previous_unescaped_quote(text, previous - 1)
    if opening < 0:
        return None
    colon = opening - 1
    while colon >= 0 and text[colon].isspace():
        colon -= 1
    if colon < 0 or text[colon] != ":":
        return None
    key_end = colon - 1
    while key_end >= 0 and text[key_end].isspace():
        key_end -= 1
    if key_end < 0 or text[key_end] != '"':
        return None
    key_start = _previous_unescaped_quote(text, key_end - 1)
    if key_start < 0:
        return None
    key = text[key_start + 1:key_end]
    if key not in {"overall_summary", "visual_description", "sound"}:
        return None
    if _next_unescaped_quote(text, position) < 0:
        return None
    return text[:previous] + "\\" + text[previous:]


def _quote_is_escaped(text: str, position: int) -> bool:
    backslashes = 0
    index = position - 1
    while index >= 0 and text[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 1


def _previous_unescaped_quote(text: str, start: int) -> int:
    for index in range(start, -1, -1):
        if text[index] == '"' and not _quote_is_escaped(text, index):
            return index
    return -1


def _next_unescaped_quote(text: str, start: int) -> int:
    for index in range(start, len(text)):
        if text[index] == '"' and not _quote_is_escaped(text, index):
            return index
    return -1


def _validate_response(payload: dict, plan, *, final: bool) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ScriptDraftError("模型返回 Schema 版本无效")
    extra_payload_keys = set(payload) - {"schema_version", "overall_summary", "shots"}
    if extra_payload_keys:
        raise ScriptDraftError(f"模型返回包含 Schema 外字段: {sorted(extra_payload_keys)}")
    shots = payload.get("shots")
    if not isinstance(shots, list) or len(shots) != len(plan.script_rows):
        raise ScriptDraftError("模型返回镜头数与 ScriptPlan 不一致")
    for index, (shot, script) in enumerate(
        zip(shots, plan.script_rows),
        start=1,
    ):
        if not isinstance(shot, dict):
            raise ScriptDraftError(f"镜 {index} 返回不是对象")
        extra_shot_keys = set(shot) - {
            "shot_no", "visual_description", "sound", "voiceover", "subtitle",
            "pending_confirmations"
        }
        if extra_shot_keys:
            raise ScriptDraftError(
                f"镜 {index} 不得输出结构、时长或来源等不可变字段: {sorted(extra_shot_keys)}"
            )
        if str(shot.get("shot_no") or "") != str(script.get("镜号") or ""):
            raise ScriptDraftError(f"镜 {index} 镜号被模型修改")
        if not str(shot.get("visual_description") or "").strip():
            raise ScriptDraftError(f"镜 {index} 画面描述为空")
        if not isinstance(shot.get("sound"), str) or not shot["sound"].strip():
            raise ScriptDraftError(f"镜 {index} 声音不能为空")
        for field in ("voiceover", "subtitle"):
            if field not in shot:
                raise ScriptDraftError(f"镜 {index} 缺少 {field} 字段")
            if not isinstance(shot[field], str):
                raise ScriptDraftError(f"镜 {index} {field} 必须为字符串")
        if not isinstance(shot.get("pending_confirmations", []), list):
            raise ScriptDraftError(f"镜 {index} 待确认事项必须为列表")
        if final and len(shot.get("pending_confirmations") or []) > 1:
            raise ScriptDraftError(f"镜 {index} 待确认事项超过1项，存在过度依赖")
    if final:
        pending_count = sum(
            len(shot.get("pending_confirmations") or [])
            for shot in shots
        )
        if pending_count > MAX_PENDING_CONFIRMATIONS:
            raise ScriptDraftError(
                f"待确认事项共{pending_count}项，超过上限{MAX_PENDING_CONFIRMATIONS}，存在过度依赖"
            )
        visible_text = "\n".join(
            [str(payload.get("overall_summary") or "")]
            + [
                str(shot.get("visual_description") or "")
                + "\n"
                + str(shot.get("sound") or "")
                for shot in shots
            ]
        )
        found = [phrase for phrase in INTERNAL_PHRASES if phrase in visible_text]
        if found:
            raise ScriptDraftError(f"编辑结果仍含内部过程语言: {found}")


def _normalize_pending_confirmations(payload: dict) -> dict:
    normalized = dict(payload)
    shots = []
    kept_total = 0
    seen = set()
    for shot in payload.get("shots") or []:
        row = dict(shot)
        kept = []
        for item in shot.get("pending_confirmations") or []:
            text = str(item or "").strip()
            if (
                not text
                or text in seen
                or any(term in text for term in LOW_VALUE_PENDING_TERMS)
                or kept_total >= MAX_PENDING_CONFIRMATIONS
            ):
                continue
            kept.append(text)
            seen.add(text)
            kept_total += 1
            break
        row["pending_confirmations"] = kept
        shots.append(row)
    normalized["shots"] = shots
    return normalized


def _normalize_response_schema(payload: dict) -> dict:
    """Fill only an omitted response version; explicit wrong versions still fail."""
    if "schema_version" in payload:
        return payload
    return {"schema_version": SCHEMA_VERSION, **payload}


def _apply_response(script_rows: list[dict], payload: dict) -> list[dict]:
    rows = []
    for source, drafted in zip(script_rows, payload["shots"]):
        row = dict(source)
        row["画面描述"] = str(drafted["visual_description"]).strip()
        row["声音"] = str(drafted.get("sound") or "").strip()
        row["旁白"] = str(drafted.get("voiceover") or "").strip()
        row["字幕"] = str(drafted.get("subtitle") or "").strip()
        rows.append(row)
    return rows


def _pending_confirmations(payload: dict) -> list[dict]:
    rows = []
    for shot in payload["shots"]:
        for item in shot.get("pending_confirmations") or []:
            text = str(item).strip()
            if text:
                rows.append({
                    "shot_no": str(shot.get("shot_no") or ""),
                    "item": text,
                    "status": "待确认",
                })
    return rows
