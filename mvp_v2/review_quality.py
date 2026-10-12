from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

from .editorial_gate import (
    ADJACENT_REVIEW_COLUMNS,
    ADJACENT_REVIEW_DIMENSIONS,
    ADJACENT_REVIEW_SHEET_NAME,
    FULL_FILM_REVIEW_COLUMNS,
    FULL_FILM_REVIEW_DIMENSIONS,
    FULL_FILM_REVIEW_SHEET_NAME,
    VALID_REVIEW_STATUSES,
    build_stage1_editorial_gate,
)
from .quality_contract import (
    compute_human_review_status,
    compute_review_metrics,
    thresholds_for_shot_count,
)


REVIEW_SHEET_NAME = "剪辑复核"
REVIEW_DIMENSIONS = {
    "structure": "结构审核",
    "narrative": "叙事审核",
    "rhythm": "节奏审核",
    "creative": "创意审核",
    "visual": "配图审核",
}
OVERALL_REVIEW_COLUMN = "总体审核"
EDITORIAL_LEGACY_COLUMNS = [
    "项目名称",
    "slot_index",
    "镜号",
    "结构",
    "时长(秒)",
    "画面描述",
    "声音",
    "image_id",
    *REVIEW_DIMENSIONS.values(),
    OVERALL_REVIEW_COLUMN,
    "修改意见",
    "审核人",
    "审核日期",
]
EDITORIAL_REVIEW_COLUMNS = [
    *EDITORIAL_LEGACY_COLUMNS[:2],
    "逻辑槽位",
    *EDITORIAL_LEGACY_COLUMNS[2:8],
    "原图路径",
    "预览图",
    *EDITORIAL_LEGACY_COLUMNS[8:14],
    "重大修改",
    "问题代码",
    "问题严重级别",
    *EDITORIAL_LEGACY_COLUMNS[14:],
]
EDITORIAL_OPTIONAL_COLUMNS = [
    "逻辑槽位",
    "重大修改",
    "问题代码",
    "问题严重级别",
    "修改意见",
    "审核人",
    "审核日期",
]


class ReviewWorkbookError(ValueError):
    pass


def build_editorial_review_rows(
    script_rows: list[dict],
    detail_rows: list[dict],
    visual_asset_rows: list[dict] | None = None,
) -> list[dict]:
    details_by_shot = {
        str(row.get("模板镜号") or row.get("slot_index") or ""): row
        for row in detail_rows
    }
    assets_by_shot = {
        str(row.get("镜号") or row.get("slot_index") or ""): row
        for row in (visual_asset_rows or [])
    }
    rows = []
    for index, script in enumerate(script_rows, start=1):
        shot_no = str(script.get("镜号") or index)
        detail = details_by_shot.get(shot_no, {})
        asset = assets_by_shot.get(shot_no, {})
        rows.append({
            "项目名称": detail.get("项目名称", ""),
            "slot_index": detail.get("slot_index", index),
            "逻辑槽位": detail.get("逻辑槽位", ""),
            "镜号": shot_no,
            "结构": script.get("结构", ""),
            "时长(秒)": script.get("时长(秒)", ""),
            "画面描述": script.get("画面描述", ""),
            "声音": script.get("声音", ""),
            "image_id": asset.get("image_id", ""),
            "原图路径": asset.get("原图路径", ""),
            "预览图": asset.get("原图路径", ""),
            **{column: "" for column in REVIEW_DIMENSIONS.values()},
            OVERALL_REVIEW_COLUMN: "",
            "重大修改": "",
            "问题代码": "",
            "问题严重级别": "",
            "修改意见": "",
            "审核人": "",
            "审核日期": "",
        })
    return rows


