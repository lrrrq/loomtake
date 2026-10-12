from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

from .models import SourceShot

INPUT_COLUMNS = [
    "项目名称",
    "项目区域文件名",
    "脚本类型",
    "品牌或客户",
    "行业或活动类别",
    "视频时长",
    "风格调性",
    "必须出现的内容",
    "禁忌或不能出现的内容",
    "素材目录或素材文件",
    "备注",
]

REQUIRED_INPUT_COLUMNS = [
    "项目名称",
    "项目区域文件名",
    "脚本类型",
    "视频时长",
    "风格调性",
    "必须出现的内容",
]

OUTPUT_COLUMNS = [
    "项目名称",
    "需求行号",
    "镜号",
    "结构",
    "时长(秒)",
    "画面示意",
    "效果示意",
    "效果",
    "景别",
    "运镜",
    "画面描述",
    "声音",
    "备注",
    "参考图路径",
    "效果示意路径",
    "来源文件",
    "来源sheet",
    "来源行",
    "匹配理由",
    "审核结果",
    "修改意见",
]

IMAGE_ANCHOR_RE = re.compile(r"^(?P<file>.+)#(?P<sheet>.+)!R(?P<row>\d+)C(?P<col>\d+)$")


class BriefError(ValueError):
    pass


def cell_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def read_briefs(path: str | Path) -> list[dict]:
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    headers = [cell_text(cell.value) for cell in ws[1]]
    missing = [name for name in REQUIRED_INPUT_COLUMNS if name not in headers]
    if missing:
        wb.close()
        raise BriefError(f"缺少必填字段: {', '.join(missing)}")

    index = {name: headers.index(name) for name in headers if name}
    briefs = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(cell_text(v) for v in row):
            continue
        brief = {name: cell_text(row[index[name]]) if index[name] < len(row) else "" for name in INPUT_COLUMNS}
        missing_values = [name for name in REQUIRED_INPUT_COLUMNS if not brief.get(name)]
        if missing_values:
            wb.close()
            raise BriefError(f"第 {row_no} 行缺少必填值: {', '.join(missing_values)}")
        brief["input_row"] = row_no
        briefs.append(brief)
    wb.close()
    return briefs


def load_source_shots(source_root: str | Path) -> list[SourceShot]:
    root = Path(source_root)
    specs = [
        ("年会", root / "M+年会类通用脚本库.xlsx", None, 3, 4, 5),
        ("展会", root / "M+展会类通用脚本库.xlsx", None, 3, 4, 5),
        ("赛事", root / "M+赛事类通用脚本库.xlsx", None, 3, 4, 5),
        ("音乐节", root / "M+音乐节通用脚本库.xlsx", None, 3, 4, 5),
        ("发布会", root / "M+发布会类通用脚本库.xlsx", "模版", 5, 6, 7),
        ("发布会", root / "M+发布会类通用脚本库.xlsx", "创意库", 5, 6, 7),
        ("发布会", root / "发布会类" / "家电-发布会-【美的智美万象发布会】分镜脚本.xlsx", None, 5, 6, 7),
    ]
    shots = []
    for scene_type, path, sheet, top_header_row, sub_header_row, data_start in specs:
        if not path.exists():
            continue
        shots.extend(_read_source_workbook(scene_type, path, sheet, top_header_row, sub_header_row, data_start))
    return shots


def _read_source_workbook(
    scene_type: str,
    path: Path,
    sheet_name: str | None,
    top_header_row: int,
    sub_header_row: int,
    data_start: int,
) -> list[SourceShot]:
    wb = load_workbook(path, read_only=False, data_only=True)
    ws = wb[sheet_name] if sheet_name else wb[wb.sheetnames[0]]
    top_headers = _row_values(ws, top_header_row)
    sub_headers = _row_values(ws, sub_header_row)
    headers = _merged_headers(top_headers, sub_headers)
    image_index = _image_anchor_index(path, ws)
    current_structure = ""
    shots = []

    empty_streak = 0
    for row_no, row in enumerate(ws.iter_rows(min_row=data_start, max_col=len(headers), values_only=True), start=data_start):
        values = {headers[col]: cell_text(row[col]) if col < len(row) else "" for col in range(len(headers))}
        description = values.get("画面描述", "")
        if not description:
            empty_streak += 1
            if empty_streak >= 80:
                break
            continue
        empty_streak = 0

        structure = values.get("结构", "")
        if structure:
            current_structure = structure
        else:
            structure = current_structure

        shots.append(SourceShot(
            scene_type=scene_type,
            source_file=path.name,
            source_sheet=ws.title,
            source_row=row_no,
            shot_no=values.get("镜号", ""),
            structure=structure,
            form=values.get("形式", ""),
            duration_seconds=values.get("时长(秒)", ""),
            effect=values.get("效果", ""),
            shot_size=values.get("景别", ""),
            camera_movement=values.get("运镜", ""),
            description=description,
            audio=values.get("声音", ""),
            note=values.get("备注", ""),
            reference_image_path=_image_path_for_label(image_index, headers, row_no, "画面示意"),
            effect_reference_path=_image_path_for_label(image_index, headers, row_no, "效果示意"),
        ))
    wb.close()
    return shots


def _row_values(ws, row_no: int) -> list[str]:
    return [cell_text(value) for value in next(ws.iter_rows(min_row=row_no, max_row=row_no, values_only=True))]


