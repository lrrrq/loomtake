from __future__ import annotations

import json
import hashlib
import shutil
from pathlib import Path

from openpyxl import load_workbook

from .pipeline_runtime import (
    MANIFEST_FILENAME,
    resolve_current_release,
    resolve_pipeline_run,
)
from .run_contract import sha256_file, sha256_json


PROVENANCE_SHEET_NAME = "复核来源"
PROVENANCE_SCHEMA_VERSION = "stage2-review-copy/v2"
LEGACY_PROVENANCE_SCHEMA_VERSION = "stage2-review-copy/v1"
EDITABLE_REVIEW_SHEETS = frozenset({"剪辑复核", "相邻镜头复核", "整片复核"})
EDITABLE_REVIEW_COLUMNS = {
    "剪辑复核": frozenset({
        "结构审核", "叙事审核", "节奏审核", "创意审核", "配图审核", "总体审核",
        "重大修改", "问题代码", "问题严重级别", "修改意见", "审核人", "审核日期",
    }),
    "相邻镜头复核": frozenset({
        "叙事承接", "动作衔接", "运镜衔接", "色彩衔接", "声音衔接", "总体审核",
        "问题代码", "问题严重级别", "修改意见", "审核人", "审核日期",
    }),
    "整片复核": frozenset({
        "叙事弧", "节奏曲线", "音乐曲线", "高潮与收束", "总体审核",
        "问题代码", "问题严重级别", "修改意见", "审核人", "审核日期",
    }),
}


class ReviewPackageError(ValueError):
    pass


def export_review_workbook(
    cache_dir: str | Path,
    output_path: str | Path,
    *,
    run_id: str | None = None,
) -> Path:
    from io import BytesIO

    from .excel_io import _preserve_workbook_package

    cache, selected_run_id = _normalize_cache_run(cache_dir, run_id)
    release = _resolve_run(cache, selected_run_id) if selected_run_id else _resolve_release(cache)
    source = release["workbook"]
    run_dir = source.parent
    output = Path(output_path).resolve()
    if output == source.resolve() or output.parent == run_dir.resolve():
        raise ReviewPackageError("复核副本必须写到正式 run 目录之外")
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, output)

    immutable_hash = _immutable_workbook_sha256(source)
    manifest_path = run_dir / MANIFEST_FILENAME
    manifest = _read_json(manifest_path)
    workbook = load_workbook(output)
    try:
        if PROVENANCE_SHEET_NAME in workbook.sheetnames:
            del workbook[PROVENANCE_SHEET_NAME]
        sheet = workbook.create_sheet(PROVENANCE_SHEET_NAME)
        provenance = {
            "schema_version": PROVENANCE_SCHEMA_VERSION,
            "project_name": str(manifest.get("project_name") or ""),
            "source_cache_dir": str(cache),
            "source_run_id": run_dir.name,
            "source_workbook_path": str(source),
            "source_workbook_sha256": sha256_file(source),
            "machine_audit_sha256": sha256_file(release["audit"]),
            "source_manifest_sha256": sha256_file(manifest_path),
            "immutable_content_sha256": immutable_hash,
        }
        _set_text_cell(sheet.cell(1, 1), "字段")
        _set_text_cell(sheet.cell(1, 2), "值")
        for row, (key, value) in enumerate(provenance.items(), start=2):
            _set_text_cell(sheet.cell(row, 1), key)
            _set_text_cell(sheet.cell(row, 2), value)
        sheet.freeze_panes = "A2"
        sheet.column_dimensions["A"].width = 32
        sheet.column_dimensions["B"].width = 72
        rendered = BytesIO()
        workbook.save(rendered)
        output.write_bytes(_preserve_workbook_package(source, rendered.getvalue(), {PROVENANCE_SHEET_NAME}))
    finally:
        workbook.close()
    return output