def analyze_review_workbooks(paths: list[str | Path]) -> dict:
    if not paths:
        raise ReviewWorkbookError("至少提供一个审核工作簿")
    projects = [_analyze_workbook(Path(path)) for path in paths]
    if any(project["status"] == "invalid" for project in projects):
        status = "invalid"
        human_review_status = "invalid"
    elif any(project["status"] == "pending_review" for project in projects):
        status = "pending_review"
        human_review_status = _human_review_status(projects)
    else:
        status = "complete"
        human_review_status = _human_review_status(projects)
    return {
        "schema_version": "v0.2.1",
        "status": status,
        "human_review_status": human_review_status,
        "project_count": len(projects),
        "projects": projects,
        "summary": _summary(projects),
        "rule_change_candidates": _rule_change_candidates(projects),
        "rule_application": "manual_review_required",
    }


def write_review_report(path: str | Path, report: dict) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output


def _analyze_workbook(path: Path) -> dict:
    if not path.exists():
        raise ReviewWorkbookError(f"审核工作簿不存在: {path}")
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if REVIEW_SHEET_NAME not in wb.sheetnames:
            raise ReviewWorkbookError(f"工作簿缺少 {REVIEW_SHEET_NAME} sheet: {path}")
        rows = _read_rows(
            wb[REVIEW_SHEET_NAME],
            REVIEW_SHEET_NAME,
            EDITORIAL_LEGACY_COLUMNS,
            EDITORIAL_OPTIONAL_COLUMNS,
            ("镜号", "画面描述", "项目名称"),
        )
        adjacent_rows = []
        if ADJACENT_REVIEW_SHEET_NAME in wb.sheetnames:
            adjacent_rows = _read_rows(
                wb[ADJACENT_REVIEW_SHEET_NAME],
                ADJACENT_REVIEW_SHEET_NAME,
                ADJACENT_REVIEW_COLUMNS,
                (),
                ("边", "项目名称"),
            )
        film_rows = []
        if FULL_FILM_REVIEW_SHEET_NAME in wb.sheetnames:
            film_rows = _read_rows(
                wb[FULL_FILM_REVIEW_SHEET_NAME],
                FULL_FILM_REVIEW_SHEET_NAME,
                FULL_FILM_REVIEW_COLUMNS,
                (),
                ("项目名称", "总体审核"),
            )
    finally:
        wb.close()
    return _analyze_rows(path, rows, adjacent_rows, film_rows)


