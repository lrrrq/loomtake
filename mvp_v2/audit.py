from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

from .models import (
    SourceShot,
    TemplateDecision,
    split_forbidden_terms,
    validate_launch_brief,
    validate_script_brief,
)
from .scoring import normalize_structure
from .launch_contract import expected_slot_numbers, template_count_valid
from .template_logic import logical_slot_index, slot_logic_for_source
from .brand_policy import (
    brand_evidence_unverified,
    brand_verdict_status,
    conflicting_visual_brand_names,
    conflicting_brands,
    source_brand_leaks,
    source_brand_provenance_issue,
    unknown_brand_mentions,
)
from .reuse_policy import is_reuse_blocked
from .run_contract import sha256_file
from .image_understanding import analysis_asset_unusable
from .script_type_adapter import normalize_script_type
from .planning_core import brief_content_clauses, _short_action_match, _split_requirement_text


AUDIT_STATUS_PASS = "pass"
AUDIT_STATUS_FAIL = "fail"
SCRIPT_DOCUMENT_DOMAIN = "script_document"
MEDIA_READINESS_DOMAIN = "media_readiness"

_MEDIA_READINESS_ITEMS = {
    "visual_function_reference_only",
    "missing_image",
    "missing_bound_visual",
    "missing_visual_asset",
    "missing_manifest_image_id",
    "missing_visual_binding_reason",
    "low_resolution_visual_asset",
    "numbered_image_fallback_needs_review",
    "visual_content_not_read",
    "visual_pairing_mismatch",
    "visual_pairing_weak_match",
    "duplicate_visual_asset",
    "visual_candidate_pool_partially_unread",
    "visual_function_unclassified",
    "visual_function_mismatch",
    "duplicate_visual_function",
    "image_analysis_missing_provenance",
    "image_analysis_unverified_manual_seed",
    "image_analysis_missing_caption_tags",
    "image_analysis_missing_brand_verdict",
    "bound_visual_brand_unverified",
    "bound_visual_brand_conflict",
    "bound_visual_quality_blocked",
    "brief_forbidden_visual_hit",
}

UNCONFIRMED_LOCATION_TERMS = (
    "北京", "上海", "广州", "深圳", "杭州", "成都", "重庆", "武汉", "南京", "苏州",
    "西安", "厦门", "青岛", "天津", "长沙", "郑州", "宁波", "合肥", "福州", "济南",
)

SOURCE_SPECIFIC_SUBJECT_TERMS = (
    "机器人", "汽车", "啤酒", "珠宝", "家具", "家电", "空调", "赛车", "电竞",
    "运动鞋", "足球", "篮球", "网球",
)


def audit_generation(
    brief: dict,
    template_slots: list[SourceShot],
    decisions: list[TemplateDecision],
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict] | None = None,
    require_complete_visuals: bool | None = None,
    revision: bool = False,
    asset_package_evidence: dict | None = None,
) -> dict:
    checks: list[dict] = []
    next_constraints = {
        "exclude_by_slot": {},
        "force_keep_slots": [],
        "global_excludes": [],
        "exclude_visuals_by_slot": {},
    }

    raw_script_type = brief["脚本类型"] if "脚本类型" in brief else "发布会"
    try:
        expected_script_type = normalize_script_type(raw_script_type)
    except ValueError as exc:
        expected_script_type = ""
        _add_check(
            checks,
            "unsupported_script_type",
            "hard",
            "fail",
            "",
            str(exc),
            "在六个已注册脚本类别中明确选择一个",
        )
    cross_category_sources = []
    if expected_script_type:
        for shot in template_slots:
            try:
                actual_type = normalize_script_type(shot.script_type)
            except ValueError:
                actual_type = str(shot.script_type or "")
            if actual_type != expected_script_type:
                cross_category_sources.append(
                    f"{shot.source_file}/{shot.source_sheet}!{shot.source_row}:{actual_type}"
                )
        for decision in decisions:
            if not decision.variant:
                continue
            try:
                actual_type = normalize_script_type(decision.variant.script_type)
            except ValueError:
                actual_type = str(decision.variant.script_type or "")
            from .asset_package import source_in_frozen_library
            if actual_type != expected_script_type and not source_in_frozen_library(decision.variant, asset_package_evidence):
                cross_category_sources.append(
                    f"{decision.variant.source_file}/{decision.variant.source_sheet}!"
                    f"{decision.variant.source_row}:{actual_type}"
                )
    if cross_category_sources:
        _add_check(
            checks,
            "cross_category_source_forbidden",
            "hard",
            "fail",
            "",
            "模板类别不符或跨类创意未绑定: " + "；".join(cross_category_sources[:5]),
            "模板使用当前项目类别，创意从本次冻结的已登记共享库重新选择",
        )

    if not template_slots:
        _add_check(
            checks, "missing_template_slots", "hard", "fail", "", "未读取到模板槽位",
            "检查 source-root 下的 M+发布会类通用脚本库.xlsx / 模版 sheet",
        )
        next_constraints["force_template_order"] = True

    expected_structures = [normalize_structure(slot.structure) for slot in template_slots]
    actual_structures = [normalize_structure(row.get("结构", "")) for row in script_rows]
    if expected_structures != actual_structures:
        _add_check(
            checks, "structure_mismatch", "hard", "fail", "",
            f"输出结构与模板不一致: expected={expected_structures}, actual={actual_structures}",
            "按模板原始槽位顺序重新生成",
        )
        next_constraints["force_template_order"] = True

    is_launch = expected_script_type == "发布会"
    if is_launch:
        _audit_launch_contract(
            checks,
            brief,
            template_slots,
            decisions,
            script_rows,
            detail_rows,
            visual_asset_rows or [],
            require_complete_visuals=(
                visual_asset_rows is not None
                if require_complete_visuals is None
                else require_complete_visuals
            ),
        )
    elif expected_script_type:
        _audit_generic_contract(
            checks,
            brief,
            template_slots,
            decisions,
            script_rows,
            detail_rows,
        )
        _audit_required_content_evidence(
            checks,
            brief,
            decisions,
            script_rows,
            detail_rows,
            visual_asset_rows or [],
        )

    forbidden_terms = list(dict.fromkeys(term
        for field in ("必须出现的内容", "禁止或不能出现的内容")
        for clause in brief_content_clauses(brief, field)
        if clause["classification"] == "forbidden"
        for term in split_forbidden_terms(clause["forbidden_text"].replace("与", "、"))))
    for clause in brief_content_clauses(brief):
        if re.match(r"^(?:仅|只)以", clause["text"]):
            _add_check(checks, "exclusive_content_requires_review", "warning", "warning", "",
                "排他呈现边界需人工核对：" + clause["text"] + "；正向文字覆盖不证明没有其它呈现。",
                "逐镜核对同一主体的画面、字幕与声音是否超出该边界，保留原要求后再确认质量。")
    decision_by_slot = {decision.slot.slot_index: decision for decision in decisions}
    script_by_index = {
        index: row for index, row in enumerate(script_rows, start=1)
    }
    missing_metadata_by_source: dict[tuple[str, str], set[str]] = {}

    for index, detail in enumerate(detail_rows, start=1):
        slot_index = int(detail.get("slot_index") or index)
        decision = decision_by_slot.get(slot_index)
        mode = str(decision.mode if decision else detail.get("替换方式") or "")
        shot_no = str(detail.get("模板镜号") or detail.get("slot_index") or slot_index)
        adopted_source = None
        if decision:
            adopted_source = (
                decision.variant
                if decision.mode in {"REPLACE", "ADAPT"} and decision.variant
                else decision.slot
            )

        if mode in {"REPLACE", "ADAPT"}:
            brand_issue = ""
            if decision and decision.variant:
                brand_issue = source_brand_provenance_issue(
                    decision.variant.source_brand,
                    decision.variant.brand_neutral,
                    decision.variant.brand_neutral_evidence,
                )
            if brand_issue:
                _add_check(
                    checks, "missing_source_brand_provenance", "hard", "fail", shot_no,
                    brand_issue,
                    "该槽位强制 KEEP，或补充来源品牌/经源字段或人工确认的品牌中性声明",
                )
                _append_unique(next_constraints["force_keep_slots"], slot_index)
            if not (detail.get("替换来源文件") and detail.get("替换来源 sheet") and detail.get("替换来源行")):
                _add_check(
                    checks, "missing_source_trace", "hard", "fail", shot_no,
                    "REPLACE/ADAPT 缺少替换来源文件、sheet 或行号",
                    "该槽位强制 KEEP 或选择下一条可追溯候选",
                )
                _append_unique(next_constraints["force_keep_slots"], slot_index)
            elif decision and decision.variant and not _replacement_source_matches(
                detail, decision.variant
            ):
                _add_check(
                    checks,
                    "replacement_source_mismatch",
                    "hard",
                    "fail",
                    shot_no,
                    "REPLACE/ADAPT 替换来源与实际 decision.variant 不一致",
                    "按实际采用 variant 重新写入替换来源文件、sheet 和行号",
                )
                _append_unique(next_constraints["force_keep_slots"], slot_index)

        row_text = _row_text(script_by_index.get(index, {}))
        expected_brand = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
        if (
            mode in {"REPLACE", "ADAPT"}
            and adopted_source
            and source_brand_leaks(expected_brand, adopted_source.source_brand, row_text)
        ):
            _add_check(
                checks, "source_brand_leak", "hard", "fail", shot_no,
                f"最终镜头文案残留来源品牌: {adopted_source.source_brand}",
                "按 Brief 品牌改写来源文案；无品牌 cue 的来源名也必须清除",
            )
            _add_exclusion(next_constraints, slot_index, adopted_source)
        logic = (
            slot_logic_for_source(
                decision.slot if decision else detail,
                len(detail_rows),
            )
            if is_launch
            else None
        )
        if logic:
            planned_event_text = str(detail.get("规划主事件") or "")
            brief_override_hits = [
                term
                for term in logic.forbidden_terms
                if term and term in row_text
                and _planned_event_allows_slot_term(term, planned_event_text)
            ]
            if brief_override_hits:
                _add_check(
                    checks,
                    "slot_conflict_brief_override",
                    "warning",
                    "warning",
                    shot_no,
                    f"槽位禁用词由同镜确认 Brief 事件覆盖: {', '.join(brief_override_hits)}",
                    "人工复核该流程事件与最终镜头表达一致",
                )
            conflict_hits = [
                term
                for term in logic.forbidden_terms
                if term and term in row_text
                and not _planned_event_allows_slot_term(term, planned_event_text)
            ]
            if conflict_hits:
                _add_check(
                    checks, "slot_conflict", "hard", "fail", shot_no,
                    f"镜头内容命中槽位禁用词: {', '.join(conflict_hits)}",
                    "下一轮排除该槽位当前候选来源",
                )
                if decision and decision.variant:
                    _add_exclusion(next_constraints, slot_index, decision.variant)

        current_row = script_by_index.get(index, {})
        forbidden_hits = [term for term in forbidden_terms if term and any(
            _has_asserted_forbidden_term(term, str(current_row.get(field) or ""))
            for field in ("画面描述", "声音", "字幕", "备注", "效果")
        )]
        if forbidden_hits:
            _add_check(
                checks, "brief_forbidden_hit", "hard", "fail", shot_no,
                f"镜头内容命中 Brief 禁忌: {', '.join(forbidden_hits)}",
                "下一轮全局排除命中禁忌的候选素材",
            )
            if adopted_source:
                _append_unique(next_constraints["global_excludes"], _source_key_dict(adopted_source))

        source_forbidden_hits = [
            term
            for term in forbidden_terms
            if adopted_source and term and term in adopted_source.searchable_text()
        ]
        if source_forbidden_hits:
            _add_check(
                checks,
                "brief_forbidden_source_hit",
                "warning",
                "warning",
                shot_no,
                f"参考来源命中 Brief 禁忌，需核对最终镜头表达: {', '.join(source_forbidden_hits)}",
                "允许保留参考来源；请人工核对最终文案和画面描述是否符合当前项目要求",
            )
        if adopted_source and is_reuse_blocked(adopted_source.reuse_level):
            _add_check(
                checks, "disabled_source_used", "hard", "fail", shot_no,
                "可复用等级=禁用 的素材进入输出",
                "下一轮全局排除该候选素材",
            )
            _append_unique(next_constraints["global_excludes"], _source_key_dict(adopted_source))

        source = adopted_source
        if source:
            for field in source.missing_metadata:
                key = (source.source_file, source.source_sheet)
                missing_metadata_by_source.setdefault(key, set()).add(field)
            script_row = script_by_index.get(index, {})
            if not (script_row.get("画面示意") or script_row.get("效果示意")):
                _add_check(
                    checks, "missing_image", "warning", "warning", shot_no,
                    "该镜头没有可用嵌入图；文字和来源三元组保留供人工核对，配图仍待确认",
                    "脚本文档可继续审核；成片制作前再补充当前项目图片或视频",
                )
    _audit_bound_visual_domain_safety(
        checks,
        brief,
        forbidden_terms,
        detail_rows,
        visual_asset_rows or [],
        next_constraints,
    )

    for (source_file, source_sheet), fields in sorted(missing_metadata_by_source.items()):
        _add_check(
            checks, "missing_reuse_metadata", "warning", "warning", "",
            f"{source_file}/{source_sheet} 源表缺少: {', '.join(sorted(fields))}",
            "后续补充源表复用字段；MVP 阶段按源表聚合警告放行",
        )

    duplicate_sources = _duplicate_sources(detail_rows)
    for source, count in duplicate_sources.items():
        _add_check(
            checks, "duplicate_source", "warning", "warning", "",
            f"创意库来源重复 {count} 次: {source}",
            "强匹配重复可接受；人工复核是否为模板重复转场",
        )

    if revision:
        # FR-13/14 allow editing intermediate drafts. Keep deviations visible
        # without changing the confirmed Brief or weakening identity checks.
        for check in checks:
            if check["检查项"] in {"structure_mismatch", "duration_total_mismatch"} and (
                check["检查项"] != "duration_total_mismatch"
                or all((_row_duration(row) or 0) > 0 for row in script_rows)
            ):
                check.update({"级别": "warning", "状态": "warning"})
                check["修复建议"] = "草稿已保留本次修改；复核与原 Brief 的偏差，必要时通过事实修订重新确认"
    _normalize_audit_domains(checks)
    hard_fail_count = sum(1 for check in checks if check["级别"] == "hard")
    warning_count = sum(1 for check in checks if check["级别"] == "warning")
    blocking_items = list(dict.fromkeys(
        check["检查项"] for check in checks if check["级别"] == "hard"
    ))
    warning_penalty = min(30, warning_count)
    score = max(0, 100 - hard_fail_count * 25 - warning_penalty)
    _normalize_constraints(next_constraints)
    result = {
        "status": AUDIT_STATUS_PASS if hard_fail_count == 0 else AUDIT_STATUS_FAIL,
        "hard_fail_count": hard_fail_count,
        "warning_count": warning_count,
        "score": score,
        "blocking_items": blocking_items,
        "next_constraints": next_constraints,
        "checks": checks,
    }
    result.update(_domain_summaries(checks))
    return result


