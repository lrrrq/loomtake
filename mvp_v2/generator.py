from __future__ import annotations

import re

from .models import ScoredShot, brief_profile_from_dict, validate_script_brief
from .template_logic import logical_slot_index, slot_logic, slot_logic_for_source
from .brand_policy import (
    adapt_source_text_to_project,
    conflicting_brands,
    source_brand_provenance_issue,
)
from .script_type_adapter import normalize_script_type
from .audit import (
    SOURCE_SPECIFIC_SUBJECT_TERMS,
    UNCONFIRMED_LOCATION_TERMS,
    _required_term_present,
    _source_supports_required_term,
)


LAUNCH_60S_DURATIONS = (3, 3, 3, 4, 3, 3, 4, 3, 4, 3, 4, 4, 3, 3, 3, 4, 3, 3)

VISUAL_ROLE_SUFFIXES = {
    1: "开场建立品牌识别",
    2: "建立发布会场地",
    3: "作为开场主视觉快闪转场",
    4: "建立装置与主会场空间",
    5: "从空间切入真实现场互动参与",
    6: "补充嘉宾关系与现场权威感",
    7: "呈现产品与主题空间体验",
    8: "补充服务互动与现场温度",
    9: "建立展区人流热度",
    10: "回扣品牌视觉并切回主发布",
    11: "建立核心发布段落",
    12: "承载发布会核心信息",
    13: "强化内容被观看与记录的传播感",
    14: "作为核心内容之间的节奏过渡",
    15: "完成正式仪式节点的信息露出",
    16: "形成核心发布仪式的情绪高点",
    17: "收束主会场空间并承接片尾",
    18: "完成活动主题与品牌落版",
}

VISUAL_ADAPT_FIELDS = {
    2: {"类型": "地面", "景别": "全景", "运镜": "缓慢前推", "声音": "环境氛围声"},
    5: {"类型": "地面", "景别": "中景", "运镜": "固定", "声音": "机械运转声"},
    6: {"类型": "地面", "景别": "中景", "运镜": "固定", "声音": "快门声"},
    8: {"类型": "地面", "景别": "中景", "运镜": "倾角前推", "声音": "机械臂声"},
    14: {"类型": "地面", "景别": "综合", "运镜": "综合", "声音": "现场掌声"},
    16: {"类型": "地面", "景别": "全景", "运镜": "摇移", "声音": "欢呼声+掌声"},
    17: {"类型": "地面", "景别": "全景", "运镜": "匀速后撤", "声音": "“咻”的转场声"},
}

JEWELRY_VISUAL_TERMS = (
    "珠宝", "首饰", "项链", "戒指", "钻石", "黄金", "金饰", "宝石",
    "耳环", "耳饰", "手镯", "胸针", "吊坠", "模特佩戴",
)

JEWELRY_CONFLICT_TERMS = (
    "家电", "橱柜", "冰箱", "洗衣机", "机器人", "爆米花机", "汽车",
    "车内", "智能家居", "游戏主题", "科技产品",
)


def generate_script_rows(brief: dict, scored: list[ScoredShot]) -> list[dict]:
    duration = _target_duration(brief)
    if not scored:
        return []
    count = _target_count(brief)
    per_shot_duration = max(1, round(duration / count)) if duration else ""
    rows = []
    for idx in range(count):
        item = scored[idx % len(scored)]
        shot = item.shot
        rows.append({
            "项目名称": brief.get("项目名称", ""),
            "需求行号": brief.get("input_row", ""),
            "镜号": f"{idx + 1:03d}",
            "结构": shot.structure,
            "时长(秒)": shot.duration_seconds or per_shot_duration,
            "画面示意": "",
            "效果示意": "",
            "效果": shot.effect,
            "景别": shot.shot_size,
            "运镜": shot.camera_move,
            "画面描述": _adapt_description(brief, shot.visual_desc),
            "声音": shot.sound,
            "备注": shot.note,
            "参考图路径": shot.reference_image_path,
            "效果示意路径": shot.effect_reference_path,
            "来源文件": shot.source_file,
            "来源 sheet": shot.source_sheet,
            "来源行": shot.source_row,
            "匹配分": item.score,
            "匹配理由": item.match_reason,
            "缺口说明": item.gap_note,
            "审核结果": "",
            "修改意见": "",
        })
    return rows


