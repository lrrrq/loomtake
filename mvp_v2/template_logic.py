from __future__ import annotations

from dataclasses import dataclass

from .models import SourceShot
from .visual_function import infer_visual_functions


@dataclass(frozen=True)
class SlotLogic:
    role: str
    intent: str
    preferred_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...] = ()
    allowed_materials: tuple[str, ...] = ()
    blocked_materials: tuple[str, ...] = ()
    recommended_shot_sizes: tuple[str, ...] = ()
    recommended_camera_moves: tuple[str, ...] = ()
    recommended_sounds: tuple[str, ...] = ()
    rhythm: str = ""
    required_visual_evidence: tuple[str, ...] = ()
    repeat_policy: str = ""
    visual_adapt_queries: tuple[str, ...] = ()


LAUNCH_SLOT_LOGIC: dict[int, SlotLogic] = {
    1: SlotLogic(
        role="品牌开场识别",
        intent="片头先建立品牌 logo 或主题视觉记忆，作为后续镜头的视觉锚点。",
        preferred_terms=("品牌", "logo", "LOGO", "主题文字", "穿梭"),
        forbidden_terms=("主会场外观", "地标空镜", "主持人", "表演"),
        allowed_materials=("品牌 logo", "主题字样", "主视觉 KV", "科技视觉转场"),
        blocked_materials=("主持人报幕", "节目表演", "普通外场空镜"),
        recommended_shot_sizes=("特写", "全景"),
        recommended_camera_moves=("固定", "穿梭", "缓推"),
        recommended_sounds=("音乐起", "科技脉冲"),
        rhythm="片头 1-2 秒建立品牌识别，不能拖成活动流程说明。",
        required_visual_evidence=("清晰品牌 logo", "主题文字或主视觉"),
        repeat_policy="可与第 10 镜做品牌视觉回扣，其他槽位重复降分。",
        visual_adapt_queries=("当前项目品牌 logo 品牌标识 主视觉",),
    ),
    2: SlotLogic(
        role="场地成型",
        intent="用搭建延时或场馆变化补足空间信息，让片头从品牌视觉落到真实场景。",
        preferred_terms=("搭建", "延时", "场馆", "会场", "外观"),
        forbidden_terms=("主持人", "演讲者", "合影"),
        allowed_materials=("搭建延时", "场馆成型", "施工收尾", "外场入场空间"),
        blocked_materials=("演讲台正面", "嘉宾合影", "纯品牌 KV"),
        recommended_shot_sizes=("全景", "远景"),
        recommended_camera_moves=("延时捕捉", "航拍推进", "固定"),
        recommended_sounds=("施工声", "现场环境声"),
        rhythm="承接片头，快速交代空间从无到有。",
        required_visual_evidence=("搭建或场馆变化", "发布会现场空间"),
        repeat_policy="不建议重复；缺真实搭建素材时可降级为外场建立镜头并明确标记 ADAPT。",
        visual_adapt_queries=("发布会外场入口 品牌立体标识 蓝色主视觉 建立活动场地",),
    ),
    3: SlotLogic(
        role="主流程视觉转场",
        intent="从品牌视觉切入主发布段落，承担第一次强视觉转场。",
        preferred_terms=("品牌", "logo", "穿越机", "贴屏", "飞行", "视觉冲击"),
        forbidden_terms=(
            "主持人", "演讲者", "演讲", "茶歇", "签到", "表演",
            "颁发", "颁奖", "授牌", "启动", "合影",
        ),
        allowed_materials=("品牌快闪", "穿越机贴屏", "慢门抽帧", "大屏视觉转场"),
        blocked_materials=("签到墙", "茶歇", "主持人口播"),
        recommended_shot_sizes=("广角", "全景"),
        recommended_camera_moves=("穿越机俯冲", "快速推进", "变速飞行"),
        recommended_sounds=("飞行呼啸", "重鼓冲击"),
        rhythm="第一处强节奏冲击，帮助进入核心发布段落。",
        required_visual_evidence=("品牌视觉", "屏幕或空间穿梭动势"),
        repeat_policy="可与第 10 镜复用同一强转场作为剪辑回扣。",
    ),
    4: SlotLogic(
        role="主会场定场",
        intent="用空镜和光束建立舞台空间，给后面的演讲、表演、仪式镜头提供场景承接。",
        preferred_terms=("主会场", "空镜", "光束", "舞台", "中心"),
        forbidden_terms=("主持人", "报幕", "演讲者", "表演", "茶歇", "观众", "手机", "录屏"),
        allowed_materials=("主会场空镜", "舞台光束", "大屏定场", "装置空镜"),
        blocked_materials=("主持人", "观众反应", "外场建筑", "茶歇"),
        recommended_shot_sizes=("全景", "远景"),
        recommended_camera_moves=("缓缓前推", "固定", "横移"),
        recommended_sounds=("环境声", "音乐铺底"),
        rhythm="从动效转入稳定空间，让核心内容有落点。",
        required_visual_evidence=("内场舞台", "大屏或光束中心"),
        repeat_policy="同类空镜可少量复用，但不能替代演讲/仪式槽位。",
    ),
    5: SlotLogic(
        role="签到参与",
        intent="用签到动作补入人物进入现场的信息，让剪辑从空镜切到有人参与。",
        preferred_terms=("签到", "顾客", "互动", "入场"),
        forbidden_terms=("演讲", "颁发", "落版"),
        allowed_materials=("签到墙", "入场打卡", "顾客互动", "工作人员引导", "智能设备互动"),
        blocked_materials=("演讲者", "颁奖仪式", "品牌落版"),
        recommended_shot_sizes=("中景", "全景"),
        recommended_camera_moves=("跟随人物", "固定机位", "轻推"),
        recommended_sounds=("现场施工声", "人声环境"),
        rhythm="把无人空间切到有人参与，节奏要短。",
        required_visual_evidence=("签到动作", "参会者或顾客"),
        repeat_policy="签到素材只服务入场段，后续体验槽位重复降分。",
        visual_adapt_queries=(
            "参会者俯身在桌前填写资料 周围观众查看文件 入场登记",
            "智能设备展示 机器人操作 爆米花机",
        ),
    ),
    6: SlotLogic(
        role="嘉宾/人物背书",
        intent="用高管或嘉宾合影补充人物层级和现场关系，不是活动流程节点本身。",
        preferred_terms=("高管", "嘉宾", "合影", "打卡", "快门"),
        forbidden_terms=("茶歇", "演讲者", "空镜", "汽车", "车内", "座舱"),
        allowed_materials=("高管合影", "嘉宾打卡", "品牌墙人物", "快门闪光"),
        blocked_materials=("茶歇服务", "主会场空镜", "演讲正面"),
        recommended_shot_sizes=("中景", "全景"),
        recommended_camera_moves=("固定", "轻推", "跟随人物"),
        recommended_sounds=("快门声", "现场人声"),
        rhythm="人物背书镜头，短促补充现场关系。",
        required_visual_evidence=("人物合影或打卡", "发布会品牌环境"),
        repeat_policy="可与签到段相邻，但不能多次复用同一打卡素材。",
        visual_adapt_queries=(
            "嘉宾合影 品牌背景板 发布会人物背书",
            "嘉宾交流 摄影记录 品牌装置 权威感",
        ),
    ),
    7: SlotLogic(
        role="主题空间体验",
        intent="让主题不只停留在大屏文字，而是通过人和空间互动形成可剪的体验镜头。",
        preferred_terms=("客户", "顾客", "主题空间", "互动", "体验"),
        forbidden_terms=("签到", "合影", "茶歇", "颁发", "落版", "外观"),
        allowed_materials=("主题空间体验", "产品互动", "客户体验", "装置互动"),
        blocked_materials=("签到墙", "合影摆拍", "茶歇餐台", "外场门头"),
        recommended_shot_sizes=("全景", "中景"),
        recommended_camera_moves=("固定机位", "跟随人物", "缓推"),
        recommended_sounds=("互动人声", "机械声"),
        rhythm="体验段要有动作，不只是空镜展示。",
        required_visual_evidence=("人与主题空间互动", "产品或装置体验动作"),
        repeat_policy="可与第 9 镜人流热度形成承接，不复用签到/合影素材。",
        visual_adapt_queries=(
            "产品体验 试坐 使用产品 操作设备 装置互动 家居空间",
        ),
    ),
    8: SlotLogic(
        role="服务社交氛围",
        intent="茶歇与服务细节用于调节剪辑密度，补充现场温度。",
        preferred_terms=("茶歇", "餐台", "饮品", "服务", "宾客", "互动"),
        forbidden_terms=("演讲", "颁发", "落版"),
        allowed_materials=("茶歇", "服务台", "饮品互动", "宾客交流"),
        blocked_materials=("演讲正面", "颁发仪式", "片尾落版"),
        recommended_shot_sizes=("全景", "中景"),
        recommended_camera_moves=("倾角前推", "固定", "跟随"),
        recommended_sounds=("机械声", "人声环境"),
        rhythm="情绪降一点，作为核心发布前的密度调节。",
        required_visual_evidence=("服务或茶歇场景", "现场宾客"),
        repeat_policy="服务氛围只用一次，重复会稀释发布会重点。",
        visual_adapt_queries=(
            "机器人咖啡亭 递送咖啡 参会者互动 围观",
            "现场餐桌 菜品 餐具 嘉宾交流 服务社交",
        ),
    ),
    9: SlotLogic(
        role="人流热度",
        intent="用通道和人群密度补强现场热度，让片子不只有舞台正面镜头。",
        preferred_terms=("人群", "通道", "参观", "熙熙攘攘", "人流"),
        forbidden_terms=("空镜", "落版", "颁发"),
        allowed_materials=("通道人流", "观众参观", "人群热度", "现场互动"),
        blocked_materials=("空场舞台", "品牌落版", "颁奖节点"),
        recommended_shot_sizes=("全景", "中景"),
        recommended_camera_moves=("正推", "横移", "跟随"),
        recommended_sounds=("重鼓冲击音", "人声环境"),
        rhythm="为核心发布前做热度铺垫，画面要有人群密度。",
        required_visual_evidence=("人群", "通道或展区动线"),
        repeat_policy="可与第 7 镜承接，但不能用纯空镜替代。",
    ),
    10: SlotLogic(
        role="二次视觉转场",
        intent="从互动素材重新切回主发布内容。它与第 3 镜相似，但剪辑位置不同，承担二次转场。",
        preferred_terms=("品牌", "logo", "穿越机", "贴屏", "飞行", "视觉冲击"),
        forbidden_terms=(
            "主持人", "演讲者", "演讲", "茶歇", "签到", "表演",
            "颁发", "颁奖", "授牌", "启动", "合影",
        ),
        allowed_materials=("品牌快闪", "穿越机贴屏", "慢门抽帧", "大屏视觉转场"),
        blocked_materials=("签到墙", "茶歇", "主持人口播"),
        recommended_shot_sizes=("广角", "全景"),
        recommended_camera_moves=("穿越机俯冲", "快速推进", "变速飞行"),
        recommended_sounds=("飞行呼啸", "重鼓冲击"),
        rhythm="二次强转场，回扣第 3 镜并切回主发布。",
        required_visual_evidence=("品牌视觉", "屏幕或空间穿梭动势"),
        repeat_policy="允许与第 3 镜复用强转场，作为片中回扣。",
    ),
    11: SlotLogic(
        role="主发布画面建立",
        intent="用满场观众与大屏主题建立主发布段落的画面基准。",
        preferred_terms=("发布会现场", "座无虚席", "大屏", "主题", "主会场"),
        forbidden_terms=("茶歇", "合影墙", "外观", "搭建", "施工", "展位", "展区"),
        allowed_materials=("主会场全景", "大屏主题", "观众满场", "发布现场"),
        blocked_materials=("茶歇", "合影墙", "外场建筑"),
        recommended_shot_sizes=("全景", "中景"),
        recommended_camera_moves=("缓推", "固定", "综合运动"),
        recommended_sounds=("环境声", "音乐铺底"),
        rhythm="进入核心发布段，画面要稳。",
        required_visual_evidence=("主会场", "大屏主题或满场观众"),
        repeat_policy="主会场全景可作为段落基准，但重复需有不同信息点。",
    ),
    12: SlotLogic(
        role="核心信息承载",
        intent="演讲者、战略口号和大屏内容是剪辑里的信息主镜头。",
        preferred_terms=("演讲者", "战略", "口号", "发布", "大屏"),
        forbidden_terms=("茶歇", "签到", "外观"),
        allowed_materials=("演讲者", "战略口号", "大屏内容", "产品发布信息"),
        blocked_materials=("茶歇", "签到", "外场空镜"),
        recommended_shot_sizes=("全景", "中景"),
        recommended_camera_moves=("综合", "固定", "缓推"),
        recommended_sounds=("演讲同期", "音乐铺底"),
        rhythm="信息主镜头，停留时间可略长但不能跳流程。",
        required_visual_evidence=("演讲者", "大屏战略或主题信息"),
        repeat_policy="同一演讲角度重复降分，除非大屏内容不同。",
    ),
    13: SlotLogic(
        role="观众反应/传播感",
        intent="手机录屏是观众反应镜头，用于补充内容被观看、记录、传播的感觉。",
        preferred_terms=("观众", "手机", "录屏", "记录"),
        forbidden_terms=("主持人", "茶歇", "落版"),
        allowed_materials=("观众录屏", "手机屏幕", "观众反应", "台下视角"),
        blocked_materials=("主持人特写", "茶歇", "品牌落版"),
        recommended_shot_sizes=("特写", "中景"),
        recommended_camera_moves=("横移", "固定", "轻推"),
        recommended_sounds=("录制声", "现场声"),
        rhythm="短反应镜头，强化发布内容被记录。",
        required_visual_evidence=("观众手机", "舞台或发布内容被记录"),
        repeat_policy="只能作为反应点使用，不能替代发布主镜头。",
    ),
    14: SlotLogic(
        role="节奏调剂",
        intent="中场表演用于打断连续演讲镜头，提供动势和节奏变化。",
        preferred_terms=("中场", "表演", "直播框", "互动"),
        forbidden_terms=("签到", "落版", "外观"),
        allowed_materials=("中场表演", "互动表演", "灯光表演", "舞台节目"),
        blocked_materials=("签到", "外场门头", "品牌落版"),
        recommended_shot_sizes=("全景", "中景"),
        recommended_camera_moves=("综合", "缓推", "横移"),
        recommended_sounds=("现场音乐", "掌声"),
        rhythm="核心段中段提速，打断演讲连续性。",
        required_visual_evidence=("舞台表演", "现场互动或灯光动势"),
        repeat_policy="只允许一次节奏调剂，不能多次占用核心发布段。",
        visual_adapt_queries=("演讲者舞台发言 观众手机记录 发布会内容过渡",),
    ),
    15: SlotLogic(
        role="仪式节点镜头",
        intent="颁发镜头用于补充正式节点和情绪抬升，不等同于完整活动议程。",
        preferred_terms=("颁发", "仪式", "掌声", "欢呼"),
        forbidden_terms=("茶歇", "签到", "外观", "演讲者"),
        allowed_materials=("颁发仪式", "嘉宾登台", "掌声", "正式节点"),
        blocked_materials=("茶歇", "签到墙", "外场建筑"),
        recommended_shot_sizes=("全景", "中景"),
        recommended_camera_moves=("综合", "固定", "缓推"),
        recommended_sounds=("掌声", "欢呼声"),
        rhythm="仪式感抬升，镜头要正式、清楚。",
        required_visual_evidence=("颁发或仪式动作", "台上人物"),
        repeat_policy="仪式节点可与第 16 镜承接，但不要重复同一动作。",
        visual_adapt_queries=("嘉宾集体登台 正式仪式 舞台 掌声",),
    ),
    16: SlotLogic(
        role="高潮收束镜头",
        intent="启动和大合影是核心段落的高点，用于承接到结尾落版。",
        preferred_terms=("仪式启动", "启动", "大合影", "欢呼", "掌声"),
        forbidden_terms=("茶歇", "签到", "落版"),
        allowed_materials=("启动仪式", "大合影", "集体欢呼", "高潮节点"),
        blocked_materials=("茶歇", "签到", "品牌落版"),
        recommended_shot_sizes=("全景", "中景"),
        recommended_camera_moves=("综合", "固定", "缓推"),
        recommended_sounds=("人群掌声", "欢呼声"),
        rhythm="发布段高点，之后要能自然收向结尾。",
        required_visual_evidence=("启动动作或大合影", "人群情绪"),
        repeat_policy="无启动证据时可用核心认证或权威信息高点 ADAPT，普通合影仍不可硬凑。",
        visual_adapt_queries=("核心认证仪式 第一品牌 大屏 嘉宾 观众拍照",),
    ),
    17: SlotLogic(
        role="空间拉远收束",
        intent="360 拉远把现场空间从局部镜头收回整体，为最终 KV 留出视觉出口。",
        preferred_terms=("360", "拉远", "会场全貌", "外围", "全景"),
        forbidden_terms=("主持人", "签到", "演讲者"),
        allowed_materials=("360 拉远", "会场全貌", "空间全景", "外围收束"),
        blocked_materials=("主持人", "签到互动", "演讲者特写"),
        recommended_shot_sizes=("全景", "远景"),
        recommended_camera_moves=("拉远", "360 环绕", "航拍后撤"),
        recommended_sounds=("音乐收束", "环境声"),
        rhythm="从高潮回到空间整体，给片尾留出口。",
        required_visual_evidence=("会场整体", "拉远或环绕动势"),
        repeat_policy="可复用主会场空间，但必须和第 11 镜视角不同。",
        visual_adapt_queries=("主会场大型大屏 环形灯架 蓝色主视觉 空间收束",),
    ),
    18: SlotLogic(
        role="最终品牌落版",
        intent="白场和主视觉 KV 完成片尾记忆点，是成片收口，不是现场流程结束。",
        preferred_terms=("END", "主视觉", "KV", "画面变白", "落版"),
        forbidden_terms=("茶歇", "签到", "主持人"),
        allowed_materials=("主视觉 KV", "END 画面", "品牌落版", "主题收口"),
        blocked_materials=("茶歇", "签到", "主持人"),
        recommended_shot_sizes=("全景", "特写"),
        recommended_camera_moves=("固定", "淡出", "缓推"),
        recommended_sounds=("音乐收", "落版音效"),
        rhythm="最后 2-3 秒收束品牌与主题，不能再引入新流程。",
        required_visual_evidence=("主视觉 KV", "品牌或主题落版"),
        repeat_policy="片尾落版只出现一次，可呼应第 1 镜但不重复中段素材。",
    ),
}


