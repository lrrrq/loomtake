from __future__ import annotations

import json
from pathlib import Path

from .run_contract import sha256_file, sha256_json


TEMPLATE_RULES_SCHEMA_VERSION = "mplus-template-rules/v1"
TEMPLATE_RULES_NAME = "template_rules.json"
RULE_LEVELS = {"structural_required", "technique_default", "example_only"}

_TECHNIQUE_FIELDS = (
    "duration_seconds", "effect", "camera_type", "shot_size", "camera_move", "sound",
)
_EXAMPLE_FIELDS = (
    "visual_description", "reference_image", "visual_subject", "tags", "copy", "source_brand",
)


class TemplateRuleError(ValueError):
    pass


def builtin_template_rules(category_id: str) -> dict:
    from .script_type_adapter import metadata_for_category_id
    arcs = metadata_for_category_id(category_id).structural_arcs
    if not arcs:
        raise TemplateRuleError(f"未配置模板结构规则: {category_id}")
    return _rules_from_arcs(category_id, arcs, "模版库")


def rules_from_template_shots(category_id: str, shots: list) -> dict:
    """Bind the actual template's consecutive structure segments to its source slots."""
    from .planning_core import _event_role
    from .script_type_adapter import metadata_for_category_id
    known = {name: roles for _, _, name, roles in metadata_for_category_id(category_id).structural_arcs}
    arcs = []
    for index, shot in enumerate(shots, start=1):
        name = str(shot.structure or "未命名结构").strip()
        roles = known.get(name) or (_event_role({"title": f"{name} {shot.shot_function}"}),)
        if arcs and arcs[-1][2] == name:
            start, _, _, old_roles = arcs[-1]
            arcs[-1] = (start, index, name, tuple(dict.fromkeys((*old_roles, *roles))))
        else:
            arcs.append((index, index, name, roles))
    if not arcs:
        raise TemplateRuleError("模板没有可用镜头")
    return _rules_from_arcs(category_id, arcs, str(shots[0].source_sheet))


def _rules_from_arcs(category_id: str, arcs: tuple | list, source_sheet: str) -> dict:
    rules = []
    for arc_index, (start, end, structure, roles) in enumerate(arcs, start=1):
        selector = {"source_sheet": source_sheet, "slot_start": start, "slot_end": end}
        rules.append({
            "rule_id": f"{category_id}.structure.{arc_index:02d}",
            "selector": selector,
            "field": "structure",
            "level": "structural_required",
            "allowed_event_roles": list(roles),
            "rationale": f"{structure}为{category_id}模板的必需结构弧",
            "value": structure,
        })
        for field in _TECHNIQUE_FIELDS:
            rules.append({
                "rule_id": f"{category_id}.{field}.{arc_index:02d}",
                "selector": selector,
                "field": field,
                "level": "technique_default",
                "allowed_event_roles": list(roles),
                "rationale": "模板技术参数作为默认值，Brief明确指定时可覆盖",
            })
        for field in _EXAMPLE_FIELDS:
            rules.append({
                "rule_id": f"{category_id}.{field}.{arc_index:02d}",
                "selector": selector,
                "field": field,
                "level": "example_only",
                "allowed_event_roles": list(roles),
                "rationale": "案例内容仅用于表达参考，不构成项目事实",
            })
    payload = {
        "schema_version": TEMPLATE_RULES_SCHEMA_VERSION,
        "category_id": str(category_id),
        "rules": rules,
    }
    validate_template_rules(payload)
    return payload


