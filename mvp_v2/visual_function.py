from __future__ import annotations

from collections import defaultdict
import re

from .script_type_adapter import normalize_script_type


EXPECTED_FUNCTION_BY_SLOT = {
    1: "brand_visual",
    2: "construction",
    3: "brand_visual",
    4: "venue_wide",
    5: "registration",
    6: "guest_group",
    7: "product_experience",
    8: "service_social",
    9: "crowd_flow",
    10: "brand_visual",
    11: "venue_wide",
    12: "speech",
    13: "audience_recording",
    14: "performance",
    15: "award",
    16: "startup_group",
    17: "venue_wide",
    18: "final_kv",
}

ALLOWED_FUNCTIONS_BY_SLOT = {
    1: {"brand_visual"},
    2: {"construction", "exterior_landmark", "brand_visual", "venue_wide"},
    3: {"brand_visual"},
    4: {"venue_wide"},
    5: {"registration", "product_experience", "service_social", "crowd_flow"},
    6: {"guest_group", "crowd_flow"},
    7: {"product_experience"},
    8: {"service_social"},
    9: {"crowd_flow", "audience_recording"},
    10: {"brand_visual"},
    11: {"venue_wide", "speech", "performance"},
    12: {"speech"},
    13: {"audience_recording"},
    14: {"performance", "speech", "audience_recording"},
    15: {"award", "formal_guest_node"},
    16: {"startup_group", "award"},
    17: {"venue_wide"},
    18: {"final_kv", "brand_visual"},
}

FORBIDDEN_FUNCTIONS_BY_SLOT = {
    6: {"award", "formal_guest_node", "startup_group"},
    15: {"startup_group"},
}

FUNCTION_LABELS = {
    "brand_visual": "品牌视觉",
    "construction": "搭建施工",
    "exterior_landmark": "地标/场地外观",
    "venue_wide": "会场全景",
    "registration": "签到登记",
    "guest_group": "嘉宾合影/交流",
    "product_experience": "产品/装置体验",
    "service_social": "服务/社交",
    "crowd_flow": "人流参观",
    "speech": "演讲宣讲",
    "audience_recording": "观众录屏/传播",
    "performance": "舞台表演",
    "award": "颁奖/授牌",
    "formal_guest_node": "嘉宾正式登台节点",
    "startup_group": "启动/高潮合影",
    "final_kv": "片尾主视觉",
    "display_static": "产品/装置展示",
}

_ALLOWED_REPEAT_SLOTS = {
    "brand_visual": {1, 3, 10},
    "venue_wide": {4, 11, 17},
    "product_experience": {5, 7},
    "crowd_flow": {5, 6, 9},
    "speech": {12, 14},
}

_FUNCTION_PRIORITY_BY_SLOT = {
    5: ("registration", "service_social", "product_experience"),
    6: ("guest_group", "crowd_flow"),
    9: ("crowd_flow", "audience_recording"),
    14: ("performance", "speech", "audience_recording"),
    15: ("award", "formal_guest_node"),
}


def infer_visual_function(text: str) -> str:
    functions = infer_visual_functions(text)
    return functions[0] if functions else ""


def desired_visual_functions(text: str) -> tuple[str, ...]:
    """Project a planned event onto the existing canonical visual functions."""
    value = " ".join(str(text or "").lower().split())
    if not value:
        return ()
    functions: list[str] = []
    if _contains(value, "品牌收束", "品牌落版", "片尾", "收尾", "落版"):
        return ("final_kv", "brand_visual")
    if "情绪反应" in value:
        return ("audience_recording",)
    if "动线推进" in value:
        return ("crowd_flow",)
    if "人物入画" in value:
        if "核心节奏" in value:
            return ("performance", "speech", "guest_group")
        return ("guest_group",)
    if _contains(value, "颁奖", "授牌", "领奖", "奖杯"):
        functions.append("award")
    if _contains(value, "启动仪式", "共同启动", "启动装置"):
        functions.append("startup_group")
    if _contains(value, "产品演示", "产品体验", "装置互动", "操作设备"):
        functions.append("product_experience")
    if _contains(value, "茶歇", "服务", "餐饮", "社交"):
        functions.append("service_social")
    if _contains(value, "嘉宾发布", "说明发布", "发布重点", "主题发言", "演讲"):
        functions.append("speech")
    if _contains(value, "手机记录", "手机拍摄", "传播"):
        functions.append("audience_recording")
    if _contains(value, "表演", "演出", "节目", "核心节奏"):
        functions.extend(("performance", "speech", "audience_recording"))
    if _contains(value, "人气", "人流", "现场互动"):
        functions.append("crowd_flow")
    if _contains(value, "嘉宾交流", "嘉宾合影"):
        functions.append("guest_group")
    if _contains(value, "开场造势", "会场建立", "空间建立"):
        functions.append("venue_wide")
    return tuple(dict.fromkeys(functions))


