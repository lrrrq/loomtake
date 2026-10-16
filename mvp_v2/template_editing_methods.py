"""Bind reviewed editing methods to an immutable source workbook.

Extraction is editorial work. This projection verifies its evidence; category
names and source keywords never create a method. Case data stays in packages.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

SCHEMA_VERSION = "mplus-template-editing-methods/v2"
EVIDENCE_SCHEMA_VERSION = "mplus-template-method-evidence/v1"
_SOURCE_SHEETS = ("模版", "创意库")
_FACT_POLICY = {
    "authority": "creative_method_reference", "project_facts": False,
    "instruction": "案例说明方法来源；本项目事实来自已确认需求，素材可用性来自实际素材记录。",
    "application": "核对当前内容与素材是否满足条件，再选择借用、改编或放弃并说明原因。",
    "precedence": "已确认必现、禁写、事实顺序、时长与已接受正文优先；方法不新增必现事件、固定镜数或单镜时长。",
}
_WORKFLOW = {
    "name": "M+ 剪辑脚本规划",
    "purpose": "依据已确认需求、视频类型和所选模板结构，按整片、段落、必要子内容逐层组织表达；各层达到表达目的即停止细分。",
    "steps": [
        {"action": "核对三个依据：Brief中的目标、受众、必现与禁写等已确认需求；当前视频类型；所选模板的结构及允许调整范围。保留原事实ID、事实顺序和已采用内容。",
         "done_when": "需求、类型和模板结构均有明确来源，已确认约束与未知项分开；方法案例、取材建议与客户事实保留各自身份。"},
        {"action": "先写整片要表达什么，再按模板结构安排各段作用与前后关系，使段落共同完成整片目的。",
         "done_when": "整片符合Brief、视频类型正确、结构符合所选模板；各段的表达目的与整片关系清楚，即完成整片层大纲。"},
        {"action": "逐段按相同三项依据核对本层表达目的。已足以表达的段落停止细分；确有未交代的必要内容时，仅把该分支拆为子内容，再对每个子内容重复这一判断。",
         "done_when": "每个分支达到本层表达目的即停止；只有必要分支继续细分，不同分支可以停在不同层级，子内容数量由表达需要决定。"},
        {"action": "在需要解决表达问题的层级借用、改编或放弃有来源的方法，说明它怎样服务本层目的；涉及取材时区分已确认事实、计划拍摄、现有素材和待确认信息。",
         "done_when": "方法选择及例外有本项目依据，原event或requirement引用可追溯；images为空仅表示未附图，未知事实与现场结果仍保持未知。"},
        {"action": "按需求符合性、视频类型和模板结构核对大纲；需要修正时只处理不符合的分支，保留其余正确内容及已采用保护字段。",
         "done_when": "大纲三项检查通过且各层目的已表达即可结束。大纲不强制动作细节、机位、笑点或每项秒数，也不以缺少这些细节要求继续展开。"},
        {"action": "仅在进入正式拆镜任务时，将已完成的大纲落实为可执行镜头，交代观察重点、连续动作与机位、声画关系及段落归属；依据内容和已确认时长分配预算，实际切镜分别记录。",
         "done_when": "正式分镜的事实、可拍性、镜间关系与时长依据成立，镜头小计与段落及整片预算一致；已有保护字段保留，缺口如实说明，不靠静态延长、重复镜头或虚构事件填预算。"},
        {"action": "在当前交付层级复核：大纲只按三项依据及本层目的判断，正式分镜再检查动作、机位、声画与预算。问题引用当前原文并交回对应负责人。",
         "done_when": "符合当前层级目的即停止修订；真实未解决的问题及影响仍可见，结构或类型纠正也算有效改善，不要求为证明改进额外增加内容。"},
    ],
    "roles": {
        "discussion": "围绕当前层级的表达目的解释方法依据与适用条件，区分项目事实、案例、建议和未知项；讨论只形成建议。",
        "planning": "由原规划入口按整片、段落、必要子内容逐层推进，每层核对需求、类型和模板结构；目的达到即停止，仅必要分支继续细分。大纲完成后按任务范围决定是否进入正式拆镜。",
        "writing": "按当前交付层级落实已确认内容：大纲写清表达目的与结构关系，正式分镜写可执行画面与声音。保留来源、修改范围和保护字段，未知事实或结果反馈原规划。",
        "review": "使用当前层级的完成标准；大纲检查Brief、视频类型和模板结构，正式分镜再核对可拍性、声画与预算。引用原文说明实际不符合项，不把缺少动作细节当作大纲质量问题。",
    },
    "duration_policy": "已确认总时长与明确固定时长始终保留。大纲按表达目的取舍内容，不要求每项秒数；正式拆镜时才细分预算并核对实际内容支撑。原案例没有秒数时标为未知，不设固定层数、子内容数、镜数或单镜秒数。",
}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def method_revision_identity(evidence=None, *, compiled=None):
    """Both instructions and evidence participate in revision reuse."""
    if compiled is not None:
        return {"schema_version": compiled["schema_version"],
                "workflow_sha256": _digest(compiled["workflow"]),
                "fact_policy_sha256": _digest(compiled["fact_policy"]),
                "evidence_sha256": compiled["revision_identity"]["evidence_sha256"]}
    return {"schema_version": SCHEMA_VERSION, "workflow_sha256": _digest(_WORKFLOW),
            "fact_policy_sha256": _digest(_FACT_POLICY),
            "evidence_sha256": _digest(evidence) if evidence else ""}


def read_method_source(workbook_path):
    """Read cells and embedded-image identities without changing source files."""
    from openpyxl import load_workbook

    path = Path(workbook_path)
    workbook = load_workbook(path, read_only=False, data_only=False)
    sheets = []
    try:
        for name in _SOURCE_SHEETS:
            if name not in workbook.sheetnames:
                continue
            sheet = workbook[name]
            cells = {cell.coordinate: str(cell.value) for row in sheet.iter_rows()
                     for cell in row if cell.value is not None}
            images = []
            for embedded in sheet._images:
                anchor = getattr(embedded.anchor, "_from", None)
                if anchor is not None:
                    images.append({"anchor_row": anchor.row + 1, "anchor_col": anchor.col + 1,
                        "image_sha256": hashlib.sha256(embedded._data()).hexdigest()})
            sheets.append({"source_sheet": name, "cells": cells,
                "merged_ranges": sorted(str(item) for item in sheet.merged_cells.ranges),
                "images": sorted(images, key=lambda x: (x["anchor_row"], x["anchor_col"], x["image_sha256"]))})
    finally:
        workbook.close()
    return {"source_file": path.name, "workbook_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "source_content_sha256": _digest(sheets), "sheets": sheets}


def _text(item, key):
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("editing method requires " + key)
    return value


def _texts(item, key):
    value = item.get(key)
    if (not isinstance(value, list) or not value
            or any(not isinstance(text, str) or not text.strip() for text in value)):
        raise ValueError("editing method requires text list " + key)
    return value


def build_editing_methods(source_semantics, category_id, *, evidence=None, source_snapshot=None):
    """Bind explicit methods; absence keeps the original package usable."""
    source = source_semantics if isinstance(source_semantics, dict) else {}
    result = {"schema_version": SCHEMA_VERSION, "category_id": str(category_id),
        "status": "not_recorded", "methods": [], "workflow": deepcopy(_WORKFLOW),
        "fact_policy": deepcopy(_FACT_POLICY),
        "source_binding": {key: str(source.get(key) or "") for key in
                           ("source_file", "workbook_sha256", "original_workbook_sha256")},
        "revision_identity": method_revision_identity(evidence),
        "coverage": {"method_count": 0, "note": "本版本未附经来源核对的方法；原模板参考继续按原流程使用。"}}
    if evidence is None:
        return result
    if (not isinstance(evidence, dict) or evidence.get("schema_version") != EVIDENCE_SCHEMA_VERSION
            or evidence.get("category_id") != str(category_id)):
        raise ValueError("editing method evidence category/schema mismatch")
    if not isinstance(source_snapshot, dict):
        raise ValueError("editing methods require verified workbook source")
    original = evidence.get("source_workbook") or {}
    original_sha = _text(original, "sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", original_sha):
        raise ValueError("invalid editing method workbook digest")
    original_file = _text(original, "file_name")
    # Normalizing AI_规范表 can change the ZIP digest. Both human source sheets
    # must still match in text, merged cells and embedded image identities.
    content_sha = original.get("source_content_sha256")
    if original_sha != source_snapshot.get("workbook_sha256"):
        if not content_sha or content_sha != source_snapshot.get("source_content_sha256"):
            raise ValueError("editing method source workbook content changed")
    elif content_sha and content_sha != source_snapshot.get("source_content_sha256"):
        raise ValueError("editing method source content digest mismatch")
    sheets = {sheet["source_sheet"]: sheet for sheet in source_snapshot.get("sheets", [])}
    methods = evidence.get("methods")
    if not isinstance(methods, list) or not methods:
        raise ValueError("editing method evidence contains no reviewed methods")
    seen, verified = set(), []
    for method in methods:
        if not isinstance(method, dict):
            raise ValueError("invalid editing method")
        method_id = _text(method, "method_id")
        if method_id in seen:
            raise ValueError("duplicate editing method identity")
        seen.add(method_id)
        for key in ("purpose", "audience_change"):
            _text(method, key)
        for key in ("applies_when", "material_conditions", "guidance", "exceptions", "review_questions", "case_fact_boundary"):
            _texts(method, key)
        for key in ("short", "long", "boundary"):
            _text(method.get("duration_adaptation") or {}, key)
        if method.get("evidence_kind") not in {"source_explicit", "source_interpretation"}:
            raise ValueError("editing method must distinguish source and interpretation")
        sequence = method.get("sequence")
        if not isinstance(sequence, list) or len(sequence) < 2:
            raise ValueError("editing method requires an explained source sequence")
        supported_rows = set()
        for step in sequence:
            _text(step, "role")
            _text(step, "information_change")
            sheet = sheets.get(step.get("source_sheet"))
            rows = step.get("source_rows")
            if not sheet or not isinstance(rows, list) or not rows:
                raise ValueError("editing method sequence has no source location")
            for row in rows:
                if type(row) is not int or row < 1 or not any(
                        re.search(r"\d+$", cell).group() == str(row) for cell in sheet["cells"]):
                    raise ValueError("editing method sequence refers to an empty source row")
                supported_rows.add((step["source_sheet"], row))
        refs = method.get("evidence_refs")
        if not isinstance(refs, list) or not refs:
            raise ValueError("editing method requires verbatim source evidence")
        bound_refs, cited_rows = [], set()
        for ref in refs:
            sheet = sheets.get(ref.get("source_sheet"))
            quote, cell = _text(ref, "quote"), _text(ref, "cell")
            if not sheet or cell not in sheet["cells"] or quote not in sheet["cells"][cell]:
                raise ValueError("editing method quote does not match source cell")
            row = int(re.search(r"\d+$", cell).group())
            cited_rows.add((ref["source_sheet"], row))
            bound_refs.append({**deepcopy(ref), "source_file": original_file, "source_row": row,
                               "workbook_sha256": original_sha})
        if not supported_rows.issubset(cited_rows):
            raise ValueError("editing method sequence lacks cited row evidence")
        for visual in method.get("visual_evidence") or []:
            sheet = sheets.get(visual.get("source_sheet"))
            identity = {key: visual.get(key) for key in ("anchor_row", "anchor_col", "image_sha256")}
            if not sheet or identity not in sheet["images"]:
                raise ValueError("editing method image identity does not match embedded source")
            _text(visual, "observation")
            _text(visual, "limitation")
        verified.append({**deepcopy(method), "evidence_refs": bound_refs,
                         "fact_policy": "editorial_guidance_only"})
    result.update(status="evidence_bound", methods=verified,
        source_binding={**result["source_binding"], "evidence_source_file": original_file,
            "evidence_workbook_sha256": original_sha,
            "source_content_sha256": source_snapshot["source_content_sha256"]},
        source_issues=deepcopy(evidence.get("source_issues") or []),
        coverage={**deepcopy(evidence.get("coverage") or {}), "method_count": len(verified),
                  "note": "来源已校验；方法是条件化创作参考，模型实际效果仍需验证。"})
    return result