def audit_rows(result: dict) -> list[dict]:
    return list(result.get("checks", []))


def _audit_generic_contract(
    checks: list[dict],
    brief: dict,
    template_slots: list[SourceShot],
    decisions: list[TemplateDecision],
    script_rows: list[dict],
    detail_rows: list[dict],
) -> None:
    script_type = normalize_script_type(brief.get("脚本类型", ""))
    try:
        target_duration = validate_script_brief(brief)
    except ValueError as exc:
        _add_check(
            checks,
            "invalid_script_duration",
            "hard",
            "fail",
            "",
            str(exc),
            f"把{script_type} Brief 时长修正为正整数秒",
        )
        target_duration = 0

    expected_count = len(template_slots)
    counts = {
        "template": expected_count,
        "decisions": len(decisions),
        "script": len(script_rows),
        "details": len(detail_rows),
    }
    if not expected_count or any(value != expected_count for value in counts.values()):
        _add_check(
            checks,
            "invalid_script_slot_count",
            "hard",
            "fail",
            "",
            f"{script_type}必须按模板动态镜数生成: {counts}",
            "按当前类别模板的完整槽位顺序重新生成",
        )

    durations = [_row_duration(row) for row in script_rows]
    if target_duration and (
        any(value is None for value in durations)
        or sum(value or 0 for value in durations) != target_duration
    ):
        _add_check(
            checks,
            "duration_total_mismatch",
            "hard",
            "fail",
            "",
            f"逐镜时长未满足 Brief: target={target_duration}s, actual={sum(value or 0 for value in durations)}s",
            "重新分配逐镜时长并保证总和等于 Brief",
        )

    expected_brand = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
    decision_by_slot = {
        int(decision.slot.slot_index or index): decision
        for index, decision in enumerate(decisions, start=1)
    }
    for index, detail in enumerate(detail_rows, start=1):
        shot_no = str(detail.get("模板镜号") or index)
        if not (
            detail.get("采用来源文件")
            and detail.get("采用来源 sheet")
            and detail.get("采用来源行")
        ):
            _add_check(
                checks,
                "missing_source_trace",
                "hard",
                "fail",
                shot_no,
                "采用来源缺少来源文件、Sheet 或行号",
                "从已审核类别表重新选择并写入完整来源三元组",
            )
        decision = decision_by_slot.get(int(detail.get("slot_index") or index))
        expected_function = str(
            (decision.slot.shot_function if decision else "") or ""
        ).strip()
        actual_function = str(detail.get("槽位功能") or "").strip()
        if expected_function and actual_function != expected_function:
            _add_check(
                checks,
                "category_function_contract_mismatch",
                "hard",
                "fail",
                shot_no,
                f"类别镜头功能被改变: expected={expected_function}, actual={actual_function}",
                "恢复该类别 AI_规范表定义的镜头功能",
            )
        adopted = None
        if decision:
            adopted = (
                decision.variant
                if decision.mode in {"REPLACE", "ADAPT"} and decision.variant
                else decision.slot
            )
        row_text = _row_text(script_rows[index - 1]) if index <= len(script_rows) else ""
        if adopted and source_brand_leaks(expected_brand, adopted.source_brand, row_text):
            _add_check(
                checks,
                "source_brand_leak",
                "hard",
                "fail",
                shot_no,
                f"最终镜头文案残留来源品牌: {adopted.source_brand}",
                "保留镜头功能，但把来源品牌与专属事实改写为当前 Brief",
            )
        source_specific_terms = _source_specific_latin_terms(
            row_text,
            str(detail.get("采用来源证据") or ""),
            brief,
        )
        if source_specific_terms:
            _add_check(
                checks,
                "source_specific_term_leak",
                "hard",
                "fail",
                shot_no,
                "最终文案残留 Brief 未提供的来源专属词: "
                + ", ".join(source_specific_terms),
                "保留镜头功能，删除或改写源案主题名、装置名和专属文字",
            )
        brief_text = " ".join(str(value or "") for value in brief.values())
        unconfirmed_locations = [
            term for term in UNCONFIRMED_LOCATION_TERMS
            if term in row_text and term not in brief_text
        ]
        if unconfirmed_locations:
            _add_check(
                checks,
                "unconfirmed_location_fact",
                "hard",
                "fail",
                shot_no,
                "最终文案写入 Brief 未提供的地点: " + ", ".join(unconfirmed_locations),
                "删除来源案例地点，或将真实举办地补入 Brief 后重新生成",
            )
        unsupported_subjects = [
            term for term in SOURCE_SPECIFIC_SUBJECT_TERMS
            if term in row_text and term not in brief_text
        ]
        if unsupported_subjects:
            _add_check(
                checks,
                "unsupported_source_subject",
                "hard",
                "fail",
                shot_no,
                "最终文案带入 Brief 未提供的来源案例主体: " + ", ".join(unsupported_subjects),
                "只保留镜头功能，把来源案例主体改写为当前项目已确认的产品、展项或人物动作",
            )

    row_texts = [_row_text(row) for row in script_rows]
    duplicate_descriptions: dict[str, list[tuple[str, str]]] = {}
    for index, row in enumerate(script_rows, start=1):
        description = _normalize_required_text(row.get("画面描述", ""))
        if not description:
            continue
        decision = decision_by_slot.get(index)
        function = str(
            (decision.slot.shot_function if decision else "") or ""
        ).strip()
        duplicate_descriptions.setdefault(description, []).append((str(index), function))
    for items in duplicate_descriptions.values():
        functions = {function for _, function in items if function}
        if len(items) > 1 and len(functions) > 1:
            shots = ",".join(shot_no for shot_no, _ in items)
            _add_check(
                checks,
                "duplicate_distinct_function_description",
                "hard",
                "fail",
                shots,
                "不同镜头功能被写成完全相同的画面描述: "
                + "、".join(sorted(functions)),
                "从 ScriptPlan 重新匹配同功能来源，并逐镜重写可执行画面",
            )
    missing_terms = [
        term
        for term in _positive_required_terms(brief)
        if not any(_required_term_present_in_row(term, row) for row in script_rows)
    ]
    if missing_terms:
        _add_check(
            checks,
            "required_content_missing",
            "hard",
            "fail",
            "",
            f"Brief 必须内容未覆盖: {', '.join(missing_terms)}",
            "将必须内容写入承担相应镜头功能的模板槽位",
        )