def _analyze_rows(
    path: Path,
    rows: list[dict],
    adjacent_rows: list[dict] | None = None,
    film_rows: list[dict] | None = None,
) -> dict:
    project_name = next((_text(row.get("项目名称")) for row in rows if _text(row.get("项目名称"))), path.stem)
    invalid_reviews = []
    pending_count = 0
    reviewed_count = 0
    provenance_missing = 0
    incomplete_review_count = 0
    overall_counts = _empty_counts()
    dimension_counts = {name: _empty_counts() for name in REVIEW_DIMENSIONS}
    issues = []
    review_columns = [*REVIEW_DIMENSIONS.values(), OVERALL_REVIEW_COLUMN]

    for excel_row, row in enumerate(rows, start=2):
        invalid_in_row = False
        values = {}
        for column in review_columns:
            value = _text(row.get(column))
            values[column] = value
            if value and value not in VALID_REVIEW_STATUSES:
                invalid_in_row = True
                invalid_reviews.append({
                    "row": excel_row,
                    "shot_no": _text(row.get("镜号")),
                    "column": column,
                    "value": value,
                })
        entered_statuses = [
            value for value in values.values()
            if value in VALID_REVIEW_STATUSES
        ]
        complete = not invalid_in_row and bool(entered_statuses)
        review_complete = complete
        if review_complete:
            reviewed_count += 1
        else:
            pending_count += 1
            incomplete_review_count += 1

        overall_status = _aggregate_review_status(entered_statuses)
        if overall_status in VALID_REVIEW_STATUSES:
            _increment(overall_counts, overall_status)
        for dimension, column in REVIEW_DIMENSIONS.items():
            dimension_status = values[column]
            if dimension_status in VALID_REVIEW_STATUSES:
                _increment(dimension_counts[dimension], dimension_status)
                if dimension_status != "通过":
                    issues.append({
                        "project_name": project_name,
                        "slot_index": _slot_value(row.get("slot_index")),
                        "shot_no": _text(row.get("镜号")),
                        "dimension": dimension,
                        "status": dimension_status,
                        "comment": _text(row.get("修改意见")),
                        "issue_code": _text(row.get("问题代码")),
                        "severity": _text(row.get("问题严重级别")),
                    })

    total = len(rows)
    issues.extend(_contract_issues(
        project_name, adjacent_rows or [], ADJACENT_REVIEW_DIMENSIONS, "adjacent", "边"
    ))
    issues.extend(_contract_issues(
        project_name, film_rows or [], FULL_FILM_REVIEW_DIMENSIONS, "full_film", "项目名称"
    ))
    status = "invalid" if invalid_reviews else ("pending_review" if incomplete_review_count else "complete")
    quality_metrics = compute_review_metrics(rows)
    declared_shot_count = _declared_shot_count(rows, adjacent_rows or [])
    thresholds = thresholds_for_shot_count(
        declared_shot_count,
        tuple(quality_metrics.get("_required_key_slots") or ()),
    )
    base_human_review_status = (
        "invalid"
        if invalid_reviews
        else compute_human_review_status(quality_metrics, thresholds)
    )
    stage1_editorial_gate = build_stage1_editorial_gate(
        rows,
        base_human_review_status,
        adjacent_rows or [],
        film_rows or [],
        review_columns,
    )
    human_review_status = stage1_editorial_gate["overall_status"]
    status = "invalid" if human_review_status == "invalid" else (
        "pending_review" if human_review_status == "pending" else "complete"
    )
    return {
        "workbook": str(path),
        "project_name": project_name,
        "status": status,
        "quality_metrics": quality_metrics,
        "human_review_status": human_review_status,
        "total_shot_count": total,
        "duration_seconds": _duration_total(rows),
        "reviewed_shot_count": reviewed_count,
        "pending_shot_count": pending_count,
        "review_coverage": _ratio(reviewed_count, total),
        "overall": _with_rates(overall_counts),
        "dimensions": {name: _with_rates(counts) for name, counts in dimension_counts.items()},
        "manual_modification_rate": _ratio(
            overall_counts["needs_revision_count"] + overall_counts["rejected_count"],
            reviewed_count,
        ),
        "image_acceptance_rate": _acceptance_rate(dimension_counts["visual"]),
        "structure_adjustment_rate": _adjustment_rate(dimension_counts["structure"]),
        "review_provenance_missing_count": provenance_missing,
        "invalid_review_count": len(invalid_reviews),
        "invalid_reviews": invalid_reviews,
        "issues": issues,
        "stage1_editorial_gate": stage1_editorial_gate,
    }


def _summary(projects: list[dict]) -> dict:
    total = sum(project["total_shot_count"] for project in projects)
    reviewed = sum(project["reviewed_shot_count"] for project in projects)
    pending = sum(project["pending_shot_count"] for project in projects)
    approved = sum(project["overall"]["approved_count"] for project in projects)
    needs_revision = sum(project["overall"]["needs_revision_count"] for project in projects)
    rejected = sum(project["overall"]["rejected_count"] for project in projects)
    return {
        "total_shot_count": total,
        "reviewed_shot_count": reviewed,
        "pending_shot_count": pending,
        "review_coverage": _ratio(reviewed, total),
        "approved_count": approved,
        "needs_revision_count": needs_revision,
        "rejected_count": rejected,
        "manual_modification_rate": _ratio(needs_revision + rejected, reviewed),
        "review_provenance_missing_count": sum(
            project["review_provenance_missing_count"] for project in projects
        ),
        "invalid_review_count": sum(project["invalid_review_count"] for project in projects),
    }


def _aggregate_review_status(statuses) -> str:
    values = set(statuses)
    if "不通过" in values:
        return "不通过"
    if "需修改" in values:
        return "需修改"
    if "通过" in values:
        return "通过"
    return ""


def _human_review_status(projects: list[dict]) -> str:
    statuses = {project["human_review_status"] for project in projects}
    if "pending" in statuses:
        return "pending"
    if "quality_fail" in statuses:
        return "quality_fail"
    return "quality_pass"