def generate_template_rows(brief: dict, decisions) -> tuple[list[dict], list[dict]]:
    script_rows = []
    detail_rows = []
    profile = brief_profile_from_dict(brief)
    profile_gap_note = "；".join(profile.gap_notes)
    slot_count = len(decisions)
    is_launch = normalize_script_type(brief.get("脚本类型") or "发布会") == "发布会"
    durations = _allocate_durations(_target_duration(brief), slot_count)
    for index, decision in enumerate(decisions, start=1):
        slot = decision.slot
        variant = decision.variant if decision.mode in {"REPLACE", "ADAPT"} else None
        brand_issue = ""
        if variant:
            brand_issue = source_brand_provenance_issue(
                variant.source_brand,
                variant.brand_neutral,
                variant.brand_neutral_evidence,
            )
        mode = "KEEP" if brand_issue else decision.mode
        if brand_issue:
            variant = None
        source = variant if mode in {"REPLACE", "ADAPT"} and variant else slot
        logic_slot_index = logical_slot_index(slot, slot_count) if is_launch else (slot.slot_index or index)
        logic = slot_logic_for_source(slot, slot_count) if is_launch else None
        source_visual_desc = (
            source.visual_desc
            if (not is_launch or mode == "REPLACE")
            else slot.visual_desc
        )
        visual_desc = (
            _brief_first_description(
                brief,
                logic_slot_index or slot.slot_index or index,
                source_visual_desc,
                source.source_brand,
            )
            if is_launch
            else _brief_first_generic_description(
                brief,
                slot,
                source_visual_desc,
                source.source_brand,
            )
        )
        reference_image_path = source.reference_image_path or slot.reference_image_path
        effect_reference_path = source.effect_reference_path or slot.effect_reference_path
        image_source = _image_source_label(slot, variant, source, reference_image_path, effect_reference_path)

        project_text = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
        adapt_source_text = not is_launch or mode == "REPLACE"
        duration = durations[index - 1] if index <= len(durations) else ""
        note = _adapt_source_field(source.note, project_text, source.source_brand) if adapt_source_text else slot.note
        if duration != "":
            note = _note_with_duration(note, duration)
        script_rows.append({
            "结构": slot.structure,
            "镜号": slot.shot_no or str(index),
            "时长(秒)": duration,
            "画面示意": reference_image_path,
            "效果示意": effect_reference_path,
            "效果": _adapt_source_field(source.effect, project_text, source.source_brand) if adapt_source_text else slot.effect,
            "类型": source.camera_type if (not is_launch or mode == "REPLACE") else slot.camera_type,
            "景别": source.shot_size if (not is_launch or mode == "REPLACE") else slot.shot_size,
            "运镜": source.camera_move if (not is_launch or mode == "REPLACE") else slot.camera_move,
            "画面描述": visual_desc,
            "声音": _adapt_source_field(source.sound, project_text, source.source_brand) if adapt_source_text else slot.sound,
            "备注": note,
        })
        detail_rows.append({
            "项目名称": brief.get("项目名称", ""),
            "需求行号": brief.get("input_row", ""),
            "slot_index": slot.slot_index or index,
            "模板槽位总数": slot_count,
            "逻辑槽位": logic_slot_index or slot.slot_index or index,
            "模板来源文件": slot.source_file,
            "模板来源 sheet": slot.source_sheet,
            "模板来源行": slot.source_row,
            "模板结构": slot.structure,
            "模板镜号": slot.shot_no or str(index),
            "槽位功能": logic.role if logic else slot.shot_function,
            "槽位选择原因": logic.intent if logic else slot.visual_subject,
            "允许素材": "、".join(logic.allowed_materials) if logic else "",
            "禁止素材": (
                "、".join(logic.blocked_materials)
                if logic
                else source.forbidden_note
            ),
            "推荐景别": "、".join(logic.recommended_shot_sizes) if logic else "",
            "推荐运镜": "、".join(logic.recommended_camera_moves) if logic else "",
            "推荐声音": "、".join(logic.recommended_sounds) if logic else "",
            "节奏说明": logic.rhythm if logic else "",
            "必须视觉证据": "、".join(logic.required_visual_evidence) if logic else "",
            "重复素材规则": logic.repeat_policy if logic else "",
            "替换方式": mode,
            "替换来源文件": variant.source_file if variant else "",
            "替换来源 sheet": variant.source_sheet if variant else "",
            "替换来源行": variant.source_row if variant else "",
            "采用来源类型": "创意库" if mode in {"REPLACE", "ADAPT"} and variant else "模板",
            "采用来源文件": source.source_file,
            "采用来源 sheet": source.source_sheet,
            "采用来源行": source.source_row,
            "采用来源类别": source.source_category_id,
            "采用来源包": source.source_package_id,
            "采用来源包版本": source.source_package_version,
            "采用来源资产ID": source.source_asset_id,
            "采用来源通用描述": source.generic_desc,
            "采用来源品牌判定原文": source.brand_judgement_original,
            "采用来源品牌": source.source_brand,
            "采用来源品牌中性": source.brand_neutral,
            "品牌中性确认依据": source.brand_neutral_evidence,
            "采用来源证据": _source_evidence_text(source),
            "Brief来源文件": brief.get("Brief来源文件", ""),
            "Brief来源 sheet": brief.get("Brief来源 sheet", ""),
            "Brief来源行": brief.get("Brief来源行", ""),
            "Brief来源 SHA256": brief.get("Brief来源 SHA256", ""),
            "Brief来源证据": brief.get("Brief来源证据", ""),
            "时长(秒)": duration,
            "匹配分": decision.score,
            "匹配理由": decision.match_reason,
            "候选命中点": "、".join(decision.hit_terms),
            "候选缺失点": "、".join(decision.missing_terms),
            "候选冲突点": "、".join(decision.conflict_terms),
            "候选选择理由": _join_notes(
                decision.selected_reason,
                "文案由Codex设计的Brief优先本地规则生成",
            ),
            "候选放弃理由": decision.rejected_reason,
            "缺口说明": _join_notes(decision.gap_note, brand_issue, profile_gap_note),
            "输出图片来源": image_source,
            "图片证据类型": _image_evidence_label(slot, variant, source),
        })
    _ensure_required_content(brief, script_rows, detail_rows)
    return script_rows, detail_rows