def slot_logic(slot_index: int | None) -> SlotLogic | None:
    return LAUNCH_SLOT_LOGIC.get(int(slot_index or 0))


def logical_slot_index(source: SourceShot | dict, slot_count: int | None = None) -> int:
    """Map a variable-length template row to the closest canonical launch role.

    The 18-slot profile remains the role dictionary, but physical row numbers are
    no longer required to equal canonical role numbers.  This lets a template
    omit an optional beat (for example the second brand callback) without
    shifting every downstream role.
    """
    if isinstance(source, dict):
        explicit = _integer(source.get("逻辑槽位") or source.get("logic_slot_index"))
        actual = _integer(
            source.get("slot_index") or source.get("模板镜号") or source.get("镜号")
        )
        total = _integer(source.get("模板槽位总数") or source.get("slot_count")) or slot_count
        text = " ".join(
            str(source.get(key) or "")
            for key in (
                "镜头功能", "槽位功能", "画面描述", "模板结构", "结构",
                "效果", "备注", "视觉内容摘要", "视觉标签",
            )
        )
    else:
        explicit = 0
        actual = int(source.slot_index or 0)
        total = slot_count
        text = " ".join(str(value or "") for value in (
            source.structure,
            source.effect,
            source.camera_type,
            source.shot_size,
            source.camera_move,
            source.visual_desc,
            source.sound,
            source.note,
            source.tags,
            source.shot_function,
            source.visual_subject,
        ))
    if explicit in LAUNCH_SLOT_LOGIC:
        return explicit
    if actual <= 0:
        return 0
    if not total and actual in LAUNCH_SLOT_LOGIC:
        return actual
    if total == 18 or (total and total < 12):
        return actual if actual in LAUNCH_SLOT_LOGIC else 0

    value = " ".join(str(text or "").split())
    functions = set(infer_visual_functions(value))
    if "construction" in functions:
        return 2
    if any(term in value for term in ("360相机", "会场全貌", "外围会场", "匀速拉远")):
        return 17
    if "final_kv" in functions or any(term in value for term in ("END", "主视觉KV", "主视觉 KV", "结尾品牌落版")):
        return 18
    if "startup_group" in functions:
        return 16
    if "award" in functions or "formal_guest_node" in functions:
        return 15
    if "performance" in functions:
        return 14
    if "audience_recording" in functions:
        return 13
    if "speech" in functions or any(term in value for term in ("核心信息发布", "战略口号", "演讲者")):
        return 12
    if any(term in value for term in ("主发布会现场", "座无虚席", "核心场景全貌", "主发布画面")):
        return 11
    if "service_social" in functions:
        return 8
    if "crowd_flow" in functions:
        return 9
    if "product_experience" in functions:
        return 7
    if "guest_group" in functions:
        return 6
    if "registration" in functions:
        return 5
    if "venue_wide" in functions or any(term in value for term in ("主会场空镜", "光束汇聚")):
        return 4
    if "brand_visual" in functions or any(term in value for term in ("logo", "LOGO", "品牌露出")):
        if actual <= 1:
            return 1
        return 10 if actual >= 9 else 3

    if total == 17 and actual >= 10:
        return min(18, actual + 1)
    return actual if actual in LAUNCH_SLOT_LOGIC else 0


def slot_logic_for_source(source: SourceShot | dict, slot_count: int | None = None) -> SlotLogic | None:
    return slot_logic(logical_slot_index(source, slot_count))


def _integer(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
