from __future__ import annotations

from .launch_contract import expected_adjacent_edges

VALID_REVIEW_STATUSES = ("通过", "需修改", "不通过")
VALID_MAJOR_CHANGE_VALUES = ("是", "否")
VALID_SEVERITIES = ("高", "中", "低")
VALID_ISSUE_CODES = frozenset({
    "STRUCTURE_GAP",
    "NARRATIVE_BRIDGE_BREAK",
    "RHYTHM_DRAG",
    "CREATIVE_MISMATCH",
    "SOUND_CURVE_COLLAPSE",
    "VISUAL_MISMATCH",
    "SOURCE_EVIDENCE_MISSING",
})

ADJACENT_REVIEW_SHEET_NAME = "相邻镜头复核"
FULL_FILM_REVIEW_SHEET_NAME = "整片复核"

ADJACENT_REVIEW_COLUMNS = [
    "项目名称",
    "边",
    "叙事承接",
    "动作衔接",
    "运镜衔接",
    "色彩衔接",
    "声音衔接",
    "总体审核",
    "问题代码",
    "问题严重级别",
    "修改意见",
    "审核人",
    "审核日期",
]
ADJACENT_REVIEW_DIMENSIONS = (
    "叙事承接",
    "动作衔接",
    "运镜衔接",
    "色彩衔接",
    "声音衔接",
    "总体审核",
)

FULL_FILM_REVIEW_COLUMNS = [
    "项目名称",
    "叙事弧",
    "节奏曲线",
    "音乐曲线",
    "高潮与收束",
    "总体审核",
    "问题代码",
    "问题严重级别",
    "修改意见",
    "审核人",
    "审核日期",
]
FULL_FILM_REVIEW_DIMENSIONS = (
    "叙事弧",
    "节奏曲线",
    "音乐曲线",
    "高潮与收束",
    "总体审核",
)

EXPECTED_ADJACENT_EDGES = tuple(f"{index}->{index + 1}" for index in range(1, 18))


def build_stage1_editorial_gate(
    editorial_rows: list[dict],
    base_per_shot_status: str,
    adjacent_rows: list[dict],
    film_rows: list[dict],
    editorial_review_columns: list[str],
) -> dict:
    per_shot_requirements = _validate_editorial_requirements(editorial_rows, editorial_review_columns)
    per_shot_status = _merge_per_shot_status(base_per_shot_status, per_shot_requirements["status"])
    row_edges = tuple(_text(row.get("边")) for row in adjacent_rows if _text(row.get("边")))
    required_edges = (
        EXPECTED_ADJACENT_EDGES
        if row_edges == EXPECTED_ADJACENT_EDGES
        else expected_adjacent_edges(len(editorial_rows))
    )
    adjacent = _validate_named_contract_rows(
        adjacent_rows,
        ADJACENT_REVIEW_DIMENSIONS,
        required_row_count=len(required_edges),
        row_label_key="边",
        allowed_row_labels=set(required_edges),
        label_name="edge",
    )
    film = _validate_named_contract_rows(
        film_rows,
        FULL_FILM_REVIEW_DIMENSIONS,
        required_row_count=1,
        row_label_key="项目名称",
        allowed_row_labels=None,
        label_name="project_name",
    )

    return {
        "overall_status": _aggregate_gate_status(
            per_shot_status,
            adjacent["status"],
            film["status"],
        ),
        "per_shot": {
            **per_shot_requirements,
            "status": per_shot_status,
        },
        "adjacent_pairs": adjacent,
        "full_film": film,
    }


