from __future__ import annotations

import re


BRAND_ALIASES: dict[str, tuple[str, ...]] = {
    "美的": ("美的", "Midea"),
    "林氏家居": ("林氏家居", "林氏", "LINSY", "LINSY HOME"),
    "Cleer": ("Cleer",),
    "COLMO": ("COLMO", "Hi COLMO"),
    "东芝": ("东芝", "TOSHIBA"),
    "阿里巴巴": ("阿里巴巴", "Alibaba"),
    "小米": ("小米", "Xiaomi", "SU7"),
    "vivo": ("vivo",),
    "XTransfer": ("XTransfer",),
    "比音勒芬": ("比音勒芬", "BIEM.L.FDLKK"),
    "喜力": ("喜力", "Heineken"),
    "蔡司": ("蔡司", "ZEISS"),
    "荣耀": ("荣耀", "HONOR", "华为荣耀"),
    "追觅": ("追觅", "DREAME"),
    "彪马": ("彪马", "PUMA"),
    "无畏契约": ("无畏契约", "VALORANT"),
    "lululemon": ("lululemon", "露露乐蒙"),
}
_BRAND_CUE_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9 ._-]{1,30}?|[\u4e00-\u9fffA-Za-z0-9]{2,16}?)"
    r"\s*(?P<cue>(?:品牌)?(?:logo|LOGO|标识|品牌墙))"
)
_WAYFINDING_SIGN_LABEL_RE = re.compile(
    r"(?:(?:主会场|会场|场馆|场地)?(?:入口|出口)(?:处)?的?)?指引"
)
_GENERIC_BRAND_LABELS = frozenset({"品牌", "当前品牌", "当前项目", "项目", "客户", "活动"})
_PRODUCT_SIGN_LABEL_RE = re.compile(r"(?:控制|操作)?面板|按钮|按键|功能|刻度|容量|温度|电源")
_GENERIC_VENUE_LABEL_RE = re.compile(
    r"(?:(?:活动|发布会|展会|年会)?现场|(?:主会场|会场|场馆|场地)(?:入口|出口)?)"
)
_NON_BRAND_CONTEXT_PARTS = (
    "主题", "字样", "画面", "出现", "收束", "穿梭", "进入", "与", "及",
    "印有", "带有", "显示", "拿着", "展示", "背景", "上有", "光影",
    "穿越", "掠过",
    "亮出", "点亮", "清晰", "项目",
)
_BRAND_CUE_ACTIONS = (
    "呈现", "亮出", "点亮", "显示", "出现", "上有", "印有", "带有",
    "展示", "穿越", "掠过", "穿过", "显现", "浮现", "带过", "扫过", "对准",
)
_BRAND_APPEARANCE_RE = re.compile(r"^.{0,12}(?:色|底|字|字样|图形|样式)$")
_EMPTY_BRAND_LABELS = frozenset({"无", "无品牌", "未知", "未识别", "none", "unknown", "n/a"})
_UNCERTAIN_BRAND_LABELS = frozenset({"未知", "未识别", "unknown", "n/a"})
_BRAND_NEUTRAL_EVIDENCE_PREFIXES = (
    "人工确认:",
    "human_review:",
)
_VISUAL_PROVENANCE_MARKS = frozenset({
    "mfilms",
    "mfilmsfilms",
    "小红书",
})


def source_brand_provenance_issue(
    source_brand: str,
    brand_neutral: bool = False,
    brand_neutral_evidence: str = "",
) -> str:
    brand = str(source_brand or "").strip()
    if brand and brand_neutral:
        return "候选来源品牌与品牌中性声明冲突"
    if brand:
        return ""
    if not brand_neutral:
        return "候选缺少来源品牌声明（source_brand）或品牌中性确认（brand_neutral=true）"
    evidence = str(brand_neutral_evidence or "").strip()
    if not any(
        evidence.startswith(prefix) and evidence[len(prefix):].strip()
        for prefix in _BRAND_NEUTRAL_EVIDENCE_PREFIXES
    ):
        return "品牌中性缺少明确源字段或人工确认"
    return ""


