from __future__ import annotations

import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from .excel_io import (
    _media_image_for_shot,
    _merged_headers,
    _project_media_dirs,
    _read_launch_workbook,
    _row_values,
    apply_creative_asset_review,
    apply_source_brand_review,
    load_creative_asset_review,
    load_source_brand_review,
)
from .models import SourceShot
from .launch_contract import template_count_valid
from .pipeline_runtime import publish_file_atomic
from .run_contract import sha256_file, sha256_json
from .run_state import exclusive_lock
from .source_policy import source_reuse_policy
from .script_type_adapter import normalize_script_type, resolve_script_type_workbook


AI_SPEC_SHEET = "AI_规范表"
AI_SPEC_SCHEMA_VERSION = "mplus-ai-spec/v2"
AI_SPEC_COLUMNS = [
    "schema_version",
    "record_id",
    "record_type",
    "record_status",
    "validation_codes",
    "source_hash",
    "slot_index",
    "shot_no",
    "structure",
    "shot_function",
    "form",
    "duration_seconds",
    "effect",
    "camera_type",
    "shot_size",
    "camera_move",
    "visual_desc",
    "visual_subject",
    "tags",
    "sound",
    "note",
    "reuse_level",
    "forbidden_note",
    "source_brand",
    "brand_neutral",
    "brand_neutral_evidence",
    "source_file",
    "source_sheet",
    "source_row",
    "reference_image_path",
    "effect_reference_path",
    "source_anchor",
    "source_brand_origin",
]
LEGACY_AI_SPEC_COLUMNS = tuple(AI_SPEC_COLUMNS)
SEMANTIC_COLUMNS = ("generic_desc", "brand_judgement_original", "sheet_overview",
                    "semantic_source_anchors", "source_semantics_version")
AI_SPEC_COLUMNS.extend(SEMANTIC_COLUMNS)

_RECORD_TYPES = {"template", "creative"}
_STATUSES = {"ready", "pending_review", "blocked"}
_VALID_REUSE_LEVELS = {
    "可复用",
    "镜头功能可复用",
    "需适配",
    "需改编",
    "限制使用",
    "禁用",
}
_RECOMMENDED_FIELDS = {
    "镜头功能": "missing_shot_function",
    "画面主体": "missing_visual_subject",
    "标签": "missing_tags",
}
_LIBRARY_NAMESPACES = (
    ("明星活动", "mplus-star-activity"),
    ("音乐节", "mplus-music-festival"),
    ("发布会", "mplus-launch"),
    ("展会", "mplus-exhibition"),
    ("年会", "mplus-annual"),
    ("赛事", "mplus-sports"),
)
_SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

ET.register_namespace("", _SPREADSHEET_NS)
ET.register_namespace("r", _OFFICE_REL_NS)


class AiSpecError(RuntimeError):
    pass


@dataclass(frozen=True)
class AiSpecRow:
    schema_version: str
    record_id: str
    record_type: str
    record_status: str
    validation_codes: str
    source_hash: str
    slot_index: int
    shot_no: str
    structure: str
    shot_function: str
    form: str
    duration_seconds: str
    effect: str
    camera_type: str
    shot_size: str
    camera_move: str
    visual_desc: str
    visual_subject: str
    tags: str
    sound: str
    note: str
    reuse_level: str
    forbidden_note: str
    source_brand: str
    brand_neutral: bool
    brand_neutral_evidence: str
    source_file: str
    source_sheet: str
    source_row: int
    reference_image_path: str
    effect_reference_path: str
    source_anchor: str
    source_brand_origin: str = ""
    generic_desc: str = ""
    brand_judgement_original: str = ""
    sheet_overview: str = ""
    semantic_source_anchors: str = ""
    source_semantics_version: str = ""


@dataclass(frozen=True)
class AiSpecReport:
    workbook_path: Path
    in_sync: bool
    usable: bool
    template_count: int
    creative_count: int
    ready_count: int
    pending_review_count: int
    blocked_count: int
    validation_codes: tuple[str, ...]
    rows: tuple[AiSpecRow, ...]
    changed: bool = False