def infer_visual_functions(text: str) -> tuple[str, ...]:
    value = " ".join(str(text or "").lower().split())
    if not value:
        return ()
    functions = []
    if _contains(value, "启动仪式", "共同启动", "启动装置", "触碰发光", "发光球", "大合影"):
        functions.append("startup_group")
    if _contains(value, "颁奖", "奖牌", "授牌", "颁发", "领奖"):
        functions.append("award")
    if _formal_guest_node(value):
        functions.append("formal_guest_node")
    if _contains(value, "搭建", "施工", "脚手架", "布景结构", "工人搬运"):
        functions.append("construction")
    if _contains(value, "天际线", "建筑外观", "外场入口", "展厅外观", "商业街区", "圆顶建筑", "场馆外观", "地标"):
        functions.append("exterior_landmark")
    if _contains(value, "签到", "登记", "填写资料", "扫码入场", "入场登记"):
        functions.append("registration")
    if _contains(value, "手机拍摄", "手机录制", "手机记录", "举起手机", "用手机", "录屏"):
        functions.append("audience_recording")
    if _contains(value, "舞蹈", "舞台表演", "现场表演", "节目表演", "演出", "节目", "歌手", "乐队"):
        functions.append("performance")
    if _contains(value, "演讲者", "发表演讲", "进行演讲", "发言", "讲解数据", "主持人"):
        functions.append("speech")
    if _contains(value, "茶歇", "餐桌", "食物", "餐具", "饮品", "咖啡", "爆米花", "用餐", "服务台"):
        functions.append("service_social")
    if _guest_group(value):
        functions.append("guest_group")
    if _product_experience(value):
        functions.append("product_experience")
    if _contains(value, "静态陈列", "产品展示", "展示空间", "人体模特", "陈列架"):
        functions.append("display_static")
    if _contains(value, "人群", "观众聚集", "参观者", "通道", "宾客", "来宾", "人流"):
        functions.append("crowd_flow")
    if _contains(value, "主视觉海报", "主题宣传图", "品牌海报", "kv", "片尾落版", "宣传海报"):
        functions.append("final_kv")
    if _contains(value, "logo", "品牌标识", "品牌视觉", "品牌字样", "品牌标语", "标题文字", "发布会标题", "home coming"):
        functions.append("brand_visual")
    if _contains(value, "发布会现场", "发布会舞台", "主会场", "舞台全景", "会场全景", "led屏", "舞台场景"):
        functions.append("venue_wide")
    return tuple(dict.fromkeys(functions))


def visual_function_matches_slot(slot_index: int, text: str) -> bool:
    slot = int(slot_index or 0)
    functions = set(infer_visual_functions(text))
    if functions.intersection(FORBIDDEN_FUNCTIONS_BY_SLOT.get(slot, set())):
        return False
    allowed = ALLOWED_FUNCTIONS_BY_SLOT.get(slot)
    if not allowed:
        return True
    return bool(allowed.intersection(functions))


