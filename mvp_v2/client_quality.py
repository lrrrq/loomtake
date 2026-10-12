from __future__ import annotations

import re

from .brand_policy import source_brand_leaks
from .models import parse_positive_seconds_strict
from .script_draft import MAX_PENDING_CONFIRMATIONS
from .visual_function import FUNCTION_LABELS, desired_visual_functions, infer_visual_functions


KNOWN_CASE_BRANDS = {
    "Cleer",
    "VIVO",
    "XTransfer",
    "林氏",
    "林氏家居",
    "美的",
    "周大福",
    "卞卡",
    "比音勒芬",
}
INTERNAL_PROCESS_PHRASES = {
    "Brief要求",
    "AI生成",
    "AI合成",
    "AI制作",
    "AI画面",
    "候选方案",
    "候选镜头",
    "候选来源",
    "镜头明确呈现",
}
ABSOLUTE_PATH_PATTERN = re.compile(r"(?:/Applications/|/Users/|[A-Za-z]:\\)")
NUMERIC_FACT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?%?(?:\s*(?:s|S|秒))?(?![A-Za-z0-9])"
)


def audit_client_visible_content(
    brief: dict,
    script_rows: list[dict],
    detail_rows: list[dict],
    *,
    overall_summary: str = "",
    pending_confirmations: list[dict] | None = None,
    quality_profile: str = "draft",
) -> list[dict]:
    checks = _audit_content_consistency(script_rows)
    if quality_profile != "client-review":
        return checks
    expected_brand = str(
        brief.get("品牌或客户")
        or brief.get("项目名称")
        or ""
    ).strip()
    visible_by_shot = {
        str(row.get("镜号") or index): _row_text(row)
        for index, row in enumerate(script_rows, start=1)
    }
    all_visible = "\n".join([overall_summary, *visible_by_shot.values()])

    source_brands = set(KNOWN_CASE_BRANDS)
    for detail in detail_rows:
        for key in (
            "来源品牌",
            "采用来源品牌",
            "源品牌",
            "source_brand",
            "来源品牌/经源",
        ):
            value = str(detail.get(key) or "").strip()
            if value:
                source_brands.add(value)
    leaks = sorted(
        brand
        for brand in source_brands
        if source_brand_leaks(expected_brand, brand, all_visible)
    )
    if leaks:
        checks.append(_check(
            "client_brand_leak",
            "hard",
            "",
            f"客户可见文案出现非当前项目品牌: {', '.join(leaks)}",
            "从客户文案删除源案例品牌，只保留 Brief 中的当前品牌",
        ))

    internal_hits = sorted(
        phrase for phrase in INTERNAL_PROCESS_PHRASES if phrase in all_visible
    )
    if internal_hits:
        checks.append(_check(
            "client_internal_process_language",
            "hard",
            "",
            f"客户可见文案包含内部过程语言: {', '.join(internal_hits)}",
            "改写为面向客户的可执行画面与声音描述",
        ))
    if "核心内容" in all_visible:
        checks.append(_check(
            "client_fallback_placeholder",
            "hard",
            "",
            "客户可见文案仍含‘核心内容’回退占位词",
            "依据当前 Brief 和已选来源改写为具体画面",
        ))
    if ABSOLUTE_PATH_PATTERN.search(all_visible):
        checks.append(_check(
            "client_absolute_path_leak",
            "hard",
            "",
            "客户可见文案包含本机绝对路径",
            "客户稿不展示本机路径或运行信息",
        ))

    evidence_text = "\n".join(
        [
            *(str(value or "") for value in brief.values()),
            *(
                str(row.get(key) or "")
                for row in detail_rows
                for key in (
                    "采用来源证据", "模板结构", "模板镜号", "时长(秒)",
                    "槽位功能", "允许素材", "禁止素材", "推荐景别", "推荐运镜",
                    "推荐声音", "节奏说明", "必须视觉证据", "重复素材规则",
                )
            ),
        ]
    )
    unsupported_numbers = sorted(_numeric_facts(all_visible) - _numeric_facts(evidence_text))
    if unsupported_numbers:
        checks.append(_check(
            "client_unsupported_numeric_fact",
            "hard",
            "",
            f"客户可见文案出现 Brief 或已审核来源未提供的数字: {', '.join(unsupported_numbers)}",
            "删除虚构数字，或在 Brief/已审核来源中补充明确依据",
        ))

    for shot_no, text in visible_by_shot.items():
        if _conflicting_core_events(text):
            checks.append(_check(
                "client_conflicting_core_events",
                "hard",
                shot_no,
                "同一镜头同时承载多个互相冲突的核心事件",
                "拆分或明确镜头的唯一主要行为",
            ))

    pending = pending_confirmations or []
    if len(pending) > MAX_PENDING_CONFIRMATIONS:
        checks.append(_check(
            "client_pending_confirmation_overuse",
            "hard",
            "",
            f"客户稿存在 {len(pending)} 项待确认，超过上限 {MAX_PENDING_CONFIRMATIONS}",
            "只保留会改变脚本内容且确实缺少依据的关键客户事实",
        ))
    elif pending:
        checks.append(_check(
            "client_pending_confirmation",
            "warning",
            "",
            f"客户稿存在 {len(pending)} 项已显式标记的待确认事实",
            "人工审核时确认责任人，不得由 AI 猜测补写",
        ))
    return checks