def build_ai_spec_rows(workbook_path: str | Path, *, include_brand_origin: bool = False,
                       include_semantics: bool = False) -> list[AiSpecRow]:
    path = _require_workbook(workbook_path)
    sheet_meta = _source_sheet_metadata(path)
    # A legacy inferred source brand is frozen metadata, not the upload folder's name.
    # Explicit human-sheet values remain authoritative; all row fields and hashes
    # are still rebuilt and compared below, so source edits remain out of sync.
    stored, stored_errors = _read_ai_spec_rows(path)
    stored_brands = {} if stored_errors else {
        (row.record_type, row.source_sheet, row.source_row): row
        for row in stored if row.source_file == path.name
    }
    template_shots = _read_launch_workbook(path, "模版", [])
    template_shots = [
        replace(shot, shot_no=shot.shot_no or str(index), slot_index=index)
        for index, shot in enumerate(template_shots, start=1)
    ]
    creative_shots = _read_launch_workbook(path, "创意库", [])

    rows: list[AiSpecRow] = []
    for record_type, shots in (("template", template_shots), ("creative", creative_shots)):
        for shot in shots:
            meta = sheet_meta.get(shot.source_sheet, {})
            raw = meta.get("rows", {}).get(shot.source_row, {})
            key = (record_type, shot.source_sheet, shot.source_row)
            explicit_brand = bool(str(raw.get("来源品牌", "")).strip())
            stored_row = stored_brands.get(key)
            origin_known = include_brand_origin or stored_row is None or bool(stored_row.source_brand_origin)
            if not explicit_brand and stored_row and stored_row.source_brand_origin == "inferred":
                shot = replace(shot, source_brand=stored_row.source_brand)
            if stored_row:
                # Source row plus actual bytes prove identity. Retain portable paths
                # after packaging; external extraction caches are only transport.
                stable_images = {}
                for field in ("reference_image_path", "effect_reference_path"):
                    previous = _resolved_paths(path, getattr(stored_row, field))
                    extracted = getattr(shot, field)
                    left, right = _split_paths(previous), _split_paths(extracted)
                    if left and len(left) == len(right) and all(
                        Path(a).is_file() and Path(b).is_file() and sha256_file(a) == sha256_file(b)
                        for a, b in zip(left, right)
                    ):
                        stable_images[field] = previous
                if stable_images:
                    shot = replace(shot, **stable_images)
            semantic_known = include_semantics or stored_row is None or bool(stored_row.source_semantics_version)
            built = _row_from_shot(path, record_type, shot, meta, include_semantics=semantic_known)
            rows.append(replace(built, source_brand_origin=(
                "explicit" if explicit_brand else "inferred"
            ) if origin_known else ""))

    counts: dict[str, int] = {}
    for row in rows:
        counts[row.record_id] = counts.get(row.record_id, 0) + 1
    return [
        _add_blocking_code(row, "duplicate_record_id")
        if counts[row.record_id] > 1 else row
        for row in rows
    ]


def check_ai_spec(workbook_path: str | Path) -> AiSpecReport:
    path = _require_workbook(workbook_path)
    expected = build_ai_spec_rows(path)
    actual, structure_codes = _read_ai_spec_rows(path)
    in_sync = not structure_codes and _rows_equal(expected, actual)
    codes = list(structure_codes)
    if not structure_codes and not in_sync:
        codes.append("ai_spec_out_of_sync")
    return _report(path, expected, in_sync, codes)


