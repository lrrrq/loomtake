from __future__ import annotations

from dataclasses import dataclass, field
from collections import defaultdict
import re

from .script_type_adapter import normalize_script_type

_POSITIVE_SECONDS_RE = re.compile(r"^\s*([1-9]\d*)(?:s|秒)?\s*$")


@dataclass(frozen=True)
class SourceShot:
    script_type: str
    source_file: str
    source_sheet: str
    source_row: int
    shot_no: str = ""
    structure: str = ""
    duration_seconds: str = ""
    effect: str = ""
    camera_type: str = ""
    shot_size: str = ""
    camera_move: str = ""
    visual_desc: str = ""
    sound: str = ""
    note: str = ""
    reference_image_path: str = ""
    effect_reference_path: str = ""
    reference_image_evidence: str = ""
    effect_reference_evidence: str = ""
    reuse_level: str = ""
    forbidden_note: str = ""
    tags: str = ""
    shot_function: str = ""
    visual_subject: str = ""
    source_brand: str = ""
    brand_neutral: bool = False
    brand_neutral_evidence: str = ""
    form: str = ""
    missing_metadata: tuple[str, ...] = field(default_factory=tuple)
    slot_index: int = 0
    structure_source_file: str = ""
    structure_source_sheet: str = ""
    structure_source_row: int = 0
    generic_desc: str = ""
    brand_judgement_original: str = ""
    sheet_overview: str = ""
    semantic_source_anchors: str = ""
    source_semantics_version: str = ""
    source_package_id: str = ""
    source_package_version: int = 0
    source_category_id: str = ""
    source_asset_id: str = ""

    def searchable_text(self) -> str:
        return " ".join([
            self.structure,
            self.effect,
            self.camera_type,
            self.shot_size,
            self.camera_move,
            self.visual_desc,
            self.generic_desc,
            self.sound,
            self.note,
            self.tags,
            self.shot_function,
            self.visual_subject,
            self.forbidden_note,
        ])


@dataclass(frozen=True)
class ScoredShot:
    shot: SourceShot
    score: int
    match_reason: str
    gap_note: str = ""


@dataclass(frozen=True)
class TemplateDecision:
    slot: SourceShot
    variant: SourceShot | None
    mode: str
    score: int
    match_reason: str
    gap_note: str = ""
    hit_terms: tuple[str, ...] = ()
    missing_terms: tuple[str, ...] = ()
    conflict_terms: tuple[str, ...] = ()
    selected_reason: str = ""
    rejected_reason: str = ""


@dataclass(frozen=True)
class BriefProfile:
    project_name: str
    project_area_filename: str
    script_type: str
    client: str
    industry: str
    duration_seconds: int
    tone: str
    required_terms: tuple[str, ...]
    forbidden_terms: tuple[str, ...]
    material_ref: str
    note: str
    gap_notes: tuple[str, ...] = ()

    def to_brief(self) -> dict:
        return {
            "项目名称": self.project_name,
            "项目区域文件名": self.project_area_filename,
            "脚本类型": self.script_type,
            "品牌或客户": self.client,
            "行业或活动类别": self.industry,
            "视频时长": f"{self.duration_seconds}s" if self.duration_seconds else "",
            "风格调性": self.tone,
            "必须出现的内容": "、".join(self.required_terms),
            "禁忌或不能出现的内容": "、".join(self.forbidden_terms),
            "素材目录或素材文件": self.material_ref,
            "备注": self.note,
        }


def brief_profile_from_dict(brief: dict) -> BriefProfile:
    required_terms = _split_terms(brief.get("必须出现的内容", ""))
    forbidden_terms = split_forbidden_terms(brief.get("禁忌或不能出现的内容", ""))
    material_ref = str(brief.get("素材目录或素材文件", "") or "").strip()
    duration_value = str(brief.get("视频时长", "") or "").strip()
    duration_seconds = _duration_seconds(duration_value)
    gap_notes = []
    if not forbidden_terms:
        gap_notes.append("禁忌字段为空")
    if not material_ref:
        gap_notes.append("素材目录为空")
    if duration_value and not duration_seconds:
        gap_notes.append("视频时长非法")
    if required_terms and all(_is_broad_required_term(term) for term in required_terms):
        gap_notes.append("必须内容过泛")
    return BriefProfile(
        project_name=str(brief.get("项目名称", "") or "").strip(),
        project_area_filename=str(brief.get("项目区域文件名", "") or "").strip(),
        script_type=str(brief.get("脚本类型", "") or "").strip(),
        client=str(brief.get("品牌或客户", "") or "").strip(),
        industry=str(brief.get("行业或活动类别", "") or "").strip(),
        duration_seconds=duration_seconds,
        tone=str(brief.get("风格调性", "") or "").strip(),
        required_terms=tuple(required_terms),
        forbidden_terms=tuple(forbidden_terms),
        material_ref=material_ref,
        note=str(brief.get("备注", "") or "").strip(),
        gap_notes=tuple(gap_notes),
    )