def _audit_content_consistency(rows: list[dict]) -> list[dict]:
    """Flag explicit contradictions for review; never infer missing event duties."""
    checks = []
    for index, row in enumerate(rows, start=1):
        shot = str(row.get("镜号") or index)
        description = _asserted_content(str(row.get("画面描述") or ""))
        ordinal = re.match(r"第\s*(\d+)\s*镜", description)
        if ordinal and ordinal.group(1) != shot:
            checks.append(_check("shot_reference_number_mismatch", "warning", shot,
                                 f"当前镜号为 {shot}，画面文字仍自称第 {ordinal.group(1)} 镜",
                                 "核对删镜或移动后的镜号引用，通过文字修订提案更新"))
        structure = _asserted_content(str(row.get("结构") or ""))
        camera = "；".join(_asserted_content(str(row.get(key) or "")) for key in ("类型", "机位", "拍摄类型"))
        camera_evidence = description + "；" + _asserted_content(str(row.get("运镜") or ""))
        aerial = any(term in camera for term in ("航拍", "无人机"))
        ground = any(term in camera for term in ("地面", "地拍", "肩扛", "手持"))
        ground_evidence = next((term for term in ("地面机位", "地面拍摄", "肩扛", "手持", "平视拍摄") if term in camera_evidence), "")
        aerial_evidence = next((term for term in ("航拍", "无人机") if term in camera_evidence), "")
        if (aerial and not ground and ground_evidence) or (ground and not aerial and aerial_evidence):
            checks.append(_check("content_camera_conflict", "warning", shot,
                f"拍摄类型/机位为‘{camera.strip('；')}’，画面或运镜同时明确‘{ground_evidence if aerial else aerial_evidence}’",
                "核对本镜实际拍法，统一类型、机位、运镜与画面说明"))

        people_action = re.search(
            r"(?:主持人|演讲者|嘉宾|观众|来宾|宾客|参与者|工作人员|工人|舞者|人物|人群|歌手|乐手)"
            r"[^。；，\n]{0,12}?(?:走上|走入|行走|发言|演讲|握手|鼓掌|交谈|签到|扫码|登台|表演|唱歌|跳舞|举起|操作|体验|迈步|挥手|入场|合影|移动|穿行|进入|涌入)",
            description,
        )
        if "空镜" in structure + description and people_action:
            checks.append(_check("content_empty_scene_people_conflict", "warning", shot,
                f"本镜标注空镜，画面同时描述人物动作‘{people_action.group(0)}’",
                "确认本镜为空间空镜还是人物镜头，统一结构与画面说明"))

        size = _asserted_content(str(row.get("景别") or ""))
        declared_sizes = _explicit_shot_sizes(size)
        described_sizes = _explicit_shot_sizes(description)
        scale_change = re.search(r"(?:推近|拉远|拉开|由.+(?:推|拉|切|到|至)|从.+(?:推|拉|切|到|至))", description)
        if declared_sizes and described_sizes and declared_sizes.isdisjoint(described_sizes) and not scale_change:
            checks.append(_check("content_shot_size_conflict", "warning", shot,
                f"景别字段为‘{size}’，画面明确使用‘{'、'.join(sorted(described_sizes))}’",
                "统一景别字段与画面；若本镜有景别变化，写明起止景别与变化方式"))

        # Broad technical labels such as 核心节奏 do not require a live event.
        expected = set(infer_visual_functions(structure))
        actual = set(infer_visual_functions(description))
        if expected and actual and expected.isdisjoint(actual):
            checks.append(_check("content_structure_conflict", "warning", shot,
                f"结构标注‘{structure}’，画面可识别内容为‘{_function_labels(actual)}’，两者职责可能不一致",
                "核对当前镜头承担的实际职责；允许调整结构标签或画面，不据此补造可选流程"))

        duration = parse_positive_seconds_strict(str(row.get("时长(秒)") or ""))
        if duration is None:
            duration_match = re.search(r"建议时长[：:]\s*(\d+)\s*(?:s|秒)", str(row.get("备注") or ""), flags=re.I)
            duration = int(duration_match.group(1)) if duration_match else None
        event_functions = actual.intersection({"registration", "guest_group", "product_experience", "service_social",
            "speech", "performance", "award", "startup_group", "audience_recording"})
        montage = any(term in description for term in ("快切", "速剪", "蒙太奇", "闪回"))
        if duration and len(event_functions) >= 3 and duration < len(event_functions) * 2 and not montage:
            checks.append(_check("content_duration_overload", "warning", shot,
                f"本镜 {duration} 秒内涉及 {len(event_functions)} 类动作：{_function_labels(event_functions)}；按每类约 2 秒的复核提示线可能过载",
                "人工核对每个动作实际占时，明确速剪意图或保留本镜主要动作；提示线不作硬性时长标准"))
        if duration:
            for key in ("旁白", "字幕"):
                text = str(row.get(key) or "").strip()
                length = len(re.findall(r"[\u4e00-\u9fff]|[A-Za-z0-9]+", text))
                if length > duration * 6:
                    checks.append(_check("content_copy_duration_overload", "warning", shot,
                        f"本镜 {duration} 秒的{key}包含约 {length} 个汉字/词，超过每秒 6 字/词的复核提示线",
                        "按实际语速或阅读节奏试读，酌情精简文字或调整时长；空旁白、空字幕无需补写"))

    for index, (left, right) in enumerate(zip(rows, rows[1:]), start=1):
        edge = f"{left.get('镜号') or index}→{right.get('镜号') or index + 1}"
        context = "；".join(str(row.get(key) or "") for row in (left, right)
            for key in ("画面描述", "效果", "备注"))
        if any(term in context for term in ("倒叙", "闪回", "蒙太奇", "章节", "回顾")):
            continue
        left_structure = _asserted_content(str(left.get("结构") or ""))
        right_structure = _asserted_content(str(right.get("结构") or ""))
        conflict = ""
        if any(term in left_structure for term in ("片尾", "落版", "结束")) and any(
            term in right_structure for term in ("开场", "片头", "入场")
        ):
            conflict = f"前镜结构为‘{left_structure}’，后镜回到‘{right_structure}’，未说明倒叙或章节转换"
        instruction = re.search(r"(?:下一镜|下镜)(?:头)?(?:切至|衔接至|进入|转至|接|为|呈现)([^。；，\n]+)",
            _asserted_content(str(left.get("备注") or "")))
        if instruction:
            target = set(desired_visual_functions(instruction.group(1)) or infer_visual_functions(instruction.group(1)))
            next_functions = set(infer_visual_functions(_asserted_content(str(right.get("画面描述") or ""))))
            if target and next_functions and target.isdisjoint(next_functions):
                conflict = f"前镜明确‘{instruction.group(0)}’，实际下一镜为‘{_function_labels(next_functions)}’"
        if conflict:
            checks.append(_check("content_transition_conflict", "warning", edge, conflict,
                "连看这两个镜头，核对实际先后关系和转场意图，必要时调整说明或顺序"))
    return checks