def validate_template_rules(payload: dict) -> dict:
    if payload.get("schema_version") != TEMPLATE_RULES_SCHEMA_VERSION:
        raise TemplateRuleError("模板规则版本不受支持")
    category_id = str(payload.get("category_id") or "").strip()
    rules = payload.get("rules")
    if not category_id or not isinstance(rules, list) or not rules:
        raise TemplateRuleError("模板规则缺少 category_id 或 rules")
    seen_ids = set()
    seen_selectors = set()
    structural_slots = set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise TemplateRuleError("模板规则条目必须为对象")
        rule_id = str(rule.get("rule_id") or "").strip()
        level = str(rule.get("level") or "").strip()
        field = str(rule.get("field") or "").strip()
        selector = rule.get("selector") or {}
        roles = rule.get("allowed_event_roles")
        if not rule_id or rule_id in seen_ids:
            raise TemplateRuleError(f"模板规则 ID 重复或为空: {rule_id}")
        if level not in RULE_LEVELS or not field:
            raise TemplateRuleError(f"模板规则级别或字段无效: {rule_id}")
        try:
            start = int(selector.get("slot_start"))
            end = int(selector.get("slot_end"))
        except (TypeError, ValueError):
            raise TemplateRuleError(f"模板规则槽位无效: {rule_id}") from None
        if start < 1 or end < start or not str(selector.get("source_sheet") or "").strip():
            raise TemplateRuleError(f"模板规则选择器无效: {rule_id}")
        if not isinstance(roles, list) or not all(str(role).strip() for role in roles):
            raise TemplateRuleError(f"模板规则缺少明确事件角色: {rule_id}")
        key = (str(selector["source_sheet"]), start, end, field)
        if key in seen_selectors:
            raise TemplateRuleError(f"模板规则重复覆盖同一字段: {rule_id}")
        seen_ids.add(rule_id)
        seen_selectors.add(key)
        if level == "structural_required" and field == "structure":
            for slot in range(start, end + 1):
                if slot in structural_slots:
                    raise TemplateRuleError(f"模板硬结构槽重复: {slot}")
                structural_slots.add(slot)
    if not structural_slots or structural_slots != set(range(1, max(structural_slots) + 1)):
        raise TemplateRuleError("模板硬结构槽必须从 1 开始连续覆盖")
    required_fields = {"structure", *_TECHNIQUE_FIELDS, *_EXAMPLE_FIELDS}
    selectors = {
        (
            str(rule["selector"]["source_sheet"]),
            int(rule["selector"]["slot_start"]),
            int(rule["selector"]["slot_end"]),
        )
        for rule in rules if rule.get("field") == "structure"
    }
    for selector in selectors:
        fields = {
            str(rule["field"])
            for rule in rules
            if (
                str(rule["selector"]["source_sheet"]),
                int(rule["selector"]["slot_start"]),
                int(rule["selector"]["slot_end"]),
            ) == selector
        }
        if fields != required_fields:
            raise TemplateRuleError(
                "模板规则未完整覆盖必需字段: "
                + "、".join(sorted(required_fields - fields))
            )
    return payload


def write_builtin_template_rules(path: str | Path, category_id: str) -> Path:
    target = Path(path).expanduser().resolve()
    target.write_text(
        json.dumps(builtin_template_rules(category_id), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def template_rule_contract(package: dict) -> dict:
    schema = str(package.get("schema_version") or "")
    if schema == "mplus-asset-package/v3":
        relative = str(package.get("template_rules") or "").strip()
        expected_sha = str(package.get("template_rules_sha256") or "").strip()
        if not relative or not expected_sha:
            raise TemplateRuleError("v3 资产包缺少模板规则绑定")
        path = (Path(package["package_root"]) / relative).resolve()
        if not path.is_file() or sha256_file(path) != expected_sha:
            raise TemplateRuleError("模板规则文件缺失或 SHA 已漂移")
        payload = validate_template_rules(json.loads(path.read_text(encoding="utf-8")))
        if payload["category_id"] != package.get("category_id"):
            raise TemplateRuleError("模板规则类别与资产包不一致")
        return {
            "status": "ready",
            "source": "package",
            "path": str(path),
            "sha256": expected_sha,
            "warning_codes": [],
            "payload": payload,
        }
    payload = builtin_template_rules(str(package.get("category_id") or ""))
    return {
        "status": "legacy_default",
        "source": "builtin_versioned_default",
        "path": "",
        "sha256": sha256_json(payload),
        "warning_codes": ["template_rules_legacy_default"],
        "payload": payload,
    }