def annotate_visual_function_fields(
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    script_type: str = "发布会",
) -> None:
    if normalize_script_type(script_type) != "发布会":
        _annotate_generic_visual_functions(detail_rows, visual_asset_rows)
        return
    details_by_slot = {
        _slot_index(detail): detail
        for detail in detail_rows
    }
    uses = defaultdict(list)
    for asset in visual_asset_rows:
        slot = _slot_index(asset)
        detail = details_by_slot.get(slot)
        functions = infer_visual_functions(" ".join([
            str(asset.get("视觉内容摘要") or ""),
            str(asset.get("视觉标签") or ""),
        ]))
        allowed = ALLOWED_FUNCTIONS_BY_SLOT.get(slot, set())
        image_id = str(asset.get("image_id") or "").strip()
        if not image_id:
            judgement = "缺图"
        elif not functions:
            judgement = "未分类"
        elif visual_function_matches_slot(slot, " ".join([
            str(asset.get("视觉内容摘要") or ""),
            str(asset.get("视觉标签") or ""),
        ])):
            judgement = "匹配"
        else:
            judgement = "不匹配"
        assigned_function = _assigned_function(slot, functions) if functions else ""
        fields = {
            "视觉功能标签": (
                "；".join(FUNCTION_LABELS[item] for item in functions)
                if functions else "未分类（视觉证据不足）"
            ),
            "主视觉功能": FUNCTION_LABELS[assigned_function] if assigned_function else "",
            "槽位允许功能": "；".join(FUNCTION_LABELS[item] for item in sorted(allowed)),
            "功能适配判断": judgement,
            "功能重复组": "",
        }
        asset.update(fields)
        if detail is not None:
            detail.update(fields)
        if functions and image_id:
            uses[_assigned_function(slot, functions)].append(
                (slot, str(asset.get("镜号") or slot), image_id, asset, detail)
            )

    for function, records in uses.items():
        slots = {slot for slot, _, _, _, _ in records}
        image_ids = {image_id for _, _, image_id, _, _ in records}
        if len(slots) < 2 or len(image_ids) < 2:
            continue
        if slots.issubset(_ALLOWED_REPEAT_SLOTS.get(function, set())):
            continue
        group = f"{FUNCTION_LABELS[function]}:镜{','.join(shot for _, shot, _, _, _ in records)}"
        for _, _, _, asset, detail in records:
            asset["功能重复组"] = group
            if detail is not None:
                detail["功能重复组"] = group


def audit_visual_function_quality(
    detail_rows: list[dict],
    script_type: str = "发布会",
) -> list[dict]:
    if normalize_script_type(script_type) != "发布会":
        return _audit_generic_visual_functions(detail_rows)
    checks = []
    uses = defaultdict(list)
    for detail in detail_rows:
        image_id = str(detail.get("image_id") or "").strip()
        if not image_id:
            continue
        slot = _slot_index(detail)
        shot_no = str(detail.get("模板镜号") or slot or "")
        functions = infer_visual_functions(" ".join([
            str(detail.get("视觉内容摘要") or ""),
            str(detail.get("视觉标签") or ""),
        ]))
        if not functions:
            checks.append(_check(
                "visual_function_unclassified",
                shot_no,
                "已绑定图片无法归入发布会镜头功能",
                "人工补充功能标签，或完善视觉 caption/tags 后重跑",
            ))
            continue
        expected = EXPECTED_FUNCTION_BY_SLOT.get(slot)
        allowed = ALLOWED_FUNCTIONS_BY_SLOT.get(slot, {expected} if expected else set())
        if allowed and not visual_function_matches_slot(slot, " ".join([
            str(detail.get("视觉内容摘要") or ""),
            str(detail.get("视觉标签") or ""),
        ])):
            checks.append(_check(
                "visual_function_mismatch",
                shot_no,
                f"槽位允许“{'、'.join(FUNCTION_LABELS[item] for item in sorted(allowed))}”，图片功能为“{'、'.join(FUNCTION_LABELS[item] for item in functions)}”",
                "重新检索符合槽位职责的图片；如属合理改编，需在人工剪辑复核中说明",
            ))
        function = _assigned_function(slot, functions)
        uses[function].append((slot, shot_no, image_id))

    for function, records in uses.items():
        slots = {slot for slot, _, _ in records}
        unique_images = {image_id for _, _, image_id in records}
        if len(unique_images) < 2 or len(slots) < 2:
            continue
        allowed = _ALLOWED_REPEAT_SLOTS.get(function, set())
        if slots.issubset(allowed):
            continue
        checks.append(_check(
            "duplicate_visual_function",
            ",".join(shot for _, shot, _ in records),
            f"不同图片重复承担“{FUNCTION_LABELS[function]}”功能: slots={sorted(slots)}",
            "保留最符合槽位职责的一张，其余槽位改用不同叙事功能；合理回扣须人工确认",
        ))
    return checks