def _asserted_content(text: str) -> str:
    # Optional/negative production notes are not evidence that an action occurs.
    return "；".join(clause for clause in re.split(r"[。；;，,\n]", text) if not re.search(
        r"可选|备选|如有|如需|如果|若|待确认|视情况|条件允许|倒叙|闪回"
        r"|不(?:安排|出现|需要|拍摄|采用|使用|是|含)|未(?:出现|安排|见)|没有|无需|禁止|避免|非(?:航拍|空镜)", clause))


def _explicit_shot_sizes(text: str) -> set[str]:
    return set(re.findall(r"特写|近景|中景|全景|远景", text))


def _function_labels(functions: set[str]) -> str:
    return "、".join(FUNCTION_LABELS.get(value, value) for value in sorted(functions))


def _row_text(row: dict) -> str:
    return "\n".join(
        str(row.get(key) or "")
        for key in ("结构", "效果", "景别", "运镜", "画面描述", "声音", "备注")
    )


def _conflicting_core_events(text: str) -> bool:
    markers = ("发布", "启动", "签约", "颁奖", "演讲", "走秀", "合影")
    hits = {marker for marker in markers if marker in text}
    return len(hits) >= 3 and any(separator in text for separator in ("同时", "并且", "+", "及"))


def _numeric_facts(text: str) -> set[str]:
    text = re.sub(r"第\s*\d+\s*镜", "", str(text or ""))
    # This established camera label is not an asserted attendance or business number.
    text = re.sub(r"(?<!\d)360\s*全景", "", text)
    return {
        re.sub(r"\s*(?:s|秒)$", "", value, flags=re.IGNORECASE)
        for value in NUMERIC_FACT_PATTERN.findall(str(text or ""))
    }


def _check(item: str, level: str, shot_no: str, detail: str, suggestion: str) -> dict:
    return {
        "检查项": item,
        "级别": level,
        "状态": "fail" if level == "hard" else "warning",
        "关联镜号": shot_no,
        "详情": detail,
        "修复建议": suggestion,
        "audit_domain": "script_document",
    }