def audit_output_workbook_format(
    path: str | Path,
    expected_image_count: int = 0,
    template_workbook_path: str | Path | None = None,
    script_type: str = "发布会",
    duration_seconds: int = 60,
) -> list[dict]:
    checks: list[dict] = []
    try:
        wb = load_workbook(path)
    except Exception as exc:
        _add_check(
            checks, "output_workbook_unreadable", "hard", "fail", "",
            f"输出 Excel 无法重新打开: {exc}",
            "重新写出 Excel 并确认文件未损坏",
        )
        return checks
    try:
        if "脚本" not in wb.sheetnames:
            _add_check(
                checks, "script_sheet_missing", "hard", "fail", "",
                "输出缺少 脚本 sheet",
                "按模板输出 脚本 sheet",
            )
            return checks
        ws = wb["脚本"]
        merges = {str(item) for item in ws.merged_cells.ranges}
        required_merges, expected_headers = _expected_template_header_contract(
            template_workbook_path
        )
        missing_merges = sorted(required_merges - merges)
        project_name = _workbook_project_name(wb)
        normalized_type = normalize_script_type(script_type)
        expected_title = (
            f"{project_name}｜{duration_seconds}秒{normalized_type}脚本"
            if project_name else "M+发布会类通用模版"
        )
        header_ok = (
            ws["A1"].value == expected_title
            and all(ws.cell(row, col).value == value for row, col, value in expected_headers)
        )
        if missing_merges or not header_ok:
            _add_check(
                checks, "template_layout_missing", "hard", "fail", "",
                f"脚本 sheet 未继承模板标题/合并表头: missing_merges={missing_merges}",
                f"使用 M+{normalized_type}类通用脚本库 / 模版 sheet 作为输出母版",
            )
        expected_widths = _expected_template_widths(template_workbook_path)
        if any(
            (ws.column_dimensions[column].width or 0) < width
            for column, width in expected_widths.items()
        ):
            _add_check(
                checks, "template_column_width_mismatch", "hard", "fail", "",
                "脚本 sheet 关键列宽低于模板规格",
                "复制模板 C/I 等列宽后再写入数据",
            )
        if ws.max_row >= 7 and (ws.row_dimensions[7].height or 0) < 120:
            _add_check(
                checks, "template_row_height_mismatch", "hard", "fail", "",
                "脚本 sheet 数据行高度低于模板规格",
                "复制模板数据行行高，给图片和分镜描述留出空间",
            )
        actual_image_count = len(getattr(ws, "_images", []))
        if actual_image_count < expected_image_count:
            _add_check(
                checks, "embedded_image_count_mismatch", "hard", "fail", "",
                f"嵌入图片数量不足: expected={expected_image_count}, actual={actual_image_count}",
                "把本地图片文件嵌入 画面示意/效果示意 单元格，而不是只写路径",
            )
        naked_paths = _naked_image_paths(ws)
        if naked_paths:
            _add_check(
                checks, "image_path_not_embedded", "hard", "fail", "",
                f"图片列仍裸露本地路径: {', '.join(naked_paths[:5])}",
                "图片嵌入成功后清空图片列文本，路径保留在抽调明细中",
            )
    finally:
        wb.close()
    return checks


def _workbook_project_name(wb) -> str:
    from .excel_io import read_managed_sheet_rows

    for row in read_managed_sheet_rows(wb, "抽调明细"):
        value = str(row.get("项目名称") or "").strip()
        if value:
            return value
    return ""


def _expected_template_widths(path: str | Path | None) -> dict[str, float]:
    if path:
        workbook_path = Path(path)
        if workbook_path.is_file():
            template = load_workbook(workbook_path, read_only=False, data_only=False)
            try:
                sheet_name = "模版" if "模版" in template.sheetnames else "脚本"
                if sheet_name in template.sheetnames:
                    ws = template[sheet_name]
                    return {
                        column: float(ws.column_dimensions[column].width or 0)
                        for column in ("C", "I")
                    }
            finally:
                template.close()
    return {"C": 80.0, "I": 60.0}


def _expected_template_header_contract(
    path: str | Path | None,
) -> tuple[set[str], list[tuple[int, int, object]]]:
    if path:
        workbook_path = Path(path)
        if workbook_path.is_file():
            template = load_workbook(workbook_path, read_only=False, data_only=False)
            try:
                if "模版" in template.sheetnames:
                    ws = template["模版"]
                    from .excel_io import _template_content_column_count
                    content_columns = _template_content_column_count(ws)
                    merges = set()
                    for merged in ws.merged_cells.ranges:
                        min_col, min_row, max_col, max_row = (
                            merged.min_col,
                            merged.min_row,
                            merged.max_col,
                            merged.max_row,
                        )
                        if max_row > 6 or min_col > content_columns:
                            continue
                        max_col = min(max_col, content_columns)
                        merges.add(
                            f"{ws.cell(min_row, min_col).coordinate}:"
                            f"{ws.cell(max_row, max_col).coordinate}"
                        )
                    headers = [
                        (row, col, ws.cell(row, col).value)
                        for row in (5, 6)
                        for col in range(1, content_columns + 1)
                        if ws.cell(row, col).value not in (None, "")
                    ]
                    return merges, headers
            finally:
                template.close()
    return (
        {"A1:K1", "A5:A6", "B5:B6", "C5:K5"},
        [(5, 1, "结构"), (5, 2, "镜号"), (6, 3, "画面示意"), (6, 9, "画面描述")],
    )


def add_audit_checks(result: dict, extra_checks: list[dict]) -> dict:
    if not extra_checks:
        return result
    merged = {**result, "checks": list(result.get("checks", [])) + extra_checks}
    _normalize_audit_domains(merged["checks"])
    hard_fail_count = sum(1 for check in merged["checks"] if check["级别"] == "hard")
    warning_count = sum(1 for check in merged["checks"] if check["级别"] == "warning")
    warning_penalty = min(30, warning_count)
    merged["hard_fail_count"] = hard_fail_count
    merged["warning_count"] = warning_count
    merged["score"] = max(0, 100 - hard_fail_count * 25 - warning_penalty)
    merged["status"] = AUDIT_STATUS_PASS if hard_fail_count == 0 else AUDIT_STATUS_FAIL
    merged["blocking_items"] = list(dict.fromkeys(
        check["检查项"] for check in merged["checks"] if check["级别"] == "hard"
    ))
    merged.update(_domain_summaries(merged["checks"]))
    return merged