def _source_evidence_text(source: SourceShot) -> str:
    return " ".join(
        str(value or "").strip()
        for value in (
            source.structure,
            source.visual_desc,
            source.generic_desc,
            source.effect,
            source.note,
            source.tags,
            source.shot_function,
            source.visual_subject,
        )
        if str(value or "").strip()
    )


def rewrite_template_rows_from_visual_evidence(
    brief: dict,
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    durations = _allocate_durations(_target_duration(brief), len(script_rows))
    details_by_slot = {
        str(row.get("slot_index") or index): row
        for index, row in enumerate(detail_rows, start=1)
    }
    assets_by_slot = {}
    for asset in visual_asset_rows:
        slot = str(asset.get("slot_index") or "")
        if slot and asset.get("图片角色") == "画面示意":
            assets_by_slot[slot] = asset

    for index, script_row in enumerate(script_rows, start=1):
        duration = durations[index - 1] if index <= len(durations) else ""
        if duration != "":
            script_row["时长(秒)"] = duration
            script_row["备注"] = _note_with_duration(script_row.get("备注", ""), duration)
        detail = details_by_slot.get(str(index))
        if detail is not None and duration != "":
            detail["时长(秒)"] = duration
        asset = assets_by_slot.get(str(index))
        logic_index = logical_slot_index(detail or {"slot_index": index, "模板槽位总数": len(script_rows)})
        if not asset or str(asset.get("配图判断") or "") != "匹配":
            continue
        summary = str(asset.get("视觉内容摘要") or "").strip()
        if not summary or not asset.get("原图路径"):
            continue
        match_mode = str(asset.get("视觉适配模式") or "primary")
        reference_reason = _functional_reference_reason(brief, logic_index or index, asset)
        if reference_reason:
            asset["配图判断"] = "功能参考"
            asset["视觉适配模式"] = "function_reference"
            asset["绑定说明"] = _join_notes(asset.get("绑定说明", ""), reference_reason)
            script_row["备注"] = _join_notes(
                script_row.get("备注", ""),
                "当前配图仅作镜头功能参考，不可作为品牌或行业匹配素材",
            )
            match_mode = "function_reference"
        elif match_mode == "adapt":
            for field, value in VISUAL_ADAPT_FIELDS.get(logic_index or index, {}).items():
                script_row[field] = value
        if detail is not None:
            detail["视觉适配模式"] = match_mode
            detail["视觉适配查询"] = asset.get("视觉适配查询", "")
            detail["视觉回写依据"] = (
                f"素材只用于配图复核，不覆盖Brief文案；"
                f"image_id={asset.get('image_id') or asset.get('visual_asset_id')}；caption={summary}"
            )
            if reference_reason:
                detail["配图判断"] = "功能参考"
                detail["图片绑定说明"] = _join_notes(
                    detail.get("图片绑定说明", ""), reference_reason
                )
                detail["缺口说明"] = _join_notes(
                    detail.get("缺口说明", ""),
                    "缺少当前品牌或行业匹配素材",
                )
            if match_mode == "adapt":
                detail["候选选择理由"] = _join_notes(
                    detail.get("候选选择理由", ""),
                    "主素材缺失，按槽位允许的视觉降级规则适配",
                )
    _ensure_required_content(brief, script_rows, detail_rows)
    return script_rows, detail_rows, visual_asset_rows


def _ensure_required_content(brief: dict, script_rows: list[dict], detail_rows: list[dict]) -> None:
    if normalize_script_type(brief.get("脚本类型") or "发布会") != "发布会":
        _ensure_generic_required_content(brief, script_rows, detail_rows)
        return
    required_terms = _split_required_terms(brief.get("必须出现的内容"))
    terms_by_row: dict[tuple[int, int], list[str]] = {}
    for term in required_terms:
        for index, script_row in enumerate(script_rows):
            script_row["备注"] = _remove_required_marker(
                script_row.get("备注", ""), f"Brief必选：{term}"
            )
            if index < len(detail_rows):
                detail_rows[index]["缺口说明"] = _remove_required_marker(
                    detail_rows[index].get("缺口说明", ""), f"Brief规划内容：{term}"
                )
        existing_rows = [
            index for index, row in enumerate(script_rows)
            if term in _row_text_for_required(row)
        ]
        if existing_rows:
            row_index = existing_rows[0]
            logic_index = logical_slot_index(
                detail_rows[row_index] if row_index < len(detail_rows)
                else {"slot_index": row_index + 1, "模板槽位总数": len(script_rows)}
            )
        else:
            logic_index = _required_content_slot(term)
            row_index = _row_index_for_logic_slot(detail_rows, logic_index)
        if row_index is None or row_index < 0 or row_index >= len(script_rows):
            continue
        terms_by_row.setdefault((row_index, logic_index), []).append(term)
        script_row = script_rows[row_index]
        script_row["备注"] = _join_notes(
            script_row.get("备注", ""), f"Brief必选：{term}"
        )
        detail_rows[row_index]["缺口说明"] = _join_notes(
            detail_rows[row_index].get("缺口说明", ""),
            f"Brief规划内容：{term}，须以已审核来源文字或参考图提供脚本文档依据；成片素材后续确认",
        )
    for (row_index, logic_index), terms in terms_by_row.items():
        script_row = script_rows[row_index]
        description = str(script_row.get("画面描述") or "")
        missing = [term for term in terms if term not in description]
        if missing:
            script_row["画面描述"] = _append_sentence(
                description,
                _required_content_sentence("、".join(missing), logic_index),
            )


def _ensure_generic_required_content(
    brief: dict,
    script_rows: list[dict],
    detail_rows: list[dict],
) -> None:
    required_terms = _split_required_terms(brief.get("必须出现的内容"))
    file_backed_brief = all(
        str(brief.get(key) or "").strip()
        for key in (
            "Brief来源文件",
            "Brief来源 sheet",
            "Brief来源行",
            "Brief来源 SHA256",
            "Brief来源证据",
            "_brief_source_path",
        )
    )
    formal_required_evidence = _has_confirmed_required_content_evidence(brief)
    for term_index, term in enumerate(dict.fromkeys(required_terms)):
        ranked = []
        for index, detail in enumerate(detail_rows):
            evidence = " ".join([
                _row_text_for_required(script_rows[index]) if index < len(script_rows) else "",
                *(str(detail.get(key) or "") for key in (
                "槽位功能", "槽位选择原因", "采用来源证据", "模板结构"
                )),
            ])
            score = _generic_required_assignment_score(term, evidence)
            ranked.append((score, -index, index))
        row_index = max(ranked)[2] if ranked else term_index % max(len(script_rows), 1)
        if row_index >= len(script_rows):
            continue
        if row_index < len(detail_rows):
            source_evidence = str(detail_rows[row_index].get("采用来源证据") or "")
            description = str(script_rows[row_index].get("画面描述") or "")
            if (
                not _required_term_present(term, description)
                and (
                    _source_supports_required_term(term, source_evidence)
                    or file_backed_brief
                    or formal_required_evidence
                )
            ):
                script_rows[row_index]["画面描述"] = _append_sentence(
                    description,
                    _generic_required_content_sentence(term),
                )
            detail_rows[row_index]["缺口说明"] = _join_notes(
                detail_rows[row_index].get("缺口说明", ""),
                f"Brief规划内容：{term}，须由已审核来源文字或已识别参考图提供证据",
            )


def _has_confirmed_required_content_evidence(brief: dict) -> bool:
    formal = brief.get("_formal_fact_evidence")
    required = formal.get("必须出现的内容") if isinstance(formal, dict) else None
    refs = required.get("evidence_refs") if isinstance(required, dict) else None
    if (
        not isinstance(required, dict)
        or not str(required.get("fact_id") or "").strip()
        or not isinstance(refs, list)
        or not refs
    ):
        return False
    return all(
        (
            isinstance(ref, str)
            and ref.startswith("artifact://formal-evidence/")
        )
        or (
            isinstance(ref, dict)
            and str(ref.get("artifact_ref") or "").startswith("artifact://formal-evidence/")
            and bool(str(ref.get("content_sha256") or "").strip())
            and bool(str(ref.get("source_document_id") or "").strip())
        )
        for ref in refs
    )


def _generic_required_assignment_score(term: str, evidence: str) -> int:
    score = sum(char in evidence for char in set(term) if char.strip())
    if term in evidence:
        score += 100
    groups = (
        (("logo", "LOGO", "标识", "主视觉"), ("标识", "主视觉", "信息建立", "落版", "收官")),
        (("入场", "入口", "邀请函"), ("入场", "入口", "邀请函", "签到")),
        (("比赛", "对抗", "对战", "竞技"), ("竞技瞬间", "比赛", "对抗", "对战", "选手", "参赛者", "运动员")),
        (("观众", "应援"), ("观众", "应援", "粉丝", "欢呼")),
        (("颁奖", "领奖"), ("颁奖", "领奖", "奖牌", "奖杯", "荣誉")),
        (("合影", "合照"), ("合影", "合照", "集体照")),
        (("落版", "收官", "END", "KV"), ("落版", "收官", "定格", "主视觉")),
    )
    for term_keywords, evidence_keywords in groups:
        if any(keyword in term for keyword in term_keywords):
            score += 10 * sum(keyword in evidence for keyword in evidence_keywords)
    return score


def _split_required_terms(value) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"[、,，;；\n]+", str(value or ""))
        if part.strip()
    ]


