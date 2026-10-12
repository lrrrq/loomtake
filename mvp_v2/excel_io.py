from __future__ import annotations

import hashlib
import json
import math
import posixpath
import re
import unicodedata
from copy import copy, deepcopy
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from openpyxl.comments import Comment
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.cell_range import CellRange
from openpyxl.drawing.image import Image as ExcelImage
from openpyxl.styles import Alignment, Font
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries
from openpyxl.utils.units import pixels_to_EMU
from PIL import Image as PILImage

from .editorial_gate import (
    ADJACENT_REVIEW_COLUMNS,
    ADJACENT_REVIEW_DIMENSIONS,
    ADJACENT_REVIEW_SHEET_NAME,
    FULL_FILM_REVIEW_COLUMNS,
    FULL_FILM_REVIEW_DIMENSIONS,
    FULL_FILM_REVIEW_SHEET_NAME,
    VALID_ISSUE_CODES,
    VALID_MAJOR_CHANGE_VALUES,
    VALID_SEVERITIES,
)
from .launch_contract import expected_adjacent_edges
from .models import SourceShot, validate_script_brief
from .reuse_policy import normalize_reuse_level
from .review_quality import (
    EDITORIAL_REVIEW_COLUMNS,
    OVERALL_REVIEW_COLUMN,
    REVIEW_DIMENSIONS,
    VALID_REVIEW_STATUSES,
    build_editorial_review_rows,
)
from .run_contract import sha256_file
from .source_policy import source_reuse_policy
from .script_type_adapter import (
    normalize_script_type,
    resolve_script_type_workbook,
)
from .source_brand_review import (
    apply_creative_asset_review,
    apply_source_brand_review,
    load_creative_asset_review,
    load_source_brand_review,
)

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

LAUNCH_WORKBOOK_FILENAME = "M+发布会类通用脚本库.xlsx"
DELIVERY_REVIEW_COLUMNS = ["审核范围", "镜号", "结论", "处理人", "处理时间"]


def resolve_launch_source_workbook(source_root: str | Path) -> Path:
    return resolve_script_type_workbook(source_root, "发布会")


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
    "来源 sheet",
    "来源行",
    "匹配分",
    "匹配理由",
    "缺口说明",
    "审核结果",
    "修改意见",
]

SCRIPT_COLUMNS = [
    "结构",
    "镜号",
    "画面示意",
    "效果示意",
    "效果",
    "类型",
    "景别",
    "运镜",
    "画面描述",
    "声音",
    "旁白",
    "字幕",
    "备注",
]

DETAIL_COLUMNS = [
    "项目名称",
    "需求行号",
    "slot_index",
    "模板槽位总数",
    "逻辑槽位",
    "模板来源文件",
    "模板来源 sheet",
    "模板来源行",
    "模板结构",
    "模板镜号",
    "槽位功能",
    "槽位选择原因",
    "允许素材",
    "禁止素材",
    "推荐景别",
    "推荐运镜",
    "推荐声音",
    "节奏说明",
    "必须视觉证据",
    "重复素材规则",
    "替换方式",
    "替换来源文件",
    "替换来源 sheet",
    "替换来源行",
    "采用来源类型",
    "采用来源文件",
    "采用来源 sheet",
    "采用来源行",
    "采用来源品牌",
    "采用来源品牌中性",
    "品牌中性确认依据",
    "采用来源证据",
    "Brief来源文件",
    "Brief来源 sheet",
    "Brief来源行",
    "Brief来源 SHA256",
    "Brief来源证据",
    "规划事件ID",
    "规划主事件",
    "规划事件集",
    "规划要求集",
    "规划Brief证据",
    "规划功能来源文件",
    "规划功能来源 sheet",
    "规划功能来源行",
    "功能参考声明",
    "叙事阶段",
    "叙事衔接理由",
    "结构决策来源",
    "模板功能来源",
    "匹配分",
    "匹配理由",
    "候选命中点",
    "候选缺失点",
    "候选冲突点",
    "候选选择理由",
    "候选放弃理由",
    "缺口说明",
    "时长(秒)",
    "输出图片来源",
    "图片证据类型",
    "视觉资产ID",
    "image_id",
    "原图路径",
    "图片绑定方式",
    "图片绑定说明",
    "视觉质量指标",
    "视觉读取状态",
    "配图置信度",
    "视觉内容摘要",
    "视觉标签",
    "视觉功能标签",
    "主视觉功能",
    "槽位允许功能",
    "功能适配判断",
    "功能重复组",
    "视觉匹配分",
    "配图判断",
    "视觉适配模式",
    "视觉适配查询",
    "视觉回写依据",
    "改编说明",
]

AUDIT_COLUMNS = [
    "检查项",
    "级别",
    "状态",
    "关联镜号",
    "详情",
    "修复建议",
]

VISUAL_ASSET_COLUMNS = [
    "visual_asset_id",
    "image_id",
    "media_id",
    "content_sha256",
    "slot_index",
    "逻辑槽位",
    "镜号",
    "图片角色",
    "来源文件",
    "来源 sheet",
    "来源行",
    "原图路径",
    "原图宽",
    "原图高",
    "文件大小(bytes)",
    "绑定方式",
    "视觉读取状态",
    "配图置信度",
    "视觉内容摘要",
    "视觉标签",
    "视觉功能标签",
    "主视觉功能",
    "槽位允许功能",
    "功能适配判断",
    "功能重复组",
    "ocr_text",
    "brand_names",
    "negative_flags",
    "视觉匹配分",
    "命中视觉要素",
    "缺失视觉要素",
    "冲突视觉要素",
    "配图判断",
    "视觉适配模式",
    "视觉适配查询",
    "质量指标",
    "可回查锚点",
    "匹配理由",
    "绑定说明",
    "审核结果",
    "修改意见",
    "预览图",
]

PAIRING_REVIEW_COLUMNS = [
    "镜号",
    "结构",
    "画面描述",
    "配图判断",
    "视觉匹配分",
    "配图置信度",
    "视觉读取状态",
    "视觉内容摘要",
    "视觉功能标签",
    "主视觉功能",
    "槽位允许功能",
    "功能适配判断",
    "功能重复组",
    "命中视觉要素",
    "缺失视觉要素",
    "冲突视觉要素",
    "质量指标",
    "原图路径",
    "预览图",
]

ADJACENT_REVIEW_EVIDENCE_COLUMNS = [
    "前镜画面描述",
    "后镜画面描述",
    "前镜声音",
    "后镜声音",
    "前镜原图路径",
    "后镜原图路径",
    "前镜预览",
    "后镜预览",
]

ADJACENT_REVIEW_OUTPUT_COLUMNS = [
    *ADJACENT_REVIEW_COLUMNS[:2],
    *ADJACENT_REVIEW_EVIDENCE_COLUMNS,
    *ADJACENT_REVIEW_COLUMNS[2:],
]

IMAGE_ANCHOR_RE = re.compile(r"^(?P<file>.+)#(?P<sheet>.+)!R(?P<row>\d+)C(?P<col>\d+)$")


class BriefError(ValueError):
    pass