def write_audit_json(path: str | Path, result: dict) -> None:
    Path(path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _add_check(
    checks: list[dict],
    item: str,
    level: str,
    status: str,
    shot_no: str,
    detail: str,
    suggestion: str,
    audit_domain: str | None = None,
) -> None:
    checks.append({
        "检查项": item,
        "级别": level,
        "状态": status,
        "关联镜号": shot_no,
        "详情": detail,
        "修复建议": suggestion,
        "audit_domain": audit_domain or _audit_domain_for_item(item),
    })


def _audit_domain_for_item(item: str) -> str:
    return MEDIA_READINESS_DOMAIN if item in _MEDIA_READINESS_ITEMS else SCRIPT_DOCUMENT_DOMAIN


def _normalize_audit_domains(checks: list[dict]) -> None:
    for check in checks:
        check.setdefault(
            "audit_domain",
            _audit_domain_for_item(str(check.get("检查项") or "")),
        )


def _domain_summaries(checks: list[dict]) -> dict:
    document_checks = [
        check for check in checks
        if check.get("audit_domain") == SCRIPT_DOCUMENT_DOMAIN
    ]
    readiness_checks = [
        check for check in checks
        if check.get("audit_domain") == MEDIA_READINESS_DOMAIN
    ]
    document_hard = sum(check.get("级别") == "hard" for check in document_checks)
    document_warnings = sum(check.get("级别") == "warning" for check in document_checks)
    readiness_hard = sum(check.get("级别") == "hard" for check in readiness_checks)
    readiness_warnings = sum(check.get("级别") == "warning" for check in readiness_checks)
    return {
        "script_document_audit": {
            "status": "fail" if document_hard else "pass",
            "hard_fail_count": document_hard,
            "warning_count": document_warnings,
        },
        "media_readiness": {
            "status": (
                "not_assessed" if not readiness_checks
                else "not_ready" if readiness_hard or readiness_warnings else "ready"
            ),
            "hard_fail_count": readiness_hard,
            "warning_count": readiness_warnings,
        },
    }


def _split_terms(value: str) -> list[str]:
    return list(dict.fromkeys(_split_requirement_text(value)))


def _source_specific_latin_terms(text: str, source_evidence: str, brief: dict) -> list[str]:
    allowed = {
        "AI", "AR", "BGM", "CEO", "CFO", "CTO", "DJ", "FPV", "IP", "KV",
        "KOL", "LED", "LOGO", "PPT", "VCR", "VJ", "VR", "XR",
    }
    brief_text = " ".join(str(value or "") for value in brief.values()).upper()
    evidence_text = str(source_evidence or "").upper()
    candidates = re.findall(
        r"(?<![A-Z0-9])[A-Z][A-Z0-9+.-]{2,}(?![A-Z0-9])",
        str(text or ""),
    )
    return list(dict.fromkeys(
        term
        for term in candidates
        if term not in allowed
        and term not in brief_text
        and term in evidence_text
    ))


def _row_text(row: dict) -> str:
    return " ".join(str(row.get(key, "") or "") for key in ("画面描述", "声音", "字幕", "备注", "效果"))


def _positive_required_terms(brief: dict) -> list[str]:
    return [clause["text"] for clause in brief_content_clauses(brief)
            if clause["classification"] == "mandatory_content"]


def _required_term_present_in_row(term: str, row: dict) -> bool:
    if str(term).strip().endswith("字幕"):
        # Caption requirements must be present in the caption field or an explicit
        # legacy inline caption instruction, not merely spoken or visible content.
        if _required_term_present(term, str(row.get("字幕") or "")):
            return True
        return any("字幕" in clause and _required_term_present(term, clause)
                   for clause in re.split(r"[。；;\n]", _row_text(row)))
    return _required_term_present(term, _row_text(row))


def _has_asserted_forbidden_term(term: str, text: str) -> bool:
    """A stated exclusion is not evidence that forbidden content is present."""
    clauses = re.split(r"[，。,;；\n]|但|然而|却|随后|转而|而是|并(?=展示|出现|呈现|拍摄|露出|引入)", text)
    negation = re.compile(r"没有|未见|未出现|不(?:出现|展示|呈现|写|拍摄|包含|含)|禁止|不得|避免|无(?:任何|明确|相关)?$")
    assertion = re.compile(r"展示|出现|呈现|拍摄|包含|露出|引入|印有|写着")
    for clause in clauses:
        for match in re.finditer(re.escape(term), clause):
            prefix = clause[:match.start()]
            negatives = list(negation.finditer(prefix))
            if not negatives or assertion.search(prefix[negatives[-1].end():]):
                return True
    return False


def _audit_launch_contract(
    checks: list[dict],
    brief: dict,
    template_slots: list[SourceShot],
    decisions: list[TemplateDecision],
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    require_complete_visuals: bool = False,
) -> None:
    try:
        target_duration = validate_launch_brief(brief)
    except ValueError as exc:
        _add_check(
            checks,
            "invalid_launch_duration",
            "hard",
            "fail",
            "",
            str(exc),
            "把 brief 视频时长填写明确的正整数总秒数，并核对逐镜时长之和表达：60、60s 或 60秒",
        )
        target_duration = 0
    counts = {
        "template": len(template_slots),
        "decisions": len(decisions),
        "script": len(script_rows),
        "details": len(detail_rows),
    }
    expected_count = len(template_slots)
    if not template_count_valid(expected_count) or any(
        value != expected_count for value in counts.values()
    ):
        _add_check(
            checks,
            "invalid_launch_slot_count",
            "hard",
            "fail",
            "",
            "发布会决策、脚本和明细数量必须与模板槽位数一致；"
            f"当前 template={counts['template']}, decisions={counts['decisions']}, "
            f"script={counts['script']}, details={counts['details']}",
            "按当前模板槽位数重建决策、脚本和明细后再执行发布会审计",
        )
    slot_mapping_valid = _audit_launch_slot_mapping(
        checks, template_slots, decisions, script_rows, detail_rows
    )
    if slot_mapping_valid:
        _audit_decision_detail_consistency(checks, decisions, detail_rows)
        _audit_script_basis_completeness(
            checks,
            decisions,
            detail_rows,
            visual_asset_rows,
        )
    if require_complete_visuals:
        _audit_launch_visual_completeness(checks, detail_rows, visual_asset_rows)
    durations = [_row_duration(row) for row in script_rows]
    if target_duration and (any(value is None for value in durations) or sum(value or 0 for value in durations) != target_duration):
        actual = sum(value or 0 for value in durations)
        missing = sum(value is None for value in durations)
        _add_check(
            checks,
            "duration_total_mismatch",
            "hard",
            "fail",
            "",
            f"逐镜时长未满足 Brief: target={target_duration}s, actual={actual}s, missing={missing}",
            "为全部模板镜头写入逐镜建议时长，并保证总和等于 Brief 时长",
        )

    row_texts = [_row_text(row) for row in script_rows]
    missing_terms = [
        term for term in _positive_required_terms(brief)
        if not any(_required_term_present_in_row(term, row) for row in script_rows)
    ]
    if missing_terms:
        _add_check(
            checks,
            "required_content_missing",
            "hard",
            "fail",
            "",
            f"Brief 必须内容未覆盖: {', '.join(missing_terms)}",
            "重新生成对应槽位，不能只在审计说明中补写",
        )
    text = " ".join(row_texts)

    if slot_mapping_valid:
        _audit_required_content_evidence(
            checks,
            brief,
            decisions,
            script_rows,
            detail_rows,
            visual_asset_rows,
        )

    expected_brand = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
    wrong_brands = list(dict.fromkeys([
        *conflicting_brands(expected_brand, text),
        *(
            brand
            for row in script_rows
            for key in ("画面描述", "声音", "字幕", "备注", "效果")
            for brand in unknown_brand_mentions(expected_brand, str(row.get(key, "") or ""))
        ),
    ]))
    if wrong_brands:
        _add_check(
            checks,
            "output_brand_conflict",
            "hard",
            "fail",
            "",
            f"最终脚本文案出现其他品牌: {', '.join(wrong_brands)}",
            "创意来源可跨品牌，但最终脚本文案和视觉资产必须回写为当前项目内容",
        )


def _should_audit_launch_contract(
    brief: dict,
    template_slots: list[SourceShot],
    script_rows: list[dict],
) -> bool:
    script_types = {
        value.strip()
        for value in re.split(r"[,，、;/；]", str(brief.get("脚本类型") or ""))
        if value.strip()
    }
    return "发布会" in script_types


def _row_duration(row: dict) -> Decimal | None:
    value = row.get("时长(秒)")
    if value not in (None, ""):
        try:
            duration = Decimal(str(value).strip())
            return duration if duration.is_finite() else None
        except (InvalidOperation, TypeError, ValueError):
            return None
    match = re.search(r"建议时长[：:]\s*(\d+)s", str(row.get("备注") or ""))
    return Decimal(match.group(1)) if match else None


def _required_term_present(term: str, text: str) -> bool:
    normalized_term = _normalize_required_text(term)
    normalized_text = _normalize_required_text(text)
    # Modal wording is not literal on-screen copy. Reuse the planner's strict
    # short-phrase check; identifiers, numbers and longer claims stay literal.
    attribute = re.fullmatch(r"(?:仅|只)以(.+?)(?:表现|呈现|展示)", normalized_term)
    if attribute:
        return _has_positive_required_signal(attribute.group(1), normalized_text)
    content_phrase = re.sub(r"^(?:某个|某款|某一款|某一位)", "", normalized_term)
    if content_phrase != normalized_term and re.fullmatch(r"[\u4e00-\u9fff]{4}", content_phrase):
        subject, action = content_phrase[:2], content_phrase[2:]
        return any(_short_action_match(content_phrase, clause)
            and _has_positive_required_signal(subject, clause)
            and _has_positive_required_signal(action, clause)
            and re.search(re.escape(subject) + r".*?" + re.escape(action), clause)
            for clause in re.split(r"[，,。；;\n]", text))
    if normalized_term.endswith("字幕") and len(normalized_term) > 2:
        return _has_positive_required_signal(normalized_term[:-2], normalized_text)
    if normalized_term == "外观":
        return any(_has_positive_required_signal(value, normalized_text)
                   for value in ("外观", "机身", "外壳", "外形", "产品全貌", "产品轮廓"))
    caption = re.search(r"(?:字幕|文案)(?:内容|文字)?[：:\s]*[“\"]?(.+?)[”\"]?$", term)
    if caption:
        return _has_positive_required_signal(caption.group(1), normalized_text)
    inscription = re.fullmatch(
        r"(杯身|杯盖|包装|背景板)(?:上)?(?:简单|清晰)?(?:写|印有|字样为)[：:\s]*[“\"]?(.+?)[”\"]?", term,
    )
    if inscription:
        return all(_has_positive_required_signal(value, normalized_text) for value in inscription.groups())
    if "空镜" in normalized_term:
        area_terms = (
            ("外场", "场外", "外部") if any(value in normalized_term for value in ("外场", "场外"))
            else ("内场", "场内", "会场内") if any(value in normalized_term for value in ("内场", "场内"))
            else ()
        )
        if (
            _has_positive_required_signal("空镜", normalized_text)
            and (not area_terms or any(_has_positive_required_signal(value, normalized_text) for value in area_terms))
        ):
            return True
    representative = re.fullmatch(r"(国内|国外)代表(\d+)发言(?:全景中景)?", normalized_term)
    if representative:
        identity = f"{representative.group(1)}代表{representative.group(2)}"
        return (
            _has_positive_required_signal(identity, normalized_text)
            and _has_positive_required_signal("发言", normalized_text)
            and _has_positive_required_signal("全景", normalized_text)
            and _has_positive_required_signal("中景", normalized_text)
        )
    if normalized_term == "机器狗展示":
        return (
            _has_positive_required_signal("机器狗", normalized_text)
            and any(_has_positive_required_signal(value, normalized_text) for value in ("展示", "出现", "表演"))
        )
    if normalized_term in {"员工入场", "全员入场"}:
        return (
            any(value in text for value in ("员工", "全员", "参会者", "来宾"))
            and any(value in text for value in (
                "入场", "进入", "走进入口", "穿过入口", "通过入口", "签到",
            ))
        )
    if normalized_term in {"主视觉", "活动主视觉"}:
        return (
            any(value in text for value in (
                "主视觉", "主题KV", "活动KV", "活动标题", "活动名称",
            ))
            or (
                any(value in text for value in ("主舞台", "大屏", "屏幕", "舞台"))
                and any(value in text for value in (
                    "品牌标识", "活动标识", "主题字样", "标题", "名称", "渐显", "浮现",
                ))
            )
        )
    if "舞台" in normalized_term and any(
        value in normalized_term for value in ("表演", "演出")
    ):
        return (
            any(value in text for value in ("舞台", "现场"))
            and any(value in text for value in (
                "表演", "演出", "演唱", "演奏", "歌手", "乐队",
            ))
        )
    if all(value in normalized_term for value in ("观众", "灯光", "互动")):
        return (
            any(value in text for value in ("观众", "人群", "现场观众", "参与者"))
            and any(value in text for value in ("灯光", "光影", "灯海", "荧光", "发光"))
            and any(value in text for value in (
                "互动", "触摸", "触碰", "打卡", "合影", "挥舞", "举起手机",
            ))
        )
    if normalized_term.endswith("场地"):
        venue_core = normalized_term[:-2]
        if len(venue_core) >= 2 and venue_core not in {"活动", "举办", "项目"}:
            return venue_core in _normalize_required_text(text)
    if normalized_term in {"选手热身", "参赛者热身"}:
        return (
            any(value in text for value in ("选手", "参赛者", "跑者", "运动员"))
            and any(value in text for value in ("热身", "拉伸", "赛前准备"))
        )
    if normalized_term in {"企业logo", "品牌logo"}:
        return any(value in text for value in (
            "企业Logo", "企业LOGO", "企业logo", "企业标识",
            "品牌Logo", "品牌LOGO", "品牌logo", "品牌标识", "字标",
        ))
    if normalized_term in {"高管致辞", "领导致辞", "高管演讲"}:
        return (
            any(value in text for value in ("高管", "领导", "负责人", "公司代表"))
            and any(value in text for value in ("致辞", "演讲", "发言", "分享", "表达"))
        )
    if normalized_term in {"全员合影", "全体员工合影"}:
        return (
            any(value in text for value in ("全员", "全体员工", "员工", "团队"))
            and any(value in text for value in (
                "合影", "合照", "集体照", "排成多层队形", "定格",
            ))
        )
    if normalized_term in {"明星出场", "明星亮相", "艺人出场", "艺人亮相"}:
        return (
            any(value in text for value in ("明星", "艺人"))
            and any(value in text for value in (
                "出场", "亮相", "登场", "到场", "入场", "转身", "正面微笑",
            ))
        )
    if normalized_term in {"产品互动", "产品体验互动"}:
        return (
            any(value in text for value in ("产品", "展品", "商品"))
            and any(value in text for value in (
                "互动", "体验", "试用", "操作", "触摸", "试香", "上手",
            ))
        )
    if normalized_term in {"冲线或颁奖", "冲线或领奖"}:
        return any(value in text for value in (
            "冲线", "冲过终点", "颁奖", "领奖", "奖牌", "奖杯",
        ))
    if normalized_term in {"起跑集结", "赛前集结"}:
        return (
            any(value in text for value in ("参赛者", "跑者", "选手", "运动员", "方阵", "人群"))
            and any(value in text for value in ("起跑", "起点", "集结", "列队", "方阵"))
        )
    if normalized_term in {"赛事标识", "赛事logo"}:
        return (
            any(value in text for value in ("赛事", "挑战赛", "比赛", "活动"))
            and any(value in text for value in ("主视觉", "品牌标识", "活动标识", "LOGO", "logo", "标题"))
        )
    if normalized_term in {"战队入场", "队伍入场"}:
        return (
            any(value in text for value in ("战队", "队伍", "选手", "参赛队"))
            and any(value in text for value in ("入场", "步入", "登场", "进入内场", "穿过入口"))
        )
    if normalized_term in {"邀请函入场", "邀请函入口"}:
        return (
            "邀请函" in text
            and any(value in text for value in ("入场", "入口", "进入", "前行"))
        )
    if normalized_term in {"设备准备", "外设准备"}:
        return (
            any(value in text for value in ("设备", "外设", "主机", "键盘", "鼠标", "电脑"))
            and any(value in text for value in ("准备", "就位", "调试", "调校", "连接", "操作"))
        )
    if normalized_term in {"比赛对抗", "竞技对抗"}:
        return (
            any(value in text for value in ("比赛", "竞技", "对局", "赛场", "对抗", "对战"))
            and any(value in text for value in ("对抗", "对战", "交锋", "对局", "开始", "选手", "玩家", "赛场"))
        )
    if normalized_term in {"观众应援", "粉丝应援"}:
        return (
            any(value in text for value in ("观众", "粉丝", "人群", "参与者"))
            and any(value in text for value in (
                "应援", "欢呼", "呐喊", "灯牌", "应援棒", "举起手机",
            ))
        )
    if normalized_term in {"颁奖合影", "领奖合影"}:
        return (
            any(value in text for value in ("颁奖", "领奖", "奖牌", "奖杯", "颁奖区"))
            and any(value in text for value in ("合影", "合照", "集体照", "定格"))
        )
    if term == "会场与品牌":
        return term in text or (
            any(value in text for value in ("会场", "活动场地", "主舞台", "门店外立面"))
            and any(value in text for value in ("品牌", "LOGO", "logo", "标识"))
        )
    if term == "艺人亮相":
        return term in text or (
            "艺人" in text
            and any(value in text for value in ("亮相", "登场", "入场", "转身", "正面微笑"))
        )
    if term == "艺人核心表达":
        return term in text or (
            "艺人" in text
            and any(value in text for value in ("核心感言", "核心表达", "发言", "演讲", "表达"))
        )
    if term == "品牌落版":
        return term in text or (
            (
                any(value in text for value in ("品牌标识", "企业标识"))
                or "logo" in text.casefold()
            )
            and any(value in text for value in ("黑场", "渐显", "浮现", "淡入", "淡出", "定格", "收官"))
        )
    if term == "城市与活动信息":
        return term in text or (
            any(value in text for value in ("城市信息", "城市名称", "城市天际线", "海岸城市"))
            and any(value in text for value in ("活动", "音乐节", "活动日期"))
        )
    if term == "歌手演出":
        return term in text or (
            "歌手" in text
            and any(value in text for value in ("演出", "表演", "演唱", "演奏"))
        )
    if term == "观众互动":
        return term in text or (
            "观众" in text
            and any(value in text for value in ("互动", "触摸", "合影", "挥手", "举杯"))
        )
    if term == "活动品牌落版":
        return term in text or (
            any(value in text for value in ("品牌标识", "品牌LOGO", "品牌主视觉"))
            and any(value in text for value in ("落版", "渐显", "定格", "停留", "淡出"))
        )
    if term == "举办城市与活动信息":
        return term in text or (
            any(value in text for value in ("举办城市", "城市主干道", "城市地标", "城市风貌"))
            and any(value in text for value in ("活动信息", "活动日期", "活动氛围", "赛事"))
        )
    if term == "发令启动":
        return term in text or (
            "发令" in text
            and any(value in text for value in ("启动", "起跑", "冲出"))
        )
    if term == "竞技瞬间":
        return term in text or any(value in text for value in (
            "冲出起点", "冲刺瞬间", "比赛瞬间", "挑战瞬间",
            "体能测试", "运动员极限", "赛道竞技",
        ))
    if term == "赛事主题落版":
        return term in text or (
            any(value in text for value in ("赛事", "挑战赛"))
            and any(value in text for value in ("主视觉", "主题落版", "品牌落版", "居中定格"))
        )
    if term == "团队颁奖":
        return term in text or (
            any(value in text for value in ("团队", "队伍", "集体"))
            and any(value in text for value in ("颁奖", "领奖", "接受颁奖"))
        )
    if term == "集体合影":
        return term in text or (
            any(value in text for value in ("全体", "集体", "团队", "员工"))
            and any(value in text for value in ("合影", "合照", "集体照", "大合照"))
        )
    if term == "人流互动":
        people_terms = (
            "人流", "人群", "观众", "参会者", "嘉宾", "围观",
            "参观者", "顾客", "客户", "来宾", "宾客", "年轻人", "参与者",
        )
        interaction_terms = ("互动", "交流", "体验", "打卡")
        return term in text or (
            any(value in text for value in people_terms)
            and any(value in text for value in interaction_terms)
        )
    if term == "核心发布仪式":
        return any(value in text for value in (
            "核心发布仪式",
            "启动仪式",
            "仪式启动",
            "认证仪式",
            "颁发仪式",
            "颁奖仪式",
            "颁奖",
        ))
    if term == "主题装置":
        return any(value in text for value in (
            "主题装置", "装置空镜", "空间装置", "舞台装置", "霓虹灯装置",
            "城市天际线装置", "城市天际线霓虹灯装置", "艺术装置",
            "路灯装置", "发光装置", "沉浸式装置",
        ))
    if term == "产品体验":
        return term in text or any(value in text for value in (
            "骑动感单车", "动感单车体验", "健身体验",
        )) or (
            any(value in text for value in ("产品", "家居", "设备", "装置"))
            and any(value in text for value in ("体验", "互动", "试用", "操作", "试坐"))
        )
    aliases = {
        "品牌logo": ("品牌logo", "logo", "LOGO", "品牌标识"),
        "活动主会场": ("活动主会场", "主会场", "发布会现场", "会场"),
        "装置空镜": (
            "装置空镜", "环形装置", "空间装置", "装置与主会场",
            "发光圆形装置", "装置艺术", "舞台装置",
        ),
    }
    values = aliases.get(term, _generic_required_aliases(term))
    return any(
        value and (
            value in text
            or _normalize_required_text(value) in normalized_text
        )
        for value in values
    ) or _compositional_required_match(term, text) or _short_action_match(normalized_term, normalized_text)


def _normalize_required_text(value: str) -> str:
    """Ignore display-only spacing and punctuation in Brief coverage checks."""
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())