def source_brand_leaks(expected_text: str, source_brand: str, output_text: str) -> bool:
    for brand in _source_brand_values(source_brand):
        if (
            conflicting_brand_names(expected_text, [brand])
            and _brand_mentioned(output_text, brand)
        ):
            return True
    return False


def expected_brand_keys(text: str) -> set[str]:
    return {
        brand
        for brand, aliases in BRAND_ALIASES.items()
        if any(_contains(text, alias) for alias in aliases)
    }


def conflicting_brands(expected_text: str, observed_text: str) -> list[str]:
    expected = expected_brand_keys(expected_text)
    observed = expected_brand_keys(observed_text)
    if expected:
        return sorted(observed - expected)
    return sorted(
        brand
        for brand in observed
        if not any(_contains(expected_text, alias) for alias in BRAND_ALIASES[brand])
    )


def conflicting_brand_names(expected_text: str, observed_names) -> list[str]:
    if isinstance(observed_names, str):
        names = re.split(r"[、,，;；\n]+", observed_names)
    else:
        names = [str(value) for value in (observed_names or [])]
    conflicts = []
    for raw_name in names:
        name = raw_name.strip()
        if not name or _empty_brand_label(name):
            continue
        known_conflicts = conflicting_brands(expected_text, name)
        if known_conflicts:
            conflicts.extend(known_conflicts)
            continue
        if expected_brand_keys(name):
            continue
        if not _same_literal_brand(expected_text, name):
            conflicts.append(name)
    return list(dict.fromkeys(conflicts))


def conflicting_visual_brand_names(expected_text: str, observed_names) -> list[str]:
    """Ignore platform/production watermarks while checking client brands."""
    if isinstance(observed_names, str):
        names = re.split(r"[、,，;；\n]+", observed_names)
    else:
        names = [str(value) for value in (observed_names or [])]
    content_brands = [
        name for name in names
        if _normalize_literal_brand(name) not in _VISUAL_PROVENANCE_MARKS
    ]
    return conflicting_brand_names(expected_text, content_brands)


def unknown_brand_mentions(expected_text: str, observed_text: str) -> list[str]:
    mentions = []
    for match in _BRAND_CUE_RE.finditer(str(observed_text or "")):
        name = _brand_cue_name(match.group("name"))
        if (
            not name
            or (match.group("cue") == "标识" and _WAYFINDING_SIGN_LABEL_RE.fullmatch(name))
            or (match.group("cue") == "标识" and _PRODUCT_SIGN_LABEL_RE.fullmatch(name))
            or name in _GENERIC_BRAND_LABELS
            or re.fullmatch(r"(?:任何)?(?:其他|其它|别的)(?:任何)?品牌(?:名字|名称)?(?:和|及|的)?", name)
            or _GENERIC_VENUE_LABEL_RE.fullmatch(name)
            or _BRAND_APPEARANCE_RE.fullmatch(name)
            or any(part in name for part in _NON_BRAND_CONTEXT_PARTS)
            or name.endswith("的品牌")
        ):
            continue
        if expected_brand_keys(name) or _same_literal_brand(expected_text, name):
            continue
        mentions.append(name)
    return list(dict.fromkeys(mentions))


def _brand_cue_name(value: str) -> str:
    """Remove an action prefix without discarding an explicit brand after it."""
    name = str(value or "").strip(" ._-")
    cuts = [
        name.rfind(action) + len(action)
        for action in _BRAND_CUE_ACTIONS
        if action in name
    ]
    if cuts:
        name = name[max(cuts):]
    name = name.strip(" ._-")
    return name[:-1] if name.endswith("的") else name