def _validate_editorial_requirements(rows: list[dict], review_columns: list[str]) -> dict:
    pending_rows = []
    invalid_rows = []
    missing_major_change_rows = []
    missing_issue_metadata_rows = []
    invalid_issue_metadata_rows = []

    for excel_row, row in enumerate(rows, start=2):
        major_change = _text(row.get("重大修改"))
        if major_change and major_change not in VALID_MAJOR_CHANGE_VALUES:
            invalid_rows.append({"row": excel_row, "column": "重大修改", "value": major_change})

        metadata_status = _issue_metadata_status(row, review_columns)
        if metadata_status == "invalid":
            invalid_rows.append({
                "row": excel_row,
                "column": "问题代码/问题严重级别/修改意见",
                "value": {
                    "问题代码": _text(row.get("问题代码")),
                    "问题严重级别": _text(row.get("问题严重级别")),
                    "修改意见": _text(row.get("修改意见")),
                },
            })
            invalid_issue_metadata_rows.append(excel_row)

    status = "quality_pass"
    if invalid_rows:
        status = "invalid"
    elif pending_rows:
        status = "pending"
    return {
        "status": status,
        "row_count": len(rows),
        "pending_row_count": len(set(pending_rows)),
        "invalid_row_count": len(invalid_rows),
        "missing_major_change_rows": missing_major_change_rows,
        "missing_issue_metadata_rows": missing_issue_metadata_rows,
        "invalid_issue_metadata_rows": invalid_issue_metadata_rows,
        "invalid_rows": invalid_rows,
    }


def _validate_named_contract_rows(
    rows: list[dict],
    review_columns: tuple[str, ...],
    required_row_count: int,
    row_label_key: str,
    allowed_row_labels: set[str] | None,
    label_name: str,
) -> dict:
    seen_labels = set()
    duplicate_labels = []
    invalid_rows = []
    pending_rows = []
    quality_fail_rows = []

    for excel_row, row in enumerate(rows, start=2):
        label = _text(row.get(row_label_key))
        if not label:
            pending_rows.append(excel_row)
        elif allowed_row_labels is not None:
            if label not in allowed_row_labels:
                invalid_rows.append({"row": excel_row, label_name: label, "reason": "unexpected_label"})
            elif label in seen_labels:
                duplicate_labels.append(label)
                invalid_rows.append({"row": excel_row, label_name: label, "reason": "duplicate_label"})
            else:
                seen_labels.add(label)

        row_status = _evaluate_contract_row(row, review_columns)
        if row_status == "invalid":
            invalid_rows.append({"row": excel_row, label_name: label, "reason": "invalid_contract"})
        elif row_status == "pending":
            pending_rows.append(excel_row)
        elif row_status == "quality_fail":
            quality_fail_rows.append(excel_row)

    if allowed_row_labels is None:
        row_count_ok = len(rows) == required_row_count
        missing_labels = []
    else:
        missing_labels = sorted(allowed_row_labels - seen_labels)
        row_count_ok = not missing_labels and len(rows) == required_row_count
    if duplicate_labels:
        status = "invalid"
    elif invalid_rows:
        status = "invalid"
    elif pending_rows or not row_count_ok:
        status = "pending"
    elif quality_fail_rows:
        status = "quality_fail"
    else:
        status = "quality_pass"
    return {
        "status": status,
        "row_count": len(rows),
        "edge_count": len(seen_labels) if allowed_row_labels is not None else len(rows),
        "missing_labels": missing_labels,
        "duplicate_labels": duplicate_labels,
        "pending_row_count": len(set(pending_rows)),
        "invalid_row_count": len(invalid_rows),
        "quality_fail_row_count": len(quality_fail_rows),
        "invalid_rows": invalid_rows,
    }


def _merge_per_shot_status(base_status: str, requirements_status: str) -> str:
    if base_status == "invalid" or requirements_status == "invalid":
        return "invalid"
    if base_status == "pending" or requirements_status == "pending":
        return "pending"
    return base_status


def _evaluate_contract_row(row: dict, review_columns: tuple[str, ...]) -> str:
    has_non_pass = False
    has_review = False
    for column in review_columns:
        status = _text(row.get(column))
        if not status:
            continue
        if status not in VALID_REVIEW_STATUSES:
            return "invalid"
        has_review = True
        if status != "通过":
            has_non_pass = True

    if not has_review:
        return "pending"
    if has_non_pass:
        return "quality_fail"
    return "quality_pass"


def _issue_metadata_status(row: dict, review_columns) -> str:
    code = _text(row.get("问题代码"))
    severity = _text(row.get("问题严重级别"))
    if code and code not in VALID_ISSUE_CODES:
        return "invalid"
    if severity and severity not in VALID_SEVERITIES:
        return "invalid"
    return "quality_pass"


def _aggregate_gate_status(*statuses: str) -> str:
    if "invalid" in statuses:
        return "invalid"
    if "pending" in statuses:
        return "pending"
    if "quality_fail" in statuses:
        return "quality_fail"
    return "quality_pass"


def _text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()