def _compositional_required_match(term: str, text: str) -> bool:
    """Match new Brief compounds without making a single generic word sufficient."""
    normalized_term = _normalize_required_text(term)
    normalized_text = _normalize_required_text(text)
    concepts = (
        (("邀请函", "邀请卡"), ("邀请函", "邀请卡", "入场卡", "通行证", "宣传卡")),
        (("第一视角", "第一人称"), ("第一视角", "第一人称", "手持", "双手举起")),
        (("入场", "入口"), ("入场", "入口", "进入", "步入", "穿过入口", "前行")),
        (("网球",), ("网球", "球拍", "ACE GIRL")),
        (("挥拍", "击球"), ("挥拍", "击球", "发球", "回球", "扣杀")),
        (("运动员", "选手", "球员"), ("运动员", "选手", "球员", "参赛者", "女子", "男子")),
        (("赛事", "比赛"), ("赛事", "比赛", "参赛", "参赛者", "运动员", "选手")),
        (("集体", "团队", "全员"), ("集体", "团队", "全员", "全体", "多人", "参赛者")),
        (("合影", "合照"), ("合影", "合照", "集体照", "大合照", "定格")),
        (("主题", "品牌"), ("主题", "品牌", "主视觉", "标识", "赛事", "活动")),
        (("入口", "入场"), ("入口", "入场", "拱门", "门头", "导视", "签到区")),
        (("赛道",), ("赛道", "跑道", "赛车场", "竞赛路线")),
        (("装置",), ("装置", "设施", "展台", "展区")),
        (("轮胎",), ("轮胎",)),
        (("赛车", "汽车"), ("赛车", "汽车", "车辆", "卡丁车", "f1")),
        (("互动", "体验"), ("互动", "体验", "操作", "试用", "拆装", "敲击")),
        (("挑战", "竞技"), ("挑战", "竞技", "比赛", "竞赛", "操作", "拆装", "敲击")),
        (("观众", "人群", "粉丝"), ("观众", "人群", "粉丝", "参与者", "围观")),
        (("应援", "欢呼"), ("应援", "欢呼", "呐喊", "加油", "灯牌", "应援棒")),
        (("情侣",), ("情侣", "伴侣")),
        (("隔板",), ("隔板", "挡板")),
        (("撤开", "移开"), ("撤开", "移开", "打开")),
        (("甜蜜", "表情", "动作"), ("甜蜜", "表情", "微笑", "相视而笑", "拥抱", "牵手", "互动")),
        (("门店",), ("门店", "店内")),
        (("礼盒", "礼物"), ("礼盒", "礼物", "小礼品")),
        (("门头",), ("门头", "门店标识")),
        (("蝴蝶",), ("蝴蝶",)),
        (("kv", "欧式花园"), ("kv", "欧式花园")),
        (("飞出",), ("飞出", "飞起")),
        (("现场布置", "空镜"), ("现场布置", "现场空镜", "空镜")),
        (("转场", "落到"), ("转场", "落到", "飞向", "衔接")),
    )
    required_groups = [
        evidence_values
        for term_values, evidence_values in concepts
        if any(value in normalized_term for value in term_values)
    ]
    if len(required_groups) < 2:
        return False
    return all(
        any(_has_positive_required_signal(value, normalized_text) for value in values)
        for values in required_groups
    )