def cell_text(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def read_briefs(path: str | Path) -> list[dict]:
    source_path = Path(path).expanduser().resolve()
    source_sha256 = sha256_file(source_path)
    wb = load_workbook(source_path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    headers = [cell_text(cell.value) for cell in ws[1]]
    missing = [name for name in REQUIRED_INPUT_COLUMNS if name not in headers]
    if missing:
        wb.close()
        raise BriefError(f"缺少必填字段: {', '.join(missing)}")

    index = {name: headers.index(name) for name in headers if name}
    briefs = []
    for row_no, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if not any(cell_text(value) for value in row):
            continue
        brief = {
            name: cell_text(row[index[name]]) if name in index and index[name] < len(row) else ""
            for name in INPUT_COLUMNS
        }
        missing_values = [name for name in REQUIRED_INPUT_COLUMNS if not brief.get(name)]
        if missing_values:
            wb.close()
            raise BriefError(f"第 {row_no} 行缺少必填值: {', '.join(missing_values)}")
        try:
            brief["脚本类型"] = normalize_script_type(brief.get("脚本类型", ""))
            validate_script_brief(brief)
        except ValueError as exc:
            wb.close()
            raise BriefError(f"第 {row_no} 行 {exc}") from exc
        brief["input_row"] = row_no
        brief.update({
            "Brief来源文件": source_path.name,
            "Brief来源 sheet": ws.title,
            "Brief来源行": row_no,
            "Brief来源 SHA256": source_sha256,
            "Brief来源证据": brief.get("必须出现的内容", ""),
            "_brief_source_path": str(source_path),
        })
        briefs.append(brief)
    wb.close()
    return briefs


def read_text_brief(text: str) -> dict:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    title = ""
    fields = {name: "" for name in INPUT_COLUMNS}
    structured_fields: dict[str, object] = {}
    key_map = {
        "项目名称": "项目名称",
        "项目区域文件名": "项目区域文件名",
        "脚本类型": "脚本类型",
        "品牌或客户": "品牌或客户",
        "品牌": "品牌或客户",
        "客户": "品牌或客户",
        "行业或活动类别": "行业或活动类别",
        "视频时长": "视频时长",
        "时长": "视频时长",
        "风格调性": "风格调性",
        "风格": "风格调性",
        "必须出现的内容": "必须出现的内容",
        "必须出现内容": "必须出现的内容",
        "禁忌或不能出现的内容": "禁忌或不能出现的内容",
        "禁忌": "禁忌或不能出现的内容",
        "素材目录或素材文件": "素材目录或素材文件",
        "素材": "素材目录或素材文件",
        "备注": "备注",
    }
    for line in lines:
        if "：" in line:
            raw_key, value = line.split("：", 1)
        elif ":" in line:
            raw_key, value = line.split(":", 1)
        else:
            if not title:
                title = line
            continue
        structured_key = {
            "M+规划事件JSON": "planning_events",
            "M+创作策略绑定JSON": "creative_strategy_selection",
        }.get(raw_key.strip())
        if structured_key:
            try:
                parsed = json.loads(value.strip())
            except json.JSONDecodeError as exc:
                raise BriefError(f"文本 Brief {raw_key.strip()} 不是有效 JSON") from exc
            if structured_key == "planning_events" and not isinstance(parsed, list):
                raise BriefError("文本 Brief M+规划事件JSON 必须是 array")
            if structured_key == "creative_strategy_selection" and not isinstance(parsed, dict):
                raise BriefError("文本 Brief M+创作策略绑定JSON 必须是 object")
            structured_fields[structured_key] = parsed
            continue
        key = key_map.get(raw_key.strip())
        if key:
            fields[key] = value.strip()

    if title and not fields["项目名称"] and len(title) <= 40 and "。" not in title:
        fields["项目名称"] = title
    if not fields["脚本类型"]:
        joined = " ".join(lines)
        inferred_types = [
            script_type
            for script_type in ("明星活动", "音乐节", "发布会", "展会", "年会", "赛事")
            if script_type in joined
        ]
        if len(inferred_types) > 1:
            raise BriefError(
                "文本 Brief 同时出现多个脚本类型: "
                + "、".join(inferred_types)
                + "；请明确填写“脚本类型”"
            )
        if inferred_types:
            fields["脚本类型"] = inferred_types[0]
    _fill_freeform_brief_fields(fields, " ".join(lines))

    missing_values = [name for name in REQUIRED_INPUT_COLUMNS if not fields.get(name)]
    if missing_values:
        raise BriefError(f"文本 Brief 缺少必填值: {', '.join(missing_values)}")
    try:
        fields["脚本类型"] = normalize_script_type(fields.get("脚本类型", ""))
        validate_script_brief(fields)
    except ValueError as exc:
        raise BriefError(f"文本 Brief {exc}") from exc
    fields["input_row"] = 1
    fields.update(structured_fields)
    return fields


def _fill_freeform_brief_fields(fields: dict, text: str) -> None:
    if not fields["项目名称"]:
        match = re.search(r"为(.+?发布会).*?(?:短片|视频|片)", text)
        if match:
            fields["项目名称"] = f"{match.group(1)}开场短片"
    if not fields["项目区域文件名"] and fields["项目名称"]:
        fields["项目区域文件名"] = fields["项目名称"]
    if not fields["视频时长"]:
        match = re.search(r"(\d+)\s*(?:秒|s|S)", text)
        if match:
            fields["视频时长"] = f"{match.group(1)}s"
    if not fields["风格调性"]:
        match = re.search(r"(?:整体要|风格(?:调性)?(?:是|为)?)([^。；;]+)", text)
        if match:
            fields["风格调性"] = match.group(1).strip("：:，, ")
    if not fields["必须出现的内容"]:
        match = re.search(r"必须出现([^。；;]+)", text)
        if match:
            fields["必须出现的内容"] = match.group(1).strip("：:，, ")
    if not fields["禁忌或不能出现的内容"]:
        negatives = re.findall(r"不要[^。；;，,]+", text)
        if negatives:
            fields["禁忌或不能出现的内容"] = "；".join(negatives)
    if not fields["品牌或客户"]:
        match = re.search(r"为([\u4e00-\u9fffA-Za-z0-9]+)", text)
        if match:
            fields["品牌或客户"] = match.group(1)


def read_text_brief_file(path: str | Path) -> dict:
    source_path = Path(path).expanduser().resolve()
    brief = read_text_brief(source_path.read_text(encoding="utf-8"))
    brief.update({
        "Brief来源文件": source_path.name,
        "Brief来源 sheet": "Brief",
        "Brief来源行": int(brief.get("input_row") or 1),
        "Brief来源 SHA256": sha256_file(source_path),
        "Brief来源证据": brief.get("必须出现的内容", ""),
        "_brief_source_path": str(source_path),
    })
    return brief


IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")


def load_launch_source_shots(source_root: str | Path, project_hint: str = "") -> list[SourceShot]:
    root = Path(source_root)
    media_dirs = _project_media_dirs(root, project_hint)
    workbook_path = resolve_launch_source_workbook(root)
    specs: list[tuple[Path, str | None]] = [
        (workbook_path, "模版"),
        (workbook_path, "创意库"),
    ]
    launch_dir = root / "发布会类"
    if launch_dir.exists():
        specs.extend((path, None) for path in sorted(launch_dir.glob("*.xlsx")) if not path.name.startswith(".~"))

    shots = []
    seen = set()
    for path, sheet_name in specs:
        key = (path, sheet_name)
        if key in seen or not path.exists():
            continue
        seen.add(key)
        shots.extend(_read_launch_workbook(path, sheet_name, media_dirs))
    return shots


def load_template_slots(
    source_root: str | Path,
    project_hint: str = "",
    script_type: str = "发布会",
    workbook_path: str | Path | None = None,
) -> list[SourceShot]:
    root = Path(source_root)
    normalized_type = normalize_script_type(script_type)
    path = Path(workbook_path) if workbook_path else resolve_script_type_workbook(root, normalized_type)
    if not path.exists():
        return []
    slots = _read_launch_workbook(
        path,
        "模版",
        _project_media_dirs(root, project_hint) if normalized_type == "发布会" else [],
        script_type=normalized_type,
    )
    return [
        SourceShot(
            **{
                **slot.__dict__,
                "shot_no": slot.shot_no or str(index),
                "slot_index": index,
            }
        )
        for index, slot in enumerate(slots, start=1)
    ]


def load_creative_variants(
    source_root: str | Path,
    project_hint: str = "",
    brand_review_path: str | Path | None = None,
    script_type: str = "发布会",
    workbook_path: str | Path | None = None,
) -> list[SourceShot]:
    root = Path(source_root)
    normalized_type = normalize_script_type(script_type)
    path = Path(workbook_path) if workbook_path else resolve_script_type_workbook(root, normalized_type)
    if not path.exists():
        return []
    variants = _read_launch_workbook(
        path,
        "创意库",
        _project_media_dirs(root, project_hint) if normalized_type == "发布会" else [],
        script_type=normalized_type,
    )
    if brand_review_path:
        variants = apply_source_brand_review(variants, load_source_brand_review(brand_review_path))
        variants = apply_creative_asset_review(variants, load_creative_asset_review(brand_review_path))
    return variants


def _read_launch_workbook(
    path: Path,
    sheet_name: str | None,
    media_dirs: list[Path] | None = None,
    script_type: str = "发布会",
) -> list[SourceShot]:
    wb = load_workbook(path, read_only=False, data_only=True)
    sheet_names = [sheet_name] if sheet_name else wb.sheetnames
    media_dirs = media_dirs or []
    shots = []
    try:
        for name in sheet_names:
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            overview_cells = _sheet_overview_cells(ws)
            headers = _merged_headers(_row_values(ws, 5), _row_values(ws, 6))
            # Only an explicit merged image heading grants adjacent columns
            # the same purpose. Blank neighbours and decoration remain excluded.
            for region in ws.merged_cells.ranges:
                if region.min_row <= 6 <= region.max_row:
                    label = cell_text(ws.cell(region.min_row, region.min_col).value)
                    if label in {"画面示意", "效果示意"}:
                        for column in range(region.min_col, region.max_col + 1):
                            headers[column - 1] = label
            image_index = _image_file_index(path, ws)
            header_set = set(headers)
            reuse_policy = source_reuse_policy(path.name, ws.title)
            missing_metadata = tuple(
                field for field in ("可复用等级", "禁用说明")
                if field not in header_set and reuse_policy is None
            )
            current_structure = ""
            empty_streak = 0
            for row_no, row in enumerate(ws.iter_rows(min_row=7, max_col=len(headers), values_only=True), start=7):
                values = {headers[col]: cell_text(row[col]) if col < len(row) else "" for col in range(len(headers))}
                visual_desc = values.get("画面描述", "")
                if not visual_desc:
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

                embedded_reference = _image_path_for_label(image_index, headers, row_no, "画面示意")
                media_reference = _media_image_for_shot(media_dirs, values.get("镜号", ""))
                reference_image_path = embedded_reference or media_reference
                reference_image_evidence = (
                    "源表嵌图" if embedded_reference
                    else ("项目编号回退" if media_reference else "")
                )
                embedded_effect = _image_path_for_label(image_index, headers, row_no, "效果示意")
                row_missing_metadata = list(missing_metadata)
                for field in ("可复用等级", "禁用说明"):
                    if field in header_set and not values.get(field, "").strip():
                        row_missing_metadata.append(field)
                reuse_level = values.get("可复用等级", "")
                forbidden_note = values.get("禁用说明", "")
                if "可复用等级" not in header_set:
                    reuse_level = (reuse_policy or {}).get("reuse_level", "")
                if "禁用说明" not in header_set:
                    forbidden_note = (reuse_policy or {}).get("forbidden_note", "")
                source_brand = values.get("来源品牌", "").strip() or _source_brand_from_path(path)
                brand_neutral = _explicit_true(values.get("品牌中性", ""))
                brand_neutral_evidence = ""
                if brand_neutral:
                    confirmed_by = (
                        values.get("品牌中性确认人", "").strip()
                        or values.get("品牌中性确认依据", "").strip()
                    )
                    brand_neutral_evidence = f"人工确认:{confirmed_by}" if confirmed_by else ""
                shots.append(SourceShot(
                    script_type=normalize_script_type(script_type),
                    source_file=path.name,
                    source_sheet=ws.title,
                    source_row=row_no,
                    shot_no=values.get("镜号", ""),
                    structure=structure,
                    duration_seconds=values.get("时长(秒)", "") or values.get("时长", ""),
                    effect=values.get("效果", ""),
                    camera_type=values.get("机位", "") or values.get("类型", "") or values.get("形式", ""),
                    shot_size=values.get("景别", ""),
                    camera_move=values.get("运镜", ""),
                    visual_desc=visual_desc,
                    sound=values.get("声音", ""),
                    note=values.get("备注", ""),
                    reference_image_path=reference_image_path,
                    effect_reference_path=embedded_effect,
                    reference_image_evidence=reference_image_evidence,
                    effect_reference_evidence="源表嵌图" if embedded_effect else "",
                    reuse_level=normalize_reuse_level(reuse_level),
                    forbidden_note=forbidden_note,
                    tags=values.get("标签", ""),
                    shot_function=values.get("镜头功能", ""),
                    visual_subject=values.get("画面主体", ""),
                    source_brand=source_brand,
                    brand_neutral=brand_neutral,
                    brand_neutral_evidence=brand_neutral_evidence,
                    form=values.get("形式", ""),
                    missing_metadata=tuple(dict.fromkeys(row_missing_metadata)),
                    generic_desc=values.get("通用描述", ""),
                    brand_judgement_original=values.get("品牌中性确认依据", ""),
                    sheet_overview="\n".join(cell["text"] for cell in overview_cells),
                    semantic_source_anchors=json.dumps({
                        "generic_desc": [_cell_coordinate(row_no, i + 1) for i, h in enumerate(headers) if h == "通用描述"],
                        "brand_judgement_original": [_cell_coordinate(row_no, i + 1) for i, h in enumerate(headers) if h == "品牌中性确认依据"],
                        "sheet_overview": [cell["cell"] for cell in overview_cells],
                    }, ensure_ascii=False, sort_keys=True),
                    source_semantics_version="mplus-source-semantics/v1",
                ))
    finally:
        wb.close()
    return shots


def _cell_coordinate(row: int, column: int) -> str:
    from openpyxl.utils import get_column_letter
    return f"{get_column_letter(column)}{row}"


def _sheet_overview_cells(sheet) -> list[dict]:
    """Preserve the library's overview cells verbatim; never infer hard rules."""
    return [{"cell": cell.coordinate, "text": cell_text(cell.value)}
            for cells in sheet.iter_rows(min_row=2, max_row=2)
            for cell in cells if cell_text(cell.value) and cell_text(cell.value) != "整体说明"]


def _source_brand_from_path(path: Path) -> str:
    parent = path.parent.name.strip()
    if parent in {"", "整理后脚本库", "发布会类"} or path.name.startswith("M+"):
        return ""
    return parent


def _explicit_true(value: str) -> bool:
    return str(value or "").strip().casefold() in {"是", "true", "1", "品牌中性"}


def _row_values(ws, row_no: int) -> list[str]:
    row = next(ws.iter_rows(min_row=row_no, max_row=row_no, values_only=True), ())
    return [cell_text(value) for value in row]


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


def _image_file_index(path: Path, ws) -> dict[tuple[int, int], str]:
    anchors = {}
    workbook_sha256 = sha256_file(path)
    for index, img in enumerate(getattr(ws, "_images", []), start=1):
        try:
            row = img.anchor._from.row + 1
            col = img.anchor._from.col + 1
        except Exception:
            continue
        key = (row, col)
        image_path = _extract_embedded_image(path, ws.title, row, col, index, img, workbook_sha256=workbook_sha256)
        existing = anchors.get(key, "")
        anchors[key] = "; ".join(value for value in (existing, image_path) if value)
    return anchors


def _extract_embedded_image(path: Path, sheet_name: str, row: int, col: int, index: int, img, *, workbook_sha256: str = "") -> str:
    source = path.resolve()
    cache_root = source.parent / "_mvp_v2_extracted_images"
    package_root = next((parent for parent in source.parents
                         if (parent / "asset_package.json").is_file()), None)
    if package_root is not None:
        from .runtime_config import load_runtime_config
        cache_root = Path(load_runtime_config()["cache_root"]).expanduser().resolve()
        if cache_root.is_relative_to(package_root):
            # A misplaced cache configuration must not mutate a frozen package.
            # Use a disposable external directory; source identity stays explicit.
            import tempfile
            cache_root = Path(tempfile.mkdtemp(prefix="mplus-workbook-images-"))
        cache_root = cache_root / "workbook-images" / (workbook_sha256 or sha256_file(source))
    cache_dir = cache_root / path.stem / sheet_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    try:
        original_bytes = img._data()
        img.ref = BytesIO(original_bytes)
        with PILImage.open(BytesIO(original_bytes)) as embedded:
            image = embedded.convert("RGB")
        data = BytesIO()
        image.save(data, format="PNG")
        payload = data.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        # Position identifies provenance; content identifies the extracted image.
        # Keep prior content intact for versions already referencing its path.
        output = (cache_dir / f"R{row}C{col}_{index}_{digest}.png").resolve()
        if not output.is_file() or sha256_file(output) != digest:
            output.write_bytes(payload)
        # Keep encoding identity and source coordinates separate from the PNG
        # used for display. Multiple identical images retain separate anchors.
        provenance = {
            "source_file": path.name, "source_sheet": sheet_name,
            "source_workbook_sha256": workbook_sha256 or sha256_file(path),
            "source_row": row, "source_column": col, "anchor_index": index,
            "original_bytes_sha256": hashlib.sha256(original_bytes).hexdigest(),
            "normalized_png_sha256": digest,
            "normalized_pixels_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
            "width": image.width, "height": image.height,
            "conversion": "Pillow RGB PNG; pixel digest is row-major RGB bytes",
        }
        provenance_path = output.with_suffix(f".{provenance['source_workbook_sha256']}.provenance.json")
        serialized = json.dumps(provenance, ensure_ascii=False, sort_keys=True)
        if not provenance_path.is_file() or provenance_path.read_text(encoding="utf-8") != serialized:
            provenance_path.write_text(serialized, encoding="utf-8")
        return str(output)
    except Exception:
        return f"{path.name}#{sheet_name}!R{row}C{col}"


def _image_path_for_label(image_index: dict[tuple[int, int], str], headers: list[str], row_no: int, label: str) -> str:
    values = []
    for col_idx, header in enumerate(headers, start=1):
        if header != label:
            continue
        image_columns = [col_idx]
        if label == "画面示意" and col_idx > 1 and headers[col_idx - 2] in {"形式", "类型"}:
            image_columns.insert(0, col_idx - 1)
        for image_col in image_columns:
            if (row_no, image_col) in image_index:
                values.append(image_index[(row_no, image_col)])
    return "; ".join(dict.fromkeys(values))


def project_hint_from_brief(brief: dict) -> str:
    return " ".join([
        str(brief.get("项目名称", "")),
        str(brief.get("项目区域文件名", "")),
        str(brief.get("素材目录或素材文件", "")),
    ])


def _project_media_dirs(root: Path, project_hint: str) -> list[Path]:
    launch_dir = root / "发布会类"
    if not launch_dir.exists() or not project_hint:
        return []
    normalized_hint = _normalize_name(project_hint)
    matches = []
    for path in sorted(launch_dir.iterdir()):
        if not path.is_dir():
            continue
        name = _normalize_name(path.name)
        if name and name in normalized_hint:
            matches.append(path)
    return matches


def _media_image_for_shot(media_dirs: list[Path], shot_no: str) -> str:
    text = cell_text(shot_no)
    if not text:
        return ""
    digits = re.sub(r"\D+", "", text)
    if not digits:
        return ""
    candidates = [digits, digits.zfill(2), f"{digits}+", f"{digits.zfill(2)}+"]
    for directory in media_dirs:
        for stem in candidates:
            for ext in IMAGE_EXTENSIONS:
                path = directory / f"{stem}{ext}"
                if path.exists():
                    return str(path)
    return ""


def _normalize_name(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def write_output_workbook(path: str | Path, rows: list[dict]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "脚本"
    _append_excel_row(ws, OUTPUT_COLUMNS)
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")

    for row in rows:
        _append_excel_row(ws, [row.get(column, "") for column in OUTPUT_COLUMNS])

    for col in ws.columns:
        letter = col[0].column_letter
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 36)
    for row in ws.iter_rows():
        for cell in row:
            cell.font = Font(name="Arial")
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    wb.save(path)


def read_managed_sheet_rows(workbook, sheet_name: str) -> list[dict]:
    """Read managed business rows by their labels, including version additions."""
    if sheet_name not in workbook.sheetnames:
        return []
    sheet = workbook[sheet_name]
    header_row, columns = _managed_sheet_headers(sheet)
    headers = [columns.get(index, "") for index in range(1, sheet.max_column + 1)]
    result = [
        {key: value for key, value in zip(headers, row) if key}
        for row in sheet.iter_rows(min_row=header_row + 1, values_only=True)
    ]
    if VERSION_ADDITIONS_SHEET in workbook.sheetnames:
        additions = workbook[VERSION_ADDITIONS_SHEET]
        for values in additions.iter_rows(min_row=2, values_only=True):
            if len(values) < 4:
                continue
            name, record, field, value = values[:4]
            if name != sheet_name or not field:
                continue
            try:
                index = int(record) - 1
            except (ValueError, TypeError):
                continue
            if 0 <= index < len(result) and field not in result[index]:
                result[index][str(field)] = value
    if sheet_name == "脚本":
        structure = ""
        for row in result:
            structure = row.get("结构") or structure
            row["结构"] = structure
    return result


VERSION_ADDITIONS_SHEET = "版本附加明细"
VERSION_ADDITIONS_COLUMNS = ["工作表", "记录序号", "字段", "值"]


def read_script_image_bytes(workbook_path: str | Path) -> dict[str, bytes]:
    """Read the first saved main image for each actual shot; effect images stay separate."""
    workbook = load_workbook(Path(workbook_path), read_only=False, data_only=False)
    try:
        return _script_image_bytes(workbook)
    finally:
        workbook.close()


def _script_image_bytes(workbook) -> dict[str, bytes]:
    if "脚本" not in workbook.sheetnames:
        return {}
    sheet = workbook["脚本"]
    header, columns = _managed_sheet_headers(sheet)
    by_name = {name: column for column, name in columns.items()}
    if "画面示意" not in by_name or "镜号" not in by_name:
        return {}
    result = {}
    for picture in sheet._images:
        row, column = _image_cell(picture)
        if row <= header or row > sheet.max_row or column != by_name["画面示意"]:
            continue
        shot_no = str(sheet.cell(row, by_name["镜号"]).value or "").strip()
        if shot_no and shot_no not in result:
            result[shot_no] = picture._data()
    return result


def _managed_sheet_headers(sheet) -> tuple[int, dict[int, str]]:
    header_row = 1
    if sheet.title == "脚本":
        for index in range(1, min(sheet.max_row, 12) + 1):
            values = {str(cell.value or "").strip() for cell in sheet[index]}
            if (("画面描述" in values and values & {"声音", "画面示意", "镜号"})
                    or {"内容", "声音"} <= values):
                header_row = index
                break
            if {"镜号", "画面示意"} <= values:
                header_row = index
    # Template output always reserves rows 1-6; a single-row header in row 5
    # may have optional text headers added in row 6, never an extra shot there.
    template_single_header = sheet.title == "脚本" and header_row == 5 and all(
        not cell.value or str(cell.value).strip() in {"旁白", "字幕"}
        for cell in sheet[6]
    )
    if template_single_header:
        columns = {index: label for index, label in enumerate(
            _merged_headers(_row_values(sheet, 5), _row_values(sheet, 6)), 1) if label}
        header_row = 6
    else:
        columns = {cell.column: str(cell.value or "").strip() for cell in sheet[header_row] if cell.value}
    if sheet.title == "脚本" and header_row > 1:
        for cell in sheet[header_row - 1]:
            if cell.value in {"结构", "镜号"} and cell.value not in columns.values():
                columns[cell.column] = cell.value
    aliases = {"通用描述": "画面描述", "机位": "类型"}
    if sheet.title == "脚本":
        aliases["内容"] = "画面描述"
    return header_row, {column: aliases.get(name, name)
                        for column, name in columns.items()}


def write_template_output_workbook(
    path: str | Path,
    script_rows: list[dict],
    detail_rows: list[dict],
    audit_rows: list[dict] | None = None,
    template_workbook_path: str | Path | None = None,
    visual_asset_rows: list[dict] | None = None,
    embed_evidence_previews: bool = False,
    embed_review_previews: bool = True,
    overall_summary: str = "",
    script_type: str = "发布会",
    duration_seconds: int = 60,
    parent_workbook_path: str | Path | None = None,
    parent_shot_nos: list[str] | None = None,
) -> None:
    wb = Workbook()
    script_ws = wb.active
    script_ws.title = "脚本"
    detail_ws = wb.create_sheet("抽调明细")
    project_name = next(
        (str(row.get("项目名称") or "").strip() for row in detail_rows if row.get("项目名称")),
        "",
    )
    if template_workbook_path and Path(template_workbook_path).exists():
        _write_template_like_script_sheet(
            script_ws,
            script_rows,
            Path(template_workbook_path),
            project_name=project_name,
            overall_summary=overall_summary,
            script_type=script_type,
            duration_seconds=duration_seconds,
        )
    else:
        _write_sheet(script_ws, SCRIPT_COLUMNS, script_rows)
        _embed_sheet_images(
            script_ws, SCRIPT_COLUMNS, script_rows, ("画面示意", "效果示意"),
            preserve_original_pixels=parent_workbook_path is not None,
        )
    _write_sheet(detail_ws, DETAIL_COLUMNS, detail_rows)
    if visual_asset_rows is not None:
        visual_ws = wb.create_sheet("视觉资产")
        _write_sheet(visual_ws, VISUAL_ASSET_COLUMNS, visual_asset_rows)
        if embed_evidence_previews:
            _embed_visual_asset_previews(visual_ws, visual_asset_rows)
        review_ws = wb.create_sheet("配图复核")
        review_rows = _pairing_review_rows(script_rows, visual_asset_rows)
        _write_sheet(review_ws, PAIRING_REVIEW_COLUMNS, review_rows)
        if embed_evidence_previews:
            _embed_pairing_review_previews(review_ws, review_rows)
    editorial_review_ws = wb.create_sheet("剪辑复核")
    editorial_review_rows = build_editorial_review_rows(script_rows, detail_rows, visual_asset_rows)
    _write_sheet(editorial_review_ws, EDITORIAL_REVIEW_COLUMNS, editorial_review_rows)
    _add_path_hyperlinks(editorial_review_ws, EDITORIAL_REVIEW_COLUMNS, editorial_review_rows, ("原图路径",))
    if embed_review_previews:
        _embed_sheet_images(
            editorial_review_ws,
            EDITORIAL_REVIEW_COLUMNS,
            editorial_review_rows,
            ("预览图",),
            clear_cell_when_embedded=True,
            max_width=320,
            max_height=180,
        )
    _add_editorial_review_validation(editorial_review_ws)
    _configure_sparse_review_view(
        editorial_review_ws,
        EDITORIAL_REVIEW_COLUMNS,
        hidden_columns=(
            *REVIEW_DIMENSIONS.values(),
            "重大修改",
            "问题代码",
            "问题严重级别",
            "审核人",
            "审核日期",
        ),
    )
    adjacent_review_ws = wb.create_sheet(ADJACENT_REVIEW_SHEET_NAME)
    adjacent_review_rows = _adjacent_review_rows(
        project_name,
        script_rows,
        visual_asset_rows or [],
    )
    _write_sheet(adjacent_review_ws, ADJACENT_REVIEW_OUTPUT_COLUMNS, adjacent_review_rows)
    _add_path_hyperlinks(
        adjacent_review_ws,
        ADJACENT_REVIEW_OUTPUT_COLUMNS,
        adjacent_review_rows,
        ("前镜原图路径", "后镜原图路径"),
    )
    if embed_review_previews:
        _embed_sheet_images(
            adjacent_review_ws,
            ADJACENT_REVIEW_OUTPUT_COLUMNS,
            adjacent_review_rows,
            ("前镜预览", "后镜预览"),
            clear_cell_when_embedded=True,
            max_width=240,
            max_height=135,
        )
    _add_contract_review_validation(
        adjacent_review_ws,
        ADJACENT_REVIEW_OUTPUT_COLUMNS,
        ADJACENT_REVIEW_DIMENSIONS,
    )
    _configure_sparse_review_view(
        adjacent_review_ws,
        ADJACENT_REVIEW_OUTPUT_COLUMNS,
        hidden_columns=(
            *ADJACENT_REVIEW_DIMENSIONS[:-1],
            "问题代码",
            "问题严重级别",
            "审核人",
            "审核日期",
        ),
    )
    film_review_ws = wb.create_sheet(FULL_FILM_REVIEW_SHEET_NAME)
    film_review_rows = [{"项目名称": project_name}]
    _write_sheet(film_review_ws, FULL_FILM_REVIEW_COLUMNS, film_review_rows)
    _add_contract_review_validation(film_review_ws, FULL_FILM_REVIEW_COLUMNS, FULL_FILM_REVIEW_DIMENSIONS)
    _configure_sparse_review_view(
        film_review_ws,
        FULL_FILM_REVIEW_COLUMNS,
        hidden_columns=(
            *FULL_FILM_REVIEW_DIMENSIONS[:-1],
            "问题代码",
            "问题严重级别",
            "审核人",
            "审核日期",
        ),
    )
    if audit_rows is not None:
        audit_ws = wb.create_sheet("质量审计")
        _write_sheet(audit_ws, AUDIT_COLUMNS, audit_rows)
    if parent_workbook_path is None:
        wb.save(path)
    else:
        parent_path = Path(parent_workbook_path).expanduser().resolve()
        if parent_path == Path(path).expanduser().resolve():
            raise ValueError("保存子版本不得覆盖父版本工作簿")
        _write_parent_layout_workbook(
            path, parent_path, wb, script_rows=script_rows, parent_shot_nos=parent_shot_nos,
        )


def _write_parent_layout_workbook(
    path: str | Path,
    parent_path: Path,
    generated,
    *,
    script_rows: list[dict],
    parent_shot_nos: list[str] | None,
) -> None:
    """Update managed cells in the saved layout, retaining unrelated workbook parts."""
    workbook = load_workbook(parent_path)
    try:
        parent_scripts = read_managed_sheet_rows(workbook, "脚本")
        old_positions = {str(row.get("镜号") or index): index
                         for index, row in enumerate(parent_scripts, 1)}
        origins = (list(parent_shot_nos) if parent_shot_nos is not None
                   else [str(row.get("镜号") or index) for index, row in enumerate(script_rows, 1)])
        if len(origins) != len(script_rows):
            raise ValueError("父子镜头版式映射数量与脚本不一致")
        positions = [old_positions.get(str(shot)) for shot in origins]
        updated_names = set(generated.sheetnames)
        additions = []
        if VERSION_ADDITIONS_SHEET in workbook.sheetnames:
            for values in workbook[VERSION_ADDITIONS_SHEET].iter_rows(min_row=2, values_only=True):
                if values[0] not in updated_names:
                    additions.append(dict(zip(VERSION_ADDITIONS_COLUMNS, values)))
        for source in generated.worksheets:
            if source.title not in workbook.sheetnames:
                target = workbook.create_sheet(source.title)
                _copy_generated_sheet(source, target)
                continue
            target = workbook[source.title]
            source_header, source_columns = _managed_sheet_headers(source)
            target_header, target_columns = _managed_sheet_headers(target)
            rows = (script_rows if source.title == "脚本"
                    else read_managed_sheet_rows(generated, source.title))
            if source.title == ADJACENT_REVIEW_SHEET_NAME:
                old_edges = {str(row.get("边") or ""): index for index, row in enumerate(
                    read_managed_sheet_rows(workbook, source.title), 1)}
                row_positions = [old_edges.get(f"{before}->{after}")
                                 for before, after in zip(origins, origins[1:])]
            elif source.title in {"视觉资产", "配图复核", "剪辑复核"}:
                old_shots = {str(row.get("镜号") or row.get("slot_index") or ""): index
                             for index, row in enumerate(read_managed_sheet_rows(workbook, source.title), 1)}
                child_origins = {str(row.get("镜号") or index): str(origins[index - 1])
                                 for index, row in enumerate(script_rows, 1)}
                row_positions = [old_shots.get(child_origins.get(str(row.get("镜号") or row.get("slot_index") or "")))
                                 for row in rows]
            elif source.title in {"脚本", "抽调明细"}:
                row_positions = positions
            else:
                row_positions = list(range(1, len(rows) + 1))
            _update_parent_sheet(
                target, source, rows, row_positions,
                target_header=target_header, source_header=source_header,
                target_columns=target_columns, source_columns=source_columns,
            )
            present = set(target_columns.values())
            for index, row in enumerate(rows, 1):
                for field in source_columns.values():
                    if field not in present:
                        additions.append({"工作表": source.title, "记录序号": index,
                                          "字段": field, "值": row.get(field, "")})
        if additions or VERSION_ADDITIONS_SHEET in workbook.sheetnames:
            if VERSION_ADDITIONS_SHEET in workbook.sheetnames:
                sheet = workbook[VERSION_ADDITIONS_SHEET]
                sheet.delete_rows(1, sheet.max_row)
            else:
                sheet = workbook.create_sheet(VERSION_ADDITIONS_SHEET)
            _write_sheet(sheet, VERSION_ADDITIONS_COLUMNS, additions)
            sheet.freeze_panes = "D2"
            sheet.column_dimensions["D"].width = 60
        output = BytesIO()
        workbook.save(output)
        Path(path).write_bytes(_preserve_workbook_package(parent_path, output.getvalue(), updated_names))
    finally:
        workbook.close()
        generated.close()


def _copy_generated_sheet(source, target) -> None:
    for row in source:
        for cell in row:
            _copy_cell(cell, target.cell(cell.row, cell.column))
            target.cell(cell.row, cell.column).hyperlink = copy(cell.hyperlink)
            target.cell(cell.row, cell.column).comment = copy(cell.comment)
    for name in ("row_dimensions", "column_dimensions"):
        for key, dimension in getattr(source, name).items():
            getattr(target, name)[key] = copy(dimension)
    target.data_validations = deepcopy(source.data_validations)
    target.freeze_panes = source.freeze_panes
    for merged in source.merged_cells.ranges:
        target.merge_cells(str(merged))
    for image in source._images:
        target.add_image(_copy_excel_image(image))


def _update_parent_sheet(
    target, source, rows: list[dict], positions: list[int | None], *,
    target_header: int, source_header: int,
    target_columns: dict[int, str], source_columns: dict[int, str],
) -> None:
    old_count = max(0, target.max_row - target_header)
    old_rows = {index: [copy(cell) for cell in target[target_header + index]]
                for index in range(1, old_count + 1)}
    dimensions = {index: copy(target.row_dimensions[target_header + index])
                  for index in range(1, old_count + 1)}
    old_merges = [CellRange(str(area)) for area in target.merged_cells.ranges
                  if area.max_row > target_header]
    for area in old_merges:
        target.unmerge_cells(str(area))
    target_by_name = {name: column for column, name in target_columns.items()}
    image_names = {"画面示意", "效果示意", "预览图", "前镜预览", "后镜预览"}
    image_columns = {column for column, name in target_columns.items() if name in image_names}
    old_images = {}
    kept_images = []
    for image in target._images:
        row, column = _image_cell(image)
        if row > target_header and column in image_columns:
            old_images.setdefault((row, column), []).append(image)
        else:
            kept_images.append(image)
    for index, values in enumerate(rows, 1):
        row_index = target_header + index
        origin = positions[index - 1] if index <= len(positions) else None
        old_row = old_rows.get(origin) or old_rows.get(min(index, old_count))
        if old_row is not None:
            for cell in old_row:
                destination = target.cell(row_index, cell.column)
                _copy_cell(cell, destination)
                destination.hyperlink = copy(cell.hyperlink)
                destination.comment = copy(cell.comment)
                if origin not in old_rows:
                    destination.value = None
                    destination.hyperlink = None
                    destination.comment = None
            dimension = dimensions[origin if origin in dimensions else min(index, old_count)]
            target.row_dimensions[row_index] = copy(dimension)
            target.row_dimensions[row_index].index = row_index
        for column, field in target_columns.items():
            if field in source_columns.values():
                _set_excel_value(target.cell(row_index, column), values.get(field, ""))
        if target.title == "脚本":
            for field in image_names:
                if field in target_by_name:
                    target.cell(row_index, target_by_name[field]).value = None
    if len(rows) < old_count:
        target.delete_rows(target_header + len(rows) + 1, old_count - len(rows))
        for row in range(target_header + len(rows) + 1, target_header + old_count + 1):
            target.row_dimensions.pop(row, None)
    for image in source._images:
        source_row, source_column = _image_cell(image)
        index = source_row - source_header
        field = source_columns.get(source_column)
        if not 1 <= index <= len(rows) or field not in target_by_name:
            continue
        column = target_by_name[field]
        origin = positions[index - 1] if index <= len(positions) else None
        previous = old_images.get((target_header + (origin or 0), column), [])
        old = previous[0] if previous else None
        same = old is not None and _excel_image_pixels(old) == _excel_image_pixels(image)
        selected = _copy_excel_image(old if same else image)
        if old is not None:
            selected.anchor = deepcopy(old.anchor)
        _move_image(selected, row=target_header + index, column=column)
        kept_images.append(selected)
        target.cell(target_header + index, column).value = None
    target._images = kept_images
    old_to_new = {target_header + origin: target_header + index
                  for index, origin in enumerate(positions, 1) if origin is not None}
    for area in old_merges:
        if target.title == "脚本" and area.min_col == area.max_col == 1:
            continue
        mapped = [old_to_new.get(row) for row in range(area.min_row, area.max_row + 1)]
        if mapped and None not in mapped and mapped == list(range(mapped[0], mapped[0] + len(mapped))):
            target.merge_cells(start_row=mapped[0], end_row=mapped[-1],
                               start_column=area.min_col, end_column=area.max_col)
    if target.title == "脚本" and target_header == 6:
        _merge_structure_cells(target, rows, start_row=7)
    if target.title == "脚本" and target_header == source_header == 6:
        for coordinate in ("A1", "A2"):
            target[coordinate].value = source[coordinate].value
        for column in range(2, source.max_column + 1):
            if source.cell(2, column).value and column <= target.max_column:
                target.cell(2, column).value = source.cell(2, column).value


def _image_cell(image) -> tuple[int, int]:
    if isinstance(image.anchor, str):
        column, row, _, _ = range_boundaries(image.anchor)
        return row, column
    marker = getattr(image.anchor, "_from", None)
    return (marker.row + 1, marker.col + 1) if marker is not None else (0, 0)


def _copy_excel_image(image):
    content = image._data()
    image.ref = BytesIO(content)
    copied = ExcelImage(BytesIO(content))
    copied.width, copied.height = image.width, image.height
    copied.anchor = deepcopy(image.anchor)
    return copied


def _excel_image_pixels(image) -> tuple:
    content = image._data()
    image.ref = BytesIO(content)
    with PILImage.open(BytesIO(content)) as pixels:
        return pixels.size, hashlib.sha256(pixels.convert("RGBA").tobytes()).hexdigest()


def _move_image(image, *, row: int, column: int) -> None:
    if isinstance(image.anchor, str):
        image.anchor = f"{get_column_letter(column)}{row}"
        return
    marker = getattr(image.anchor, "_from", None)
    if marker is not None:
        row_delta, col_delta = row - 1 - marker.row, column - 1 - marker.col
        for point in (marker, getattr(image.anchor, "to", None)):
            if point is not None:
                point.row += row_delta
                point.col += col_delta


_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOCUMENT_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORKSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _relationship_owner(name: str) -> str:
    folder, filename = name.rsplit("/_rels/", 1)
    return f"{folder}/{filename[:-5]}"


def _relationship_target(owner: str, target: str) -> str:
    return (target.lstrip("/") if target.startswith("/")
            else posixpath.normpath(posixpath.join(posixpath.dirname(owner), target)))


def _workbook_sheet_parts(package: dict[str, bytes]) -> dict[str, str]:
    relations = ET.fromstring(package["xl/_rels/workbook.xml.rels"])
    targets = {item.get("Id"): _relationship_target("xl/workbook.xml", item.get("Target", ""))
               for item in relations}
    workbook = ET.fromstring(package["xl/workbook.xml"])
    return {sheet.get("name", ""): targets[sheet.get(f"{{{_DOCUMENT_REL_NS}}}id")]
            for sheet in workbook.findall(f"{{{_WORKSHEET_NS}}}sheets/{{{_WORKSHEET_NS}}}sheet")}


def _preserve_workbook_package(parent_path: Path, rendered: bytes, updated_sheets: set[str]) -> bytes:
    """Retain opaque parent ZIP parts; patch supported changes into that package.

    This protects embedded OLE payloads and untouched business sheets. It does not
    claim to edit opaque objects or repair arbitrary Excel extension semantics.
    Generated drawings receive private names so they cannot overwrite an untouched
    sheet's original media or drawing parts.
    """
    with ZipFile(parent_path) as archive:
        original = {name: archive.read(name) for name in archive.namelist()}
    with ZipFile(BytesIO(rendered)) as archive:
        generated = {name: archive.read(name) for name in archive.namelist()}
    prefix = hashlib.sha256(rendered).hexdigest()[:16]
    renamed = {}
    existing_media = {hashlib.sha256(data).digest(): name for name, data in original.items()
                      if name.startswith("xl/media/")}
    for name in generated:
        if name.startswith(("xl/drawings/", "xl/media/", "xl/charts/")) and "/_rels/" not in name:
            if name.startswith("xl/media/"):
                identical = existing_media.get(hashlib.sha256(generated[name]).digest())
                if identical is not None:
                    renamed[name] = identical
                    continue
            folder, filename = name.rsplit("/", 1)
            candidate = f"{folder}/mplus-{prefix}-{filename}"
            while candidate in original:
                candidate = candidate.replace("/mplus-", "/mplus-x-", 1)
            renamed[name] = candidate
            relation = f"{folder}/_rels/{filename}.rels"
            if relation in generated:
                renamed[relation] = f"{folder}/_rels/{candidate.rsplit('/', 1)[1]}.rels"
    package = {}
    for name, data in generated.items():
        if name.endswith(".rels") and "/_rels/" in name:
            root = ET.fromstring(data)
            owner = _relationship_owner(name)
            destination_owner = renamed.get(owner, owner)
            for relation in root:
                if relation.get("TargetMode") == "External":
                    continue
                target = _relationship_target(owner, relation.get("Target", ""))
                target = renamed.get(target, target)
                relation.set("Target", posixpath.relpath(target, posixpath.dirname(destination_owner)))
            data = ET.tostring(root)
        elif name == "[Content_Types].xml":
            root = ET.fromstring(data)
            for item in root:
                part = item.get("PartName", "").lstrip("/")
                if part in renamed:
                    item.set("PartName", "/" + renamed[part])
            data = ET.tostring(root)
        package[renamed.get(name, name)] = data
    for name, data in original.items():
        package.setdefault(name, data)
    original_parts, output_parts = _workbook_sheet_parts(original), _workbook_sheet_parts(package)
    for title, source_part in original_parts.items():
        output_part = output_parts[title]
        old_rel = posixpath.join(posixpath.dirname(source_part), "_rels", posixpath.basename(source_part) + ".rels")
        new_rel = posixpath.join(posixpath.dirname(output_part), "_rels", posixpath.basename(output_part) + ".rels")
        if title not in updated_sheets and title != VERSION_ADDITIONS_SHEET:
            package[output_part] = original[source_part]
            if old_rel in original:
                package[new_rel] = original[old_rel]
            else:
                package.pop(new_rel, None)
            continue
        old_xml, new_xml = ET.fromstring(original[source_part]), ET.fromstring(package[output_part])
        new_tags = {item.tag for item in new_xml}
        opaque = [deepcopy(item) for item in old_xml if item.tag not in new_tags
                  and item.tag.rsplit("}", 1)[-1] in {
                      "oleObjects", "controls", "legacyDrawing", "legacyDrawingHF", "extLst",
                  }]
        if opaque:
            new_relations = (ET.fromstring(package[new_rel]) if new_rel in package
                             else ET.Element(f"{{{_PACKAGE_REL_NS}}}Relationships"))
            old_relations = ET.fromstring(original[old_rel]) if old_rel in original else []
            used_ids = {relation.get("Id") for relation in new_relations}
            wanted_ids = {value for element in opaque for node in element.iter()
                          for key, value in node.attrib.items() if key.startswith(f"{{{_DOCUMENT_REL_NS}}}")}
            for relation in old_relations:
                identifier = relation.get("Id")
                if identifier not in wanted_ids:
                    continue
                copied = deepcopy(relation)
                replacement = identifier
                while replacement in used_ids:
                    replacement = f"preserved-{replacement}"
                copied.set("Id", replacement)
                used_ids.add(replacement)
                new_relations.append(copied)
                for element in opaque:
                    for node in element.iter():
                        for key, value in list(node.attrib.items()):
                            if key.startswith(f"{{{_DOCUMENT_REL_NS}}}") and value == identifier:
                                node.set(key, replacement)
            old_order = [item.tag for item in old_xml]
            for element in opaque:
                following = set(old_order[old_order.index(element.tag) + 1:])
                next_index = next((index for index, item in enumerate(new_xml) if item.tag in following), len(new_xml))
                new_xml.insert(next_index, element)
            package[output_part] = ET.tostring(new_xml)
            package[new_rel] = ET.tostring(new_relations)
    for name in ("_rels/.rels", "xl/_rels/workbook.xml.rels"):
        if name not in original or name not in package:
            continue
        relations = ET.fromstring(package[name])
        known_types = {relation.get("Type") for relation in relations}
        known_ids = {relation.get("Id") for relation in relations}
        for relation in ET.fromstring(original[name]):
            if relation.get("Type") in known_types:
                continue
            copied = deepcopy(relation)
            identifier = copied.get("Id", "preserved")
            while identifier in known_ids:
                identifier = f"preserved-{identifier}"
            copied.set("Id", identifier)
            relations.append(copied)
            known_ids.add(identifier)
        package[name] = ET.tostring(relations)
    content_types = ET.fromstring(package["[Content_Types].xml"])
    known_types = {(item.tag, item.get("PartName") or item.get("Extension")) for item in content_types}
    for item in ET.fromstring(original["[Content_Types].xml"]):
        key = item.tag, item.get("PartName") or item.get("Extension")
        if key not in known_types:
            content_types.append(deepcopy(item))
            known_types.add(key)
    # Independent OPC readers reject a prefixed Types root. Serialize this
    # package envelope with a local default namespace, without changing the
    # global ElementTree namespace registry used by concurrent workbook writes.
    content_namespace = "http://schemas.openxmlformats.org/package/2006/content-types"
    for item in content_types.iter():
        if item.tag.startswith(f"{{{content_namespace}}}"):
            item.tag = item.tag.split("}", 1)[1]
    content_types.set("xmlns", content_namespace)
    package["[Content_Types].xml"] = ET.tostring(content_types)
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in package.items():
            archive.writestr(name, data)
    return output.getvalue()


def build_script_review_workbook(source_path: str | Path, *, version_no: int) -> bytes:
    """Copy a saved workbook for review without rebuilding its template layout."""
    workbook = load_workbook(Path(source_path).expanduser().resolve())
    try:
        sheet = workbook["脚本"]
        label = f"复核稿，非正式交付 · 版本 {version_no}"
        sheet["A1"] = f"{sheet['A1'].value or '脚本'}｜{label}"
        workbook.properties.subject = label
        updated_sheets = _scrub_delivery_workbook_internals(workbook) | {"脚本"}
        updated_sheets |= _prepare_unconfigured_review_layout(workbook)
        output = BytesIO()
        workbook.save(output)
        return _preserve_workbook_package(Path(source_path), output.getvalue(), updated_sheets)
    finally:
        workbook.close()


def _prepare_unconfigured_review_layout(workbook) -> set[str]:
    """Fill missing presentation settings on a copy, retaining configured layouts."""
    updated = set()
    for sheet in workbook:
        if sheet.title not in {"脚本", "抽调明细", "视觉资产", "配图复核", "剪辑复核",
                              ADJACENT_REVIEW_SHEET_NAME, FULL_FILM_REVIEW_SHEET_NAME,
                              "质量审计", "审核记录"}:
            continue
        if sheet.print_area:
            continue
        sheet.print_area = sheet.calculate_dimension()
        if not sheet.print_title_rows:
            sheet.print_title_rows = "1:6" if sheet.title == "脚本" else "1:1"
        if sheet.title == "脚本" and not sheet.print_title_cols:
            sheet.print_title_cols = "A:B"
        sheet.page_setup.orientation = sheet.page_setup.orientation or "landscape"
        sheet.page_setup.paperSize = sheet.page_setup.paperSize or sheet.PAPERSIZE_A3
        # Keep the normal font size across horizontal pages; a wide evidence
        # sheet fitted onto one page would make its text unreadable.
        if (sheet.page_setup.scale is None and sheet.page_setup.fitToWidth is None
                and not sheet.sheet_properties.pageSetUpPr.fitToPage):
            sheet.page_setup.scale = 100
        for row in sheet.iter_rows(min_row=7 if sheet.title == "脚本" else 2):
            required = sheet.row_dimensions[row[0].row].height or sheet.sheet_format.defaultRowHeight or 15
            for cell in row:
                if not cell.value or not cell.alignment.wrap_text or cell.__class__.__name__ == "MergedCell":
                    continue
                font_size = cell.font.sz or 11
                width = sheet.column_dimensions[cell.column_letter].width or 13
                capacity = max(1, width * 11 / font_size - 2)
                lines = sum(max(1, math.ceil(sum(
                    2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
                    for char in paragraph
                ) / capacity)) for paragraph in str(cell.value).split("\n"))
                required = max(required, lines * font_size * 1.4 + 8)
            sheet.row_dimensions[row[0].row].height = min(409.5, required)
        updated.add(sheet.title)
    return updated


def write_reviewed_delivery_workbook(
    source_path: str | Path,
    target_path: str | Path,
    *,
    review_model: dict,
) -> None:
    """Project authoritative review results into a delivery-only workbook copy."""

    source = Path(source_path).expanduser().resolve()
    target = Path(target_path).expanduser().resolve()
    if source == target:
        raise ValueError("正式交付不得覆盖版本源文件")
    issues = list(review_model.get("issues") or [])
    decisions = {
        str(item.get("issue_key") or ""): item
        for item in review_model.get("decisions") or []
    }
    final = dict(review_model.get("final_confirmation") or {})
    if (
        not issues
        or any(item.get("status") != "approve" for item in issues)
        or final.get("decision") != "quality_pass"
    ):
        raise ValueError("正式交付只能写入已逐项通过并完成整片确认的审核结果")

    workbook = load_workbook(source)
    try:
        if "审核记录" in workbook.sheetnames:
            workbook.remove(workbook["审核记录"])
        log_rows = []
        by_shot: dict[str, list[dict]] = {}
        for issue in issues:
            issue_key = str(issue.get("issue_key") or "")
            decision = decisions.get(issue_key, {})
            if decision.get("decision") != "approve":
                raise ValueError("正式交付缺少逐项人工通过记录")
            shot_no = str(issue.get("shot_no") or "")
            by_shot.setdefault(shot_no, []).append(issue)
            log_rows.append({
                "审核范围": "版本变化" if issue.get("kind") == "change" else "图片与来源",
                "镜号": shot_no,
                "结论": "通过",
                "处理人": str(decision.get("reviewer_label") or "已由有权限人员处理"),
                "处理时间": str(decision.get("decided_at") or ""),
            })
        log_rows.append({
            "审核范围": "整片确认",
            "镜号": "全部",
            "结论": "通过",
            "处理人": str(final.get("reviewer_label") or "已由有权限人员处理"),
            "处理时间": str(final.get("confirmed_at") or ""),
        })
        log_sheet = workbook.create_sheet("审核记录")
        _write_sheet(log_sheet, DELIVERY_REVIEW_COLUMNS, log_rows)
        log_sheet.freeze_panes = "A2"

        for index, row in enumerate(read_managed_sheet_rows(workbook, "剪辑复核"), 1):
            shot_no = str(row.get("镜号") or "")
            shot_issues = by_shot.get(shot_no, [])
            if not shot_issues:
                raise ValueError(f"第 {shot_no} 镜缺少权威审核结果")
            kinds = {str(item.get("kind") or "") for item in shot_issues}
            latest = max(
                (decisions[str(item.get("issue_key") or "")] for item in shot_issues),
                key=lambda item: str(item.get("decided_at") or ""),
            )
            _set_managed_row_values(workbook, "剪辑复核", index, {
                "总体审核": "通过",
                "修改意见": "图片与来源已审核；版本变化已审核" if "change" in kinds else "图片与来源已审核",
                "审核人": str(latest.get("reviewer_label") or "已由有权限人员处理"),
                "审核日期": str(latest.get("decided_at") or ""),
            })
        _set_managed_row_values(workbook, "整片复核", 1, {
            "总体审核": "通过", "修改意见": "整片人工确认已完成",
            "审核人": str(final.get("reviewer_label") or "已由有权限人员处理"),
            "审核日期": str(final.get("confirmed_at") or ""),
        })
        updated_sheets = _scrub_delivery_workbook_internals(workbook) | {"剪辑复核", "整片复核", "审核记录"}
        updated_sheets |= _prepare_unconfigured_review_layout(workbook)
        output = BytesIO()
        workbook.save(output)
        target.write_bytes(_preserve_workbook_package(source, output.getvalue(), updated_sheets))
    finally:
        workbook.close()


def _set_managed_row_values(workbook, sheet_name: str, record: int, values: dict) -> None:
    sheet = workbook[sheet_name]
    header, columns = _managed_sheet_headers(sheet)
    by_name = {name: column for column, name in columns.items()}
    for field, value in values.items():
        if field in by_name:
            _set_excel_value(sheet.cell(header + record, by_name[field]), value)
            continue
        if VERSION_ADDITIONS_SHEET not in workbook.sheetnames:
            additions = workbook.create_sheet(VERSION_ADDITIONS_SHEET)
            _write_sheet(additions, VERSION_ADDITIONS_COLUMNS, [])
        additions = workbook[VERSION_ADDITIONS_SHEET]
        existing = next((row for row in additions.iter_rows(min_row=2)
                         if row[0].value == sheet_name and row[1].value == record and row[2].value == field), None)
        if existing is not None:
            _set_excel_value(existing[3], value)
        else:
            _append_excel_row(additions, [sheet_name, record, field, value])


def _scrub_delivery_workbook_internals(workbook) -> set[str]:
    updated = set()
    if VERSION_ADDITIONS_SHEET in workbook.sheetnames:
        additions = workbook[VERSION_ADDITIONS_SHEET]
        private_rows = [row[0].row for row in additions.iter_rows(min_row=2)
                        if len(row) >= 4 and any(token in str(row[2].value or "").casefold()
                                               for token in ("路径", "sha256", "artifact", "规划事件id"))]
        for row in reversed(private_rows):
            additions.delete_rows(row)
            updated.add(VERSION_ADDITIONS_SHEET)
    for sheet in workbook.worksheets:
        if sheet.title == "质量审计":
            for row in sheet.iter_rows():
                for cell in row:
                    if not isinstance(cell.value, str):
                        continue
                    corrected = cell.value.replace(
                        "参考来源命中 Brief 禁忌，但仅作为已审核镜头依据:",
                        "参考来源命中 Brief 禁忌，需核对最终镜头表达:",
                    ).replace(
                        "允许保留来源；人工复核最终文案和画面描述已完成当前项目适配",
                        "允许保留参考来源；请人工核对最终文案和画面描述是否符合当前项目要求",
                    ).replace(
                        "该镜头没有可用嵌入图，但已审核文字和来源三元组仍可作为脚本文档依据",
                        "该镜头没有可用嵌入图；文字和来源三元组保留供人工核对，配图仍待确认",
                    )
                    if corrected != cell.value:
                        cell.value = corrected
                        updated.add(sheet.title)
        technical_columns = [
            cell.column
            for cell in sheet[1]
            if any(
                token in str(cell.value or "").casefold()
                for token in ("路径", "sha256", "artifact", "规划事件id")
            )
            and str(cell.value or "").casefold() != "content_sha256"
        ]
        _delete_delivery_columns(sheet, technical_columns)
        if technical_columns:
            updated.add(sheet.title)
        for row in sheet.iter_rows():
            for cell in row:
                value = str(cell.value or "")
                lowered = value.casefold()
                if (
                    "artifact://" in lowered
                    or "sha256=" in lowered
                    or value.startswith(("/", "~/", "file://"))
                ):
                    cell.value = None
                    cell.hyperlink = None
                    updated.add(sheet.title)
    return updated


def _delete_delivery_columns(sheet, columns: list[int]) -> None:
    """Remove internal columns while retaining the generated review layout."""
    if not columns:
        return
    removed = set(columns)

    def shifted(column: int) -> int:
        return column - sum(value < column for value in removed)

    def surviving_range(area):
        remaining = [col for col in range(area.min_col, area.max_col + 1) if col not in removed]
        if not remaining:
            return None
        return CellRange(min_col=shifted(remaining[0]), max_col=shifted(remaining[-1]), min_row=area.min_row, max_row=area.max_row)

    dimensions = []
    for key, dimension in sheet.column_dimensions.items():
        first = dimension.min or column_index_from_string(key)
        last = dimension.max or first
        remaining = [col for col in range(first, last + 1) if col not in removed]
        if remaining:
            moved = copy(dimension)
            moved.min, moved.max = shifted(remaining[0]), shifted(remaining[-1])
            moved.index = get_column_letter(moved.min)
            dimensions.append(moved)
    for image in sheet._images:
        for marker in (getattr(image.anchor, "_from", None), getattr(image.anchor, "to", None)):
            if marker is not None:
                marker.col = shifted(marker.col + 1) - 1
    for validation in list(sheet.data_validations.dataValidation):
        ranges = [surviving_range(area) for area in validation.sqref.ranges]
        validation.sqref = " ".join(str(area) for area in ranges if area is not None)
        if not validation.sqref:
            sheet.data_validations.dataValidation.remove(validation)
    for column in sorted(removed, reverse=True):
        sheet.delete_cols(column)
    sheet.column_dimensions.clear()
    for dimension in dimensions:
        sheet.column_dimensions[dimension.index] = dimension
    for image in sheet._images:
        marker = getattr(image.anchor, "_from", None)
        extent = getattr(image.anchor, "ext", None)
        if marker is None or extent is None or "预览" not in str(sheet.cell(1, marker.col + 1).value or ""):
            continue
        width = sheet.column_dimensions[get_column_letter(marker.col + 1)].width
        # Generated review sheets use the default 11pt Calibri column units.
        # Leave a small gutter, resizing only the drawing, never its image bytes.
        available = pixels_to_EMU(max(1, int(width * 7 + 5) - 8)) - marker.colOff
        if 0 < available < extent.cx:
            extent.cy = round(extent.cy * available / extent.cx)
            extent.cx = available


def scrub_workbook_paths(path: str | Path, controlled_paths: set[Path]) -> None:
    sensitive = {str(item.resolve()) for item in controlled_paths}
    workbook = load_workbook(Path(path).expanduser().resolve())
    try:
        updated_sheets = set()
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    value = str(cell.value or "")
                    if any(item in value for item in sensitive) or value.startswith("file://"):
                        cell.value = None
                        updated_sheets.add(sheet.title)
                    if cell.hyperlink is not None and (
                        str(cell.hyperlink.target or "").startswith("file://")
                        or any(item in str(cell.hyperlink.target or "") for item in sensitive)
                    ):
                        cell.hyperlink = None
                        updated_sheets.add(sheet.title)
        output = BytesIO()
        workbook.save(output)
        Path(path).write_bytes(_preserve_workbook_package(Path(path), output.getvalue(), updated_sheets))
    finally:
        workbook.close()


def _write_template_like_script_sheet(
    ws,
    rows: list[dict],
    template_workbook_path: Path,
    project_name: str = "",
    overall_summary: str = "",
    script_type: str = "发布会",
    duration_seconds: int = 60,
) -> None:
    template_wb = load_workbook(template_workbook_path)
    try:
        template_ws = template_wb["模版"]
        content_column_count = _template_content_column_count(template_ws)
        column_map = _template_script_column_map(template_ws, content_column_count)
        _copy_template_header(
            template_ws,
            ws,
            project_name=project_name,
            overall_summary=overall_summary,
            script_type=script_type,
            duration_seconds=duration_seconds,
            content_column_count=content_column_count,
        )
        _write_template_data_rows(
            template_ws,
            ws,
            rows,
            content_column_count=content_column_count,
            column_map=column_map,
        )
        _append_template_script_text_columns(
            ws, rows, column_map=column_map, content_column_count=content_column_count,
        )
        _merge_structure_cells(ws, rows, start_row=7)
        _embed_template_images(
            ws,
            rows,
            column_map,
        )
    finally:
        template_wb.close()


def _append_template_script_text_columns(
    ws,
    rows: list[dict],
    *,
    column_map: dict[str, int],
    content_column_count: int,
) -> None:
    next_column = content_column_count + 1
    for field in ("旁白", "字幕"):
        if field in column_map or not any(field in row for row in rows):
            continue
        header = ws.cell(6, next_column)
        _copy_cell(ws.cell(6, content_column_count), header)
        header.value = field
        ws.column_dimensions[header.column_letter].width = 24
        for row_index, row in enumerate(rows, start=7):
            cell = ws.cell(row_index, next_column)
            _copy_cell(ws.cell(row_index, content_column_count), cell)
            _set_excel_value(cell, row.get(field, ""))
        next_column += 1


def _copy_template_header(
    source_ws,
    target_ws,
    project_name: str = "",
    overall_summary: str = "",
    script_type: str = "发布会",
    duration_seconds: int = 60,
    content_column_count: int | None = None,
) -> None:
    content_column_count = content_column_count or len(SCRIPT_COLUMNS)
    for col_idx in range(1, content_column_count + 1):
        letter = get_column_letter(col_idx)
        target_ws.column_dimensions[letter].width = source_ws.column_dimensions[letter].width

    for row_idx in range(1, 7):
        target_ws.row_dimensions[row_idx].height = source_ws.row_dimensions[row_idx].height
        for col_idx in range(1, content_column_count + 1):
            _copy_cell(source_ws.cell(row_idx, col_idx), target_ws.cell(row_idx, col_idx))

    summary_column = next(
        (
            col_idx
            for col_idx in range(2, content_column_count + 1)
            if str(source_ws.cell(2, col_idx).value or "").strip()
        ),
        2,
    )
    for row_idx in range(2, 5):
        for col_idx in range(2, content_column_count + 1):
            target_ws.cell(row_idx, col_idx).value = None

    for merged_range in source_ws.merged_cells.ranges:
        min_col, min_row, max_col, max_row = range_boundaries(str(merged_range))
        if max_row <= 6 and max_col <= content_column_count:
            target_ws.merge_cells(str(merged_range))
        elif max_row <= 6 and min_col <= content_column_count:
            target_ws.merge_cells(
                start_row=min_row,
                start_column=min_col,
                end_row=max_row,
                end_column=content_column_count,
            )
    if project_name:
        target_ws["A1"] = f"{project_name}｜{duration_seconds}秒{normalize_script_type(script_type)}脚本"
    target_ws["A2"] = "整体说明"
    target_ws.cell(2, summary_column).value = (
        str(overall_summary).strip()
        or f"本脚本围绕{project_name or '当前项目'}的{normalize_script_type(script_type)}叙事生成，"
        "镜数、结构和参考依据详见下方分镜。"
    )


def _write_template_data_rows(
    source_ws,
    target_ws,
    rows: list[dict],
    *,
    content_column_count: int | None = None,
    column_map: dict[str, int] | None = None,
) -> None:
    content_column_count = content_column_count or len(SCRIPT_COLUMNS)
    column_map = column_map or {
        name: index for index, name in enumerate(SCRIPT_COLUMNS, start=1)
    }
    template_row_height = max(source_ws.row_dimensions[7].height or 148.5, 210)
    for offset, row in enumerate(rows, start=7):
        target_ws.row_dimensions[offset].height = template_row_height
        for col_idx in range(1, content_column_count + 1):
            cell = target_ws.cell(offset, col_idx)
            _copy_cell(source_ws.cell(7, col_idx), cell)
            cell.value = None
        for column_name, col_idx in column_map.items():
            value = row.get(
                "画面描述" if column_name == "通用描述" else column_name,
                "",
            )
            _set_excel_value(target_ws.cell(offset, col_idx), value)


def _template_content_column_count(ws) -> int:
    for col_idx, label in enumerate(_merged_headers(_row_values(ws, 5), _row_values(ws, 6)), 1):
        if label == "镜头功能":
            return max(1, col_idx - 1)
    return min(ws.max_column, len(SCRIPT_COLUMNS))


def _template_script_column_map(ws, content_column_count: int) -> dict[str, int]:
    mapping = {"结构": 1, "镜号": 2}
    aliases = {"机位": "类型"}
    headers = _merged_headers(_row_values(ws, 5), _row_values(ws, 6))
    for col_idx, label in enumerate(headers[:content_column_count], 1):
        if col_idx < 3 or not label:
            continue
        mapping[aliases.get(label, label)] = col_idx
    return mapping


def _embed_template_images(ws, rows: list[dict], column_map: dict[str, int]) -> None:
    for column_name in ("画面示意", "效果示意"):
        col_idx = column_map.get(column_name)
        if not col_idx:
            continue
        letter = get_column_letter(col_idx)
        ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, 112)
        for row_idx, row in enumerate(rows, start=7):
            image_path = _first_existing_image_path(row.get(column_name, ""))
            if not image_path:
                continue
            try:
                image = _excel_image_from_path(
                    image_path,
                    max_width=760,
                    max_height=260,
                    preserve_original_pixels=True,
                )
            except Exception:
                continue
            ws.row_dimensions[row_idx].height = max(ws.row_dimensions[row_idx].height or 0, 210)
            ws.cell(row=row_idx, column=col_idx).value = None
            ws.add_image(image, ws.cell(row=row_idx, column=col_idx).coordinate)


def _copy_cell(source_cell, target_cell) -> None:
    target_cell.value = source_cell.value
    if source_cell.has_style:
        target_cell._style = copy(source_cell._style)
    if source_cell.number_format:
        target_cell.number_format = source_cell.number_format
    if source_cell.font:
        target_cell.font = copy(source_cell.font)
    if source_cell.fill:
        target_cell.fill = copy(source_cell.fill)
    if source_cell.border:
        target_cell.border = copy(source_cell.border)
    if source_cell.alignment:
        target_cell.alignment = copy(source_cell.alignment)
    if source_cell.protection:
        target_cell.protection = copy(source_cell.protection)


def _merge_structure_cells(ws, rows: list[dict], start_row: int) -> None:
    group_start = start_row
    previous = None
    for index, row in enumerate(rows, start=start_row):
        structure = row.get("结构", "")
        if previous is None:
            previous = structure
            continue
        if structure != previous:
            if index - group_start > 1:
                ws.merge_cells(start_row=group_start, start_column=1, end_row=index - 1, end_column=1)
            group_start = index
            previous = structure
    if rows and start_row + len(rows) - group_start > 1:
        ws.merge_cells(start_row=group_start, start_column=1, end_row=start_row + len(rows) - 1, end_column=1)


def _write_sheet(ws, columns: list[str], rows: list[dict]) -> None:
    _append_excel_row(ws, columns)
    for cell in ws[1]:
        cell.font = Font(name="Arial", bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row in rows:
        _append_excel_row(ws, [row.get(column, "") for column in columns])
    for col in ws.columns:
        letter = col[0].column_letter
        max_len = max(len(str(cell.value or "")) for cell in col)
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 36)
    for row in ws.iter_rows():
        for cell in row:
            cell.font = Font(name="Arial")
            cell.alignment = Alignment(wrap_text=True, vertical="top")


def _append_excel_row(ws, values) -> None:
    row_index = (
        1
        if ws.max_row == 1 and ws.max_column == 1 and ws.cell(1, 1).value is None
        else ws.max_row + 1
    )
    for column_index, value in enumerate(values, start=1):
        _set_excel_value(ws.cell(row_index, column_index), value)


def _set_excel_value(cell, value) -> None:
    cell.value = value
    if isinstance(value, str) and value.lstrip().startswith("="):
        cell.data_type = "s"


def _add_editorial_review_validation(ws) -> None:
    validation = DataValidation(
        type="list",
        formula1=f'"{",".join(VALID_REVIEW_STATUSES)}"',
        allow_blank=True,
    )
    validation.error = "请选择：通过、需修改或不通过"
    validation.errorTitle = "审核结果无效"
    validation.showErrorMessage = True
    ws.add_data_validation(validation)
    end_row = max(ws.max_row, 2)
    for column in [*REVIEW_DIMENSIONS.values(), OVERALL_REVIEW_COLUMN]:
        column_index = EDITORIAL_REVIEW_COLUMNS.index(column) + 1
        letter = get_column_letter(column_index)
        validation.add(f"{letter}2:{letter}{end_row}")
    _add_list_validation(ws, EDITORIAL_REVIEW_COLUMNS, "重大修改", VALID_MAJOR_CHANGE_VALUES)
    _add_list_validation(ws, EDITORIAL_REVIEW_COLUMNS, "问题严重级别", VALID_SEVERITIES)
    _add_list_validation(ws, EDITORIAL_REVIEW_COLUMNS, "问题代码", sorted(VALID_ISSUE_CODES))


def _add_contract_review_validation(ws, columns: list[str], review_columns: tuple[str, ...]) -> None:
    for column in review_columns:
        _add_list_validation(ws, columns, column, VALID_REVIEW_STATUSES)
    _add_list_validation(ws, columns, "问题严重级别", VALID_SEVERITIES)
    _add_list_validation(ws, columns, "问题代码", sorted(VALID_ISSUE_CODES))


def _add_list_validation(ws, columns: list[str], column: str, values) -> None:
    if column not in columns:
        return
    validation = DataValidation(
        type="list",
        formula1=f'"{",".join(values)}"',
        allow_blank=True,
    )
    validation.error = "请选择列表中的值"
    validation.errorTitle = "审核值无效"
    validation.showErrorMessage = True
    ws.add_data_validation(validation)
    column_index = columns.index(column) + 1
    letter = get_column_letter(column_index)
    validation.add(f"{letter}2:{letter}{max(ws.max_row, 2)}")


def _configure_sparse_review_view(ws, columns: list[str], hidden_columns) -> None:
    for column in hidden_columns:
        if column not in columns:
            continue
        letter = get_column_letter(columns.index(column) + 1)
        ws.column_dimensions[letter].hidden = True
    if OVERALL_REVIEW_COLUMN in columns:
        overall_cell = ws.cell(1, columns.index(OVERALL_REVIEW_COLUMN) + 1)
        overall_cell.comment = Comment(
            "只需在本列填写一个明确结论；其他审核列均为可选，空白不会阻断审核。",
            "Loomtake",
        )


def _add_path_hyperlinks(
    ws,
    columns: list[str],
    rows: list[dict],
    path_columns: tuple[str, ...],
) -> None:
    for column_name in path_columns:
        if column_name not in columns:
            continue
        column_index = columns.index(column_name) + 1
        for row_index, row in enumerate(rows, start=2):
            path = _first_existing_image_path(row.get(column_name, ""))
            if not path:
                continue
            cell = ws.cell(row=row_index, column=column_index)
            cell.hyperlink = path.resolve().as_uri()
            cell.style = "Hyperlink"


def _embed_sheet_images(
    ws,
    columns: list[str],
    rows: list[dict],
    image_columns: tuple[str, ...],
    start_row: int = 2,
    clear_cell_when_embedded: bool = False,
    max_width: int = 140,
    max_height: int = 90,
    preserve_original_pixels: bool = False,
) -> None:
    for column_name in image_columns:
        if column_name not in columns:
            continue
        col_idx = columns.index(column_name) + 1
        letter = ws.cell(row=start_row, column=col_idx).column_letter
        min_width = 112 if clear_cell_when_embedded and column_name == "画面示意" else 22
        ws.column_dimensions[letter].width = max(ws.column_dimensions[letter].width or 0, min_width)
        for row_idx, row in enumerate(rows, start=start_row):
            image_path = _first_existing_image_path(row.get(column_name, ""))
            if not image_path:
                continue
            try:
                image = _excel_image_from_path(
                    image_path,
                    max_width=max_width,
                    max_height=max_height,
                    preserve_original_pixels=preserve_original_pixels,
                )
            except Exception:
                continue
            min_height = 210 if clear_cell_when_embedded else 76
            ws.row_dimensions[row_idx].height = max(ws.row_dimensions[row_idx].height or 0, min_height)
            if clear_cell_when_embedded:
                ws.cell(row=row_idx, column=col_idx).value = None
            ws.add_image(image, ws.cell(row=row_idx, column=col_idx).coordinate)


def _first_existing_image_path(value: str) -> Path | None:
    for part in str(value or "").split(";"):
        text = part.strip()
        if not text:
            continue
        path = Path(text)
        if path.exists() and (
            path.suffix.lower() in IMAGE_EXTENSIONS or not path.suffix
        ):
            return path
    return None


def _excel_image_from_path(
    path: Path,
    max_width: int,
    max_height: int,
    preserve_original_pixels: bool = False,
) -> ExcelImage:
    image = PILImage.open(path)
    if getattr(image, "is_animated", False):
        image.seek(0)
    image = image.convert("RGB")
    if preserve_original_pixels:
        width, height = image.size
        scale = min(max_width / width, max_height / height, 1)
        data = BytesIO()
        image.save(data, format="PNG")
        data.seek(0)
        excel_image = ExcelImage(data)
        excel_image.width = int(width * scale)
        excel_image.height = int(height * scale)
        return excel_image
    image.thumbnail((max_width, max_height))
    data = BytesIO()
    image.save(data, format="PNG")
    data.seek(0)
    return ExcelImage(data)


def _embed_visual_asset_previews(ws, rows: list[dict]) -> None:
    if "预览图" not in VISUAL_ASSET_COLUMNS:
        return
    preview_col = VISUAL_ASSET_COLUMNS.index("预览图") + 1
    preview_letter = ws.cell(row=1, column=preview_col).column_letter
    ws.column_dimensions[preview_letter].width = 100
    for row_idx, row in enumerate(rows, start=2):
        image_path = _first_existing_image_path(row.get("原图路径", ""))
        if not image_path:
            continue
        try:
            image = _excel_image_from_path(
                image_path,
                max_width=960,
                max_height=540,
                preserve_original_pixels=True,
            )
        except Exception:
            continue
        ws.row_dimensions[row_idx].height = max(ws.row_dimensions[row_idx].height or 0, 420)
        ws.add_image(image, ws.cell(row=row_idx, column=preview_col).coordinate)


def _pairing_review_rows(script_rows: list[dict], visual_asset_rows: list[dict]) -> list[dict]:
    script_by_shot = {str(row.get("镜号") or ""): row for row in script_rows}
    rows = []
    for asset in visual_asset_rows:
        shot_no = str(asset.get("镜号") or "")
        script_row = script_by_shot.get(shot_no, {})
        rows.append({
            "镜号": shot_no,
            "结构": script_row.get("结构", ""),
            "画面描述": script_row.get("画面描述", ""),
            "配图判断": asset.get("配图判断", ""),
            "视觉匹配分": asset.get("视觉匹配分", ""),
            "配图置信度": asset.get("配图置信度", ""),
            "视觉读取状态": asset.get("视觉读取状态", ""),
            "视觉内容摘要": asset.get("视觉内容摘要", ""),
            "视觉功能标签": asset.get("视觉功能标签", ""),
            "主视觉功能": asset.get("主视觉功能", ""),
            "槽位允许功能": asset.get("槽位允许功能", ""),
            "功能适配判断": asset.get("功能适配判断", ""),
            "功能重复组": asset.get("功能重复组", ""),
            "命中视觉要素": asset.get("命中视觉要素", ""),
            "缺失视觉要素": asset.get("缺失视觉要素", ""),
            "冲突视觉要素": asset.get("冲突视觉要素", ""),
            "质量指标": asset.get("质量指标", ""),
            "原图路径": asset.get("原图路径", ""),
            "预览图": "",
        })
    return rows


def _adjacent_review_rows(
    project_name: str,
    script_rows: list[dict],
    visual_asset_rows: list[dict],
) -> list[dict]:
    scripts = {str(row.get("镜号") or ""): row for row in script_rows}
    assets = {
        str(row.get("镜号") or row.get("slot_index") or ""): row
        for row in visual_asset_rows
    }
    rows = []
    for edge in expected_adjacent_edges(len(script_rows)):
        before, after = edge.split("->", 1)
        before_script = scripts.get(before, {})
        after_script = scripts.get(after, {})
        before_path = assets.get(before, {}).get("原图路径", "")
        after_path = assets.get(after, {}).get("原图路径", "")
        rows.append({
            "项目名称": project_name,
            "边": edge,
            "前镜画面描述": before_script.get("画面描述", ""),
            "后镜画面描述": after_script.get("画面描述", ""),
            "前镜声音": before_script.get("声音", ""),
            "后镜声音": after_script.get("声音", ""),
            "前镜原图路径": before_path,
            "后镜原图路径": after_path,
            "前镜预览": before_path,
            "后镜预览": after_path,
        })
    return rows


def _embed_pairing_review_previews(ws, rows: list[dict]) -> None:
    preview_col = PAIRING_REVIEW_COLUMNS.index("预览图") + 1
    preview_letter = ws.cell(row=1, column=preview_col).column_letter
    ws.column_dimensions[preview_letter].width = 100
    for row_idx, row in enumerate(rows, start=2):
        image_path = _first_existing_image_path(row.get("原图路径", ""))
        if not image_path:
            continue
        try:
            image = _excel_image_from_path(
                image_path,
                max_width=960,
                max_height=540,
                preserve_original_pixels=True,
            )
        except Exception:
            continue
        ws.row_dimensions[row_idx].height = max(ws.row_dimensions[row_idx].height or 0, 420)
        ws.add_image(image, ws.cell(row=row_idx, column=preview_col).coordinate)
