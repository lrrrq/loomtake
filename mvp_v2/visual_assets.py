from __future__ import annotations

import hashlib
import re
from pathlib import Path

from PIL import Image as PILImage

from .visual_function import visual_function_matches_slot
from .template_logic import logical_slot_index

from .excel_io import IMAGE_EXTENSIONS
from .brand_policy import (
    brand_evidence_unverified,
    brand_verdict_status,
    conflicting_brands,
    conflicting_visual_brand_names,
)


IMAGE_COLUMNS = ("画面示意", "效果示意")

VISUAL_ALLOCATION_PRIORITY = (
    18, 16, 15, 13, 5, 1, 12, 11, 6, 8, 7, 9, 17, 14, 4, 2, 10, 3,
)

VISUAL_REQUIREMENT_GROUPS = (
    (
        "城市/楼宇",
        ("城市主干道", "城市街道", "城市楼宇", "城市高楼", "城市天际线", "城市地标", "CBD", "楼宇街道"),
    ),
    (
        "邀请函/入场物料",
        ("邀请函", "邀请卡", "入场卡", "通行证", "宣传卡", "手持卡片"),
    ),
    ("logo/品牌标识", ("logo", "LOGO", "品牌logo", "品牌标识", "品牌标志", "标识特写", "logo动效", "立体字", "品牌立体字", "品牌标语", "主题字样", "标题文字")),
    ("主会场/大屏", ("主会场", "大屏", "舞台", "LED屏幕", "弧形屏幕")),
    (
        "外场/建筑",
        ("外场", "外景", "外立面", "建筑", "门头", "入口", "场地", "主会场外观", "场馆外景", "城市夜景", "主干道", "街道"),
    ),
    ("搭建/施工延时", ("搭建", "施工", "延时")),
    ("签到/打卡墙", ("签到", "签到墙", "打卡", "合影墙", "品牌墙")),
    (
        "登记/填写资料",
        ("登记", "填写资料", "填写表格", "查看文件", "路线图", "通行证", "入场证", "参赛物料"),
    ),
    ("人群/观众", ("人群", "观众", "宾客", "嘉宾", "参观", "台下", "粉丝", "乐迷")),
    ("嘉宾/人物", ("嘉宾", "高管", "主持人", "人物", "三位", "摄影团队", "交流")),
    (
        "互动体验",
        ("互动", "体验", "交流", "交谈", "产品互动", "顾客互动", "试坐", "坐在", "使用产品", "操作设备"),
    ),
    (
        "产品/空间体验",
        (
            "产品体验", "产品与主题空间体验", "家居体验", "试坐", "坐在", "使用产品", "操作设备",
            "体验设备", "装置互动", "木马", "动感单车", "骑行", "健身体验",
        ),
    ),
    (
        "服务/茶歇",
        (
            "茶歇", "餐台", "餐桌", "饮品", "小食", "零食", "菜品", "食物",
            "餐具", "聚餐", "用餐", "服务台", "饮品服务", "手持饮料",
        ),
    ),
    ("智能设备展示", ("智能设备", "机器人", "爆米花机", "机械臂", "自动操作")),
    ("演讲者/主持人", ("演讲者", "主持人", "报幕", "战略口号")),
    ("手机录屏", ("手机", "录屏", "录制", "屏幕录制")),
    ("颁发/证书", ("颁发", "颁奖", "颁奖典礼", "证书", "奖牌", "奖项", "授牌")),
    ("正式仪式", ("正式仪式", "启动仪式", "共同启动", "品牌亮相", "集体登台")),
    ("启动/大合影", ("启动", "大合影", "合影", "仪式启动")),
    ("表演", ("表演", "走秀", "舞蹈", "中场")),
    ("主视觉/KV/END", ("主视觉", "KV", "END", "落版", "海报", "宣传图", "第一品牌", "品牌宣传", "企业形象")),
    (
        "360/会场全貌",
        ("360", "拉远", "会场全貌", "外围会场", "会场全景", "舞台全景", "全景舞台", "全貌"),
    ),
    (
        "设备/外设",
        ("电竞外设", "电脑外设", "游戏外设", "键盘", "鼠标", "耳机", "显示器", "电脑主机"),
    ),
    (
        "接力棒/火炬",
        ("接力棒", "接力棍", "接力火炬", "赛事火炬"),
    ),
    (
        "参赛者/运动员",
        ("参赛者", "运动员", "选手", "跑者", "跑步者", "赛事选手"),
    ),
    (
        "IP/吉祥物",
        ("吉祥物", "IP形象", "赛事IP", "卡通人偶", "充气人偶"),
    ),
    (
        "电竞场景",
        ("电竞", "电竞舱", "游戏比赛", "游戏选手", "电子竞技"),
    ),
    (
        "赛车/车辆",
        ("赛车", "F1", "车手", "方向盘", "赛车模拟器", "汽车", "车辆", "驾驶舱", "卡丁车"),
    ),
    ("轮胎", ("轮胎",)),
    ("应援动作", ("应援", "欢呼", "呐喊", "加油", "灯牌", "应援棒")),
    ("集体合影", ("集体合影", "团队合影", "全员合影", "多人合影", "大合影", "集体照", "大合照")),
    ("球类击球动作", ("球拍", "挥拍", "击球", "发球", "回球", "扣杀")),
    ("饮料/杯", ("饮料", "饮品", "倒入杯", "注入杯", "杯中", "碰杯", "举杯")),
    ("航拍/俯视", ("航拍", "鸟瞰", "俯瞰", "俯拍", "俯视", "无人机视角")),
    ("合唱/演唱", ("合唱", "演唱", "领唱", "唱歌", "高歌")),
)

# Specific subject/action/angle evidence for an edited current shot. Missing
# evidence makes it a reference candidate; it does not forbid manual use.
CURRENT_SHOT_REQUIREMENTS = (
    ("会场/活动现场", ("会场", "会展中心", "展区", "活动现场"),
     ("会场", "会展中心", "展区", "展厅", "场馆", "舞台", "观众席", "发布会现场", "活动现场")),
    ("歌手/演唱者", ("歌手", "主唱", "乐队"), ("歌手", "主唱", "乐队", "演唱", "唱歌")),
    ("持麦/麦克风", ("持麦", "麦克风", "话筒"), ("持麦", "麦克风", "话筒", "拿着麦", "手持麦")),
    ("发言/致辞", ("致辞", "发言", "讲话", "演讲"), ("致辞", "发言", "讲话", "演讲", "讲解")),
    ("面部/表情", ("面部", "脸部", "汗水", "喘息"), ("面部", "脸部", "脸庞", "面孔", "表情", "汗水", "喘息")),
    ("握手", ("握手",), ("握手",)),
    ("低机位/仰拍", ("低机位", "仰拍", "仰视"), ("低机位", "低角度", "仰拍", "仰视", "low angle", "low-angle")),
    ("圆形屏", ("圆形屏", "圆屏", "环形屏"), ("圆形屏", "圆屏", "环形屏", "圆形大屏", "圆形LED")),
    ("特写构图", ("特写",), ("特写", "close-up", "closeup")),
    ("吉他", ("吉他",), ("吉他",)),
    ("乐器演奏", ("演奏",), ("演奏", "弹奏", "弹吉他", "弹琴", "打鼓")),
    ("跑步动作", ("跑步", "奔跑", "起跑", "冲刺", "赛跑"),
     ("跑步", "奔跑", "起跑", "冲刺", "赛跑", "跑动")),
    ("篮球", ("篮球",), ("篮球",)),
    ("运球动作", ("运球",), ("运球", "带球", "控球")),
    ("领奖台", ("领奖台",), ("领奖台", "颁奖台")),
    ("展台/展馆", ("展台", "展馆", "展厅"), ("展台", "展馆", "展厅", "展位", "展区")),
    ("产品/展品", ("产品", "展品"), ("产品", "展品", "商品", "展出设备", "展示设备")),
    ("酒杯", ("酒杯",), ("酒杯", "高脚杯", "香槟杯", "红酒杯")),
    ("红毯", ("红毯",), ("红毯", "红地毯")),
    ("走秀", ("走秀",), ("走秀", "时装秀", "T台", "t台", "秀场行走")),
    ("上手操作", ("上手", "操作", "试用"),
     ("上手", "操作", "试用", "操控", "试玩", "试戴", "触摸", "使用产品")),
    ("媒体/记者", ("媒体", "记者"), ("媒体", "记者", "摄影师", "摄像师")),
    ("相机/摄像机", ("相机", "摄像机"), ("相机", "摄像机", "摄影机")),
    ("拍摄动作", ("拍摄", "拍照", "录制"), ("拍摄", "拍照", "录制", "摄影", "录像")),
    ("颁发动作", ("颁发", "颁奖", "授奖"), ("颁发", "颁奖", "授奖", "授牌", "递交奖杯")),
    ("电饭煲主体", ("电饭煲", "电饭锅", "rice cooker"), ("电饭煲", "电饭锅", "rice cooker")),
    ("厨房场景", ("厨房",), ("厨房", "kitchen")),
    ("上盖控制面板", ("上盖控制面板", "上盖面板", "上盖顶面控制", "上盖顶面的"),
     ("上盖控制面板", "上盖面板", "上盖顶面", "上盖按键", "盖顶按键", "上盖的控制", "上盖上的控制")),
    ("按键操作", ("手指按键", "按键操作", "按下按键", "手指按", "按开/关"),
     ("按键操作", "按下按键", "手指按", "按下按钮", "按开/关", "按键启动")),
    ("备米入锅", ("备米", "米放入", "米放进", "放入内锅"),
     ("备米", "米放入", "米放进", "放米", "倒米", "米倒入", "向内锅放米", "放入内锅")),
    ("开盖成饭", ("成饭", "开盖成饭", "米饭状态"),
     ("米饭", "熟饭", "成饭", "白米饭")),
    ("开盖状态", ("开盖", "打开锅盖", "打开上盖"),
     ("开盖", "打开锅盖", "打开上盖", "盖子打开", "上盖打开", "锅盖打开", "敞开的")),
    ("盛饭动作", ("盛饭",), ("盛饭", "盛出米饭", "盛出白米饭", "米饭盛入", "把饭盛入")),
    ("端饭上桌", ("端至", "端到", "盛饭上桌"),
     ("端至", "端到", "端饭", "端着饭碗", "把饭碗放在", "饭碗端上", "盛饭上桌")),
)