def brand_evidence_unverified(expected_text: str, observed_names, ocr_text: str) -> bool:
    if not str(ocr_text or "").strip():
        return False
    if isinstance(observed_names, str):
        names = re.split(r"[、,，;；\n]+", observed_names)
    else:
        names = [str(value) for value in (observed_names or [])]
    normalized = [name.strip() for name in names if name.strip()]
    if any(_empty_brand_label(name) for name in normalized):
        return False
    verified = [
        name.strip() for name in names
        if name.strip() and not _empty_brand_label(name)
    ]
    return not verified


def brand_verdict_status(observed_names) -> str:
    if isinstance(observed_names, str):
        names = re.split(r"[、,，;；\n]+", observed_names)
    else:
        names = [str(value) for value in (observed_names or [])]
    normalized = [name.strip().casefold() for name in names if name.strip()]
    if not normalized:
        return "missing"
    if any(name in _UNCERTAIN_BRAND_LABELS for name in normalized):
        return "uncertain"
    if any(_empty_brand_label(name) for name in normalized):
        return "explicit"
    return "explicit"


def _empty_brand_label(value: str) -> bool:
    normalized = str(value or "").strip(" 。.!！").casefold()
    if normalized in _EMPTY_BRAND_LABELS:
        return True
    return (
        any(marker in normalized for marker in ("未见", "未发现", "没有", "无可见", "无明确"))
        and any(marker in normalized for marker in ("品牌", "标识", "logo"))
    )


def expected_brand_aliases(text: str) -> tuple[str, ...]:
    aliases = []
    for brand in sorted(expected_brand_keys(text)):
        aliases.extend(BRAND_ALIASES[brand])
    return tuple(dict.fromkeys(aliases))


def adapt_source_text_to_project(
    source_text: str,
    project_text: str,
    source_brand: str = "",
) -> str:
    """Keep a creative shot's function while removing another client's brand claim.

    Creative-library rows are reusable references, but their prose often contains
    the original client's name. The final script must describe the current brief,
    while the detail sheet still preserves the original source trace.
    """
    text = str(source_text or "")
    target_brands = expected_brand_keys(project_text)
    if not text or not str(project_text or "").strip():
        return text
    target = (
        _preferred_project_brand(project_text, target_brands)
        if target_brands
        else str(project_text).strip()
    )
    source_brands = expected_brand_keys(text) - target_brands
    for brand in sorted(source_brands, key=len, reverse=True):
        for alias in sorted(BRAND_ALIASES[brand], key=len, reverse=True):
            text = _replace_alias(text, alias, target)
    for explicit_source in _source_brand_values(source_brand):
        if not _same_literal_brand(project_text, explicit_source):
            text = _replace_alias(text, explicit_source, target)
    for source_brand in unknown_brand_mentions(project_text, text):
        text = _replace_alias(text, source_brand, target)
    return text


def _preferred_project_brand(project_text: str, brands: set[str]) -> str:
    text = str(project_text or "")
    for brand in sorted(brands, key=len, reverse=True):
        for alias in sorted(BRAND_ALIASES[brand], key=len, reverse=True):
            if _contains(text, alias):
                return alias
    return sorted(brands)[0]


def _replace_alias(text: str, alias: str, replacement: str) -> str:
    if alias.isascii():
        return re.sub(re.escape(alias), replacement, text, flags=re.IGNORECASE)
    return text.replace(alias, replacement)


def _contains(text: str, alias: str) -> bool:
    if alias.isascii():
        return alias.casefold() in str(text or "").casefold()
    return alias in str(text or "")


def _same_literal_brand(expected_text: str, observed_name: str) -> bool:
    expected = _normalize_literal_brand(expected_text)
    observed = _normalize_literal_brand(observed_name)
    if not expected or not observed:
        return False
    return expected in observed or observed in expected


def _source_brand_values(value: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[、,，;；\n|/]+", str(value or ""))
        if item.strip()
    ]


def _brand_mentioned(text: str, brand: str) -> bool:
    known = expected_brand_keys(brand)
    if known:
        return any(
            _contains(text, alias)
            for key in known
            for alias in BRAND_ALIASES[key]
        )
    return _contains(text, brand)


def _normalize_literal_brand(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").casefold())