def _rule_change_candidates(projects: list[dict]) -> list[dict]:
    grouped = defaultdict(list)
    for project in projects:
        for issue in project["issues"]:
            grouped[(str(issue["slot_index"]), issue["dimension"])].append(issue)
    candidates = []
    for (_, dimension), issues in grouped.items():
        projects_by_name = {}
        for issue in issues:
            projects_by_name.setdefault(issue["project_name"], issue)
        if len(projects_by_name) < 2:
            continue
        evidence = [
            {
                "project_name": name,
                "shot_no": issue["shot_no"],
                "status": issue["status"],
                "comment": issue["comment"],
            }
            for name, issue in sorted(projects_by_name.items())
        ]
        candidates.append({
            "slot_index": issues[0]["slot_index"],
            "dimension": dimension,
            "project_count": len(projects_by_name),
            "projects": sorted(projects_by_name),
            "evidence": evidence,
            "action": "进入规则评审，不自动修改槽位知识",
        })
    return sorted(candidates, key=lambda row: (str(row["slot_index"]), row["dimension"]))


def _empty_counts() -> dict:
    return {"approved_count": 0, "needs_revision_count": 0, "rejected_count": 0}


def _increment(counts: dict, status: str) -> None:
    key = {"通过": "approved_count", "需修改": "needs_revision_count", "不通过": "rejected_count"}[status]
    counts[key] += 1


def _with_rates(counts: dict) -> dict:
    reviewed = sum(counts.values())
    return {
        **counts,
        "reviewed_count": reviewed,
        "acceptance_rate": _ratio(counts["approved_count"], reviewed),
    }


def _acceptance_rate(counts: dict) -> float:
    return _ratio(counts["approved_count"], sum(counts.values()))


def _adjustment_rate(counts: dict) -> float:
    return _ratio(counts["needs_revision_count"] + counts["rejected_count"], sum(counts.values()))


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _duration_total(rows: list[dict]) -> int | float:
    total = 0.0
    for row in rows:
        try:
            total += float(row.get("时长(秒)") or 0)
        except (TypeError, ValueError):
            return -1
    return int(total) if total.is_integer() else round(total, 4)


def _contract_issues(
    project_name: str,
    rows: list[dict],
    dimensions: tuple[str, ...],
    scope: str,
    label_key: str,
) -> list[dict]:
    issues = []
    for row in rows:
        for dimension in dimensions:
            status = _text(row.get(dimension))
            if status and status != "通过":
                issues.append({
                    "project_name": project_name,
                    "slot_index": f"{scope}:{_text(row.get(label_key))}",
                    "shot_no": _text(row.get(label_key)),
                    "dimension": dimension,
                    "status": status,
                    "comment": _text(row.get("修改意见")),
                    "issue_code": _text(row.get("问题代码")),
                    "severity": _text(row.get("问题严重级别")),
                })
    return issues


def _slot_value(value):
    text = _text(value)
    return int(text) if text.isdigit() else text


def _declared_shot_count(rows: list[dict], adjacent_rows: list[dict]) -> int:
    edge_targets = []
    for row in adjacent_rows:
        match = re.fullmatch(r"(\d+)->(\d+)", _text(row.get("边")))
        if match:
            edge_targets.append(int(match.group(2)))
    return max([len(rows), *edge_targets], default=0)


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _read_rows(
    ws,
    sheet_name: str,
    required_columns: list[str],
    optional_columns: list[str] | tuple[str, ...],
    row_keys: tuple[str, ...],
) -> list[dict]:
    from .excel_io import read_managed_sheet_rows

    headers = {_text(cell.value) for cell in ws[1]}
    merged_rows = read_managed_sheet_rows(ws.parent, sheet_name)
    headers.update(column for row in merged_rows for column in row)
    missing = [column for column in required_columns if column not in headers]
    if missing:
        raise ReviewWorkbookError(f"{sheet_name} 缺少字段: {', '.join(missing)}")
    rows = []
    for row in merged_rows:
        for column in optional_columns:
            row.setdefault(column, "")
        if any(_text(row.get(key)) for key in row_keys):
            rows.append(row)
    return rows