def _merged_headers(top: list[str], sub: list[str]) -> list[str]:
    headers = []
    last_top = ""
    for top_name, sub_name in zip(top, sub):
        if top_name:
            last_top = top_name
        name = sub_name or top_name or last_top
        if name == "内容":
            name = ""
        headers.append(name)
    return headers


def _image_anchor_index(path: Path, ws) -> dict[tuple[int, int], str]:
    anchors = {}
    for img in getattr(ws, "_images", []):
        try:
            row = img.anchor._from.row + 1
            col = img.anchor._from.col + 1
        except Exception:
            continue
        anchors[(row, col)] = f"{path.name}#{ws.title}!R{row}C{col}"
    return anchors


def _image_path_for_label(image_index: dict[tuple[int, int], str], headers: list[str], row_no: int, label: str) -> str:
    values = []
    for col_idx, header in enumerate(headers, start=1):
        if header == label and (row_no, col_idx) in image_index:
            values.append(image_index[(row_no, col_idx)])
    return "; ".join(dict.fromkeys(values))


def write_output_workbook(path: str | Path, generated: list[dict], source_root: str | Path | None = None) -> None:
    output_path = Path(path)
    asset_dir = output_path.with_name(f"{output_path.stem}_assets") if source_root else None
    source_root_path = Path(source_root) if source_root else None
    source_file_cache = {}
    image_data_cache = {}
    wb = Workbook()
    ws = wb.active
    ws.title = "脚本"
    ws.append(OUTPUT_COLUMNS)
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    for item in generated:
        brief = item["brief"]
        for idx, shot in enumerate(item["shots"], start=1):
            ws.append([
                brief.get("项目名称", ""),
                brief.get("input_row", ""),
                idx,
                shot.get("structure", ""),
                shot.get("duration_seconds", ""),
                "",
                "",
                shot.get("effect", ""),
                shot.get("shot_size", ""),
                shot.get("camera_movement", ""),
                shot.get("description", ""),
                shot.get("audio", ""),
                shot.get("note", ""),
                _preview_paths(shot.get("reference_image_path", ""), source_root_path, asset_dir, source_file_cache, image_data_cache),
                _preview_paths(shot.get("effect_reference_path", ""), source_root_path, asset_dir, source_file_cache, image_data_cache),
                shot.get("source_file", ""),
                shot.get("source_sheet", ""),
                shot.get("source_row", ""),
                shot.get("match_reason", ""),
                "",
                "",
            ])

    for col in ws.columns:
        letter = col[0].column_letter
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 36)
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.font = Font(name="Arial")
    wb.save(output_path)


def _preview_paths(
    value: str,
    source_root: Path | None,
    asset_dir: Path | None,
    source_file_cache: dict[str, Path | None],
    image_data_cache: dict[tuple[Path, str], dict[tuple[int, int], tuple[bytes, str]]],
) -> str:
    if not value or not source_root or not asset_dir:
        return value
    converted = []
    for part in [item.strip() for item in value.split(";") if item.strip()]:
        converted.append(_extract_preview_path(part, source_root, asset_dir, source_file_cache, image_data_cache))
    return "; ".join(converted)


def _extract_preview_path(
    anchor: str,
    source_root: Path,
    asset_dir: Path,
    source_file_cache: dict[str, Path | None],
    image_data_cache: dict[tuple[Path, str], dict[tuple[int, int], tuple[bytes, str]]],
) -> str:
    match = IMAGE_ANCHOR_RE.match(anchor)
    if not match:
        return anchor

    source_file = match.group("file")
    sheet = match.group("sheet")
    row = int(match.group("row"))
    col = int(match.group("col"))
    source_path = _resolve_source_file(source_root, source_file, source_file_cache)
    if not source_path:
        return anchor

    cache_key = (source_path, sheet)
    if cache_key not in image_data_cache:
        image_data_cache[cache_key] = _load_image_data_index(source_path, sheet)
    image = image_data_cache[cache_key].get((row, col))
    if not image:
        return anchor

    data, ext = image
    asset_dir.mkdir(parents=True, exist_ok=True)
    target = asset_dir / f"{_safe_name(source_path.stem)}__{_safe_name(sheet)}__R{row}C{col}.{ext}"
    if not target.exists():
        target.write_bytes(data)
    return target.relative_to(asset_dir.parent).as_posix()


def _resolve_source_file(source_root: Path, source_file: str, cache: dict[str, Path | None]) -> Path | None:
    if source_file in cache:
        return cache[source_file]

    direct = source_root / source_file
    if direct.exists():
        cache[source_file] = direct
        return direct

    for candidate in source_root.rglob("*.xlsx"):
        if candidate.name == source_file and not candidate.name.startswith(".~"):
            cache[source_file] = candidate
            return candidate
    cache[source_file] = None
    return None


def _load_image_data_index(source_path: Path, sheet: str) -> dict[tuple[int, int], tuple[bytes, str]]:
    wb = load_workbook(source_path, read_only=False, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            return {}
        ws = wb[sheet]
        images = {}
        for img in getattr(ws, "_images", []):
            try:
                row = img.anchor._from.row + 1
                col = img.anchor._from.col + 1
                data = img._data()
            except Exception:
                continue
            images[(row, col)] = (data, _image_extension(data, getattr(img, "format", "")))
        return images
    finally:
        wb.close()


def _image_extension(data: bytes, image_format: str) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    if data.startswith(b"GIF8"):
        return "gif"
    fmt = (image_format or "").lower()
    if fmt in {"jpeg", "jpg"}:
        return "jpg"
    if fmt in {"png", "gif"}:
        return fmt
    return "png"


def _safe_name(value: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", value).strip("_") or "image"