def _has_positive_required_signal(value: str, normalized_text: str) -> bool:
    needle = _normalize_required_text(value)
    if not needle:
        return False
    start = 0
    while True:
        index = normalized_text.find(needle, start)
        if index < 0:
            return False
        prefix = normalized_text[max(0, index - 8):index]
        negated = re.search(
            r"(?:没有|并无|缺少|缺乏|未出现|未提供|不含|不出现|不展示|不呈现|不拍摄|禁止|不得|避免|不要|未写|不写|未印|不印).{0,5}$"
            r"|无(?:明确|相关|任何|人员|人物|实际)?$",
            prefix,
        )
        if not negated:
            return True
        start = index + len(needle)


def _generic_required_aliases(term: str) -> tuple[str, ...]:
    common_aliases = {
        "品牌展台": ("品牌展台", "展台", "展区"),
        "核心产品": ("核心产品", "产品"),
        "互动体验": ("互动体验", "互动", "体验"),
        "品牌落版": ("品牌落版", "logo落版", "落版"),
        "员工签到": ("员工签到", "员工在签到", "签到墙", "签到仪式"),
        "团队颁奖": ("团队颁奖", "获奖团队", "团队接受颁奖", "团队领奖"),
        "荣誉环节": ("荣誉环节", "荣誉表彰", "颁奖", "领奖", "优秀团队"),
        "全员合影": ("全员合影", "全体员工合影", "员工集体合影", "大合影"),
        "赛事标识": ("赛事标识", "赛事logo", "赛事主视觉", "赛事主题"),
        "选手热身": ("选手热身", "参赛者热身", "跑者热身", "赛前热身"),
        "接力竞技": ("接力竞技", "接力比赛", "接力赛", "交接棒"),
        "主视觉": ("主视觉", "主题kv", "活动kv"),
        "活动场地": ("活动场地", "举办地", "主舞台", "会场", "场地属性"),
        "舞台表演": ("舞台表演", "舞台演出", "歌手演唱", "歌手表演", "乐队演出"),
        "活动空间": ("活动空间", "活动场地", "门店外立面", "主舞台", "会场"),
        "明星出场": ("明星出场", "明星亮相", "艺人出场", "艺人亮相", "艺人登场", "艺人到场"),
    }
    values = list(common_aliases.get(_normalize_required_text(term), (term,)))
    for suffix in ("主题字样", "主题文字", "字样"):
        if term.endswith(suffix):
            core = term[:-len(suffix)].strip()
            if len(core) >= 2:
                values.extend((core, f"{core}主题"))
            break
    return tuple(dict.fromkeys(values))


def _planned_event_allows_slot_term(term: str, planned_event_text: str) -> bool:
    if term in planned_event_text:
        return True
    return term == "落版" and any(
        marker in planned_event_text
        for marker in ("收束", "收尾", "片尾", "结尾", "结束")
    )


def _audit_required_content_evidence(
    checks: list[dict],
    brief: dict,
    decisions: list[TemplateDecision],
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
) -> None:
    details_by_slot = {
        int(row.get("slot_index") or index): row
        for index, row in enumerate(detail_rows, start=1)
    }
    decisions_by_slot = {
        int(decision.slot.slot_index or index): decision
        for index, decision in enumerate(decisions, start=1)
    }
    for term in _positive_required_terms(brief):
        matching_slots = {
            index
            for index, row in enumerate(script_rows, start=1)
            if _required_term_present(term, _row_text(row))
        }
        if not matching_slots:
            continue
        planned_slots = {
            slot
            for slot, detail in details_by_slot.items()
            if _required_term_present(
                term,
                " ".join(str(detail.get(field) or "") for field in (
                    "规划主事件", "规划Brief证据", "缺口说明",
                )),
            )
        }
        expected_slots = planned_slots or (
            _required_term_expected_slots(term, decisions)
            if normalize_script_type(brief.get("脚本类型") or "发布会") == "发布会"
            else {decision.slot.slot_index for decision in decisions}
        )
        eligible_slots = matching_slots & expected_slots
        if not eligible_slots:
            _add_check(
                checks,
                "required_content_slot_mismatch",
                "hard",
                "fail",
                ",".join(str(slot) for slot in sorted(matching_slots)),
                f"Brief 必须内容出现在错误槽位: {term}",
                "把必须内容放回承担该剪辑职责的槽位，并保留已审核来源依据",
            )
            continue
        if any(
            _detail_supports_required_term(
                term,
                details_by_slot.get(slot, {}),
                decisions_by_slot.get(slot),
                visual_asset_rows,
                brief,
            )
            for slot in eligible_slots
        ):
            continue
        _add_check(
            checks,
            "required_content_evidence_missing",
            "hard",
            "fail",
            ",".join(str(slot) for slot in sorted(eligible_slots)),
            f"Brief 必须内容只有脚本文字，没有来源或视觉证据: {term}",
            "重新选择已审核源文字或参考图能够支持的记录，禁止只补写生成文案",
        )


def _detail_supports_required_term(
    term: str,
    detail: dict,
    decision: TemplateDecision | None,
    visual_asset_rows: list[dict],
    brief: dict | None = None,
) -> bool:
    if brief and _brief_supports_required_term(term, brief, detail):
        return True
    adopted_source = None
    if decision is not None:
        adopted_source = (
            decision.variant
            if decision.mode in {"REPLACE", "ADAPT"} and decision.variant
            else decision.slot
        )
    if adopted_source is not None and _adopted_source_matches(detail, adopted_source):
        if _source_supports_required_term(term, _source_evidence_text(adopted_source)):
            return True
        reference_evidence = " ".join(filter(None, (
            adopted_source.reference_image_evidence,
            adopted_source.effect_reference_evidence,
        )))
        if reference_evidence and _required_term_present(term, reference_evidence):
            return True
    return any(
        _verified_visual_supports(term, detail, asset)
        for asset in visual_asset_rows
        if _slot_number(asset.get("slot_index")) == _slot_number(detail.get("slot_index"))
    )


def _brief_supports_required_term(term: str, brief: dict, detail: dict) -> bool:
    formal = brief.get("_formal_fact_evidence")
    required_evidence = formal.get("必须出现的内容") if isinstance(formal, dict) else None
    if (
        isinstance(required_evidence, dict)
        and str(required_evidence.get("fact_id") or "").strip()
        and isinstance(required_evidence.get("evidence_refs"), list)
        and required_evidence["evidence_refs"]
        and all(_is_formal_evidence_ref(ref) for ref in required_evidence["evidence_refs"])
        and term in _positive_required_terms(brief)
    ):
        return True
    planning_evidence = str(detail.get("规划Brief证据") or "")
    if (
        str(detail.get("规划事件ID") or detail.get("规划要求集") or "").strip()
        and _required_term_present(term, planning_evidence)
        and term in _positive_required_terms(brief)
    ):
        return True
    if (
        not formal
        and not str(brief.get("Brief来源文件") or "").strip()
        and str(detail.get("需求行号") or "").strip()
        and term in _positive_required_terms(brief)
    ):
        # 兼容旧文本 Brief 入口：该路径没有文件证据三元组，
        # 但只信任服务端写入的原 Brief 条款和需求行号。
        return True
    source_path = Path(str(brief.get("_brief_source_path") or ""))
    source_file = str(brief.get("Brief来源文件") or "").strip()
    source_sheet = str(brief.get("Brief来源 sheet") or "").strip()
    source_row = str(brief.get("Brief来源行") or "").strip()
    source_sha256 = str(brief.get("Brief来源 SHA256") or "").strip()
    evidence = str(brief.get("Brief来源证据") or "")
    if not all((source_file, source_sheet, source_row, source_sha256, evidence)):
        return False
    if not source_path.is_file() or source_path.name != source_file:
        return False
    try:
        if sha256_file(source_path) != source_sha256:
            return False
    except OSError:
        return False
    if (
        str(detail.get("Brief来源文件") or "") != source_file
        or str(detail.get("Brief来源 sheet") or "") != source_sheet
        or str(detail.get("Brief来源行") or "") != source_row
        or str(detail.get("Brief来源 SHA256") or "") != source_sha256
        or str(detail.get("Brief来源证据") or "") != evidence
    ):
        return False
    normalized_term = _normalize_required_text(term)
    normalized_evidence = _normalize_required_text(evidence)
    return bool(normalized_term and normalized_term in normalized_evidence)


