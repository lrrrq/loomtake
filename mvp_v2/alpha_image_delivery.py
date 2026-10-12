from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Iterable

from PIL import Image as PILImage
from openpyxl import load_workbook

from .audit import audit_output_workbook_format
from .excel_io import write_template_output_workbook


class AlphaImageDeliveryError(ValueError):
    pass


@dataclass(frozen=True)
class AlphaImageDelivery:
    workbook_path: Path
    workbook_sha256: str
    run_id: str
    version_id: str
    bindings: list[dict]


def create_alpha_image_delivery(
    root: str | Path,
    *,
    script_rows: Iterable[dict],
    image_decisions: Iterable[dict],
    run_id: str,
    version_id: str,
) -> AlphaImageDelivery:
    output_root = _isolated_root(root)
    rows = [dict(row) for row in script_rows]
    decisions_by_shot = _decision_by_shot(image_decisions)
    script_shots = _script_shots(rows)
    normalized_run_id = str(run_id).strip()
    normalized_version_id = str(version_id).strip()
    if not normalized_run_id or not normalized_version_id:
        raise AlphaImageDeliveryError("Alpha 交付必须绑定明确 run_id 与 version_id")
    if not rows:
        raise AlphaImageDeliveryError("Alpha 交付至少需要一镜脚本")

    bindings = []
    detail_rows = []
    if set(decisions_by_shot) != set(script_shots):
        raise AlphaImageDeliveryError("人工图片确认必须与脚本镜号一一对应")
    for index, (row, shot_no) in enumerate(zip(rows, script_shots), start=1):
        decision = decisions_by_shot.get(shot_no)
        if decision is None:
            raise AlphaImageDeliveryError(f"镜头 {shot_no} 缺少人工图片确认")
        image_path = _confirmed_image_path(decision, output_root)
        row["镜号"] = shot_no
        row["画面示意"] = str(image_path)
        row["效果示意"] = ""
        bindings.append({
            "shot_no": shot_no,
            "image_id": str(decision["image_id"]),
            "image_sha256": _sha256_file(image_path),
            "original_path": str(image_path),
            "human_confirmed_by": str(decision["confirmed_by"]),
            "human_confirmed_at": str(decision["confirmed_at"]),
            "run_id": normalized_run_id,
            "version_id": normalized_version_id,
            **_anonymous_source(decision),
        })
        detail_rows.append({
            "项目名称": "匿名 Alpha 项目",
            "slot_index": index,
            "采用来源文件": bindings[-1]["source_document_id"],
            "采用来源 sheet": bindings[-1]["source_sheet"],
            "采用来源行": bindings[-1]["source_row"],
            "image_id": decision["image_id"],
            "原图路径": str(image_path),
            "图片绑定说明": "Alpha 人工确认逐镜单图",
        })

    snapshot_key = hashlib.sha256(
        json.dumps(
            {
                "run_id": normalized_run_id,
                "version_id": normalized_version_id,
                "bindings": bindings,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    workbook_path = output_root / f"alpha-image-delivery-{snapshot_key}.xlsx"
    write_template_output_workbook(
        workbook_path,
        rows,
        detail_rows,
        visual_asset_rows=[{
            "slot_index": index,
            "镜号": item["shot_no"],
            "image_id": item["image_id"],
            "原图路径": item["original_path"],
            "配图判断": "人工确认",
            "配图置信度": "人工确认",
        } for index, item in enumerate(bindings, start=1)],
    )
    _clear_embedded_image_paths(workbook_path)
    checks = audit_output_workbook_format(workbook_path, expected_image_count=len(rows))
    failures = [
        item for item in checks
        if item.get("检查项") in {
            "output_workbook_unreadable",
            "script_sheet_missing",
            "embedded_image_count_mismatch",
            "image_path_not_embedded",
        }
        and item.get("级别") == "hard"
        and item.get("状态") == "fail"
    ]
    if failures:
        raise AlphaImageDeliveryError(
            f"Alpha Excel 交付校验失败: {failures[0]['检查项']}"
        )
    return AlphaImageDelivery(
        workbook_path,
        _sha256_file(workbook_path),
        normalized_run_id,
        normalized_version_id,
        bindings,
    )


def make_alpha_isolated_root() -> Path:
    return Path(tempfile.mkdtemp(prefix="mplus-alpha-"))


def _isolated_root(root: str | Path) -> Path:
    path = Path(root).resolve()
    if (
        path.parent != Path(tempfile.gettempdir()).resolve()
        or not path.name.startswith("mplus-alpha-")
    ):
        raise AlphaImageDeliveryError(
            "Alpha 写入目录必须是系统临时目录下 mplus-alpha-* 隔离根"
        )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _decision_by_shot(decisions: Iterable[dict]) -> dict[str, dict]:
    result = {}
    for item in decisions:
        decision = dict(item)
        shot_no = str(decision.get("shot_no") or "").strip()
        if not shot_no or shot_no in result:
            raise AlphaImageDeliveryError("每镜必须且只能有一条人工图片确认")
        if not str(decision.get("image_id") or "").strip():
            raise AlphaImageDeliveryError(f"镜头 {shot_no} 缺少 image_id")
        if str(decision.get("resolution_status") or "") == "text_only_approved":
            raise AlphaImageDeliveryError(f"镜头 {shot_no} 不允许 text_only_approved")
        if (
            not str(decision.get("confirmed_by") or "").strip()
            or not str(decision.get("confirmed_at") or "").strip()
        ):
            raise AlphaImageDeliveryError(f"镜头 {shot_no} 缺少人工确认人或时间")
        result[shot_no] = decision
    return result


def _script_shots(rows: list[dict]) -> list[str]:
    shots = [str(row.get("镜号") or "").strip() for row in rows]
    if not all(shots) or len(set(shots)) != len(shots):
        raise AlphaImageDeliveryError("脚本镜号必须唯一且非空")
    return shots


def _anonymous_source(decision: dict) -> dict:
    document_id = str(decision.get("source_document_id") or "").strip()
    sheet = str(decision.get("source_sheet") or "").strip()
    row = str(decision.get("source_row") or "").strip()
    if document_id and sheet and row:
        return {
            "source_document_id": document_id,
            "source_sheet": sheet,
            "source_row": row,
        }
    raise AlphaImageDeliveryError(
        "镜头图片缺少已确认匿名来源三元组（source_document_id/source_sheet/source_row）"
    )


def _confirmed_image_path(decision: dict, root: Path) -> Path:
    image_path = Path(str(decision.get("image_path") or "")).resolve()
    if not image_path.is_file() or not image_path.is_relative_to(root):
        raise AlphaImageDeliveryError("Alpha 图片必须位于同一隔离根")
    try:
        with PILImage.open(image_path) as image:
            width, height = image.size
            image.verify()
    except Exception as exc:
        raise AlphaImageDeliveryError(f"图片无法读取: {image_path.name}") from exc
    if width < 320 or height < 180:
        raise AlphaImageDeliveryError(f"图片不满足清晰度下限: {image_path.name}")
    return image_path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clear_embedded_image_paths(workbook_path: Path) -> None:
    workbook = load_workbook(workbook_path)
    try:
        sheet = workbook["脚本"]
        image_column = next(
            (
                cell.column
                for cell in sheet[1]
                if str(cell.value or "").strip() == "画面示意"
            ),
            None,
        )
        if image_column is None:
            raise AlphaImageDeliveryError("Alpha Excel 缺少画面示意列")
        for row_index in range(2, sheet.max_row + 1):
            sheet.cell(row_index, image_column).value = None
        workbook.save(workbook_path)
    finally:
        workbook.close()
