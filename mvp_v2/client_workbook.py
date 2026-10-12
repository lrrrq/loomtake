from __future__ import annotations

import re
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font


CLIENT_SHEETS = ("脚本", "待确认事项")
PENDING_COLUMNS = ("镜号", "缺失事实", "需要谁确认", "当前状态")
SENSITIVE_PATH_PATTERN = re.compile(r"(?:/Applications/|/Users/|[A-Za-z]:\\)")
INTERNAL_HEADERS = {
    "来源文件",
    "来源 sheet",
    "来源行",
    "机器分数",
    "run_id",
    "audit_domain",
}
INTERNAL_PROCESS_PHRASES = (
    "Brief要求",
    "Brief必选",
    "Brief规划内容",
    "AI生成",
    "AI合成",
    "AI制作",
    "AI画面",
    "候选方案",
    "候选镜头",
    "候选来源",
)


class ClientWorkbookError(ValueError):
    pass


def write_client_review_workbook(
    internal_workbook_path: str | Path,
    output_path: str | Path,
    pending_confirmations: list[dict] | None = None,
) -> Path:
    source = Path(internal_workbook_path)
    output = Path(output_path)
    if not source.is_file():
        raise ClientWorkbookError(f"内审工作簿不存在: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook = load_workbook(source)
    try:
        if "脚本" not in workbook.sheetnames:
            raise ClientWorkbookError("内审工作簿缺少 脚本 sheet")
        for sheet_name in list(workbook.sheetnames):
            if sheet_name != "脚本":
                del workbook[sheet_name]
        _sanitize_script_sheet(workbook["脚本"])
        pending_sheet = workbook.create_sheet("待确认事项")
        for column, header in enumerate(PENDING_COLUMNS, start=1):
            cell = pending_sheet.cell(1, column, header)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        for row_index, item in enumerate(pending_confirmations or [], start=2):
            values = (
                item.get("shot_no") or item.get("镜号") or "",
                item.get("item") or item.get("缺失事实") or "",
                item.get("owner") or item.get("需要谁确认") or "项目负责人",
                item.get("status") or item.get("当前状态") or "待确认",
            )
            for column, value in enumerate(values, start=1):
                cell = pending_sheet.cell(row_index, column, str(value))
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        pending_sheet.freeze_panes = "A2"
        for column, width in zip(("A", "B", "C", "D"), (10, 52, 20, 16)):
            pending_sheet.column_dimensions[column].width = width
        workbook.save(output)
    finally:
        workbook.close()
    validate_client_review_workbook(output)
    return output


def validate_client_review_workbook(path: str | Path) -> dict:
    workbook_path = Path(path)
    workbook = load_workbook(workbook_path, data_only=False)
    try:
        if tuple(workbook.sheetnames) != CLIENT_SHEETS:
            raise ClientWorkbookError(
                f"客户稿 sheet 必须且只能为: {', '.join(CLIENT_SHEETS)}"
            )
        formula_cells = []
        sensitive_cells = []
        internal_headers = []
        internal_process_cells = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    value = cell.value
                    text = str(value or "")
                    if cell.data_type == "f" or text.startswith("="):
                        formula_cells.append(f"{sheet.title}!{cell.coordinate}")
                    if SENSITIVE_PATH_PATTERN.search(text):
                        sensitive_cells.append(f"{sheet.title}!{cell.coordinate}")
                    if cell.row == 1 and text.strip() in INTERNAL_HEADERS:
                        internal_headers.append(text.strip())
                    if any(phrase in text for phrase in INTERNAL_PROCESS_PHRASES):
                        internal_process_cells.append(f"{sheet.title}!{cell.coordinate}")
        if formula_cells:
            raise ClientWorkbookError(f"客户稿包含公式: {formula_cells[:5]}")
        if sensitive_cells:
            raise ClientWorkbookError(f"客户稿包含本机绝对路径: {sensitive_cells[:5]}")
        if internal_headers:
            raise ClientWorkbookError(f"客户稿包含内部字段: {sorted(set(internal_headers))}")
        if internal_process_cells:
            raise ClientWorkbookError(
                f"客户稿包含内部过程语言: {internal_process_cells[:5]}"
            )
        return {
            "sheet_names": list(workbook.sheetnames),
            "script_image_count": len(workbook["脚本"]._images),
            "pending_count": max(workbook["待确认事项"].max_row - 1, 0),
        }
    finally:
        workbook.close()


def _sanitize_script_sheet(sheet) -> None:
    note_column = None
    note_header_row = None
    for row in sheet.iter_rows():
        for cell in row:
            if str(cell.value or "").strip() == "备注":
                note_column = cell.column
                note_header_row = cell.row
                break
        if note_column is not None:
            break
    if note_column is None or note_header_row is None:
        return
    for row_index in range(note_header_row + 1, sheet.max_row + 1):
        cell = sheet.cell(row_index, note_column)
        text = str(cell.value or "")
        if not text:
            continue
        segments = [part.strip() for part in re.split(r"[；;]", text)]
        visible = [
            part for part in segments
            if part and not part.startswith(("Brief必选", "Brief规划内容"))
        ]
        cell.value = "；".join(visible)