def refresh_ai_spec(workbook_path: str | Path) -> AiSpecReport:
    path = _require_workbook(workbook_path)
    lock_path = Path(f"{path}.ai-spec.lock")
    with exclusive_lock(lock_path):
        current = check_ai_spec(path)
        if current.in_sync and all(row.source_brand_origin and row.source_semantics_version for row in current.rows):
            return current

        expected = build_ai_spec_rows(path, include_brand_origin=True, include_semantics=True)
        original_hash = sha256_file(path)
        original_mode = path.stat().st_mode & 0o7777
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.stem}.ai-spec.", suffix=path.suffix, dir=path.parent
        )
        os.close(fd)
        temp_path = Path(temp_name)
        backup_path = Path(f"{path}.ai-spec.bak")
        replaced = False
        try:
            shutil.copyfile(path, temp_path)
            _write_ai_spec_sheet(temp_path, expected)
            os.chmod(temp_path, original_mode)
            _validate_saved_ai_spec(temp_path, expected)
            if sha256_file(path) != original_hash:
                raise AiSpecError("刷新期间原工作簿发生变化，已取消替换")
            publish_file_atomic(path, backup_path)
            os.chmod(backup_path, original_mode)
            os.replace(temp_path, path)
            replaced = True
            refreshed = check_ai_spec(path)
            if not refreshed.in_sync:
                raise AiSpecError("刷新后的AI_规范表复验失败")
            return replace(refreshed, changed=True)
        except Exception:
            if replaced:
                publish_file_atomic(backup_path, path)
                os.chmod(path, original_mode)
            if sha256_file(path) != original_hash:
                raise AiSpecError("刷新失败，且原工作簿回滚校验失败")
            raise
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def load_ai_spec_shots(
    source_root: str | Path,
    brand_review_path: str | Path | None = None,
    project_hint: str = "",
    script_type: str = "发布会",
    workbook_path: str | Path | None = None,
) -> tuple[list[SourceShot], list[SourceShot]]:
    root = Path(source_root)
    normalized_type = normalize_script_type(script_type)
    path = Path(workbook_path) if workbook_path else resolve_script_type_workbook(root, normalized_type)
    media_dirs = _project_media_dirs(root, project_hint) if normalized_type == "发布会" else []
    report = check_ai_spec(path)
    if not report.in_sync:
        raise AiSpecError("AI_规范表与人工表不同步，请先执行 refresh")
    if not report.usable:
        summary = ", ".join(report.validation_codes) or "存在不可用记录"
        raise AiSpecError(f"AI_规范表未通过质量门禁: {summary}")

    template: list[SourceShot] = []
    creative: list[SourceShot] = []
    sheet_meta = _source_sheet_metadata(path)
    for row in report.rows:
        if row.record_status != "ready":
            continue
        shot = _shot_from_row(
            path,
            row,
            media_dirs,
            sheet_meta.get(row.source_sheet, {}),
            script_type=normalized_type,
        )
        if row.record_type == "template":
            template.append(shot)
        else:
            creative.append(shot)
    if brand_review_path:
        creative = apply_source_brand_review(creative, load_source_brand_review(brand_review_path))
        creative = apply_creative_asset_review(creative, load_creative_asset_review(brand_review_path))
    return template, creative


