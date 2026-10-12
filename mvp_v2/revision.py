from __future__ import annotations

from hashlib import sha256
import json
import re
from pathlib import Path

from openpyxl import load_workbook

from .run_contract import sha256_file
from .run_state import write_json_atomic


REVISION_STATUSES = {"需修改", "不通过"}
REVIEW_SHEET_ORDER = ("剪辑复核", "相邻镜头复核", "整片复核")


def write_revision_constraints(
    review_workbook: str | Path,
    output_path: str | Path,
    *,
    parent_run_id: str,
) -> Path:
    review = Path(review_workbook)
    constraints = extract_revision_constraints(review)
    user_feedback = [_as_user_feedback(item) for item in constraints]
    review_sha256 = sha256_file(review)
    confirmed_review_requirements = [
        _confirmed_review_requirement(item, review, review_sha256)
        for item in constraints
        if item["category"] in {"brand", "fact", "structure"}
    ]
    exclude_visuals_by_slot = {}
    for item in constraints:
        if item["category"] != "image" or not item["shot_no"]:
            continue
        excluded = [
            value for value in (item["current_image_id"], item["current_image_path"])
            if value
        ]
        if excluded:
            exclude_visuals_by_slot[item["shot_no"]] = excluded
    output = Path(output_path)
    write_json_atomic(output, {
        "schema_version": "mplus-ui-revision-constraints/v1",
        "source_schema_version": "mplus-revision-constraints/v1",
        "parent_run_id": parent_run_id,
        "parent_review_workbook": str(review.resolve()),
        "parent_review_sha256": review_sha256,
        "constraint_count": len(constraints),
        "constraints": constraints,
        "restart_stage": _restart_stage(user_feedback),
        "user_feedback": user_feedback,
        "confirmed_review_requirements": confirmed_review_requirements,
        "exclude_visuals_by_slot": exclude_visuals_by_slot,
    })
    return output


def extract_revision_constraints(review_workbook: str | Path) -> list[dict]:
    from .excel_io import read_managed_sheet_rows

    workbook = load_workbook(review_workbook, read_only=True, data_only=True)
    try:
        script_baseline = _script_baseline(workbook)
        rows = []
        for sheet_name in REVIEW_SHEET_ORDER:
            if sheet_name not in workbook.sheetnames:
                continue
            for row_index, values in enumerate(read_managed_sheet_rows(workbook, sheet_name), start=2):
                row = {name: str(value or "").strip() for name, value in values.items()}
                review_columns = [
                    name for name in row
                    if name.endswith("审核") or name in {"总体审核", "叙事弧", "节奏曲线", "音乐曲线", "高潮与收束"}
                ]
                declared_statuses = {
                    row[name]
                    for name in review_columns
                }
                declared_statuses.discard("")
                statuses = declared_statuses & REVISION_STATUSES
                comment = row.get("修改意见", "")
                shot_no = row.get("镜号", "")
                changes = {}
                if sheet_name == "剪辑复核" and (script_baseline or not shot_no):
                    changes = _reviewed_changes(row, script_baseline.get(shot_no))
                if not statuses and not comment and not changes:
                    continue
                desired_change = "；".join(
                    f"{name}={value}" for name, value in changes.items()
                )
                effective_comment = comment or "复核表已修改镜头内容"
                rows.append({
                    "sheet": sheet_name,
                    "row": row_index,
                    "shot_no": shot_no,
                    "from_shot": row.get("前镜号", ""),
                    "to_shot": row.get("后镜号", ""),
                    "statuses": sorted(statuses) or ["复核提出"],
                    "declared_statuses": sorted(declared_statuses),
                    "issue_code": row.get("问题代码", ""),
                    "severity": row.get("问题严重级别", ""),
                    "comment": effective_comment,
                    "desired_change": desired_change,
                    "scope": "shot" if shot_no else "whole_film",
                    "category": _feedback_category(
                        sheet_name,
                        effective_comment,
                        changes,
                    ),
                    "current_image_id": row.get("image_id", ""),
                    "current_image_path": row.get("原图路径", ""),
                    "reviewer": row.get("审核人", ""),
                    "review_date": row.get("审核日期", ""),
                })
        return rows
    finally:
        workbook.close()