def validate_review_workbook(
    cache_dir: str | Path,
    review_path: str | Path,
    release_snapshot: dict[str, Path] | None = None,
    *,
    run_id: str | None = None,
) -> dict:
    cache, selected_run_id = _normalize_cache_run(cache_dir, run_id)
    review = Path(review_path).resolve()
    if not review.is_file():
        raise ReviewPackageError(f"复核副本不存在: {review}")
    if release_snapshot is not None and run_id is not None:
        raise ReviewPackageError("release_snapshot 与 run_id 不能同时提供")
    release = (
        release_snapshot
        if release_snapshot is not None
        else (_resolve_run(cache, selected_run_id) if selected_run_id else _resolve_release(cache))
    )
    source = release["workbook"]
    run_dir = source.parent
    provenance = _read_provenance(review)
    schema_version = provenance.get("schema_version")
    if schema_version not in {
        PROVENANCE_SCHEMA_VERSION,
        LEGACY_PROVENANCE_SCHEMA_VERSION,
    }:
        raise ReviewPackageError("复核副本来源合同版本无效")
    if provenance.get("source_run_id") != run_dir.name:
        raise ReviewPackageError("复核副本不是当前 run")
    if Path(provenance.get("source_cache_dir") or "").resolve() != cache:
        raise ReviewPackageError("复核副本不属于当前项目缓存")
    if Path(provenance.get("source_workbook_path") or "").resolve() != source.resolve():
        raise ReviewPackageError("复核副本记录的源工作簿路径不一致")
    if provenance.get("source_workbook_sha256") != sha256_file(source):
        raise ReviewPackageError("复核副本记录的源工作簿哈希不一致")
    if schema_version == PROVENANCE_SCHEMA_VERSION:
        if provenance.get("machine_audit_sha256") != sha256_file(release["audit"]):
            raise ReviewPackageError("复核副本记录的机器审计哈希不一致")
    manifest_path = run_dir / MANIFEST_FILENAME
    if provenance.get("source_manifest_sha256") != sha256_file(manifest_path):
        raise ReviewPackageError("复核副本记录的 run manifest 哈希不一致")
    _validate_review_project_names(review, provenance.get("project_name") or "")
    immutable_hash = _immutable_workbook_sha256(review)
    if provenance.get("immutable_content_sha256") != immutable_hash:
        raise ReviewPackageError("复核副本的不可编辑内容已被修改")
    if _immutable_workbook_sha256(source) != immutable_hash:
        raise ReviewPackageError("复核副本与当前源工作簿内容不一致")
    return {
        **provenance,
        "review_workbook": str(review),
        "source_workbook": str(source),
    }


def _immutable_workbook_sha256(path: Path) -> str:
    from .excel_io import VERSION_ADDITIONS_SHEET

    workbook = load_workbook(path, read_only=False, data_only=False)
    try:
        records = []
        for sheet in workbook.worksheets:
            if sheet.title == PROVENANCE_SHEET_NAME:
                continue
            headers = {
                cell.column: str(cell.value or "")
                for cell in sheet[1]
            }
            editable_columns = EDITABLE_REVIEW_COLUMNS.get(sheet.title, frozenset())
            cells = []
            for row in sheet.iter_rows():
                editable_addition = False
                if sheet.title == VERSION_ADDITIONS_SHEET and row[0].row > 1 and len(row) >= 4:
                    target, record, field = (cell.value for cell in row[:3])
                    editable_addition = (
                        field in EDITABLE_REVIEW_COLUMNS.get(target, frozenset())
                        and target in workbook.sheetnames
                        and isinstance(record, int) and not isinstance(record, bool)
                        and 1 <= record < workbook[target].max_row
                    )
                for cell in row:
                    record = {
                        "coordinate": cell.coordinate,
                        "style_id": cell.style_id,
                        "hyperlink": str(cell.hyperlink.target) if cell.hyperlink else "",
                        "comment": str(cell.comment.text) if cell.comment else "",
                    }
                    if (cell.row == 1 or headers.get(cell.column) not in editable_columns) and not (
                        editable_addition and cell.column == 4
                    ):
                        record.update({
                            "data_type": cell.data_type,
                            "value": _stable_cell_value(cell.value),
                        })
                    cells.append(record)
            records.append({
                "sheet": sheet.title,
                "max_row": sheet.max_row,
                "max_column": sheet.max_column,
                "merged_ranges": sorted(str(value) for value in sheet.merged_cells.ranges),
                "cells": cells,
                "images": [_image_record(image) for image in sheet._images],
            })
        return sha256_json(records)
    finally:
        workbook.close()