def _row_from_shot(path: Path, record_type: str, shot: SourceShot, meta: dict, *, include_semantics: bool = True) -> AiSpecRow:
    raw = meta.get("rows", {}).get(shot.source_row, {})
    headers = meta.get("headers", set())
    visual_col = int(meta.get("visual_col", 0) or 0)
    raw_reuse = str(raw.get("可复用等级", "")).strip()
    reuse_level = shot.reuse_level
    blocking: list[str] = []
    pending: list[str] = []
    if not shot.shot_no.strip():
        blocking.append("missing_shot_no")
    if not shot.structure.strip():
        blocking.append("missing_structure")
    if not shot.visual_desc.strip():
        blocking.append("missing_visual_desc")
    if "可复用等级" in headers and raw_reuse and raw_reuse not in _VALID_REUSE_LEVELS:
        blocking.append("invalid_reuse_level")
    if not reuse_level or reuse_level not in _VALID_REUSE_LEVELS:
        blocking.append("invalid_reuse_level")
    if reuse_level == "禁用" and not shot.forbidden_note.strip():
        blocking.append("forbidden_note_required")
    if shot.brand_neutral and not shot.brand_neutral_evidence.strip():
        blocking.append("brand_neutral_evidence_required")

    reference_path = _portable_paths(path, shot.reference_image_path)
    effect_path = _portable_paths(path, shot.effect_reference_path)
    for value in (reference_path, effect_path):
        if value and not _paths_resolve(path, value):
            blocking.append("image_path_missing")
            break

    review_values = (
        shot.shot_no,
        shot.structure,
        shot.shot_function,
        shot.form,
        shot.duration_seconds,
        shot.effect,
        shot.camera_type,
        shot.shot_size,
        shot.camera_move,
        shot.visual_desc,
        shot.visual_subject,
        shot.tags,
        shot.sound,
        shot.note,
        shot.reuse_level,
        shot.forbidden_note,
        shot.source_brand,
        shot.brand_neutral_evidence,
    )
    advisory = []
    if any("待确认" in str(value) for value in review_values):
        advisory.append("review_hint_present")
    for header, code in _RECOMMENDED_FIELDS.items():
        if header in headers and not str(raw.get(header, "")).strip():
            pending.append(code)

    codes = tuple(dict.fromkeys(blocking + pending + advisory))
    status = "blocked" if blocking else ("pending_review" if pending else "ready")
    source_anchor = f"{path.name}#{shot.source_sheet}!R{shot.source_row}C{visual_col}"
    values = {
        "schema_version": AI_SPEC_SCHEMA_VERSION if include_semantics else "mplus-ai-spec/v1",
        "record_id": f"{_record_namespace(path)}:{record_type}:{shot.shot_no}",
        "record_type": record_type,
        "record_status": status,
        "validation_codes": "; ".join(codes),
        "source_hash": "",
        "slot_index": shot.slot_index if record_type == "template" else 0,
        "shot_no": shot.shot_no,
        "structure": shot.structure,
        "shot_function": shot.shot_function,
        "form": shot.form,
        "duration_seconds": shot.duration_seconds,
        "effect": shot.effect,
        "camera_type": shot.camera_type,
        "shot_size": shot.shot_size,
        "camera_move": shot.camera_move,
        "visual_desc": shot.visual_desc,
        "visual_subject": shot.visual_subject,
        "tags": shot.tags,
        "sound": shot.sound,
        "note": shot.note,
        "reuse_level": reuse_level,
        "forbidden_note": shot.forbidden_note,
        "source_brand": shot.source_brand,
        "brand_neutral": shot.brand_neutral,
        "brand_neutral_evidence": shot.brand_neutral_evidence,
        "source_file": shot.source_file,
        "source_sheet": shot.source_sheet,
        "source_row": shot.source_row,
        "reference_image_path": reference_path,
        "effect_reference_path": effect_path,
        "source_anchor": source_anchor,
    }
    if include_semantics:
        values.update({column: getattr(shot, column) for column in SEMANTIC_COLUMNS})
    hash_values = {
        key: value for key, value in values.items()
        if key not in {"record_status", "validation_codes", "source_hash"}
    }
    values["source_hash"] = sha256_json(hash_values)
    return AiSpecRow(**values)


def _record_namespace(path: Path) -> str:
    for marker, namespace in _LIBRARY_NAMESPACES:
        if marker in path.name:
            return namespace
    return "mplus-generic"


def _add_blocking_code(row: AiSpecRow, code: str) -> AiSpecRow:
    codes = [value.strip() for value in row.validation_codes.split(";") if value.strip()]
    if code not in codes:
        codes.append(code)
    return replace(row, record_status="blocked", validation_codes="; ".join(codes))


def _source_sheet_metadata(path: Path) -> dict[str, dict]:
    workbook = load_workbook(path, read_only=True, data_only=False)
    result: dict[str, dict] = {}
    try:
        for sheet_name in ("模版", "创意库"):
            if sheet_name not in workbook.sheetnames:
                continue
            sheet = workbook[sheet_name]
            headers_list = _merged_headers(_row_values(sheet, 5), _row_values(sheet, 6))
            rows: dict[int, dict[str, str]] = {}
            empty_streak = 0
            for row_no, cells in enumerate(
                sheet.iter_rows(min_row=7, max_col=len(headers_list), values_only=True), start=7
            ):
                values = {
                    header: _cell_text(cells[index] if index < len(cells) else None)
                    for index, header in enumerate(headers_list)
                }
                if not values.get("画面描述", ""):
                    empty_streak += 1
                    if empty_streak >= 80:
                        break
                    continue
                empty_streak = 0
                rows[row_no] = values
            result[sheet_name] = {
                "headers": set(headers_list),
                "rows": rows,
                "visual_col": (
                    headers_list.index("画面描述") + 1 if "画面描述" in headers_list else 0
                ),
            }
    finally:
        workbook.close()
    return result