def _annotate_generic_visual_functions(
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
) -> None:
    details_by_slot = {_slot_index(detail): detail for detail in detail_rows}
    for asset in visual_asset_rows:
        slot = _slot_index(asset)
        detail = details_by_slot.get(slot)
        expected = str((detail or {}).get("槽位功能") or "").strip()
        text = " ".join([
            str(asset.get("视觉内容摘要") or ""),
            str(asset.get("视觉标签") or ""),
        ])
        image_id = str(asset.get("image_id") or "").strip()
        if not image_id:
            judgement = "缺图"
        elif not text.strip():
            judgement = "未分类"
        elif _generic_function_match(expected, text):
            judgement = "匹配"
        else:
            judgement = "待人工确认"
        fields = {
            "视觉功能标签": expected or "未定义",
            "主视觉功能": expected,
            "槽位允许功能": expected,
            "功能适配判断": judgement,
            "功能重复组": "",
        }
        asset.update(fields)
        if detail is not None:
            detail.update(fields)


def _audit_generic_visual_functions(detail_rows: list[dict]) -> list[dict]:
    checks = []
    for detail in detail_rows:
        image_id = str(detail.get("image_id") or "").strip()
        if not image_id:
            continue
        expected = str(detail.get("槽位功能") or "").strip()
        text = " ".join([
            str(detail.get("视觉内容摘要") or ""),
            str(detail.get("视觉标签") or ""),
        ])
        if not text.strip():
            checks.append(_check(
                "visual_function_unclassified",
                str(detail.get("模板镜号") or detail.get("slot_index") or ""),
                f"已绑定图片尚未读取，无法验证“{expected}”镜头功能",
                "脚本文档可继续审核；读取图片后再核对类别镜头功能",
            ))
        elif expected and not _generic_function_match(expected, text):
            checks.append(_check(
                "visual_function_mismatch",
                str(detail.get("模板镜号") or detail.get("slot_index") or ""),
                f"图片信息尚未证明当前类别槽位功能“{expected}”",
                "人工确认图片是否承担该镜头功能，或更换当前类别参考图",
            ))
    return checks


def _generic_function_match(expected: str, text: str) -> bool:
    terms = [
        term.strip()
        for term in re.split(r"[、,，;；/\s]+", str(expected or ""))
        if len(term.strip()) >= 2
    ]
    return not terms or any(term in str(text or "") for term in terms)


def _product_experience(text: str) -> bool:
    if _contains(text, "骑动感单车", "动感单车体验", "健身体验", "家居体验"):
        return True
    subjects = ("产品", "家居", "设备", "装置", "机器人", "儿童房", "家电")
    actions = ("体验", "试用", "操作", "触控", "试坐", "坐在木马")
    return any(value in text for value in subjects) and any(value in text for value in actions)


def _guest_group(text: str) -> bool:
    if _contains(text, "合影", "合照", "集体照", "嘉宾交流", "品牌墙打卡"):
        return True
    return "嘉宾" in text and _contains(
        text,
        "交流", "集体", "登台", "打卡", "摄影", "并排站立", "站在舞台", "舞台中央", "鼓掌",
    )


def _formal_guest_node(text: str) -> bool:
    if "嘉宾" not in text or not _contains(text, "舞台", "登台"):
        return False
    return _contains(
        text,
        "并排站立", "站在", "舞台中央", "鼓掌", "品牌亮相", "正式仪式", "正式节点",
    )


def _assigned_function(slot: int, functions: tuple[str, ...]) -> str:
    forbidden = FORBIDDEN_FUNCTIONS_BY_SLOT.get(slot, set())
    for function in functions:
        if function in forbidden:
            return function
    expected = EXPECTED_FUNCTION_BY_SLOT.get(slot)
    if expected in functions:
        return expected
    for function in _FUNCTION_PRIORITY_BY_SLOT.get(slot, ()):
        if function in functions:
            return function
    allowed = ALLOWED_FUNCTIONS_BY_SLOT.get(slot, set())
    for function in functions:
        if function in allowed:
            return function
    return functions[0]


def _contains(text: str, *terms: str) -> bool:
    return any(term in text for term in terms)


def _slot_index(detail: dict) -> int:
    try:
        return int(detail.get("逻辑槽位") or detail.get("slot_index") or 0)
    except (TypeError, ValueError):
        return 0


def _check(item: str, shot_no: str, detail: str, suggestion: str) -> dict:
    return {
        "检查项": item,
        "级别": "warning",
        "状态": "warning",
        "关联镜号": shot_no,
        "详情": detail,
        "修复建议": suggestion,
        "audit_domain": "media_readiness",
    }
