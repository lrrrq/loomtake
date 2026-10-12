from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook


class BrandReviewError(ValueError):
    pass


REQUIRED_HEADERS = (
    "来源文件", "来源sheet", "来源行", "来源品牌", "品牌中性", "确认依据",
    "审核人", "审核日期", "审核状态",
)
ASSET_REQUIRED_HEADERS = (
    "来源文件", "来源sheet", "来源行", "原图路径", "图片审核结论", "审核人", "审核日期",
)
ASSET_REVIEW_VERDICTS = {"待审核", "可用", "存疑", "禁用"}
BLOCKED_REVIEW = {
    "source_brand": "",
    "brand_neutral": False,
    "brand_neutral_evidence": "",
}


def load_source_brand_review(path: str | Path) -> dict[tuple[str, str, int], dict]:
    review_path = Path(path)
    try:
        workbook = load_workbook(review_path, read_only=True, data_only=True)
    except Exception as exc:
        raise BrandReviewError(f"品牌审核表不可读取: {review_path}") from exc
    try:
        if "创意库审核" not in workbook.sheetnames:
            raise BrandReviewError("品牌审核表缺少“创意库审核”sheet")
        sheet = workbook["创意库审核"]
        headers = [str(cell.value or "").strip() for cell in sheet[5]]
        duplicates = sorted({header for header in headers if header and headers.count(header) > 1})
        if duplicates:
            raise BrandReviewError(f"品牌审核表存在重复字段: {duplicates}")
        missing = [header for header in REQUIRED_HEADERS if header not in headers]
        if missing:
            raise BrandReviewError(f"品牌审核表缺少字段: {missing}")
        result = {}
        for row_no, cells in enumerate(sheet.iter_rows(min_row=6, values_only=True), start=6):
            values = {
                header: cells[index] if index < len(cells) else None
                for index, header in enumerate(headers)
            }
            if not any(str(value or "").strip() for value in values.values()):
                continue
            key = _source_key(values, row_no)
            if key in result:
                raise BrandReviewError(f"品牌审核表来源三元组重复: {key}")
            status = str(values.get("审核状态") or "").strip()
            if status == "已确认":
                record = _validated_record(values, row_no)
                result[key] = {
                    "source_brand": record["来源品牌"],
                    "brand_neutral": record["品牌中性"] == "是",
                    "brand_neutral_evidence": (
                        f"人工确认:{record['审核人']};{record['确认依据']};{record['审核日期']}"
                    ),
                }
            elif status in {"待审核", "驳回"}:
                result[key] = dict(BLOCKED_REVIEW)
            else:
                raise BrandReviewError(f"品牌审核表第 {row_no} 行审核状态无效")
        return result
    finally:
        workbook.close()


def apply_source_brand_review(shots, review: dict[tuple[str, str, int], dict]):
    return [
        replace(shot, **review.get(
            (shot.source_file, shot.source_sheet, shot.source_row),
            BLOCKED_REVIEW,
        ))
        for shot in shots
    ]


def load_creative_asset_review(
    path: str | Path,
) -> dict[tuple[str, str, int], list[dict]] | None:
    review_path = Path(path)
    try:
        workbook = load_workbook(review_path, read_only=True, data_only=True)
    except Exception as exc:
        raise BrandReviewError(f"图片审核表不可读取: {review_path}") from exc
    try:
        if "图片审核" not in workbook.sheetnames:
            return None
        sheet = workbook["图片审核"]
        headers = [str(cell.value or "").strip() for cell in sheet[5]]
        duplicates = sorted({header for header in headers if header and headers.count(header) > 1})
        if duplicates:
            raise BrandReviewError(f"图片审核表存在重复字段: {duplicates}")
        missing = [header for header in ASSET_REQUIRED_HEADERS if header not in headers]
        if missing:
            raise BrandReviewError(f"图片审核表缺少字段: {missing}")
        result: dict[tuple[str, str, int], list[dict]] = {}
        seen = set()
        for row_no, cells in enumerate(sheet.iter_rows(min_row=6, values_only=True), start=6):
            values = {
                header: cells[index] if index < len(cells) else None
                for index, header in enumerate(headers)
            }
            if not any(str(value or "").strip() for value in values.values()):
                continue
            key = _source_key(values, row_no)
            image_path = str(values.get("原图路径") or "").strip()
            verdict = str(values.get("图片审核结论") or "").strip()
            if not image_path:
                raise BrandReviewError(f"图片审核表第 {row_no} 行缺少原图路径")
            if verdict not in ASSET_REVIEW_VERDICTS:
                raise BrandReviewError(f"图片审核表第 {row_no} 行图片审核结论无效")
            record_key = key + (_normalized_path(image_path),)
            if record_key in seen:
                raise BrandReviewError(f"图片审核表图片重复: {record_key}")
            seen.add(record_key)
            result.setdefault(key, []).append({"path": image_path, "verdict": verdict})
        return result
    finally:
        workbook.close()