def _row_text_for_required(row: dict) -> str:
    return " ".join(
        str(row.get(key) or "")
        for key in ("画面描述", "声音", "备注", "效果")
    )


def _required_content_slot(term: str) -> int:
    slot_terms = (
        (1, ("logo", "LOGO", "品牌标识", "主题字样")),
        (2, ("搭建", "施工", "场馆成型")),
        (4, ("主会场", "装置空镜", "舞台空镜")),
        (5, ("签到", "入场")),
        (6, ("高管", "嘉宾")),
        (7, ("主题装置", "产品", "体验", "互动")),
        (8, ("茶歇", "服务")),
        (9, ("人流", "观众")),
        (12, ("演讲", "战略", "口号", "核心发布")),
        (13, ("手机", "录屏", "传播")),
        (14, ("表演",)),
        (15, ("颁发", "授牌")),
        (16, ("启动", "仪式", "合影")),
        (18, ("落版", "END", "KV")),
    )
    for slot_index, keywords in slot_terms:
        if any(keyword in term for keyword in keywords):
            return slot_index
    return 11


def _required_content_sentence(term: str, slot_index: int) -> str:
    if slot_index == 7:
        return f"呈现{term}相关的材质、工艺与使用细节"
    if slot_index == 11:
        return f"进入{term}环节，以连续动作建立发布段落的视觉高潮"
    if slot_index == 12:
        return f"舞台进入{term}环节，关键信息由人物与主屏共同承载"
    if slot_index == 18:
        return f"片尾主视觉明确落下{term}"
    return f"画面呈现{term}"


