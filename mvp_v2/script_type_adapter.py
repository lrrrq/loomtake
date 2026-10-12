from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
from typing import Mapping, Protocol, Sequence, runtime_checkable

from .stage_contract import StageError


SourceRecord = Mapping[str, object]
OutputRecord = Mapping[str, object]


@runtime_checkable
class ScriptTypeAdapter(Protocol):
    category_id: str

    def parse_source_workbook(self, workbook_path: Path) -> Sequence[SourceRecord]:
        ...

    def validate_source_workbook(
        self,
        workbook_path: Path,
        source_records: Sequence[SourceRecord],
    ) -> Sequence[StageError]:
        ...

    def shot_count(self, source_records: Sequence[SourceRecord]) -> int:
        ...

    def generation_rules(self) -> Mapping[str, object]:
        ...

    def audit_generation(
        self,
        brief: Mapping[str, object],
        source_records: Sequence[SourceRecord],
        decisions: Sequence[object],
        script_rows: Sequence[OutputRecord],
        detail_rows: Sequence[OutputRecord],
    ) -> Mapping[str, object]:
        ...


@dataclass(frozen=True)
class ScriptTypeMetadata:
    category_id: str
    display_name: str
    source_workbook_name: str
    source_sheet_names: tuple[str, ...]
    dynamic_shot_count: bool
    workbook_glob: str = ""
    aliases: tuple[str, ...] = ()
    planning_profile: str = "ai_spec_function_contract"
    audit_profile: str = "data_driven_category_contract"
    duration_policy: str = "positive_integer_seconds"
    cross_category_sources: bool = False
    structural_arcs: tuple = ()
    pacing_profile: str = "even"


def load_script_type_metadata(path: str | Path) -> tuple[ScriptTypeMetadata, ...]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if payload.get("schema_version") != "mplus-categories/v1":
        raise ValueError("类别配置版本不受支持")
    items = []
    identities = set()
    for raw in payload.get("items", []):
        data = dict(raw)
        for field in ("source_sheet_names", "aliases"):
            data[field] = tuple(data.get(field, ()))
        data["structural_arcs"] = tuple(
            (int(start), int(end), str(name), tuple(roles))
            for start, end, name, roles in data.get("structural_arcs", ())
        )
        item = ScriptTypeMetadata(**data)
        if (not item.category_id or not item.display_name or not item.workbook_glob
                or not item.source_sheet_names or not item.structural_arcs
                or item.pacing_profile not in {"even", "launch_60s"}):
            raise ValueError("类别配置缺少有效来源、结构或节奏")
        for value in {item.category_id, item.display_name, *item.aliases}:
            key = value.casefold().replace(" ", "")
            if key in identities:
                raise ValueError(f"类别名称或别名重复: {value}")
            identities.add(key)
        next_slot = 1
        for start, end, name, roles in item.structural_arcs:
            if start != next_slot or end < start or not name or not roles:
                raise ValueError(f"类别结构槽位不连续: {item.category_id}")
            next_slot = end + 1
        items.append(item)
    if not items:
        raise ValueError("类别配置不能为空")
    return tuple(items)


SCRIPT_TYPE_METADATA = load_script_type_metadata(
    os.environ.get("MPLUS_CATEGORY_DEFINITIONS")
    or Path(__file__).with_name("category_definitions.json")
)
# Compatibility names; category consumers use the catalog above.
(LAUNCH_SCRIPT_TYPE_METADATA, EXHIBITION_SCRIPT_TYPE_METADATA,
 ANNUAL_SCRIPT_TYPE_METADATA, SPORTS_SCRIPT_TYPE_METADATA,
 MUSIC_FESTIVAL_SCRIPT_TYPE_METADATA, STAR_ACTIVITY_SCRIPT_TYPE_METADATA) = (
    next(item for item in SCRIPT_TYPE_METADATA if item.category_id == key)
    for key in ("launch", "exhibition", "annual", "sports", "music_festival", "star_activity")
)

_DESCRIPTIVE_CATEGORY_HINTS = {
    "star_activity": (
        "七夕快闪互动",
        "明星应援",
        "艺人应援",
        "粉丝应援",
    ),
}


def metadata_for_script_type(value: str) -> ScriptTypeMetadata:
    text = str(value or "").strip()
    folded = text.casefold().replace(" ", "")
    for metadata in SCRIPT_TYPE_METADATA:
        candidates = (
            metadata.category_id,
            metadata.display_name,
            f"{metadata.display_name}类",
            f"{metadata.display_name}脚本",
            f"{metadata.display_name}类脚本",
            *metadata.aliases,
        )
        for candidate in candidates:
            normalized = str(candidate).casefold().replace(" ", "")
            if folded == normalized:
                return metadata
    raise ValueError(f"不支持的脚本类型: {text or '(空)'}")