BACKGROUND_DEPTH_MARKERS = (
    "背景为",
    "背景中",
    "背景可见",
    "背景有",
    "远处可见",
    "远处是",
    "后方可见",
    "背景是",
    "在模糊的",
    "在虚化的",
)

SUBJECT_SALIENCE_GROUPS = frozenset({
    "城市/楼宇",
    "外场/建筑",
    "人群/观众",
    "嘉宾/人物",
    "参赛者/运动员",
    "IP/吉祥物",
    "演讲者/主持人",
    "手机录屏",
    "颁发/证书",
    "正式仪式",
    "启动/大合影",
    "表演",
})

# Composition subjects can be requested alongside a setting without requiring
# that setting to be the foreground. These aliases affect depth evidence only;
# they do not create new mandatory requirements or change candidate scoring.
SUBJECT_SALIENCE_CONTEXT_GROUPS = (
    ("飞鸟", ("飞鸟", "鸟", "灰鹭", "鸽子")),
    ("广告牌", ("广告牌", "户外广告", "外墙广告", "外立面广告")),
)

STRICT_PRIMARY_VISUAL_GROUPS = frozenset({
    "城市/楼宇",
    "邀请函/入场物料",
    "外场/建筑",
    "登记/填写资料",
    "设备/外设",
    "接力棒/火炬",
    "IP/吉祥物",
    "主视觉/KV/END",
    "赛车/车辆",
    "轮胎",
    "集体合影",
    "球类击球动作",
    "饮料/杯",
})