def _read_ai_spec_rows(path: Path) -> tuple[list[AiSpecRow], list[str]]:
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        if AI_SPEC_SHEET not in workbook.sheetnames:
            return [], ["ai_spec_sheet_missing"]
        sheet = workbook[AI_SPEC_SHEET]
        headers = [_cell_text(cell.value) for cell in sheet[1]]
        if headers not in (AI_SPEC_COLUMNS, [name for name in AI_SPEC_COLUMNS if name != 'source_brand_origin'],
                           list(LEGACY_AI_SPEC_COLUMNS), list(LEGACY_AI_SPEC_COLUMNS[:-1])):
            return [], ["ai_spec_headers_invalid"]
        rows = []
        for values in sheet.iter_rows(min_row=2, max_col=len(AI_SPEC_COLUMNS), values_only=True):
            if not any(value not in (None, "") for value in values):
                continue
            raw = dict(zip(headers, values))
            if any(isinstance(value, str) and value.startswith("=") for value in raw.values()):
                return [], ["ai_spec_formula_forbidden"]
            try:
                rows.append(_ai_row_from_mapping(raw))
            except (TypeError, ValueError):
                return [], ["ai_spec_row_invalid"]
        return rows, []
    finally:
        workbook.close()


def _ai_row_from_mapping(raw: dict) -> AiSpecRow:
    values = {column: _cell_text(raw.get(column)) for column in AI_SPEC_COLUMNS}
    values["slot_index"] = _integer(raw.get("slot_index"))
    values["source_row"] = _integer(raw.get("source_row"))
    values["brand_neutral"] = _boolean(raw.get("brand_neutral"))
    if values["record_type"] not in _RECORD_TYPES:
        raise ValueError("invalid record type")
    if values["record_status"] not in _STATUSES:
        raise ValueError("invalid record status")
    if values.get("source_brand_origin") not in {"", "explicit", "inferred"}:
        raise ValueError("invalid source brand origin")
    if values["schema_version"] not in {"mplus-ai-spec/v1", AI_SPEC_SCHEMA_VERSION}:
        raise ValueError("invalid schema version")
    return AiSpecRow(**values)


def _write_ai_spec_sheet(path: Path, rows: list[AiSpecRow]) -> None:
    with ZipFile(path, "r") as source:
        members = {info.filename: (info, source.read(info.filename)) for info in source.infolist()}
    workbook_root = ET.fromstring(members["xl/workbook.xml"][1])
    rels_root = ET.fromstring(members["xl/_rels/workbook.xml.rels"][1])
    content_types_root = ET.fromstring(members["[Content_Types].xml"][1])

    relationship_by_id = {
        relationship.get("Id", ""): relationship
        for relationship in rels_root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
    }
    sheets = workbook_root.find(f"{{{_SPREADSHEET_NS}}}sheets")
    if sheets is None:
        raise AiSpecError("工作簿缺少 sheets 定义")
    ai_sheet = next(
        (sheet for sheet in sheets if sheet.get("name") == AI_SPEC_SHEET), None
    )
    added_sheet = ai_sheet is None
    if ai_sheet is not None:
        relationship_id = ai_sheet.get(f"{{{_OFFICE_REL_NS}}}id", "")
        relationship = relationship_by_id.get(relationship_id)
        if relationship is None:
            raise AiSpecError("AI_规范表缺少工作簿关系")
        sheet_part = _worksheet_part_name(relationship.get("Target", ""))
    else:
        sheet_numbers = [
            int(match.group(1))
            for name in members
            if (match := re.fullmatch(r"xl/worksheets/sheet(\d+)\.xml", name))
        ]
        sheet_part = f"xl/worksheets/sheet{max(sheet_numbers, default=0) + 1}.xml"
        relationship_numbers = [
            int(match.group(1))
            for relationship_id in relationship_by_id
            if (match := re.fullmatch(r"rId(\d+)", relationship_id))
        ]
        relationship_id = f"rId{max(relationship_numbers, default=0) + 1}"
        sheet_ids = [int(sheet.get("sheetId", "0")) for sheet in sheets]
        ET.SubElement(
            sheets,
            f"{{{_SPREADSHEET_NS}}}sheet",
            {
                "name": AI_SPEC_SHEET,
                "sheetId": str(max(sheet_ids, default=0) + 1),
                f"{{{_OFFICE_REL_NS}}}id": relationship_id,
            },
        )
        ET.SubElement(
            rels_root,
            f"{{{_PACKAGE_REL_NS}}}Relationship",
            {
                "Id": relationship_id,
                "Type": f"{_OFFICE_REL_NS}/worksheet",
                "Target": sheet_part.removeprefix("xl/"),
            },
        )
        override_name = f"/{sheet_part}"
        has_override = any(
            item.get("PartName") == override_name
            for item in content_types_root.findall(f"{{{_CONTENT_TYPES_NS}}}Override")
        )
        if not has_override:
            ET.SubElement(
                content_types_root,
                f"{{{_CONTENT_TYPES_NS}}}Override",
                {
                    "PartName": override_name,
                    "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml",
                },
            )

    header_style = _existing_header_style(members, workbook_root, rels_root, sheet_part)
    replacements = {sheet_part: _build_ai_sheet_xml(rows, header_style)}
    if added_sheet:
        replacements.update({
            "xl/workbook.xml": ET.tostring(workbook_root, encoding="utf-8", xml_declaration=True),
            "xl/_rels/workbook.xml.rels": ET.tostring(rels_root, encoding="utf-8", xml_declaration=True),
            "[Content_Types].xml": ET.tostring(content_types_root, encoding="utf-8", xml_declaration=True),
        })
    sheet_rels = f"xl/worksheets/_rels/{Path(sheet_part).name}.rels"
    members.pop(sheet_rels, None)

    fd, output_name = tempfile.mkstemp(
        prefix=f".{path.stem}.package.", suffix=path.suffix, dir=path.parent
    )
    os.close(fd)
    output_path = Path(output_name)
    try:
        with ZipFile(output_path, "w") as output:
            for name, (info, data) in members.items():
                output.writestr(info, replacements.pop(name, data))
            for name, data in replacements.items():
                info = ZipInfo(name)
                info.compress_type = ZIP_DEFLATED
                output.writestr(info, data)
        os.replace(output_path, path)
    finally:
        try:
            output_path.unlink()
        except FileNotFoundError:
            pass