def _remove_required_marker(value: str, prefix: str) -> str:
    return "；".join(
        part.strip()
        for part in re.split(r"[;；]", str(value or ""))
        if part.strip() and not part.strip().startswith(prefix)
    )


def _append_sentence(value: str, sentence: str) -> str:
    head = str(value or "").strip().rstrip("。；")
    tail = str(sentence or "").strip().rstrip("。；")
    if not head:
        return tail
    if not tail or tail in head:
        return head
    return f"{head}；{tail}"


def _row_index_for_slot(detail_rows: list[dict], slot_index: int) -> int | None:
    for index, detail in enumerate(detail_rows):
        try:
            if int(detail.get("slot_index") or 0) == slot_index:
                return index
        except (TypeError, ValueError):
            continue
    return None


def _row_index_for_logic_slot(detail_rows: list[dict], logic_index: int) -> int | None:
    for index, detail in enumerate(detail_rows):
        if logical_slot_index(detail, len(detail_rows)) == logic_index:
            return index
    if logic_index == 18 and detail_rows:
        return len(detail_rows) - 1
    return _row_index_for_slot(detail_rows, logic_index)


def _image_source_label(slot, variant, source, reference_image_path: str, effect_reference_path: str) -> str:
    if not (reference_image_path or effect_reference_path):
        return ""
    if variant and source is variant and (source.reference_image_path or source.effect_reference_path):
        return variant.source_sheet
    return slot.source_sheet


def _image_evidence_label(slot, variant, source) -> str:
    if variant and source is variant:
        return variant.reference_image_evidence or variant.effect_reference_evidence
    return slot.reference_image_evidence or slot.effect_reference_evidence


def _adapt_description(brief: dict, description: str) -> str:
    return description


def _adapt_source_field(value: str, project_text: str, source_brand: str = "") -> str:
    return adapt_source_text_to_project(value, project_text, source_brand=source_brand)


def _join_notes(*notes: str) -> str:
    parts = []
    for note in notes:
        parts.extend(part.strip() for part in str(note or "").split("；") if part.strip())
    return "；".join(dict.fromkeys(parts))


def _polish_description(brief: dict, description: str, source_brand: str = "") -> str:
    project_text = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
    result = adapt_source_text_to_project(
        description, project_text, source_brand=source_brand
    )
    client = str(brief.get("品牌或客户", "")).strip()
    theme = _theme_from_brief(brief)
    if client:
        result = result.replace("品牌 logo", f"{client}品牌logo")
        result = result.replace("品牌LOGO", f"{client}品牌logo")
        result = result.replace("品牌logo", f"{client}品牌logo")
    if theme:
        result = result.replace("活动主题文字", f"{theme}主题文字")
        result = result.replace("活动主题", f"{theme}主题")
        result = result.replace("本次活动主题", f"{theme}主题")
        result = result.replace("主视觉 KV", f"{theme}主视觉 KV")
        result = result.replace("主视觉KV", f"{theme}主视觉KV")
    return result


def _brief_first_generic_description(
    brief: dict,
    slot,
    source_description: str,
    source_brand: str = "",
) -> str:
    """Retain the approved shot action while removing source-project facts."""
    script_type = normalize_script_type(brief.get("脚本类型") or "")
    project = str(brief.get("项目名称") or brief.get("品牌或客户") or "当前项目").strip()
    brief_text = " ".join(str(value or "") for value in brief.values())
    result = str(source_description or "").strip()
    function = str(getattr(slot, "shot_function", "") or "").strip()
    controlled = _controlled_generic_description(brief, function)
    if controlled:
        return controlled
    result = _polish_description(brief, result, source_brand)
    result = _replace_source_project_titles(result, brief_text, project)

    for location in UNCONFIRMED_LOCATION_TERMS:
        if location not in brief_text:
            result = result.replace(location, "活动所在城市")

    subject_replacements = _generic_subject_replacements(script_type)
    for subject in SOURCE_SPECIFIC_SUBJECT_TERMS:
        if subject not in brief_text and subject in result:
            result = result.replace(subject, subject_replacements.get(subject, "项目内容"))

    result = _remove_source_specific_ascii(result, source_description, brief_text)
    result = re.sub(r"(?<!\d)(?:19|20)\d{2}(?!\d)", "本届", result)
    result = _replace_unconfirmed_quoted_copy(result, brief_text, project)
    result = _clean_generic_description(
        result,
        project,
        str(brief.get("品牌或客户") or "").strip(),
        script_type,
    )

    if any(keyword in function for keyword in ("落版", "信息建立", "品牌识别")):
        result = _project_identified_description(result, project, function)
    if not result:
        result = f"围绕{project}完成{function or '当前镜头功能'}的可执行画面"
    return result