def _stable_cell_value(value) -> str:
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except TypeError:
            pass
    return str(value)


def _image_record(image) -> dict:
    anchor = image.anchor
    if hasattr(anchor, "_from"):
        anchor_value = {"from": _anchor_marker(anchor._from)}
        if hasattr(anchor, "to"):
            anchor_value["to"] = _anchor_marker(anchor.to)
        if hasattr(anchor, "ext"):
            anchor_value["ext"] = [anchor.ext.cx, anchor.ext.cy]
    else:
        anchor_value = str(anchor)
    return {
        "anchor": anchor_value,
        "width": image.width,
        "height": image.height,
        "sha256": hashlib.sha256(image._data()).hexdigest(),
    }


def _anchor_marker(marker) -> list[int]:
    return [marker.col, marker.row, marker.colOff, marker.rowOff]


def _set_text_cell(cell, value) -> None:
    cell.value = str(value or "")
    cell.data_type = "s"


def _read_provenance(path: Path) -> dict:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if PROVENANCE_SHEET_NAME not in workbook.sheetnames:
            raise ReviewPackageError("复核副本缺少来源 sheet")
        sheet = workbook[PROVENANCE_SHEET_NAME]
        return {
            str(row[0].value or ""): str(row[1].value or "")
            for row in sheet.iter_rows(min_row=2, max_col=2)
            if row[0].value
        }
    finally:
        workbook.close()


def _validate_review_project_names(path: Path, expected_project: str) -> None:
    from .excel_io import read_managed_sheet_rows

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet_name in EDITABLE_REVIEW_SHEETS:
            if sheet_name not in workbook.sheetnames:
                raise ReviewPackageError(f"复核副本缺少 {sheet_name} sheet")
            rows = read_managed_sheet_rows(workbook, sheet_name)
            if any("项目名称" not in row for row in rows):
                raise ReviewPackageError(f"{sheet_name} 缺少项目名称列")
            values = {
                str(row.get("项目名称") or "").strip() for row in rows
            }
            if values != {expected_project}:
                raise ReviewPackageError(f"{sheet_name} 的项目名称与来源不一致")
    finally:
        workbook.close()


def _resolve_release(cache: Path) -> dict[str, Path]:
    try:
        return resolve_current_release(cache)
    except RuntimeError as exc:
        raise ReviewPackageError(f"项目 CURRENT 无法验证: {cache}: {exc}") from exc


def _resolve_run(cache: Path, run_id: str) -> dict[str, Path]:
    try:
        return resolve_pipeline_run(
            cache,
            run_id,
            allowed_statuses={"blocked", "waiting_human_review", "succeeded", "completed"},
            artifact_names={"workbook", "audit"},
        )
    except RuntimeError as exc:
        raise ReviewPackageError(
            f"待人工审核 run 无法验证: {run_id}: {exc}"
        ) from exc


def _normalize_cache_run(
    cache_dir: str | Path, run_id: str | None
) -> tuple[Path, str | None]:
    candidate = Path(cache_dir).resolve()
    if run_id is not None:
        return candidate, run_id
    if (candidate / MANIFEST_FILENAME).is_file() and candidate.parent.name == "runs":
        return candidate.parent.parent, candidate.name
    return candidate, None


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewPackageError(f"run manifest 不可读取: {path}") from exc
    if not isinstance(value, dict):
        raise ReviewPackageError(f"run manifest 必须是对象: {path}")
    return value