def _build_ai_sheet_xml(rows: list[AiSpecRow], header_style: str) -> bytes:
    root = ET.Element(f"{{{_SPREADSHEET_NS}}}worksheet")
    sheet_properties = ET.SubElement(root, f"{{{_SPREADSHEET_NS}}}sheetPr")
    ET.SubElement(sheet_properties, f"{{{_SPREADSHEET_NS}}}tabColor", {"rgb": "FF64748B"})
    ET.SubElement(
        root,
        f"{{{_SPREADSHEET_NS}}}dimension",
        {"ref": f"A1:{get_column_letter(len(AI_SPEC_COLUMNS))}{max(1, len(rows) + 1)}"},
    )
    views = ET.SubElement(root, f"{{{_SPREADSHEET_NS}}}sheetViews")
    view = ET.SubElement(
        views,
        f"{{{_SPREADSHEET_NS}}}sheetView",
        {"showGridLines": "0", "workbookViewId": "0"},
    )
    ET.SubElement(
        view,
        f"{{{_SPREADSHEET_NS}}}pane",
        {"ySplit": "1", "topLeftCell": "A2", "activePane": "bottomLeft", "state": "frozen"},
    )
    ET.SubElement(root, f"{{{_SPREADSHEET_NS}}}sheetFormatPr", {"defaultRowHeight": "15"})
    widths = [20, 34, 12, 16, 28, 20, 10, 10, 16, 18, 14, 14, 18, 14, 12, 14,
              46, 24, 24, 24, 30, 18, 30, 18, 14, 28, 28, 16, 12, 44, 44, 38]
    columns = ET.SubElement(root, f"{{{_SPREADSHEET_NS}}}cols")
    for index, width in enumerate(widths, start=1):
        ET.SubElement(
            columns,
            f"{{{_SPREADSHEET_NS}}}col",
            {"min": str(index), "max": str(index), "width": str(width), "customWidth": "1"},
        )
    sheet_data = ET.SubElement(root, f"{{{_SPREADSHEET_NS}}}sheetData")
    _append_xml_row(sheet_data, 1, dict(zip(AI_SPEC_COLUMNS, AI_SPEC_COLUMNS)), header_style)
    for row_index, row in enumerate(rows, start=2):
        _append_xml_row(sheet_data, row_index, asdict(row), "")
    ET.SubElement(
        root,
        f"{{{_SPREADSHEET_NS}}}autoFilter",
        {"ref": f"A1:{get_column_letter(len(AI_SPEC_COLUMNS))}{max(1, len(rows) + 1)}"},
    )
    ET.SubElement(root, f"{{{_SPREADSHEET_NS}}}sheetProtection", {"sheet": "1"})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _append_xml_row(parent, row_index: int, values: dict, style: str) -> None:
    attributes = {"r": str(row_index)}
    if row_index == 1:
        attributes.update({"ht": "24", "customHeight": "1"})
    row = ET.SubElement(parent, f"{{{_SPREADSHEET_NS}}}row", attributes)
    for column_index, name in enumerate(AI_SPEC_COLUMNS, start=1):
        value = values.get(name, "")
        cell_attributes = {"r": f"{get_column_letter(column_index)}{row_index}"}
        if style:
            cell_attributes["s"] = style
        if isinstance(value, bool):
            cell_attributes["t"] = "b"
            cell = ET.SubElement(row, f"{{{_SPREADSHEET_NS}}}c", cell_attributes)
            ET.SubElement(cell, f"{{{_SPREADSHEET_NS}}}v").text = "1" if value else "0"
        elif isinstance(value, int):
            cell = ET.SubElement(row, f"{{{_SPREADSHEET_NS}}}c", cell_attributes)
            ET.SubElement(cell, f"{{{_SPREADSHEET_NS}}}v").text = str(value)
        else:
            cell_attributes["t"] = "inlineStr"
            cell = ET.SubElement(row, f"{{{_SPREADSHEET_NS}}}c", cell_attributes)
            inline = ET.SubElement(cell, f"{{{_SPREADSHEET_NS}}}is")
            text = ET.SubElement(inline, f"{{{_SPREADSHEET_NS}}}t")
            if str(value).startswith(" ") or str(value).endswith(" "):
                text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            text.text = str(value)