def _controlled_generic_description(brief: dict, function: str) -> str:
    project = str(brief.get("项目名称") or brief.get("品牌或客户") or "当前项目").strip()
    required_terms = _split_required_terms(brief.get("必须出现的内容"))
    presentation_topic = next(
        (
            term for term in required_terms
            if any(value in term for value in ("论坛", "演讲", "宣讲", "发布", "分享"))
        ),
        "",
    )
    if "论坛" in presentation_topic:
        presentation_description = (
            f"{project}论坛空间与现场观众正反打，{presentation_topic}宣讲与议题画面交替呈现，"
            "具体议题按确认版本呈现"
        )
    else:
        presentation_description = (
            f"{project}主舞台与现场观众正反打，主讲人发言与"
            f"{presentation_topic or '经确认的演讲主题'}画面交替呈现"
        )
    controlled = {
        "举办地与展会信息建立": f"航拍活动所在城市与展馆周边，叠加{project}名称、日期和举办信息，具体信息按确认版本呈现",
        "展台揭幕": f"{project}品牌展台由暗场逐步点亮，灯带勾勒空间结构后揭示展台全貌",
        "穿越机空间巡游": f"FPV贴近展馆外部动线飞行，再穿过{project}入口进入展馆内部，连续交代空间关系",
        "入口与品牌动线": f"从{project}主题地贴上摇前推至入口，品牌标识与参观动线依次进入画面",
        "KOL打卡传播": f"KOL自拍与{project}嘉宾在签名墙摆拍交叉剪辑，突出到场传播与观众参与",
        "高管宣讲与发布": presentation_description,
        "新品揭幕与合影": f"{project}舞台核心展品完成揭幕，随后嘉宾与品牌团队围绕展品合影，具体产品信息待确认",
        "品牌展区规模展示": f"航拍{project}展区、主题展台与参观人流，展示整体规模和功能分区",
        "高光片段遮罩预告": "以遮罩滑动交替预告舞台、员工互动和团队活动高光，保持人物与装置关系清晰",
        "日转夜气口": "夕阳穿过舞台桁架与品牌标识，人物和设备形成剪影，作为日转夜的节奏气口",
        "高管分屏致辞": "主舞台全景与高管致辞近景分屏并置，具体姓名和年度信息按已确认版本呈现",
        "倒计时启动": f"{project}主舞台进入倒计时，推拉镜头配合灯光变化将现场情绪推向启动高潮",
        "品牌产品现场特写": f"特写{project}现场展项的包装、材质与品牌识别，具体产品内容按确认版本呈现",
        "特殊视角现场全貌": f"360°相机将{project}活动区压缩为小行星视角，环绕展示赛道、装置与观众",
        "路书与参赛物料展示": f"工作人员手持{project}路线图和通行证，近景交代赛事路线与身份物料",
        "赛事IP互动": f"缓推{project}主题入口与现场人流，切至赛事IP装置在展区巡游互动",
        "产品饮用与干杯": "特写现场补给饮品倒入透明杯，再切参赛者在赛事现场完成碰杯",
        "主舞台嘉宾互动": f"{project}主舞台嘉宾群像与观众反应并置，以画中画强化舞台和现场关系",
        "城市与活动建立": f"以城市地标或天际线为开场，合成{project}主舞台与品牌标识，建立举办地和活动属性",
        "夜场空间建立": f"沿舞台通道与灯架透视前推至{project}夜场舞台，交代观众与灯光层次",
        "烟火秀穿越机高光": f"FPV保持安全距离穿越{project}舞台与烟火阵列，呈现多层烟火轨迹",
    }
    if "落版" in function:
        return f"{project}主视觉与品牌标识居中定格，活动日期和举办信息按确认版本呈现，完成落版"
    if function == "品牌识别":
        return f"环绕展示{project}场地外立面、活动空间与品牌标识，建立清晰的品牌识别"
    return controlled.get(function, "")


def _generic_subject_replacements(script_type: str) -> dict[str, str]:
    default = {
        "机器人": "互动装置",
        "汽车": "主题展项",
        "啤酒": "现场补给饮品",
        "珠宝": "核心展品",
        "家具": "核心展品",
        "家电": "核心展品",
        "空调": "核心展项",
        "赛车": "赛事主题装置",
        "电竞": "赛事",
        "运动鞋": "运动装备",
        "足球": "竞技项目",
        "篮球": "竞技项目",
        "网球": "竞技项目",
    }
    if script_type == "赛事":
        default.update({
            "汽车": "赛事主题装置",
            "珠宝": "赛事纪念物",
            "家具": "赛事装置",
            "家电": "赛事装置",
        })
    elif script_type == "年会":
        default.update({
            "汽车": "团队主题装置",
            "赛车": "团队互动项目",
            "足球": "团队竞技项目",
            "篮球": "团队竞技项目",
            "网球": "团队竞技项目",
        })
    return default