def apply_creative_asset_review(shots, review: dict[tuple[str, str, int], list[dict]] | None):
    if review is None:
        return shots
    filtered = []
    for shot in shots:
        records = review.get((shot.source_file, shot.source_sheet, shot.source_row), [])
        reference_paths = _approved_paths(shot.reference_image_path, records)
        effect_paths = _approved_paths(shot.effect_reference_path, records)
        filtered.append(replace(
            shot,
            reference_image_path="; ".join(reference_paths),
            effect_reference_path="; ".join(effect_paths),
            reference_image_evidence=shot.reference_image_evidence if reference_paths else "",
            effect_reference_evidence=shot.effect_reference_evidence if effect_paths else "",
        ))
    return filtered


def _source_key(values: dict, row_no: int) -> tuple[str, str, int]:
    source_file = str(values.get("来源文件") or "").strip()
    source_sheet = str(values.get("来源sheet") or "").strip()
    try:
        source_row = int(values.get("来源行"))
    except (TypeError, ValueError) as exc:
        raise BrandReviewError(f"品牌审核表第 {row_no} 行的来源行无效") from exc
    if not source_file or not source_sheet:
        raise BrandReviewError(f"品牌审核表第 {row_no} 行缺少来源文件或来源sheet")
    return source_file, source_sheet, source_row


def _validated_record(values: dict, row_no: int) -> dict:
    record = {
        key: str(values.get(key) or "").strip()
        for key in ("来源文件", "来源sheet", "来源品牌", "品牌中性", "确认依据", "审核人")
    }
    try:
        record["来源行"] = int(values.get("来源行"))
    except (TypeError, ValueError) as exc:
        raise BrandReviewError(f"品牌审核表第 {row_no} 行的来源行无效") from exc
    record["审核日期"] = _date_text(values.get("审核日期"))
    required = ("来源文件", "来源sheet", "确认依据", "审核人", "审核日期")
    missing = [key for key in required if not record[key]]
    if missing:
        raise BrandReviewError(f"品牌审核表第 {row_no} 行已确认但缺少: {missing}")
    if record["品牌中性"] not in {"是", "否"}:
        raise BrandReviewError(f"品牌审核表第 {row_no} 行品牌中性必须为“是”或“否”")
    if bool(record["来源品牌"]) == (record["品牌中性"] == "是"):
        raise BrandReviewError(f"品牌审核表第 {row_no} 行必须在来源品牌和品牌中性中二选一")
    return record


def _date_text(value) -> str:
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return str(value or "").strip()


def _approved_paths(raw_paths: str, records: list[dict]) -> list[str]:
    approved = [record["path"] for record in records if record["verdict"] == "可用"]
    return [
        path for path in (part.strip() for part in str(raw_paths or "").split(";"))
        if path and any(_same_review_path(path, reviewed_path) for reviewed_path in approved)
    ]


def _same_review_path(actual: str, reviewed: str) -> bool:
    actual_path = _normalized_path(actual)
    reviewed_path = _normalized_path(reviewed)
    return actual_path == reviewed_path or actual_path.endswith("/" + reviewed_path)


def _normalized_path(value: str) -> str:
    return str(value or "").strip().replace("\\", "/").lstrip("./")