def attach_visual_assets(script_rows: list[dict], detail_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    enriched_details = [dict(row) for row in detail_rows]
    visual_assets = []
    for index, script_row in enumerate(script_rows):
        detail = enriched_details[index] if index < len(enriched_details) else {}
        row_assets = []
        for role in IMAGE_COLUMNS:
            raw_value = str(script_row.get(role, "") or "").strip()
            if not raw_value:
                continue
            asset = _visual_asset_from_value(raw_value, role, script_row, detail)
            visual_assets.append(asset)
            row_assets.append(asset)
        _attach_asset_summary(detail, row_assets)
    return enriched_details, visual_assets


def hydrate_visual_asset_identities(
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    manifest_rows: list[dict] | None,
) -> tuple[list[dict], list[dict]]:
    """只使用路径将已审核参考图绑定到稳定 image_id，不冒充视觉模型分析。"""
    details_by_slot = {
        str(row.get("slot_index") or index): row
        for index, row in enumerate(detail_rows, start=1)
    }
    manifest_by_path = {}
    for row in manifest_rows or []:
        paths = [row.get("原图路径"), *(row.get("alternate_paths") or [])]
        for value in paths:
            if value:
                manifest_by_path[
                    str(Path(str(value)).expanduser().resolve())
                ] = row
    for asset in visual_asset_rows:
        raw_path = str(asset.get("原图路径") or "").strip()
        if not raw_path:
            continue
        manifest = manifest_by_path.get(str(Path(raw_path).expanduser().resolve()))
        if not manifest:
            continue
        asset["image_id"] = manifest.get("image_id", "")
        asset["索引语义依据"] = manifest.get("semantic_basis", "")
        asset["资产角色"] = manifest.get("asset_role", "")
        detail = details_by_slot.get(str(asset.get("slot_index") or ""))
        if detail is not None:
            _attach_asset_summary(detail, [asset])
    return detail_rows, visual_asset_rows


def apply_visual_analysis(
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    analysis: dict,
) -> tuple[list[dict], list[dict]]:
    detail_by_slot = {
        str(row.get("slot_index") or index): row
        for index, row in enumerate(detail_rows, start=1)
    }
    for asset in visual_asset_rows:
        item = _analysis_for_asset(asset, analysis)
        if not item:
            continue
        detail = detail_by_slot.get(str(asset.get("slot_index") or ""))
        tags = _analysis_tags(item)
        summary = str(item.get("summary") or item.get("caption") or "")
        source = str(item.get("source") or "外部分析")
        script_row = _script_row_for_asset(script_rows, asset)
        match = _score_visual_match(_visual_match_context(script_row, detail), summary, tags, observations=item)

        asset["视觉读取状态"] = source
        asset["视觉内容摘要"] = summary
        asset["视觉标签"] = "；".join(tags)
        asset["ocr_text"] = str(item.get("ocr_text") or "")
        asset["brand_names"] = "；".join(str(value) for value in item.get("brand_names") or [])
        asset["negative_flags"] = "；".join(str(value) for value in item.get("negative_flags") or [])
        asset["视觉匹配分"] = match["score"]
        asset["命中视觉要素"] = "；".join(match["hits"])
        asset["缺失视觉要素"] = "；".join(match["missing"])
        asset["冲突视觉要素"] = "；".join(match["conflicts"])
        asset["配图判断"] = match["judgement"]
        asset["配图置信度"] = match["confidence"]
        asset["视觉适配模式"] = "primary"
        asset["视觉适配查询"] = _visual_match_context(script_row, detail)

        if detail is not None:
            _apply_asset_analysis_to_detail(detail, asset)
    return detail_rows, visual_asset_rows


def rematch_visual_assets(
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    analysis: dict,
    candidate_paths: list[Path],
    min_score: int = 80,
    manifest_rows: list[dict] | None = None,
    analysis_rows: list[dict] | None = None,
    brief: dict | None = None,
    visual_excludes_by_slot: dict[str, list[str]] | None = None,
    visual_preferences_by_slot: dict[str, str] | None = None,
    forced_visuals_by_slot: dict[str, dict] | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    detail_by_slot = {
        str(row.get("slot_index") or index): row
        for index, row in enumerate(detail_rows, start=1)
    }
    used_paths: set[str] = set()
    used_slots_by_path: dict[str, set[int]] = {}
    manifest_by_path = {
        str(Path(str(row.get("原图路径") or ""))): row
        for row in (manifest_rows or [])
        if row.get("原图路径")
    }
    approved_paths = {
        str(Path(str(row.get("原图路径") or "")))
        for row in (manifest_rows or [])
        if row.get("原图路径")
    }
    preferences = visual_preferences_by_slot or {}
    forced = forced_visuals_by_slot or {}
    use_launch_contract = str((brief or {}).get("脚本类型") or "发布会").strip() == "发布会"
    from .image_retrieval import (
        _event_domain_affinity,
        _event_domain_conflict,
        _event_domains,
        slot_forbidden_visuals,
    )
    _add_feedback_placeholders(script_rows, detail_rows, visual_asset_rows, preferences)
    for index in _visual_allocation_indices(
        visual_asset_rows,
        use_launch_contract=use_launch_contract,
    ):
        asset = visual_asset_rows[index]
        detail = detail_by_slot.get(str(asset.get("slot_index") or ""))
        script_row = _script_row_for_asset(script_rows, asset)
        _hydrate_manifest_identity(asset, detail, manifest_by_path)
        current_path = str(asset.get("原图路径") or "")
        current_untrusted = (
            manifest_rows is not None
            and _should_clear_untrusted_asset(asset, approved_paths)
        )
        excluded_visuals = (visual_excludes_by_slot or {}).get(str(asset.get("slot_index") or ""), [])
        current_excluded = _asset_is_explicitly_excluded(asset, excluded_visuals)
        current_slot = logical_slot_index(detail or asset)
        feedback_slot = str(
            asset.get("slot_index")
            or (detail or {}).get("slot_index")
            or current_slot
        )
        visual_preference = str(
            preferences.get(feedback_slot)
            or preferences.get(str(current_slot))
            or ""
        ).strip()
        current_judgement = str(asset.get("配图判断") or "")
        current_score = _as_int(asset.get("视觉匹配分"), default=0)
        current_low_res = "低清" in str(asset.get("质量指标") or "")
        current_source_reference = str(
            asset.get("asset_role") or asset.get("资产角色") or ""
        ) == "source_reference"
        from .image_understanding import analysis_asset_unusable
        current_quality_blocked = analysis_asset_unusable(asset)
        current_slot_conflicts = (
            slot_forbidden_visuals(
                current_slot,
                str(asset.get("视觉内容摘要") or ""),
                _analysis_tags(asset),
            )
            if use_launch_contract else []
        )
        current_function_mismatch = bool(
            use_launch_contract
            and not visual_function_matches_slot(
                current_slot,
                " ".join([
                    str(asset.get("视觉内容摘要") or ""),
                    " ".join(_analysis_tags(asset)),
                ]),
            )
        )
        current_brand_issue = _bound_visual_brand_issue(asset, brief or {})
        current_salience_conflicts = _background_only_required_groups(
            _visual_match_context(script_row, detail),
            str(asset.get("视觉内容摘要") or ""),
        )
        current_visual_context = " ".join(
            str(value)
            for value in (
                script_row.get("画面描述"),
                (detail or {}).get("槽位功能"),
                (detail or {}).get("主视觉功能"),
                (detail or {}).get("必须视觉证据"),
            )
            if value
        )
        current_strict_missing = _strict_primary_visual_missing(
            current_visual_context,
            str(asset.get("视觉内容摘要") or ""),
            _analysis_tags(asset),
        ) if not use_launch_contract else []
        brief_domain_text = " ".join(
            str((brief or {}).get(key) or "")
            for key in ("项目名称", "行业或活动类别", "必须出现的内容")
        )
        current_domain_weak = bool(
            not use_launch_contract
            and current_score < 85
            and _event_domains(brief_domain_text)
            and not _event_domain_affinity(
                brief or {},
                str(asset.get("视觉内容摘要") or ""),
                _analysis_tags(asset),
            )
        )
        current_domain_conflict = (
            _event_domain_conflict(
                brief or {},
                current_visual_context,
                str(asset.get("视觉内容摘要") or ""),
                _analysis_tags(asset),
            )
            if not use_launch_contract else ""
        )
        callback_slots = used_slots_by_path.get(current_path, set()) | {current_slot}
        duplicate_with_assigned_slot = bool(
            current_path
            and current_path in used_paths
            and callback_slots != {3, 10}
        )
        duplicate_requires_rematch = duplicate_with_assigned_slot
        if (
            current_judgement in {"匹配", "功能参考"}
            and (not current_low_res or current_source_reference)
            and not current_quality_blocked
            and not duplicate_requires_rematch
            and not current_slot_conflicts
            and not current_function_mismatch
            and not current_brand_issue
            and not current_salience_conflicts
            and not current_strict_missing
            and not current_domain_weak
            and not current_domain_conflict
            and not current_excluded
            and not current_untrusted
            and not visual_preference
        ):
            if current_path:
                used_paths.add(current_path)
                used_slots_by_path.setdefault(current_path, set()).add(current_slot)
            continue
        search_script_row = dict(script_row)
        if visual_preference:
            search_script_row["画面描述"] = "；".join(
                value for value in (
                    str(script_row.get("画面描述") or "").strip(),
                    f"用户配图要求：{visual_preference}",
                ) if value
            )
            if detail is not None:
                _append_detail_note(detail, f"本轮按用户配图反馈重配：{visual_preference}")
        if current_slot_conflicts and detail is not None:
            _append_detail_note(detail, "当前配图命中槽位禁止视觉: " + "、".join(current_slot_conflicts))
        if current_function_mismatch and detail is not None:
            _append_detail_note(detail, "当前配图不符合槽位允许的镜头功能")
        if current_quality_blocked and detail is not None:
            _append_detail_note(detail, "当前配图命中视觉质量阻断项，必须重新检索")
        if current_brand_issue and detail is not None:
            _append_detail_note(detail, "当前配图品牌证据与项目不一致，必须重新检索")
        if current_salience_conflicts and detail is not None:
            _append_detail_note(
                detail,
                "当前配图主体仅出现在背景，必须重新检索: "
                + "、".join(current_salience_conflicts),
            )
        if current_strict_missing and detail is not None:
            _append_detail_note(
                detail,
                "当前配图缺少必须主体，必须重新检索: "
                + "、".join(current_strict_missing),
            )
        if current_domain_weak and detail is not None:
            _append_detail_note(detail, "当前配图与Brief活动领域关联较弱，尝试检索更准确候选")
        if current_domain_conflict and detail is not None:
            _append_detail_note(detail, f"当前配图活动领域冲突：{current_domain_conflict}")
        if current_untrusted and detail is not None:
            _append_detail_note(detail, "当前图片未入库，不能作为自动配图证据")
        using_manifest = manifest_rows is not None and analysis_rows is not None
        forced_visual = forced.get(feedback_slot) or forced.get(str(current_slot)) or {}
        best = _best_manifest_candidate(
            search_script_row,
            detail,
            brief or {},
            manifest_rows,
            analysis_rows,
            used_paths - _callback_reuse_paths(current_slot, used_slots_by_path),
            excluded_visuals,
            allow_reuse=False,
            forced_visual=forced_visual,
        ) if using_manifest else None
        if best is None and not using_manifest:
            best = _best_visual_candidate(
                search_script_row,
                analysis,
                candidate_paths,
                used_paths,
                detail,
                excluded_visuals,
            )
        acceptance_score = 55 if forced_visual else min_score
        if not best or best["score"] < acceptance_score:
            if detail is not None:
                gap = best.get("gap_reason") if best else ""
                note = f"视觉重配未找到高分候选，保留当前图片并标缺口{f': {gap}' if gap else ''}"
                _append_detail_note(detail, note)
            if duplicate_requires_rematch:
                _clear_untrusted_visual_asset(
                    script_row,
                    detail,
                    asset,
                    "非回扣槽位重复使用同一图片，且未找到达到阈值的独立候选",
                )
            elif current_domain_conflict:
                _clear_untrusted_visual_asset(
                    script_row,
                    detail,
                    asset,
                    current_domain_conflict,
                )
            elif current_excluded:
                _clear_untrusted_visual_asset(
                    script_row,
                    detail,
                    asset,
                    "上一轮视觉审计已排除该素材，且未找到达到阈值的替代候选",
                )
            elif current_untrusted:
                _clear_untrusted_visual_asset(
                    script_row,
                    detail,
                    asset,
                    "当前图片未入库或视觉识别不匹配，已从脚本图列移除",
                )
            elif current_brand_issue:
                _clear_untrusted_visual_asset(
                    script_row,
                    detail,
                    asset,
                    "当前配图品牌证据与项目不一致，且未找到达到阈值的替代候选",
                )
            elif current_low_res and not current_source_reference:
                _clear_untrusted_visual_asset(
                    script_row,
                    detail,
                    asset,
                    "当前配图低清，且未找到达到自动阈值的高清替代候选",
                )
            elif current_salience_conflicts:
                _clear_untrusted_visual_asset(
                    script_row,
                    detail,
                    asset,
                    "当前配图要求的主体只在背景出现，且未找到达到阈值的替代候选",
                )
            elif current_strict_missing:
                _clear_untrusted_visual_asset(
                    script_row,
                    detail,
                    asset,
                    "当前配图缺少必须主体，且未找到达到阈值的替代候选",
                )
            continue
        candidate_path = best["path"]
        script_row["画面示意"] = str(candidate_path)
        replacement = _visual_asset_from_candidate(candidate_path, script_row, detail or {}, best)
        visual_asset_rows[index] = replacement
        if detail is not None:
            prior_binding_note = str(detail.get("图片绑定说明") or "")
            _attach_asset_summary(detail, [replacement])
            detail["图片绑定说明"] = "；".join(dict.fromkeys(
                note for note in (prior_binding_note, replacement["绑定说明"])
                if note
            ))
        used_paths.add(str(candidate_path))
        used_slots_by_path.setdefault(str(candidate_path), set()).add(current_slot)
    return script_rows, detail_rows, visual_asset_rows


def _add_feedback_placeholders(
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict],
    preferences: dict[str, str],
) -> None:
    existing_slots = {
        str(asset.get("slot_index") or logical_slot_index(asset))
        for asset in visual_asset_rows
        if asset.get("slot_index") or logical_slot_index(asset)
    }
    for index, detail in enumerate(detail_rows):
        display_slot = str(detail.get("slot_index") or index + 1)
        logical_slot = str(logical_slot_index(detail))
        preference = preferences.get(display_slot) or preferences.get(logical_slot)
        if not preference or display_slot in existing_slots or index >= len(script_rows):
            continue
        visual_asset_rows.append(
            _visual_asset_from_value("", "画面示意", script_rows[index], detail)
        )
        existing_slots.add(display_slot)


def _bound_visual_brand_issue(asset: dict, brief: dict) -> bool:
    if str(asset.get("asset_role") or asset.get("资产角色") or "") == "source_reference":
        return False
    expected_brand = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
    brand_names = asset.get("brand_names")
    if brand_verdict_status(brand_names) != "explicit":
        return True
    brand_text = " ".join([
        str(asset.get("视觉内容摘要") or ""),
        str(asset.get("视觉标签") or ""),
        str(asset.get("ocr_text") or ""),
        str(brand_names or ""),
    ])
    if conflicting_brands(expected_brand, brand_text):
        return True
    if conflicting_visual_brand_names(expected_brand, brand_names):
        return True
    return brand_evidence_unverified(expected_brand, brand_names, asset.get("ocr_text"))


def _asset_is_explicitly_excluded(asset: dict, excluded_visuals: list[str]) -> bool:
    excluded = {str(value).strip() for value in excluded_visuals if str(value).strip()}
    path = str(asset.get("原图路径") or "").strip()
    image_id = str(asset.get("image_id") or "").strip()
    return bool(
        (image_id and image_id in excluded)
        or (path and path in excluded)
        or (path and Path(path).name in excluded)
    )


def _visual_allocation_indices(
    assets: list[dict],
    *,
    use_launch_contract: bool = True,
) -> list[int]:
    if not use_launch_contract:
        return sorted(
            range(len(assets)),
            key=lambda index: (
                _as_int(
                    assets[index].get("slot_index") or assets[index].get("镜号"),
                    default=index + 1,
                ),
                index,
            ),
        )
    rank = {slot: index for index, slot in enumerate(VISUAL_ALLOCATION_PRIORITY)}

    def sort_key(index: int) -> tuple[int, int]:
        slot = logical_slot_index(assets[index])
        return rank.get(slot, len(rank)), index

    return sorted(range(len(assets)), key=sort_key)


def _callback_reuse_paths(current_slot: int, used_slots_by_path: dict[str, set[int]]) -> set[str]:
    if current_slot not in {3, 10}:
        return set()
    return {
        path for path, slots in used_slots_by_path.items()
        if slots and slots <= {3, 10}
    }


def _hydrate_manifest_identity(asset: dict, detail: dict | None, manifest_by_path: dict[str, dict]) -> None:
    path = str(asset.get("原图路径") or "")
    if not path:
        return
    manifest = manifest_by_path.get(str(Path(path)))
    if not manifest:
        return
    asset["image_id"] = manifest.get("image_id", "")
    asset["asset_role"] = manifest.get("asset_role", "")
    asset["资产角色"] = manifest.get("asset_role", "")
    if detail is not None:
        _attach_asset_summary(detail, [asset])


def _should_clear_untrusted_asset(asset: dict, approved_paths: set[str]) -> bool:
    path = str(asset.get("原图路径") or "")
    if not path:
        return False
    if path not in approved_paths:
        return True
    return str(asset.get("配图判断") or "") in {"", "不匹配"}


def _clear_untrusted_visual_asset(script_row: dict, detail: dict | None, asset: dict, reason: str) -> None:
    role = str(asset.get("图片角色") or "画面示意")
    if role in script_row:
        script_row[role] = ""
    asset["原图路径"] = ""
    asset["image_id"] = ""
    asset["原图宽"] = ""
    asset["原图高"] = ""
    asset["文件大小(bytes)"] = ""
    asset["绑定方式"] = "缺口"
    asset["视觉读取状态"] = "未入库"
    asset["配图置信度"] = "低"
    asset["视觉匹配分"] = 0
    asset["配图判断"] = "不匹配"
    asset["视觉内容摘要"] = ""
    asset["视觉标签"] = ""
    asset["ocr_text"] = ""
    asset["brand_names"] = ""
    asset["negative_flags"] = ""
    asset["命中视觉要素"] = ""
    asset["缺失视觉要素"] = ""
    asset["冲突视觉要素"] = ""
    asset["绑定说明"] = reason
    if detail is not None:
        _attach_asset_summary(detail, [asset])
        detail["图片绑定说明"] = reason


def audit_visual_evidence(detail_rows: list[dict]) -> list[dict]:
    checks = []
    visual_uses: dict[str, list[tuple[int, str]]] = {}
    for detail in detail_rows:
        mode = str(detail.get("替换方式") or "")
        shot_no = str(detail.get("模板镜号") or detail.get("slot_index") or "")
        visual_key = str(detail.get("image_id") or detail.get("原图路径") or "")
        if visual_key:
            try:
                slot_index = int(detail.get("slot_index") or 0)
            except (TypeError, ValueError):
                slot_index = 0
            visual_uses.setdefault(visual_key, []).append((slot_index, shot_no))
        judgement = str(detail.get("配图判断") or "")
        if judgement == "功能参考":
            _add_check(
                checks,
                "visual_function_reference_only",
                "warning",
                "warning",
                shot_no,
                "当前图片是已审核的镜头功能参考，不代表当前项目成片素材",
                "脚本文档可继续审核；成片制作前补充当前项目素材",
            )
        if mode not in {"REPLACE", "ADAPT"}:
            continue
        if not detail.get("视觉资产ID"):
            _add_check(
                checks,
                "missing_visual_asset",
                "warning",
                "warning",
                shot_no,
                "REPLACE/ADAPT 镜头缺少 visual_asset_id",
                "为该镜头绑定行来源图片，或写明模板/图片池 fallback 理由",
            )
        automated_visual_read = str(detail.get("视觉读取状态") or "").lower() in {
            "minimax",
            "api",
            "openai",
        }
        if automated_visual_read and not detail.get("image_id"):
            _add_check(
                checks,
                "missing_manifest_image_id",
                "warning",
                "warning",
                shot_no,
                "REPLACE/ADAPT 镜头缺少资产层 image_id",
                "把最终图片绑定到 image_manifest.jsonl，不能只保留临时 visual_asset_id",
            )
        if not detail.get("图片绑定方式") or not detail.get("图片绑定说明"):
            _add_check(
                checks,
                "missing_visual_binding_reason",
                "warning",
                "warning",
                shot_no,
                "REPLACE/ADAPT 镜头缺少图片绑定方式或绑定说明",
                "在视觉资产清单中写明行绑定、模板回退或图片池回退原因",
            )
        if "低清" in str(detail.get("视觉质量指标") or ""):
            _add_check(
                checks,
                "low_resolution_visual_asset",
                "warning",
                "warning",
                shot_no,
                "视觉资产低于 1280x720",
                "替换为高清原图，或仅作为低清参考放行",
            )
        if (
            str(detail.get("图片绑定方式") or "") == "编号回退"
            and (
                "未读取" in str(detail.get("视觉读取状态") or "")
                or str(detail.get("配图判断") or "") != "匹配"
            )
        ):
            _add_check(
                checks,
                "numbered_image_fallback_needs_review",
                "warning",
                "warning",
                shot_no,
                "图片来自项目编号回退，不是源表该行嵌图，也没有真实视觉读取确认",
                "使用视觉模型或人工复核确认图像内容后再转为高置信绑定",
            )
        if "未读取" in str(detail.get("视觉读取状态") or ""):
            _add_check(
                checks,
                "visual_content_not_read",
                "warning",
                "warning",
                shot_no,
                "当前仅有来源/尺寸证据，没有真实图像内容 caption 或视觉标签",
                "接入视觉模型生成 caption/标签，并用标签参与配图评分",
            )
        if judgement == "不匹配":
            _add_check(
                checks,
                "visual_pairing_mismatch",
                "warning",
                "warning",
                shot_no,
                f"视觉分析判断配图不匹配，匹配分={detail.get('视觉匹配分', '')}",
                "替换图片、调整镜头文案，或人工确认后回写审核结果",
            )
        elif judgement == "弱匹配":
            _add_check(
                checks,
                "visual_pairing_weak_match",
                "warning",
                "warning",
                shot_no,
                f"视觉分析判断配图弱匹配，匹配分={detail.get('视觉匹配分', '')}",
                "人工复核该镜头是否可接受，或补充更准确素材",
            )
    for uses in visual_uses.values():
        slots = {slot for slot, _ in uses}
        if len(uses) < 2 or slots == {3, 10}:
            continue
        _add_check(
            checks,
            "duplicate_visual_asset",
            "warning",
            "warning",
            ",".join(shot for _, shot in uses),
            f"非回扣槽位重复使用同一图片: slots={sorted(slots)}",
            "保留职责更难替代的槽位图片，为其他槽位重新检索独立素材",
        )
    return checks


def audit_candidate_visual_coverage(analysis: dict, candidate_paths: list[Path]) -> list[dict]:
    if not candidate_paths:
        return []
    unread = [path for path in candidate_paths if not _analysis_for_path(path, analysis)]
    if not unread:
        return []
    read_count = len(candidate_paths) - len(unread)
    sample = "、".join(path.name for path in unread[:8])
    return [{
        "检查项": "visual_candidate_pool_partially_unread",
        "级别": "warning",
        "状态": "warning",
        "关联镜号": "",
        "详情": f"项目图片池视觉标注覆盖 {read_count}/{len(candidate_paths)}；未标注候选不会参与视觉重配。示例未标注: {sample}",
        "修复建议": "补齐候选图片 caption/标签，或接入视觉模型批量生成视觉分析 JSON",
        "audit_domain": "media_readiness",
    }]


def _visual_asset_from_value(value: str, role: str, script_row: dict, detail: dict) -> dict:
    image_path = _first_existing_image_path(value)
    source_file, source_sheet, source_row = _source_trace(detail)
    width, height, size_bytes, quality = _image_quality(image_path)
    anchor = _trace_anchor(source_file, source_sheet, source_row) or ("" if image_path else value)
    binding_method, binding_note = _binding_evidence(detail, image_path)
    asset_id = _asset_id(source_file, source_sheet, source_row, role, str(image_path or value))
    return {
        "visual_asset_id": asset_id,
        "image_id": "",
        "slot_index": detail.get("slot_index", ""),
        "逻辑槽位": detail.get("逻辑槽位", ""),
        "镜号": script_row.get("镜号") or detail.get("模板镜号", ""),
        "图片角色": role,
        "来源文件": source_file,
        "来源 sheet": source_sheet,
        "来源行": source_row,
        "原图路径": str(image_path or ""),
        "原图宽": width,
        "原图高": height,
        "文件大小(bytes)": size_bytes,
        "绑定方式": binding_method,
        "视觉读取状态": "未读取",
        "配图置信度": _pairing_confidence(binding_method),
        "视觉内容摘要": "",
        "视觉标签": "",
        "ocr_text": "",
        "brand_names": "",
        "negative_flags": "",
        "视觉匹配分": "",
        "命中视觉要素": "",
        "缺失视觉要素": "",
        "冲突视觉要素": "",
        "配图判断": "",
        "视觉适配模式": "",
        "视觉适配查询": "",
        "质量指标": quality,
        "可回查锚点": anchor,
        "匹配理由": detail.get("匹配理由", ""),
        "绑定说明": binding_note,
        "审核结果": "",
        "修改意见": "",
        "预览图": "",
    }


def _visual_asset_from_candidate(path: Path, script_row: dict, detail: dict, candidate: dict) -> dict:
    width, height, size_bytes, quality = _image_quality(path)
    asset = {
        "visual_asset_id": _asset_id("项目图片池", "视觉重配", str(detail.get("slot_index") or ""), "画面示意", str(path)),
        "image_id": candidate.get("image_id", ""),
        "asset_role": candidate.get("asset_role", ""),
        "资产角色": candidate.get("asset_role", ""),
        "slot_index": detail.get("slot_index", ""),
        "逻辑槽位": detail.get("逻辑槽位", ""),
        "镜号": script_row.get("镜号") or detail.get("模板镜号", ""),
        "图片角色": "画面示意",
        "来源文件": candidate.get("来源文件") or "项目图片池",
        "来源 sheet": candidate.get("来源 sheet") or "视觉重配",
        "来源行": candidate.get("来源行") or "",
        "原图路径": str(path),
        "原图宽": width,
        "原图高": height,
        "文件大小(bytes)": size_bytes,
        "绑定方式": "视觉重配",
        "视觉读取状态": candidate["source"],
        "配图置信度": candidate["confidence"],
        "视觉内容摘要": candidate["summary"],
        "视觉标签": "；".join(candidate["tags"]),
        "ocr_text": candidate.get("ocr_text", ""),
        "brand_names": "；".join(str(value) for value in candidate.get("brand_names") or []),
        "negative_flags": "；".join(str(value) for value in candidate.get("negative_flags") or []),
        "视觉匹配分": candidate["score"],
        "命中视觉要素": "；".join(candidate["hits"]),
        "缺失视觉要素": "；".join(candidate["missing"]),
        "冲突视觉要素": "；".join(candidate["conflicts"]),
        "配图判断": candidate["judgement"],
        "视觉适配模式": candidate.get("match_mode", "primary"),
        "视觉适配查询": candidate.get("matched_query", ""),
        "质量指标": quality,
        "可回查锚点": str(path),
        "匹配理由": detail.get("匹配理由", ""),
        "绑定说明": candidate.get("match_reason") or "原配图视觉分析不匹配，已从项目图片池选择更高分候选",
        "审核结果": "",
        "修改意见": "",
        "预览图": "",
    }
    return asset


def _attach_asset_summary(detail: dict, assets: list[dict]) -> None:
    detail["视觉资产ID"] = "；".join(asset["visual_asset_id"] for asset in assets)
    detail["image_id"] = "；".join(asset.get("image_id", "") for asset in assets if asset.get("image_id"))
    detail["原图路径"] = "；".join(asset["原图路径"] for asset in assets if asset["原图路径"])
    detail["图片绑定方式"] = "；".join(dict.fromkeys(asset["绑定方式"] for asset in assets))
    detail["图片绑定说明"] = "；".join(dict.fromkeys(asset["绑定说明"] for asset in assets))
    detail["视觉质量指标"] = "；".join(dict.fromkeys(asset["质量指标"] for asset in assets))
    detail["视觉读取状态"] = "；".join(dict.fromkeys(asset["视觉读取状态"] for asset in assets))
    detail["配图置信度"] = "；".join(dict.fromkeys(asset["配图置信度"] for asset in assets))
    detail["视觉内容摘要"] = "；".join(dict.fromkeys(asset.get("视觉内容摘要", "") for asset in assets if asset.get("视觉内容摘要")))
    detail["视觉标签"] = "；".join(dict.fromkeys(asset.get("视觉标签", "") for asset in assets if asset.get("视觉标签")))
    detail["视觉匹配分"] = "；".join(str(asset.get("视觉匹配分", "")) for asset in assets if asset.get("视觉匹配分") != "")
    detail["配图判断"] = "；".join(dict.fromkeys(asset.get("配图判断", "") for asset in assets if asset.get("配图判断")))
    detail["视觉适配模式"] = "；".join(dict.fromkeys(asset.get("视觉适配模式", "") for asset in assets if asset.get("视觉适配模式")))
    detail["视觉适配查询"] = "；".join(dict.fromkeys(asset.get("视觉适配查询", "") for asset in assets if asset.get("视觉适配查询")))


def _source_trace(detail: dict) -> tuple[str, str, str]:
    output_sheet = str(detail.get("输出图片来源") or "")
    replacement_sheet = str(detail.get("替换来源 sheet") or "")
    if replacement_sheet and output_sheet == replacement_sheet:
        return (
            str(detail.get("替换来源文件") or ""),
            replacement_sheet,
            str(detail.get("替换来源行") or ""),
        )
    if replacement_sheet and not output_sheet and str(detail.get("替换方式") or "") in {"REPLACE", "ADAPT"}:
        return (
            str(detail.get("替换来源文件") or ""),
            replacement_sheet,
            str(detail.get("替换来源行") or ""),
        )
    return (
        str(detail.get("模板来源文件") or ""),
        str(detail.get("模板来源 sheet") or ""),
        str(detail.get("模板来源行") or ""),
    )


def _binding_evidence(detail: dict, image_path: Path | None) -> tuple[str, str]:
    mode = str(detail.get("替换方式") or "")
    output_sheet = str(detail.get("输出图片来源") or "")
    replacement_sheet = str(detail.get("替换来源 sheet") or "")
    evidence_type = str(detail.get("图片证据类型") or "")
    if "项目编号回退" in evidence_type:
        return "编号回退", "源表该行无嵌图，按项目图片编号临时回退，需视觉复核"
    if image_path and mode in {"REPLACE", "ADAPT"} and replacement_sheet and output_sheet == replacement_sheet:
        return "行绑定", "使用抽调行对应图片，保持创意库来源与视觉素材一致"
    if image_path and mode in {"REPLACE", "ADAPT"} and output_sheet and output_sheet != replacement_sheet:
        return "模板回退", "抽调候选无可用图片，回退模板槽位图片"
    if image_path:
        return "行绑定", "使用当前输出行对应图片"
    return "锚点引用", "仅保留源 Excel 图片锚点，尚未抽取为本地原图"


def _pairing_confidence(binding_method: str) -> str:
    if binding_method == "编号回退":
        return "低"
    if binding_method == "锚点引用":
        return "低"
    if binding_method == "模板回退":
        return "中"
    return "中"


def _analysis_for_asset(asset: dict, analysis: dict) -> dict | None:
    candidates = [
        str(asset.get("visual_asset_id") or ""),
        str(asset.get("原图路径") or ""),
        Path(str(asset.get("原图路径") or "")).name,
        str(asset.get("可回查锚点") or ""),
    ]
    for key in candidates:
        if key and key in analysis:
            return dict(analysis[key])
    return None


def _analysis_tags(item: dict) -> list[str]:
    raw_tags = item.get("tags") or item.get("视觉标签") or []
    if isinstance(raw_tags, str):
        tags = re.split(r"[、,，;；\s\n]+", raw_tags)
    else:
        tags = [str(tag) for tag in raw_tags]
    return [tag.strip() for tag in tags if tag and tag.strip()]


def _script_row_for_asset(script_rows: list[dict], asset: dict) -> dict:
    shot_no = str(asset.get("镜号") or "")
    for row in script_rows:
        if str(row.get("镜号") or "") == shot_no:
            return row
    try:
        index = int(asset.get("slot_index") or 0) - 1
    except (TypeError, ValueError):
        index = -1
    if 0 <= index < len(script_rows):
        return script_rows[index]
    return {}


def _physical_product_evidence(summary: str, observations: dict | None) -> str:
    observations = observations or {}
    foreground = [str(observations.get("main_subject") or "")]
    for key in ("foreground_subjects", "subjects"):
        values = observations.get(key) or []
        foreground.extend([values] if isinstance(values, str) else [str(value) for value in values])
    background = observations.get("background_subjects") or []
    if isinstance(background, str):
        background = [background]
    parts = [*foreground, summary]
    parts.extend("背景为" + str(value) for value in background)
    clauses = []
    for part in parts:
        text = re.sub(r'[“「『\"][^”」』\"]*[”」』\"]', "", str(part))
        for clause in re.split(r"[，,。；;！？!?]", text):
            if re.search(r"(?:屏幕|界面|海报|宣传页|广告牌).*(?:显示|展示|呈现|印有|出现)|(?:显示|展示|呈现).*(?:屏幕|界面)|电饭(?:煲|锅)(?:图片|图案|宣传|广告|文字|介绍视频)", clause):
                continue
            clauses.append(clause)
    return "，".join(clauses)


def _score_visual_match(
    description: str,
    summary: str,
    tags: list[str],
    *,
    apply_launch_conflicts: bool = True,
    apply_brand_conflicts: bool = True,
    observations: dict | None = None,
    current_shot_authority: bool = False,
) -> dict:
    required = _required_groups(description)
    if current_shot_authority and not _has_positive_any(description, ("搭建", "施工")):
        # Time-lapse describes capture technique; it does not imply construction.
        required = [(name, terms) for name, terms in required if name != "搭建/施工延时"]
    if current_shot_authority:
        # Pulling back is a camera movement, not evidence of a 360-degree picture.
        required = [(name, terms) for name, terms in required if name != "360/会场全貌"]
    visual_text = " ".join([summary, " ".join(tags)])
    hits = [name for name, terms in required if _has_positive_any(visual_text, terms)]
    missing = [name for name, terms in required if not _has_positive_any(visual_text, terms)]
    if current_shot_authority:
        evidence = " ".join([summary, *(str((observations or {}).get(key) or "") for key in
                              ("main_subject", "action_type", "camera_angle", "shot_type", "scene"))]).replace("_", "-")
        for name, requested, observed in CURRENT_SHOT_REQUIREMENTS:
            if _has_positive_any(description, requested):
                content = evidence
                if name == "电饭煲主体":
                    # Text on a screen, OCR and unordered tags do not establish
                    # that the requested physical product is visible.
                    content = _physical_product_evidence(summary, observations)
                if name == "产品/展品":
                    # Event titles and text about a product do not establish a visible product.
                    content = re.sub(r"(?:新?产品|新品)(?:发布会|发布活动|发布|宣传文字|介绍文字|介绍|理念|宣传画面|宣传视频|宣传片|界面|信息|方案|图片|画面|文字|视频|负责人)", "说明", content)
                    content = re.sub(r"(?:观看|聆听|关注)(?:新?产品|新品)(?:展示|演示|讲解)", "观看活动", content)
                (hits if _has_positive_any(content, observed) else missing).append(name)
        if "备米入锅" in hits and _has_positive_any(description, ("内锅", "入锅", "锅中", "锅里")):
            rice_into_pot = any(
                not re.search(r"(?:未|没有|并未|尚未)[^，。；]{0,20}(?:倒|放|入锅)", clause)
                and re.search(r"(?:大米|生米|米(?!饭))[^，。；]{0,40}(?:倒|放|入)[^，。；]{0,30}(?:内锅|锅内|锅中|锅里)|(?:内锅|锅内|锅中|锅里)[^，。；]{0,30}(?:倒|放)[^，。；]{0,10}(?:大米|生米|米(?!饭))|(?:放米|倒米)入锅", clause)
                for clause in re.split(r"[，。；]", evidence)
            )
            if not rice_into_pot:
                missing.append("备米入锅")
        if "开盖成饭" in hits and re.search(r"米饭[^，。；]{0,12}(?:未熟|未煮熟|夹生)|(?:尚未|没有|并未)[^，。；]{0,8}(?:煮熟|成饭)|(?:生米|未煮熟)", evidence):
            missing.append("开盖成饭")
        if "端饭上桌" in hits and _has_positive_any(description, ("餐桌", "饭桌", "上桌")):
            carried_to_table = any(
                not re.search(r"(?:未|没有|并未|尚未)[^，。；]{0,12}(?:端|拿|放)", clause)
                and re.search(r"(?:端|拿|放)[^，。；]{0,30}(?:餐桌|饭桌|上桌)", clause)
                for clause in re.split(r"[，。；]", evidence)
            )
            if not carried_to_table:
                missing.append("端饭上桌")
        if _has_positive_any(description, ("电饭煲", "电饭锅", "rice cooker")):
            product_terms = ("电饭煲", "电饭锅", "rice cooker")
            requested_foreground, requested_background = _subject_depth_parts(description)
            actual_foreground, actual_background = _subject_depth_parts(
                _physical_product_evidence(summary, observations)
            )
            foreground = " ".join(actual_foreground)
            background = " ".join(actual_background)
            explicitly_background = (
                _has_positive_any(" ".join(requested_background), product_terms)
                and not _has_positive_any(" ".join(requested_foreground), product_terms)
            )
            if not explicitly_background and not _has_positive_any(foreground, product_terms) and _has_positive_any(background, product_terms):
                missing.append("电饭煲主体")
            requested_models = [model for model in re.findall(
                r"(?<![A-Za-z0-9])[A-Za-z]{1,8}-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9])",
                description,
            ) if _has_positive_any(description, (model,))]
            model_evidence = " ".join([
                _physical_product_evidence(summary, observations),
                str((observations or {}).get("ocr_text") or ""),
            ]).casefold()
            for model in dict.fromkeys(requested_models):
                name = f"产品型号 {model}"
                model_visible = re.search(r"(?<![a-z0-9-])" + re.escape(model.casefold()) + r"(?![a-z0-9-])", model_evidence)
                (hits if model_visible and "电饭煲主体" not in missing else missing).append(name)
        if _has_positive_any(description, ("会场全貌", "会场全景", "会场整体空间", "全景舞台", "舞台全景", "整体会场")):
            venue = _has_positive_any(evidence, ("会场", "场馆", "展厅", "舞台", "观众席", "发布会现场"))
            wide = _has_positive_any(evidence, ("全貌", "全景", "整体空间", "wide-shot", "wide shot", "long-shot", "远景")) or str((observations or {}).get('shot_type') or '').casefold() == 'wide'
            local_subject = re.search(r"(?:中央|中心|前景)[^，。；]{0,16}(?:吉祥物|人偶|卡通|展品|产品特写)", summary)
            if local_subject and not re.search(r"会场全貌|会场全景|场馆全景|整体会场|观众席|远处", summary):
                wide = False
            # Wide framing of a crowd or aisle does not establish the whole venue.
            overview = re.search(r"全貌|全景|整体空间|整个(?:会场|场馆|展厅)|整体会场|观众席", summary)
            if not overview and not re.search(r"(?:舞台|主舞台).*(?:台下|展区)|(?:展区|台下).*舞台", summary):
                wide = False
            (hits if venue and wide else missing).append("会场全貌")
        if _has_positive_any(description, ('穿过会场通道', '会场通道', '穿过走廊')):
            (hits if _has_positive_any(evidence, ('通道', '走道', '过道', '走廊', '廊道', 'aisle')) else missing).append('空间通道')
        if _has_positive_any(description, ("360", "全景环视")):
            (hits if _has_positive_any(evidence, ("360", "全景环视", "panoramic")) else missing).append("360全景")
        if _stage_people_required(description):
            (hits if _stage_people_observed(evidence) else missing).append("台上人物与动作")
        hits, missing = list(dict.fromkeys(hits)), list(dict.fromkeys(missing))
        hits = [name for name in hits if name not in missing]
    conflicts = _visual_conflicts(
        description,
        visual_text,
        apply_launch_conflicts=apply_launch_conflicts,
        apply_brand_conflicts=apply_brand_conflicts,
    )
    conflicts.extend(
        f"主体要求“{name}”仅出现在背景"
        for name in _background_only_required_groups(description, summary, observations=observations)
    )
    angle = str((observations or {}).get("camera_angle") or "")
    if _has_any(description, ("航拍", "无人机", "鸟瞰")) and _has_positive_any(angle, ("地面平视", "平视", "仰拍", "仰视", "eye-level", "ground level")):
        conflicts.append("所需航拍与已识别的地面机位不符")
    conflicts = list(dict.fromkeys(conflicts))
    if current_shot_authority and "台上人物与动作" in missing and _has_positive_any(summary, ("吧台", "茶歇", "餐台", "饮品区")):
        conflicts.append("需要台上人物与动作，候选呈现吧台或茶歇交流")
    score = max(0, min(100, 100 - 28 * len(missing) - 35 * len(conflicts)))
    has_requirements = bool(required) or (current_shot_authority and bool(hits or missing))
    if has_requirements and not hits:
        score = min(score, 45)
    if conflicts:
        score = min(score, 45)
    if not has_requirements:
        score = 60
    if score >= 80:
        judgement = "匹配"
        confidence = "高"
    elif score >= 55:
        judgement = "弱匹配"
        confidence = "中"
    else:
        judgement = "不匹配"
        confidence = "低"
    return {
        "score": score,
        "hits": hits,
        "missing": missing,
        "conflicts": conflicts,
        "judgement": judgement,
        "confidence": confidence,
        "requirements": [{"requirement": name, "status": "matched" if name in hits else "evidence_missing",
            "evidence": summary if name in hits else "现有图片描述与结构化分析未提供该依据"}
            for name in dict.fromkeys([*hits, *missing])] if current_shot_authority else [],
        "reference_notes": ["静态参考图不证明运镜执行效果"] if current_shot_authority and
            _has_positive_any(description, ("拉远", "推进", "前推", "环绕", "缓推", "摇镜")) else [],
    }


def _stage_people_required(text: str) -> bool:
    return bool(re.search(r"(?:台上|舞台(?:上|中央))[^，。；]{0,18}(?:人物|嘉宾|主持|演讲|发言|动作|完成)", text)
        or re.search(r"(?:人物|嘉宾|主持|演讲者)[^，。；]{0,18}(?:台上|舞台)", text))


def _stage_people_observed(text: str) -> bool:
    people = r"(?:人物|嘉宾|代表|来宾|主持|演讲者|演讲嘉宾|男性|女性|男士|女士|男子|女子|歌手|艺人|表演者|speaker|performer)"
    stage = r"(?:台上|舞台|讲台|stage)"
    return bool(re.search(stage + r"[^，。；]{0,28}" + people, text, re.I)
        or re.search(people + r"[^，。；]{0,28}" + stage, text, re.I))


def _background_only_required_groups(description: str, summary: str, *, observations: dict | None = None) -> list[str]:
    """Return subject requirements that the caption explicitly places in depth.

    Existing vision captions often say exactly which element is foreground and
    which is only visible in a blurred background. This conservative check uses
    that existing text and never infers salience from unordered tags.
    """
    observations = observations or {}
    foreground_parts = [str(observations.get("main_subject") or ""), *(observations.get("foreground_subjects") or [])]
    background_parts = list(observations.get("background_subjects") or [])
    if not (any(foreground_parts) and background_parts):
        foreground_parts, background_parts = _subject_depth_parts(summary)
    if not background_parts:
        return []
    foreground = "；".join(foreground_parts)
    background = "；".join(background_parts)
    required = _required_groups(description)
    requested_foreground, requested_background = _subject_depth_parts(description)
    foreground_query = "；".join(requested_foreground)
    background_query = "；".join(requested_background)
    composition_groups = [*required, *SUBJECT_SALIENCE_CONTEXT_GROUPS]
    conflicts = []
    for name, terms in required:
        if name not in SUBJECT_SALIENCE_GROUPS:
            continue
        if _has_positive_any(foreground, terms):
            continue
        if _has_positive_any(background_query, terms) and not _has_positive_any(foreground_query, terms):
            continue
        if _has_positive_any(background, terms):
            if any(
                other_name != name
                and (other_name in SUBJECT_SALIENCE_GROUPS or (other_name, other_terms) in SUBJECT_SALIENCE_CONTEXT_GROUPS)
                and _has_positive_any(foreground_query, other_terms)
                and _has_positive_any(foreground, other_terms)
                for other_name, other_terms in composition_groups
            ):
                continue
            conflicts.append(name)
    return conflicts


def _subject_depth_parts(text: str) -> tuple[list[str], list[str]]:
    foreground, background = [], []
    in_background = False
    for clause in re.split(r"[，,。；;！？!?]", text):
        postposed_background = re.search(r"(?:作为|为|是)(?:虚化的|模糊的)?背景", clause)
        if postposed_background:
            background.append(clause[:postposed_background.end()])
            foreground.append(clause[postposed_background.end():])
            in_background = False
            continue
        positions = [clause.find(marker) for marker in BACKGROUND_DEPTH_MARKERS if marker in clause]
        if not positions:
            if any(marker in clause for marker in ("主体", "前景", "画面中央", "画面中心")):
                in_background = False
            (background if in_background else foreground).append(clause)
            continue
        position = min(positions)
        foreground.append(clause[:position])
        background.append(clause[position:])
        in_background = True
    return foreground, background


def _strict_primary_visual_missing(
    description: str,
    summary: str,
    tags: list[str],
) -> list[str]:
    visual_text = " ".join([summary, " ".join(tags)])
    return [
        name
        for name, terms in _required_groups(description)
        if name in STRICT_PRIMARY_VISUAL_GROUPS
        and not _has_positive_any(visual_text, terms)
    ]


def _required_groups(description: str) -> list[tuple[str, tuple[str, ...]]]:
    return [
        (name, terms)
        for name, terms in VISUAL_REQUIREMENT_GROUPS
        if _has_any(description, terms)
    ]


def _visual_conflicts(
    description: str,
    visual_text: str,
    *,
    apply_launch_conflicts: bool = True,
    apply_brand_conflicts: bool = True,
) -> list[str]:
    conflicts = []
    construction_in_progress = _has_any(visual_text, ("搭建", "施工")) and not _has_any(
        visual_text,
        ("完成搭建", "搭建完成", "施工完成", "已经完工", "已完工", "成型"),
    )
    if _has_any(description, ("主会场空镜", "舞台空镜", "光束")) and construction_in_progress:
        conflicts.append("需要成型后的主会场空镜但画面仍在搭建施工")
    if _has_any(description, ("logo", "LOGO", "品牌logo")) and _has_any(visual_text, ("外场", "建筑", "门头")):
        conflicts.append("需要 logo 动效但画面是外场建筑")
    if _has_any(description, ("logo", "LOGO", "品牌logo", "品牌标识")) and _has_any(visual_text, ("虚焦", "不够清晰", "低清晰标识")):
        conflicts.append("需要清晰 logo/品牌标识但画面虚焦或标识不清晰")
    if (
        apply_launch_conflicts
        and _has_any(description, ("产品互动", "体验"))
        and _has_any(visual_text, ("车机", "汽车"))
    ):
        conflicts.append("需要家电互动但画面是车机/汽车")
    if (
        apply_launch_conflicts
        and _has_any(description, ("主发布会现场", "大屏"))
        and _has_any(visual_text, ("控制屏", "车机", "触控屏"))
    ):
        conflicts.append("需要发布会大屏但画面是设备控制屏")
    exterior_visual = _has_any(
        visual_text,
        ("外场", "建筑外观", "场馆外观", "门头", "主会场外观", "外立面", "户外建筑"),
    ) or (
        "建筑" in visual_text
        and _has_any(visual_text, ("户外", "外景", "外部", "入口"))
    )
    explicit_exterior = _has_any(description, ("户外", "室外", "外场", "外立面", "广场", "建筑外墙"))
    explicit_interior = _has_any(description, ("室内", "内场"))
    if (
        _has_any(description, ("主会场空镜", "大屏", "光束", "舞台"))
        and exterior_visual
        and (explicit_interior or not explicit_exterior)
    ):
        conflicts.append("需要内场舞台/大屏但画面是外场建筑")
    if (
        apply_launch_conflicts
        and _has_any(description, ("主会场", "发布会现场", "舞台", "大屏"))
        and _has_any(visual_text, ("汽车", "车机", "汽车展区"))
    ):
        conflicts.append("需要发布会主会场/舞台但画面是汽车展区")
    if apply_brand_conflicts:
        brand_conflicts = conflicting_brands(description, visual_text)
        if brand_conflicts:
            conflicts.append(f"需要当前项目品牌素材但画面出现其他品牌: {', '.join(brand_conflicts)}")
    if _has_any(description, ("大合影", "全体合影")) and _has_any(visual_text, ("三人", "单人", "中景", "证书")):
        conflicts.append("需要大合影但画面是少量人物或证书中景")
    if _has_any(description, ("大合影", "仪式启动", "启动仪式")):
        ordinary_display = _has_any(visual_text, ("展区", "打卡")) or (
            "装置" in visual_text
            and not _has_any(visual_text, ("启动", "共同触碰", "发光球", "启动球"))
        )
        if ordinary_display:
            conflicts.append("需要启动/大合影但画面是展区打卡")
    if (
        _has_any(description, ("END", "主视觉", "KV", "落版"))
        and _has_any(visual_text, ("颁奖", "证书", "三位嘉宾", "授牌"))
        and not _has_any(visual_text, ("主视觉", "KV", "宣传图", "品牌落版", "主题落版"))
    ):
        conflicts.append("需要片尾主视觉/KV但画面是颁奖或证书舞台")
    if _has_any(description, ("嘉宾", "观众", "顾客", "机器人", "咖啡", "茶歇", "围观")) and _has_any(visual_text, ("表情包", "卡通", "飞吻", "动态图形")):
        conflicts.append("需要真实现场互动但画面是卡通/表情包动效")
    return conflicts


def _has_any(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    return any(
        term and term in (text.replace("演唱会", "") if term == "演唱" else text)
        for term in terms
    )


def _has_positive_any(text: str, terms: tuple[str, ...] | list[str]) -> bool:
    for term in terms:
        # An event type does not prove that singing is visible in this shot.
        evidence = text.replace("演唱会", "") if term == "演唱" else text
        if not term or term not in evidence:
            continue
        if _is_negated(evidence, term):
            continue
        return True
    return False


def _is_negated(text: str, term: str) -> bool:
    for match in re.finditer(re.escape(term), text):
        prefix = text[max(0, match.start() - 8):match.start()]
        if any(marker in prefix for marker in ("不是", "非", "没有", "无", "未见", "未出现", "看不到")):
            return True
        suffix = text[match.end():match.end() + 8]
        if any(marker in suffix for marker in ("不明确", "不清晰", "不明显", "不突出")):
            return True
    return False


def _apply_asset_analysis_to_detail(detail: dict, asset: dict) -> None:
    detail["视觉读取状态"] = asset.get("视觉读取状态", "")
    detail["配图置信度"] = asset.get("配图置信度", "")
    detail["视觉内容摘要"] = asset.get("视觉内容摘要", "")
    detail["视觉标签"] = asset.get("视觉标签", "")
    detail["视觉匹配分"] = asset.get("视觉匹配分", "")
    detail["配图判断"] = asset.get("配图判断", "")
    judgement = str(asset.get("配图判断") or "")
    if judgement:
        note = f"视觉分析判断: {judgement}"
        existing = str(detail.get("图片绑定说明") or "")
        detail["图片绑定说明"] = "；".join(part for part in [existing, note] if part)


def _best_visual_candidate(
    script_row: dict,
    analysis: dict,
    candidate_paths: list[Path],
    used_paths: set[str],
    detail: dict | None = None,
    excluded_visuals: list[str] | None = None,
) -> dict | None:
    description = _visual_match_context(script_row, detail)
    best = None
    for raw_path in candidate_paths:
        path = Path(raw_path)
        if str(path) in set(excluded_visuals or []) or path.name in set(excluded_visuals or []):
            continue
        used_penalty = 10 if str(path) in used_paths else 0
        item = _analysis_for_path(path, analysis)
        if not item:
            continue
        _, _, _, quality = _image_quality(path)
        if "低清" in quality:
            continue
        tags = _analysis_tags(item)
        summary = str(item.get("summary") or item.get("caption") or "")
        match = _score_visual_match(description, summary, tags, observations=item)
        selection_score = match["score"] + _specific_match_bonus(description, summary, tags) - used_penalty
        candidate = {
            "path": path,
            "summary": summary,
            "tags": tags,
            "ocr_text": str(item.get("ocr_text") or ""),
            "brand_names": list(item.get("brand_names") or []),
            "negative_flags": list(item.get("negative_flags") or []),
            "source": str(item.get("source") or "外部分析"),
            "selection_score": selection_score,
            **match,
        }
        if best is None or candidate["selection_score"] > best["selection_score"]:
            best = candidate
    return best


def _best_manifest_candidate(
    script_row: dict,
    detail: dict | None,
    brief: dict,
    manifest_rows: list[dict] | None,
    analysis_rows: list[dict] | None,
    used_paths: set[str],
    excluded_visuals: list[str] | None = None,
    allow_reuse: bool = True,
    forced_visual: dict | None = None,
) -> dict | None:
    if not manifest_rows or analysis_rows is None:
        return None
    from .image_retrieval import search_images_for_slot

    slot = dict(detail or {})
    slot.update(script_row)
    result = search_images_for_slot(
        slot,
        brief,
        manifest_rows,
        analysis_rows,
        # 用户已经在候选界面看图并明确选定时，必须在完整安全候选池中
        # 重新验证该素材。只查前 8 名会让原本可选的候选因正文轻微
        # 改写而跌出截断窗口，随后被错误清空。
        top_k=len(manifest_rows) if forced_visual else 8,
        excluded_visuals=excluded_visuals,
    )
    requested_id = str((forced_visual or {}).get("image_id") or "").strip()
    requested_path = str((forced_visual or {}).get("path") or "").strip()
    if requested_id or requested_path:
        for candidate in result["candidates"]:
            if requested_id and str(candidate.get("image_id") or "") != requested_id:
                continue
            if requested_path and str(candidate.get("原图路径") or "") != requested_path:
                continue
            if int(candidate.get("score") or 0) < 55:
                break
            path = Path(str(candidate.get("原图路径") or ""))
            if not path.is_file():
                break
            item = dict(candidate)
            item["path"] = path
            item["selection_score"] = item["score"]
            item["match_reason"] = f"人工从安全候选中选定；{item.get('match_reason', '')}"
            return item
        return {"score": 0, "gap_reason": "人工选定图片已变化、不可用或未通过品牌/镜头功能安全门"}
    fallback = None
    for candidate in result["candidates"]:
        if fallback is None:
            fallback = candidate
        if str(candidate.get("原图路径") or "") in used_paths:
            continue
        if int(candidate.get("score") or 0) < 80:
            continue
        path = Path(str(candidate.get("原图路径") or ""))
        if not path.exists():
            continue
        item = dict(candidate)
        item["path"] = path
        item["selection_score"] = item["score"]
        return item
    if fallback and allow_reuse:
        path = Path(str(fallback.get("原图路径") or ""))
        if path.exists():
            item = dict(fallback)
            item["path"] = path
            item["selection_score"] = item["score"]
            item["match_reason"] = f"{item.get('match_reason', '')}；未找到未占用候选，复用最高分素材"
            return item
    if fallback and not allow_reuse:
        return {"score": 0, "gap_reason": "高分候选已被其他非回扣槽位占用"}
    if result["gap_reason"]:
        return {"score": 0, "gap_reason": result["gap_reason"]}
    return None


def _visual_match_context(script_row: dict, detail: dict | None = None) -> str:
    parts = [str(script_row.get("画面描述") or "")]
    if detail:
        parts.append(str(detail.get("项目名称") or ""))
    return " ".join(part for part in parts if part)


def _specific_match_bonus(description: str, summary: str, tags: list[str]) -> int:
    visual_text = " ".join([summary, " ".join(tags)])
    terms = set()
    for term in re.split(r"[，。,、：:；;\s\n（）()\"“”]+", description):
        term = term.strip()
        if len(term) >= 2:
            terms.add(term)
    for _, group_terms in VISUAL_REQUIREMENT_GROUPS:
        for term in group_terms:
            if term and term in description:
                terms.add(term)
    for tag in tags:
        for term in re.split(r"[，。,、：:；;\s\n（）()\"“”/]+", str(tag)):
            term = term.strip()
            if len(term) >= 2 and term in description:
                terms.add(term)
    bonus = 0
    for term in terms:
        if term in visual_text:
            bonus += 3
    return min(bonus, 18)


def _analysis_for_path(path: Path, analysis: dict) -> dict | None:
    for key in (str(path), path.name, path.stem):
        if key in analysis:
            return dict(analysis[key])
    return None


def _append_detail_note(detail: dict, note: str) -> None:
    existing = str(detail.get("图片绑定说明") or "")
    detail["图片绑定说明"] = "；".join(part for part in [existing, note] if part)


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _image_quality(path: Path | None) -> tuple[int | str, int | str, int | str, str]:
    if not path:
        return "", "", "", "锚点待抽取"
    try:
        with PILImage.open(path) as image:
            width, height = image.size
        size_bytes = path.stat().st_size
    except Exception:
        return "", "", "", "无法读取尺寸"
    quality = "高清原图" if width >= 1280 and height >= 720 else "低清原图"
    return width, height, size_bytes, quality


def _first_existing_image_path(value: str) -> Path | None:
    for part in str(value or "").split(";"):
        text = part.strip()
        if not text:
            continue
        path = Path(text)
        if path.exists() and path.suffix.lower() in IMAGE_EXTENSIONS:
            return path
    return None


def _trace_anchor(source_file: str, source_sheet: str, source_row: str) -> str:
    if source_file and source_sheet and source_row:
        return f"{source_file}#{source_sheet}!R{source_row}"
    return ""


def _asset_id(source_file: str, source_sheet: str, source_row: str, role: str, value: str) -> str:
    raw = "|".join([source_file, source_sheet, source_row, role, value])
    return "va_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _add_check(
    checks: list[dict],
    item: str,
    level: str,
    status: str,
    shot_no: str,
    detail: str,
    suggestion: str,
) -> None:
    checks.append({
        "检查项": item,
        "级别": level,
        "状态": status,
        "关联镜号": shot_no,
        "详情": detail,
        "修复建议": suggestion,
        "audit_domain": "media_readiness",
    })