def _replace_unconfirmed_quoted_copy(result: str, brief_text: str, project: str) -> str:
    def replace(match: re.Match) -> str:
        content = str(match.group(1) or "").strip()
        if content and content in brief_text:
            return match.group(0)
        return f"“{project}活动信息”"

    return re.sub(r"[“\"]([^”\"]+)[”\"]", replace, result)


def _replace_source_project_titles(result: str, brief_text: str, project: str) -> str:
    pattern = re.compile(
        r"[A-Za-z0-9\u4e00-\u9fff]{2,24}"
        r"(?:生活节|音乐节|电音节|马拉松|英雄荟|年度盛典|挑战赛|发布会|展会|年会)"
    )

    def replace(match: re.Match) -> str:
        title = match.group(0)
        return title if title in brief_text or project in title else project

    return pattern.sub(replace, result)


def _remove_source_specific_ascii(result: str, source_description: str, brief_text: str) -> str:
    allowed = {
        "AI", "AR", "BGM", "CEO", "CFO", "CTO", "DJ", "FPV", "IP", "KV",
        "KOL", "LED", "LOGO", "PPT", "VCR", "VJ", "VR", "XR",
    }
    source_tokens = re.findall(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9+.-]*(?![A-Za-z0-9])", source_description)
    for token in dict.fromkeys(source_tokens):
        if token.upper() in allowed or token.casefold() in brief_text.casefold():
            continue
        result = re.sub(
            rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])",
            "",
            result,
            flags=re.IGNORECASE,
        )
    return result


def _clean_generic_description(
    result: str,
    project: str,
    client: str,
    script_type: str,
) -> str:
    result = re.sub(r"\s{2,}", " ", str(result or ""))
    result = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", result)
    result = re.sub(r"\s*[/|]\s*(?=[，。；、])", "", result)
    result = re.sub(r"([，。；、])\s*([，。；、])", r"\1", result)
    result = result.replace(f"{project}{project}", project)
    if client:
        result = result.replace(f"{project}{client}", project)
        result = re.sub(rf"(?:{re.escape(client)}\s*){{2,}}", client, result)
    if project.endswith(script_type):
        result = result.replace(f"{project}{script_type}", project)
    result = result.replace("品牌品牌logo", "品牌logo")
    result = result.replace("品牌品牌LOGO", "品牌LOGO")
    result = result.replace("品牌品牌字标", "品牌字标")
    result = result.replace("活动活动", "活动")
    result = result.replace("活动所在城市城市", "活动所在城市")
    return result.strip(" ，。；、/")


def _project_identified_description(
    result: str,
    project: str,
    function: str,
) -> str:
    if "落版" in function:
        return _append_sentence(result, f"{project}主视觉与品牌标识居中定格，完成落版")
    if project in result:
        return result
    return _append_sentence(result, f"叠加{project}活动名称与已确认信息")


def _generic_required_content_sentence(term: str) -> str:
    if term == "起跑集结":
        return "航拍起跑区与参赛者方阵，完整呈现起跑集结规模"
    if term in {"冲线或颁奖", "冲线或领奖"}:
        return "记录参赛者冲线后的情绪与颁奖节点，具体赛果与获奖信息待确认"
    return f"画面完整呈现{term}"


def _brief_first_description(
    brief: dict,
    slot_index: int,
    fallback_description: str,
    source_brand: str = "",
) -> str:
    """Generate launch copy from the Brief; source copy is only a fallback skeleton."""
    if not any(
        str(brief.get(key) or "").strip()
        for key in ("品牌或客户", "行业或活动类别", "风格调性", "必须出现的内容")
    ):
        return _polish_description(brief, fallback_description, source_brand)
    brand = str(brief.get("品牌或客户") or brief.get("项目名称") or "当前品牌").strip()
    project = str(brief.get("项目名称") or f"{brand}发布会").strip()
    industry = str(brief.get("行业或活动类别") or "品牌").strip()
    tone = str(brief.get("风格调性") or "清晰、克制").strip()
    required = str(brief.get("必须出现的内容") or "")
    subject = _brief_subject(brief)
    is_jewelry = "珠宝" in industry or any(term in required for term in JEWELRY_VISUAL_TERMS)
    product = "珠宝产品" if is_jewelry else subject
    runway = "发布会走秀" if "走秀" in required else f"{product}集中展示"
    speech = "明星演讲" if "明星演讲" in required else "主讲嘉宾演讲"
    descriptions = {
        1: f"以{tone}的光影语言打开{project}主视觉，{brand}品牌logo与发布会标题依次显现",
        2: f"镜头由场馆外立面推进至入口，建立{project}的空间规模与到场氛围",
        3: f"灯光沿{industry}主题纹理快速收束，衔接主舞台并完成开场节奏转场",
        4: f"主会场全景展开，舞台中央以{product}为视觉核心，材质灯光保持{tone}",
        5: f"嘉宾从签到区进入会场，镜头记录入场、交流与对{product}的第一眼关注",
        6: f"{brand}品牌代表与重要嘉宾在项目主视觉前合影，建立正式发布的权威感",
        7: f"镜头贴近{product}，通过特写与移动光线呈现材质、工艺和核心设计细节",
        8: f"来宾在展陈区近距离观看并体验{product}，用真实互动补充现场温度",
        9: f"观众与媒体围绕{product}展区交流拍摄，建立发布会现场的人流热度",
        10: f"{brand}主视觉纹理再次闪现，由产品细节转回舞台核心发布段落",
        11: f"进入{runway}环节，以全景、人物中景和{product}特写连续推进视觉高潮",
        12: f"舞台进入{speech}环节，人物表达、{product}画面与核心信息在主屏同步呈现",
        13: f"观众举起手机记录{product}与舞台高光，形成现场传播和观看关系",
        14: f"用佩戴动作、人物神态与{product}微距细节交叉剪辑，保持{tone}的情绪节奏",
        15: f"品牌代表共同完成{product}正式揭幕，主屏定格发布信息与仪式节点",
        16: f"全场灯光汇聚至舞台中央，嘉宾共同完成{brand}发布启动仪式并合影",
        17: f"镜头从舞台缓慢拉远，带出观众、展陈与{product}空间的完整会场关系",
        18: f"画面收束为{project}主视觉KV，{brand}品牌标识、项目标题与END完成落版",
    }
    generated = descriptions.get(int(slot_index or 0), "")
    if generated:
        return generated
    return _polish_description(brief, fallback_description, source_brand)