def _source_supports_required_term(term: str, text: str) -> bool:
    if _required_term_present(term, text):
        return True
    normalized_term = _normalize_required_text(term)
    normalized_text = _normalize_required_text(text)
    if "材料" in normalized_term and any(
        value in normalized_term for value in ("样品", "样本", "展品", "展示")
    ):
        return (
            any(value in normalized_text for value in (
                "材料", "材质", "面料", "纹理", "产品", "展品",
            ))
            and any(value in normalized_text for value in (
                "样品", "样本", "展示", "特写", "细节", "展台", "体验",
            ))
        )
    if "论坛" in normalized_term:
        return (
            any(value in normalized_text for value in (
                "论坛", "宣讲", "演讲", "发言", "分享", "致辞",
            ))
            and any(value in normalized_text for value in (
                "舞台", "观众", "会场", "展馆", "大屏", "现场",
            ))
        )
    if "装置" in normalized_term and any(
        value in normalized_term for value in ("材料", "可持续", "环保", "循环")
    ):
        return (
            "装置" in normalized_text
            and any(value in normalized_text for value in (
                "空间", "展台", "展馆", "艺术", "体验", "材质", "材料",
            ))
        )
    if normalized_term.endswith("场地"):
        return any(value in normalized_text for value in (
            "场地", "会场", "公园", "草坪", "草原", "户外", "环境", "目的地", "地标",
        ))
    if all(value in normalized_term for value in ("观众", "灯光", "互动")):
        return (
            any(value in normalized_text for value in ("观众", "人群", "参与者", "年轻人"))
            and any(value in normalized_text for value in (
                "互动", "触摸", "触碰", "打卡", "合影", "挥舞", "体验",
            ))
        )
    functional_aliases = {
        "员工入场": ("员工", "签到", "入口", "入场", "参会者", "来宾"),
        "年度回顾": ("高管", "致辞", "年度", "绩效", "愿景", "回顾", "影像", "大屏"),
        "赛事标识": ("赛事", "主视觉", "标题", "logo", "标识", "品牌"),
        "选手热身": ("选手", "参赛者", "运动员", "跑者", "热身", "运动", "挑战", "互动"),
        "接力竞技": ("赛事", "竞技", "比赛", "运动员", "选手", "跑者", "跑步", "赛道", "挑战"),
        "主视觉": ("主视觉", "kv", "标题", "品牌标识", "logo", "活动信息"),
    }
    aliases = functional_aliases.get(normalized_term, ())
    return any(value.casefold() in text.casefold() for value in aliases)


def _adopted_source_matches(detail: dict, source: SourceShot) -> bool:
    return (
        str(detail.get("采用来源文件") or "") == str(source.source_file or "")
        and str(detail.get("采用来源 sheet") or "") == str(source.source_sheet or "")
        and str(detail.get("采用来源行") or "") == str(source.source_row or "")
    )


def _replacement_source_matches(detail: dict, source: SourceShot) -> bool:
    return (
        str(detail.get("替换来源文件") or "") == str(source.source_file or "")
        and str(detail.get("替换来源 sheet") or "") == str(source.source_sheet or "")
        and str(detail.get("替换来源行") or "") == str(source.source_row or "")
    )


def _audit_bound_visual_domain_safety(
    checks: list[dict],
    brief: dict,
    forbidden_terms: list[str],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    next_constraints: dict,
) -> None:
    details_by_slot = {
        _slot_number(row.get("slot_index") or index): row
        for index, row in enumerate(detail_rows, start=1)
    }
    expected_brand = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
    for asset in visual_asset_rows:
        slot_index = _slot_number(asset.get("slot_index"))
        detail = details_by_slot.get(slot_index)
        if not detail or not _visual_asset_is_bound(detail, asset):
            continue
        brand_text = _joined_asset_fields(asset, (
            "caption",
            "tags",
            "brand_names",
            "ocr_text",
            "summary",
            "视觉内容摘要",
        ))
        verdict_status = brand_verdict_status(asset.get("brand_names"))
        wrong_brands = list(dict.fromkeys([
            *conflicting_brands(expected_brand, brand_text),
            *conflicting_visual_brand_names(expected_brand, asset.get("brand_names")),
        ]))
        if verdict_status != "explicit":
            _add_check(
                checks,
                "bound_visual_brand_unverified",
                "warning",
                "warning",
                str(slot_index),
                "实际绑定视觉资产缺少明确品牌结论或结论不确定",
                "重新执行视觉分析；无品牌必须显式返回 brand_names=[无]",
            )
        elif wrong_brands:
            _add_check(
                checks,
                "bound_visual_brand_conflict",
                "warning",
                "warning",
                str(slot_index),
                f"实际绑定视觉资产出现其他品牌: {', '.join(wrong_brands)}",
                "下一轮排除该槽位的错品牌视觉资产",
            )
        elif brand_evidence_unverified(
            expected_brand, asset.get("brand_names"), asset.get("ocr_text")
        ):
            _add_check(
                checks,
                "bound_visual_brand_unverified",
                "warning",
                "warning",
                str(slot_index),
                "实际绑定视觉资产含 OCR 文本但缺少结构化品牌判定",
                "重新执行视觉分析并人工复核 brand_names，未知品牌不得自动放行",
            )

        if analysis_asset_unusable(asset):
            _add_check(
                checks,
                "bound_visual_quality_blocked",
                "warning",
                "warning",
                str(slot_index),
                "实际绑定视觉资产命中失焦、模糊、低分辨率或明显噪点等质量阻断项",
                "下一轮排除该槽位的低质量视觉资产并重新检索",
            )

        forbidden_text = _joined_asset_fields(asset, (
            "caption",
            "tags",
            "ocr_text",
            "negative_flags",
            "summary",
            "视觉内容摘要",
        ))
        forbidden_hits = [
            term for term in forbidden_terms if term and term in forbidden_text
        ]
        if forbidden_hits:
            _add_check(
                checks,
                "brief_forbidden_visual_hit",
                "warning",
                "warning",
                str(slot_index),
                f"实际绑定视觉资产命中 Brief 禁忌: {', '.join(forbidden_hits)}",
                "下一轮排除该槽位的禁忌视觉资产",
            )


def _visual_asset_is_bound(detail: dict, asset: dict) -> bool:
    comparisons = []
    image_id = str(asset.get("image_id") or "").strip()
    if image_id:
        detail_ids = {
            value.strip()
            for value in str(detail.get("image_id") or "").split("；")
            if value.strip()
        }
        comparisons.append(image_id in detail_ids)
    image_path = str(asset.get("原图路径") or "").strip()
    if image_path:
        detail_paths = {
            _normalized_path(value)
            for value in str(detail.get("原图路径") or "").split("；")
            if value.strip()
        }
        comparisons.append(_normalized_path(image_path) in detail_paths)
    return bool(comparisons) and all(comparisons)


def _audit_launch_visual_completeness(
    checks: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
) -> None:
    assets_by_slot: dict[int, list[dict]] = {}
    for asset in visual_asset_rows:
        assets_by_slot.setdefault(_slot_number(asset.get("slot_index")), []).append(asset)
    details_by_slot = {
        _slot_number(detail.get("slot_index") or index): detail
        for index, detail in enumerate(detail_rows, start=1)
    }
    missing = []
    for slot_index in sorted(details_by_slot):
        detail = details_by_slot.get(slot_index, {})
        valid = any(
            _visual_asset_is_bound(detail, asset)
            and str(asset.get("image_id") or "").strip()
            and Path(str(asset.get("原图路径") or "")).is_file()
            and str(asset.get("视觉读取状态") or "").strip().lower()
            in {"minimax", "api", "openai", "human_review", "人工复核"}
            and str(asset.get("配图判断") or "") == "匹配"
            for asset in assets_by_slot.get(slot_index, [])
        )
        if not valid:
            missing.append(slot_index)
    if missing:
        _add_check(
            checks,
            "missing_bound_visual",
            "warning",
            "warning",
            ",".join(str(slot) for slot in missing),
            "部分镜头只有已审核文字或功能参考图，当前项目成片素材尚未就绪",
            "脚本文档可继续人工审核；成片制作前补充真实素材、版权与授权证明",
        )


def _audit_script_basis_completeness(
    checks: list[dict],
    decisions: list[TemplateDecision],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
) -> None:
    decisions_by_slot = {
        _slot_number(decision.slot.slot_index or index): decision
        for index, decision in enumerate(decisions, start=1)
    }
    assets_by_slot: dict[int, list[dict]] = {}
    for asset in visual_asset_rows:
        assets_by_slot.setdefault(_slot_number(asset.get("slot_index")), []).append(asset)
    missing = []
    for index, detail in enumerate(detail_rows, start=1):
        slot_index = _slot_number(detail.get("slot_index") or index)
        decision = decisions_by_slot.get(slot_index)
        source = None
        if decision is not None:
            source = (
                decision.variant
                if decision.mode in {"REPLACE", "ADAPT"} and decision.variant
                else decision.slot
            )
        source_text = _source_evidence_text(source) if source is not None else ""
        source_reference = bool(source and any((
            source.reference_image_path,
            source.effect_reference_path,
            source.reference_image_evidence,
            source.effect_reference_evidence,
        )))
        bound_reference = any(
            str(asset.get("原图路径") or asset.get("可回查锚点") or "").strip()
            for asset in assets_by_slot.get(slot_index, [])
        )
        trace_valid = bool(source and _adopted_source_matches(detail, source))
        if not trace_valid or not (source_text or source_reference or bound_reference):
            missing.append(slot_index)
    if missing:
        _add_check(
            checks,
            "missing_script_basis",
            "hard",
            "fail",
            ",".join(str(slot) for slot in missing),
            "镜头缺少可核对的已审核文字/参考图依据，或来源三元组与实际采用记录不一致",
            "补充已审核源文字或参考图，并保留来源文件、来源 Sheet、来源行",
        )