def _existing_header_style(members: dict, workbook_root, rels_root, ai_sheet_part: str) -> str:
    candidate_parts = [ai_sheet_part]
    relationships = {
        item.get("Id", ""): _worksheet_part_name(item.get("Target", ""))
        for item in rels_root.findall(f"{{{_PACKAGE_REL_NS}}}Relationship")
        if item.get("Type", "").endswith("/worksheet")
    }
    sheets = workbook_root.find(f"{{{_SPREADSHEET_NS}}}sheets")
    if sheets is not None:
        for sheet in sheets:
            if sheet.get("name") == "模版":
                candidate_parts.append(relationships.get(sheet.get(f"{{{_OFFICE_REL_NS}}}id", ""), ""))
    for index, part in enumerate(candidate_parts):
        if not part or part not in members:
            continue
        root = ET.fromstring(members[part][1])
        references = ("A1",) if index == 0 else ("A5", "A1")
        for reference in references:
            cell = root.find(f".//{{{_SPREADSHEET_NS}}}c[@r='{reference}']")
            if cell is not None and cell.get("s"):
                return cell.get("s", "")
    return ""


def _worksheet_part_name(target: str) -> str:
    normalized = str(target or "").lstrip("/")
    if normalized.startswith("xl/"):
        return normalized
    return f"xl/{normalized}"


def _validate_saved_ai_spec(path: Path, expected: list[AiSpecRow]) -> None:
    actual, codes = _read_ai_spec_rows(path)
    if codes or not _rows_equal(expected, actual):
        raise AiSpecError(f"临时工作簿复验失败: {', '.join(codes) or 'rows_mismatch'}")


def _report(path: Path, rows: list[AiSpecRow], in_sync: bool, codes: list[str]) -> AiSpecReport:
    template = [row for row in rows if row.record_type == "template"]
    creative = [row for row in rows if row.record_type == "creative"]
    ready = sum(row.record_status == "ready" for row in rows)
    pending = sum(row.record_status == "pending_review" for row in rows)
    blocked = sum(row.record_status == "blocked" for row in rows)
    if not template_count_valid(len(template)):
        codes.append("template_count_zero")
    if any(row.record_status != "ready" for row in template):
        codes.append("template_not_all_ready")
    if any(row.record_status != "ready" for row in creative):
        codes.append("creative_not_all_ready")
    usable = in_sync and template_count_valid(len(template)) and pending == 0 and blocked == 0
    return AiSpecReport(
        workbook_path=path,
        in_sync=in_sync,
        usable=usable,
        template_count=len(template),
        creative_count=len(creative),
        ready_count=ready,
        pending_review_count=pending,
        blocked_count=blocked,
        validation_codes=tuple(dict.fromkeys(codes)),
        rows=tuple(rows),
    )