def _brief_subject(brief: dict) -> str:
    required_terms = _split_required_terms(brief.get("必须出现的内容"))
    for term in required_terms:
        if any(keyword in term for keyword in ("产品", "珠宝", "家居", "汽车", "服装", "设备")):
            return term
    return "本次发布主题与体验内容"


def _functional_reference_reason(brief: dict, slot_index: int, asset: dict) -> str:
    expected = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
    evidence = " ".join(str(asset.get(key) or "") for key in (
        "原图路径", "来源文件", "来源 sheet", "视觉内容摘要", "视觉标签",
        "ocr_text", "brand_names",
    ))
    wrong_brands = conflicting_brands(expected, evidence) if expected else []
    if wrong_brands:
        return f"素材来自其他品牌或项目（{','.join(wrong_brands)}），仅保留镜头功能参考"

    industry = str(brief.get("行业或活动类别") or "")
    if "珠宝" in industry:
        if any(term in evidence for term in JEWELRY_CONFLICT_TERMS):
            return "素材内容属于非珠宝行业，仅保留镜头功能参考"
        subject_slots = {4, 7, 8, 9, 11, 13, 14, 15}
        if slot_index in subject_slots and not any(term in evidence for term in JEWELRY_VISUAL_TERMS):
            return "素材未出现珠宝主体，仅保留镜头功能参考"

    required_for_slot = [
        term for term in re.split(r"[、,，;；\s\n]+", str(brief.get("必须出现的内容") or ""))
        if term and _required_content_slot(term) == slot_index
    ]
    missing = [term for term in required_for_slot if term not in evidence]
    if missing:
        return f"素材未证明Brief必选内容（{'、'.join(missing)}），仅保留镜头功能参考"
    return ""


def _theme_from_brief(brief: dict) -> str:
    text = " ".join([
        str(brief.get("项目名称", "")),
        str(brief.get("必须出现的内容", "")),
    ])
    match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]+)主题字样", text)
    if match:
        return match.group(1)
    match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]+)发布会", text)
    if match:
        return match.group(1)
    return ""


def _visual_role_suffix(brief: dict, slot_index: int) -> str:
    if slot_index == 18:
        theme = _theme_from_brief(brief) or str(brief.get("品牌或客户") or "活动").strip()
        return f"完成{theme}主题与品牌落版"
    return VISUAL_ROLE_SUFFIXES.get(slot_index, "符合当前剪辑槽位职责")


def _target_duration(brief: dict) -> int:
    return validate_script_brief(brief)


def _allocate_durations(total: int, count: int) -> list[int | str]:
    if count <= 0:
        return []
    if total <= 0:
        return [""] * count
    if total == 60 and count == 18:
        return list(LAUNCH_60S_DURATIONS)
    base, remainder = divmod(total, count)
    return [base + (1 if index < remainder else 0) for index in range(count)]


def _note_with_duration(note: str, duration: int) -> str:
    parts = [
        part.strip()
        for part in str(note or "").split("；")
        if part.strip() and not re.match(r"建议时长[：:]", part.strip())
    ]
    return "；".join([f"建议时长：{duration}s", *parts])


def _compact_caption(summary: str, max_length: int = 76) -> str:
    text = re.sub(r"\s+", " ", str(summary or "")).strip(" 。")
    parts = [
        part.strip()
        for part in re.split(r"[，。；]", text)
        if part.strip()
        and not any(mark in part.casefold() for mark in ("小红书", "m+films", "m+ films"))
    ]
    text = "，".join(parts)
    if len(text) <= max_length:
        return text
    selected = []
    length = 0
    for part in parts:
        part = part.strip()
        if not part:
            continue
        extra = len(part) + (1 if selected else 0)
        if selected and length + extra > max_length:
            break
        selected.append(part)
        length += extra
    return "，".join(selected) or text[:max_length]


def _target_count(brief: dict) -> int:
    duration = _target_duration(brief)
    if 50 <= duration <= 70:
        return 16
    if duration:
        return max(1, round(duration / 4))
    return min(16, max(1, len(brief)))