def _script_baseline(workbook) -> dict[str, dict[str, str]]:
    from .excel_io import read_managed_sheet_rows

    if "脚本" not in workbook.sheetnames:
        return {}
    sheet = workbook["脚本"]
    header_row = 0
    structure_column = 0
    shot_column = 0
    content_column = 0
    sound_column = 0
    for row_index in range(1, min(sheet.max_row, 12) + 1):
        values = [str(cell.value or "").strip() for cell in sheet[row_index]]
        if "镜号" not in values or "结构" not in values:
            continue
        header_row = row_index
        structure_column = values.index("结构") + 1
        shot_column = values.index("镜号") + 1
        if "内容" in values:
            content_column = values.index("内容") + 1
        if "声音" in values:
            sound_column = values.index("声音") + 1
        break
    if not header_row:
        return {}
    detail_header_row = 0
    for row_index in range(header_row, min(sheet.max_row, header_row + 3) + 1):
        values = [str(cell.value or "").strip() for cell in sheet[row_index]]
        if "画面描述" in values:
            content_column = values.index("画面描述") + 1
            detail_header_row = row_index
        if "声音" in values:
            sound_column = values.index("声音") + 1
            detail_header_row = max(detail_header_row, row_index)
    baseline = {}
    current_structure = ""
    start_row = max(header_row, detail_header_row) + 1
    for row_index in range(start_row, sheet.max_row + 1):
        structure = str(sheet.cell(row_index, structure_column).value or "").strip()
        if structure:
            current_structure = structure
        shot_no = str(sheet.cell(row_index, shot_column).value or "").strip()
        if not shot_no:
            continue
        baseline[shot_no] = {
            "结构": current_structure,
            "画面描述": (
                str(sheet.cell(row_index, content_column).value or "").strip()
                if content_column else ""
            ),
            "声音": (
                str(sheet.cell(row_index, sound_column).value or "").strip()
                if sound_column else ""
            ),
        }
    for row in read_managed_sheet_rows(workbook, "脚本"):
        shot_no = str(row.get("镜号") or "").strip()
        if shot_no in baseline:
            baseline[shot_no].update({name: str(row[name] or "").strip()
                                      for name in ("结构", "画面描述", "声音") if name in row})
    return baseline


def _reviewed_changes(
    row: dict[str, str],
    baseline: dict[str, str] | None,
) -> dict[str, str]:
    current = {
        name: row.get(name, "")
        for name in ("结构", "画面描述", "声音")
    }
    if baseline is None:
        return {name: value for name, value in current.items() if value}
    return {
        name: value
        for name, value in current.items()
        if value and _normalized_cell(value) != _normalized_cell(baseline.get(name, ""))
    }


def _normalized_cell(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _feedback_category(sheet_name: str, comment: str, changes: dict[str, str]) -> str:
    text = " ".join((comment, *changes.values()))
    if re.search(r"配图|图片|图像|参考图|原图", text):
        return "image"
    if re.search(r"品牌|logo|标识", text, re.IGNORECASE):
        return "brand"
    if (
        sheet_name in {"相邻镜头复核", "整片复核"}
        or "结构" in changes
        or re.search(r"结构|顺序|开头|开场|结尾|收束|重复|少了|缺少|遗漏|流程|环节|仪式|发言", text)
    ):
        return "structure"
    if re.search(r"事实|名字|名称|身份|地点|日期|数字", text):
        return "fact"
    return "copy"


def _as_user_feedback(item: dict) -> dict:
    return {
        "scope": item["scope"],
        "shot_no": item["shot_no"],
        "category": item["category"],
        "comment": item["comment"],
        "desired_change": item["desired_change"],
        "keep_fields": [],
        "selected_image_id": "",
        "selected_image_path": "",
        "supplemental_image_path": "",
        "approve_text_only": False,
    }


def _confirmed_review_requirement(item: dict, review: Path, review_sha256: str) -> dict:
    text = "；".join(filter(None, (item.get("comment"), item.get("desired_change"))))
    identity = json.dumps({
        "review_sha256": review_sha256,
        "sheet": item.get("sheet"),
        "row": item.get("row"),
        "shot_no": item.get("shot_no"),
        "text": text,
    }, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "requirement_id": f"review_{sha256(identity).hexdigest()[:16]}",
        "text": text,
        "scope": item.get("scope"),
        "shot_no": item.get("shot_no"),
        "category": item.get("category"),
        "classification": (
            "position_constraint"
            if item.get("category") == "structure"
            and "开头" in text and "空镜" in text
            else "review_preference"
        ),
        "decision_source": (
            "template_contract_review"
            if item.get("category") == "structure"
            and "开头" in text and "空镜" in text
            else "confirmed_review"
        ),
        "evidence_refs": [{
            "type": "review_workbook",
            "source": {
                "file": review.name,
                "sheet": item.get("sheet"),
                "row": item.get("row"),
                "sha256": review_sha256,
            },
        }],
    }


def _restart_stage(feedback: list[dict]) -> str:
    stages = {
        "brand": "script_plan",
        "fact": "script_plan",
        "structure": "script_plan",
        "style": "script_draft",
        "detail": "script_draft",
        "copy": "script_draft",
        "image": "visual_binding",
    }
    order = {"script_plan": 0, "script_draft": 1, "visual_binding": 2}
    values = [stages[item["category"]] for item in feedback]
    return min(values, key=order.__getitem__) if values else ""