def _rows_equal(expected: list[AiSpecRow], actual: list[AiSpecRow]) -> bool:
    return [asdict(row) for row in expected] == [asdict(row) for row in actual]


def _shot_from_row(
    path: Path,
    row: AiSpecRow,
    media_dirs: list[Path] | None = None,
    source_meta: dict | None = None,
    script_type: str = "发布会",
) -> SourceShot:
    embedded_reference = _resolved_paths(path, row.reference_image_path)
    media_reference = _media_image_for_shot(media_dirs or [], row.shot_no)
    source_meta = source_meta or {}
    headers = set(source_meta.get("headers", set()))
    raw = source_meta.get("rows", {}).get(row.source_row, {})
    reuse_policy = source_reuse_policy(path.name, row.source_sheet)
    missing_metadata = [
        field for field in ("可复用等级", "禁用说明")
        if field not in headers and reuse_policy is None
    ]
    missing_metadata.extend(
        field for field in ("可复用等级", "禁用说明")
        if field in headers and not str(raw.get(field, "")).strip()
    )
    return SourceShot(
        script_type=normalize_script_type(script_type),
        source_file=row.source_file,
        source_sheet=row.source_sheet,
        source_row=row.source_row,
        shot_no=row.shot_no,
        structure=row.structure,
        duration_seconds=row.duration_seconds,
        effect=row.effect,
        camera_type=row.camera_type,
        shot_size=row.shot_size,
        camera_move=row.camera_move,
        visual_desc=row.visual_desc,
        sound=row.sound,
        note=row.note,
        reference_image_path=embedded_reference or media_reference,
        effect_reference_path=_resolved_paths(path, row.effect_reference_path),
        reference_image_evidence=(
            "源表嵌图" if embedded_reference
            else ("项目编号回退" if media_reference else "")
        ),
        effect_reference_evidence="源表嵌图" if row.effect_reference_path else "",
        reuse_level=row.reuse_level,
        forbidden_note=row.forbidden_note,
        tags=row.tags,
        shot_function=row.shot_function,
        visual_subject=row.visual_subject,
        source_brand=row.source_brand,
        brand_neutral=row.brand_neutral,
        brand_neutral_evidence=row.brand_neutral_evidence,
        form=row.form,
        missing_metadata=tuple(dict.fromkeys(missing_metadata)),
        slot_index=row.slot_index,
        **{column: getattr(row, column) for column in SEMANTIC_COLUMNS},
    )


def _portable_paths(workbook_path: Path, value: str) -> str:
    output = []
    root = workbook_path.parent.resolve()
    for item in _split_paths(value):
        candidate = Path(item)
        if not candidate.is_absolute() or "#" in item and "!R" in item:
            output.append(item)
            continue
        try:
            output.append(candidate.resolve().relative_to(root).as_posix())
        except ValueError:
            output.append(str(candidate))
    return "; ".join(output)


def _resolved_paths(workbook_path: Path, value: str) -> str:
    output = []
    for item in _split_paths(value):
        candidate = Path(item)
        if candidate.is_absolute() or "#" in item and "!R" in item:
            output.append(item)
        else:
            output.append(str((workbook_path.parent / candidate).absolute()))
    return "; ".join(output)


def _paths_resolve(workbook_path: Path, value: str) -> bool:
    for item in _split_paths(value):
        if "#" in item and "!R" in item:
            continue
        candidate = Path(item)
        if not candidate.is_absolute():
            candidate = workbook_path.parent / candidate
        if not candidate.exists():
            return False
    return True


def _split_paths(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(";") if item.strip()]


def _require_workbook(workbook_path: str | Path) -> Path:
    path = Path(workbook_path).expanduser().absolute()
    if not path.is_file():
        raise AiSpecError(f"工作簿不存在: {path}")
    if path.suffix.lower() != ".xlsx":
        raise AiSpecError(f"仅支持 .xlsx 工作簿: {path}")
    return path


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _integer(value) -> int:
    if value in (None, ""):
        return 0
    return int(float(value))


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "是", "y"}