def metadata_for_script_type_hint(value: str) -> ScriptTypeMetadata:
    """Resolve one unambiguous category mentioned inside a descriptive type."""
    text = str(value or "").strip()
    try:
        return metadata_for_script_type(text)
    except ValueError:
        pass
    folded = text.casefold().replace(" ", "")
    matched = []
    for metadata in SCRIPT_TYPE_METADATA:
        token = metadata.display_name.casefold().replace(" ", "")
        start = folded.find(token)
        if start < 0:
            continue
        prefix = folded[max(0, start - 2):start]
        if prefix.endswith(("非", "不是")):
            continue
        matched.append(metadata)
    if len(matched) == 1:
        return matched[0]
    if len(matched) > 1:
        names = "、".join(item.display_name for item in matched)
        raise ValueError(f"脚本具体类型同时匹配多个类别: {names}")
    hinted = [
        metadata_for_category_id(category_id)
        for category_id, hints in _DESCRIPTIVE_CATEGORY_HINTS.items()
        if any(hint.casefold().replace(" ", "") in folded for hint in hints)
    ]
    if len(hinted) == 1:
        return hinted[0]
    raise ValueError(f"不支持的脚本类型描述: {text or '(空)'}")


def resolve_script_type_selection(raw_script_type: str, specific_script_type: str = "") -> dict[str, str]:
    """Return the shared Brief/UI/planning routing preflight result."""
    raw = str(raw_script_type or "").strip()
    specific = str(specific_script_type or "").strip()

    def resolve(value: str) -> ScriptTypeMetadata | None:
        if not value:
            return None
        try:
            return metadata_for_script_type_hint(value)
        except ValueError:
            return None

    raw_metadata = resolve(raw)
    specific_metadata = resolve(specific)
    if raw_metadata and specific_metadata and raw_metadata != specific_metadata:
        return {
            "status": "needs_clarification",
            "raw_script_type": raw,
            "specific_script_type": specific,
            "message": f"脚本类型匹配{raw_metadata.display_name}，具体类型匹配{specific_metadata.display_name}，请确认采用哪一类模板。",
        }
    metadata = raw_metadata or specific_metadata
    if metadata is None:
        return {
            "status": "unsupported",
            "raw_script_type": raw,
            "specific_script_type": specific,
            "message": "未匹配生成模板，请选择" + "、".join(item.display_name for item in SCRIPT_TYPE_METADATA) + "。",
        }
    authority = "script_type" if raw_metadata else "industry_or_activity_category"
    return {
        "status": "ready",
        "raw_script_type": raw,
        "specific_script_type": specific,
        "script_type": metadata.display_name,
        "category_id": metadata.category_id,
        "source_workbook_name": metadata.source_workbook_name,
        "authority": authority,
        "message": f"生成将采用{metadata.display_name}模板；资料原文保持不变。",
    }


def metadata_for_category_id(value: str) -> ScriptTypeMetadata:
    text = str(value or "").strip()
    for metadata in SCRIPT_TYPE_METADATA:
        if text == metadata.category_id:
            return metadata
    raise ValueError(f"不支持的类别 ID: {text or '(空)'}")


def normalize_script_type(value: str) -> str:
    return metadata_for_script_type(value).display_name


def category_id_for_script_type(value: str) -> str:
    return metadata_for_script_type(value).category_id


def route_contract_for_script_type(value: str) -> dict[str, object]:
    metadata = metadata_for_script_type(value)
    return {
        "category_id": metadata.category_id,
        "script_type": metadata.display_name,
        "source_sheets": list(metadata.source_sheet_names),
        "dynamic_shot_count": metadata.dynamic_shot_count,
        "planning_profile": metadata.planning_profile,
        "audit_profile": metadata.audit_profile,
        "duration_policy": metadata.duration_policy,
        "cross_category_sources": metadata.cross_category_sources,
        "render_profile": "source_workbook_template",
    }


def resolve_script_type_workbook(
    source_root: str | Path,
    script_type: str,
) -> Path:
    metadata = metadata_for_script_type(script_type)
    root = Path(source_root).expanduser()
    if root.is_file():
        if root.match(metadata.workbook_glob):
            return root
        return root.parent / metadata.source_workbook_name
    candidates = [
        path
        for path in root.rglob(metadata.workbook_glob)
        if path.is_file() and not path.name.startswith((".~", "~$"))
    ]
    if candidates:
        return max(
            candidates,
            key=lambda path: (
                "AI版" in path.name,
                path.stat().st_mtime_ns,
                path.name,
            ),
        )
    return root / metadata.source_workbook_name


def resolve_brief_category(value: dict) -> dict[str, str]:
    facts = value.get("facts", [])
    conflicts = value.get("conflicts", [])
    blocked = {
        fact_id
        for conflict in conflicts if conflict.get("resolution_status") == "unresolved"
        for fact_id in conflict.get("fact_ids", [])
    }
    selected = {
        str(conflict.get("field_path") or ""): str(conflict.get("resolved_fact_id") or "")
        for conflict in conflicts if conflict.get("resolution_status") == "resolved"
    }
    values: dict[str, str] = {}
    for fact in facts:
        field = str(fact.get("field_path") or "")
        fact_id = str(fact.get("fact_id") or "")
        if field not in {"脚本类型", "行业或活动类别"} or fact_id in blocked:
            continue
        if selected.get(field) and selected[field] != fact_id:
            continue
        if field in values:
            continue
        values[field] = _brief_fact_text(fact.get("value_json"))
    return resolve_script_type_selection(
        values.get("脚本类型", ""), values.get("行业或活动类别", "")
    )


def _brief_fact_text(value: object) -> str:
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError:
        return str(value or "")
    return parsed if isinstance(parsed, str) else str(parsed)