def _normalized_path(value: str) -> str:
    return str(Path(str(value or "")).expanduser().resolve(strict=False))


def _joined_asset_fields(asset: dict, fields: tuple[str, ...]) -> str:
    return " ".join(_flatten_text(asset.get(field)) for field in fields)


def _flatten_text(value) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return str(value or "")


def _exclude_bound_visual(next_constraints: dict, slot_index: int, asset: dict) -> None:
    excluded = next_constraints["exclude_visuals_by_slot"].setdefault(str(slot_index), [])
    for value in (asset.get("image_id"), asset.get("原图路径")):
        token = str(value or "").strip()
        if token:
            _append_unique(excluded, token)


def _source_evidence_text(source: SourceShot) -> str:
    return source.searchable_text().strip()


def _verified_visual_supports(term: str, detail: dict, asset: dict) -> bool:
    image_id = str(asset.get("image_id") or "").strip()
    image_path = str(asset.get("原图路径") or "").strip()
    status = str(asset.get("视觉读取状态") or "").strip().lower()
    if not image_id or not image_path or not Path(image_path).is_file():
        return False
    if status not in {"minimax", "api", "openai", "human_review", "人工复核"}:
        return False
    if status in {"human_review", "人工复核"} and not (
        asset.get("人工复核记录")
        or str(asset.get("review_status") or "").lower() in {"approved", "pass", "通过"}
    ):
        return False
    visual_judgement = str(asset.get("配图判断") or "")
    source_reference = str(asset.get("asset_role") or "") == "source_reference"
    if visual_judgement != "匹配" and not (
        source_reference and visual_judgement == "功能参考"
    ):
        return False
    if not _visual_asset_matches_detail(detail, asset):
        return False
    evidence = " ".join(str(asset.get(key) or "") for key in (
        "视觉内容摘要",
        "视觉标签",
        "命中视觉要素",
    ))
    return _required_term_present(term, evidence)


def _visual_asset_matches_detail(detail: dict, asset: dict) -> bool:
    for key in ("image_id", "原图路径", "视觉读取状态", "配图判断"):
        asset_value = str(asset.get(key) or "").strip()
        detail_values = {
            value.strip()
            for value in str(detail.get(key) or "").split("；")
            if value.strip()
        }
        if not asset_value or asset_value not in detail_values:
            return False
    for key in ("视觉内容摘要", "视觉标签"):
        asset_value = str(asset.get(key) or "").strip()
        if not asset_value or asset_value not in str(detail.get(key) or ""):
            return False
    return True


def _audit_launch_slot_mapping(
    checks: list[dict],
    template_slots: list[SourceShot],
    decisions: list[TemplateDecision],
    script_rows: list[dict],
    detail_rows: list[dict],
) -> bool:
    slot_count = len(template_slots)
    expected_order = list(expected_slot_numbers(slot_count))
    expected = set(expected_order)
    slot_sets = {
        "template": [_slot_number(slot.slot_index) for slot in template_slots],
        "decisions": [_slot_number(decision.slot.slot_index) for decision in decisions],
        "script": [
            _slot_number(row.get("slot_index") or row.get("镜号"))
            for row in script_rows
        ],
        "details": [_slot_number(row.get("slot_index")) for row in detail_rows],
    }
    invalid = {
        name: values
        for name, values in slot_sets.items()
        if (
            len(values) != slot_count
            or len(set(values)) != slot_count
            or set(values) != expected
            or values != expected_order
        )
    }
    template_by_slot = {
        slot.slot_index: slot
        for slot in template_slots
        if _slot_number(slot.slot_index) in expected
    }
    misaligned = []
    if not invalid:
        for decision in decisions:
            template_slot = template_by_slot.get(decision.slot.slot_index)
            if template_slot is None or _source_identity(template_slot) != _source_identity(decision.slot):
                misaligned.append(decision.slot.slot_index)
    if not invalid and not misaligned:
        return True
    details = []
    if invalid:
        details.append(", ".join(f"{name}={values}" for name, values in invalid.items()))
    if misaligned:
        details.append(f"decision/template 错位={misaligned}")
    _add_check(
        checks,
        "invalid_launch_slot_mapping",
        "hard",
        "fail",
        "",
        f"{slot_count} 镜槽位必须唯一完整对应 1..{slot_count}，且 decision.slot 必须与模板槽位对齐；"
        + "；".join(details),
        "从原模板重建完整槽位映射，禁止重复键覆盖",
    )
    return False


def _is_formal_evidence_ref(value: object) -> bool:
    if isinstance(value, str):
        return value.startswith("artifact://formal-evidence/")
    if not isinstance(value, dict):
        return False
    return (
        str(value.get("artifact_ref") or "").startswith("artifact://formal-evidence/")
        and bool(str(value.get("content_sha256") or "").strip())
        and bool(str(value.get("source_document_id") or "").strip())
    )


def _audit_decision_detail_consistency(
    checks: list[dict],
    decisions: list[TemplateDecision],
    detail_rows: list[dict],
) -> None:
    details_by_slot = {int(row["slot_index"]): row for row in detail_rows}
    valid_modes = {"KEEP", "REPLACE", "ADAPT"}
    for decision in decisions:
        slot_index = decision.slot.slot_index
        detail = details_by_slot[slot_index]
        expected_type = "创意库" if decision.mode in {"REPLACE", "ADAPT"} else "模板"
        adopted_source = (
            decision.variant
            if decision.mode in {"REPLACE", "ADAPT"} and decision.variant
            else decision.slot
        )
        invalid_variant = decision.mode in {"REPLACE", "ADAPT"} and decision.variant is None
        template_mismatch = not (
            str(detail.get("模板镜号") or "") == str(decision.slot.shot_no or slot_index)
            and str(detail.get("模板来源文件") or "") == str(decision.slot.source_file or "")
            and str(detail.get("模板来源 sheet") or "") == str(decision.slot.source_sheet or "")
            and str(detail.get("模板来源行") or "") == str(decision.slot.source_row or "")
        )
        adopted_source_mismatch = not _adopted_source_matches(detail, adopted_source)
        if (
            decision.mode not in valid_modes
            or invalid_variant
            or str(detail.get("替换方式") or "") != decision.mode
            or str(detail.get("采用来源类型") or "") != expected_type
            or template_mismatch
            or adopted_source_mismatch
        ):
            _add_check(
                checks,
                "decision_detail_mismatch",
                "hard",
                "fail",
                str(slot_index),
                f"明细 mode/type 与决策不一致: decision={decision.mode}/{expected_type}, "
                f"detail={detail.get('替换方式')}/{detail.get('采用来源类型')}",
                "按 decision 重新生成抽调明细，不允许手工改写 mode/type",
            )


def _source_identity(source: SourceShot) -> tuple[str, str, str, int]:
    return (
        str(source.source_file or ""),
        str(source.source_sheet or ""),
        str(source.source_row or ""),
        int(source.slot_index or 0),
    )


def _slot_number(value) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _required_term_expected_slots(
    term: str,
    decisions: list[TemplateDecision],
) -> set[int]:
    text = str(term or "")
    rules = (
        (("logo", "LOGO", "品牌标识"), {1, 3, 10, 18}),
        (("活动主会场", "主会场"), {2, 4, 11, 17}),
        (("主题字样",), {1, 3, 10, 11, 18}),
        (("装置空镜",), {4}),
        (("主题装置",), {4, 7}),
        (("产品体验",), {7}),
        (("人流", "互动"), {5, 7, 8, 9, 13}),
        (("发布仪式", "启动仪式", "核心仪式"), {12, 15, 16}),
    )
    for keywords, slots in rules:
        if any(keyword in text for keyword in keywords):
            return {
                decision.slot.slot_index
                for decision in decisions
                if logical_slot_index(decision.slot, len(decisions)) in slots
            }
    return {decision.slot.slot_index for decision in decisions}


def _source_key_dict(source: SourceShot) -> dict:
    return {
        "source_file": source.source_file,
        "source_sheet": source.source_sheet,
        "source_row": source.source_row,
    }


def _add_exclusion(next_constraints: dict, slot_index: int, source: SourceShot) -> None:
    key = str(slot_index)
    next_constraints["exclude_by_slot"].setdefault(key, [])
    _append_unique(next_constraints["exclude_by_slot"][key], _source_key_dict(source))


def _append_unique(items: list, value) -> None:
    if value not in items:
        items.append(value)


def _normalize_constraints(next_constraints: dict) -> None:
    next_constraints["force_keep_slots"] = sorted(set(next_constraints["force_keep_slots"]))


def _duplicate_sources(detail_rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    slots_by_source: dict[str, set[int]] = {}
    for row in detail_rows:
        source_row = row.get("替换来源行")
        if not source_row:
            continue
        key = f"{row.get('替换来源文件')}|{row.get('替换来源 sheet')}|{source_row}"
        counts[key] = counts.get(key, 0) + 1
        try:
            slots_by_source.setdefault(key, set()).add(int(row.get("slot_index") or 0))
        except (TypeError, ValueError):
            slots_by_source.setdefault(key, set()).add(0)
    return {
        key: count for key, count in counts.items()
        if count > 1 and slots_by_source.get(key) != {3, 10}
    }


def _naked_image_paths(ws) -> list[str]:
    values = []
    for row in ws.iter_rows(min_row=2, min_col=3, max_col=4):
        for cell in row:
            text = str(cell.value or "").strip()
            if text.startswith("/") and Path(text.split(";", 1)[0]).exists():
                values.append(cell.coordinate)
    return values