def _split_terms(value: str) -> list[str]:
    terms = []
    for part in re.split(r"[、,，;；\s\n]+", str(value or "")):
        term = part.strip()
        if term:
            terms.append(term)
    return list(dict.fromkeys(terms))


_FORBIDDEN_PREFIXES = (
    "不得出现", "禁止出现", "不允许出现", "不要出现", "不能出现", "不可出现", "不出现",
    "不得使用", "禁止使用", "不允许使用", "不要使用", "不能使用", "不可使用",
    "不得包含", "禁止包含", "不允许包含", "不要包含", "不能包含", "不可包含",
    "不得虚构", "禁止虚构", "不允许虚构", "不要虚构", "不能虚构", "不可虚构", "不虚构",
    "避免出现", "避免使用", "避免包含", "禁止", "不允许", "不要", "不能", "不得", "不可", "避免",
)


def split_forbidden_terms(value: str) -> list[str]:
    """把用户自然语言禁忌转成可确定性匹配的原子词。"""
    terms = []
    for part in re.split(r"(?:以及|或者|或|和)|[、,，;；/。\s\n]+", str(value or "")):
        term = part.strip("：:.-")
        for prefix in _FORBIDDEN_PREFIXES:
            if term.startswith(prefix):
                term = term[len(prefix):].strip("：:.-")
                break
        if term:
            terms.append(term)
    return list(dict.fromkeys(terms))


def parse_positive_seconds_strict(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = _POSITIVE_SECONDS_RE.fullmatch(text)
    return int(match.group(1)) if match else None


def _duration_seconds(value: str) -> int:
    return parse_positive_seconds_strict(value) or 0


def parse_duration_constraints(value: str, *, infer_total: bool = True) -> dict[str, set[int]]:
    """Read explicit production durations; quoted copy is not an instruction.

    Zero marks an invalid numeric constraint, so an invalid stated total cannot
    be replaced by shot arithmetic. Planning requires an explicit total;
    compatibility checks may also compare the total implied by shot counts.
    """
    text = re.sub(r"\s+", "", str(value))
    text = re.sub(r'“[^”]*”|‘[^’]*’|"[^"]*"', "", text)
    unit = r"(?:秒|seconds?|sec|s)(?![A-Za-z])"
    number = r"([+-]?\d+(?:\.\d+)?)"
    result: dict[str, set[int]] = defaultdict(set)
    patterns = {
        "total": rf"(?:全片|整片|成片|总计|总共|总|时长|片长)(?:时长)?(?:为|是|约|[:：])?{number}{unit}",
        "shot_count": rf"(?<![\d第])(?:总镜数(?:为|[:：])?|总共|总计|全片|整片|共)?{number}(?:个)?镜(?:头)?",
        "per_shot": rf"每(?:个)?镜(?:头)?(?:时长)?(?:为|是|各|[:：])?{number}{unit}",
    }
    def seconds(raw: str) -> int:
        return int(raw) if re.fullmatch(r"[1-9]\d*", raw) else 0
    per_shot_spans = [match.span() for match in re.finditer(patterns["per_shot"], text, re.IGNORECASE)]
    for label, pattern in patterns.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if label == "total" and any(start <= match.start() < end for start, end in per_shot_spans):
                continue
            result[label].add(seconds(match[1]))
    bare = re.match(rf"(?:约)?{number}{unit}", text, re.IGNORECASE)
    if bare:
        result["total"].add(seconds(bare[1]))
    if infer_total and len(result["shot_count"]) == len(result["per_shot"]) == 1:
        result["total"].add(next(iter(result["shot_count"])) * next(iter(result["per_shot"])))
    return {key: values for key, values in result.items() if values}


def validate_launch_brief(brief: dict) -> int:
    raw_value = str(brief.get("视频时长", "") or "").strip()
    if not raw_value:
        raise ValueError("发布会 brief 视频时长不能为空，且必须是正整数秒，如 60s")
    duration_seconds = parse_positive_seconds_strict(raw_value)
    if not duration_seconds:
        raise ValueError(f"发布会 brief 视频时长必须是正整数秒，如 60s；当前收到: {raw_value}")
    return duration_seconds


def validate_script_brief(brief: dict) -> int:
    script_type = normalize_script_type(brief.get("脚本类型") or "发布会")
    if script_type == "发布会":
        return validate_launch_brief(brief)
    raw_value = str(brief.get("视频时长", "") or "").strip()
    duration_seconds = parse_positive_seconds_strict(raw_value)
    if not duration_seconds:
        raise ValueError(
            f"{script_type} brief 视频时长必须是正整数秒，如 60s；当前收到: {raw_value or '(空)'}"
        )
    return duration_seconds


def _is_broad_required_term(value: str) -> bool:
    return value in {"品牌", "客户", "活动", "现场", "氛围", "高级", "科技感", "宣传"}
