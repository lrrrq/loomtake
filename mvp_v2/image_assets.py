from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image as PILImage

from .excel_io import IMAGE_EXTENSIONS


MANIFEST_FIELDS = [
    "image_id",
    "原图路径",
    "预览路径",
    "来源文件",
    "来源 sheet",
    "来源行",
    "项目名",
    "活动类型",
    "图片角色",
    "尺寸",
    "文件大小(bytes)",
    "hash",
    "是否低清",
    "是否来自 Excel 嵌图",
    "是否来自项目图片池",
    "是否来自视频抽帧",
    "可回查锚点",
]


def build_image_manifest(
    source_root: str | Path,
    project_hint: str = "",
    preview_root: str | Path | None = None,
    script_type: str = "",
) -> list[dict]:
    root = Path(source_root)
    rows = []
    for path in _iter_image_paths(root, project_hint):
        rows.append(_asset_row(path, root, project_hint, preview_root, script_type))
    rows.sort(key=lambda row: (row["项目名"], row["原图路径"]))
    return rows


def build_reviewed_image_manifest(
    source_root: str | Path,
    review: dict[tuple[str, str, int], list[dict]] | None,
    script_type: str = "发布会",
) -> list[dict]:
    root = Path(source_root)
    rows = []
    for (source_file, source_sheet, source_row), records in (review or {}).items():
        for record in records:
            if str(record.get("verdict") or "") != "可用":
                continue
            raw_path = Path(str(record.get("path") or ""))
            path = raw_path if raw_path.is_absolute() else root / raw_path
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            project_name = f"{script_type}创意库"
            row = _asset_row(path, root, project_name, None, script_type)
            row.update({
                "来源文件": source_file,
                "来源 sheet": source_sheet,
                "来源行": str(source_row),
                "项目名": project_name,
                "活动类型": script_type,
                "是否来自 Excel 嵌图": True,
                "是否来自项目图片池": False,
                "可复用等级": "人工审核可用",
                "图片审核结论": "可用",
                "可回查锚点": _anchor(source_file, source_sheet, str(source_row)),
            })
            rows.append(row)
    return sorted(rows, key=lambda row: (row["来源文件"], row["来源 sheet"], row["来源行"], row["原图路径"]))


def build_supplemental_image_asset(
    path: str | Path,
    project_name: str = "",
    script_type: str = "",
) -> dict:
    image = Path(path).expanduser().resolve()
    if not image.is_file() or image.suffix.casefold() not in IMAGE_EXTENSIONS:
        raise ValueError(f"补充图片不存在或格式不支持: {image}")
    row = _asset_row(image, image.parent, project_name, None, script_type)
    row.update({
        "来源文件": str(image),
        "来源 sheet": "用户补充图片",
        "来源行": "manual",
        "是否来自项目图片池": True,
        "可回查锚点": str(image),
    })
    return row


def build_video_frame_asset(
    video_path: str | Path,
    frame_path: str | Path,
    timecode: str,
    frame_index: int,
    project_name: str = "",
) -> dict:
    frame = Path(frame_path)
    row = _asset_row(frame, frame.parent, project_name, None)
    video = Path(video_path)
    row.update({
        "image_id": _image_id(frame, extra=f"{video.name}|{timecode}|{frame_index}"),
        "来源文件": str(video),
        "来源 sheet": "video_keyframe",
        "来源行": str(frame_index),
        "项目名": project_name,
        "图片角色": "视频关键帧",
        "是否来自 Excel 嵌图": False,
        "是否来自项目图片池": False,
        "是否来自视频抽帧": True,
        "可回查锚点": f"{video}#{timecode}#frame{frame_index}",
    })
    return row


def write_jsonl(path: str | Path, rows: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def read_jsonl(path: str | Path) -> list[dict]:
    source = Path(path)
    if not source.exists():
        return []
    rows = []
    for line in source.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def _iter_image_paths(root: Path, project_hint: str) -> list[Path]:
    if not root.exists():
        return []
    hint = _normalize(project_hint)
    paths = [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
        and "_minimax_frames" not in path.parts
    ]
    if hint:
        matched = [path for path in paths if hint in _normalize(str(path))]
        if matched:
            return sorted(matched)
    return sorted(paths)


def _asset_row(
    path: Path,
    root: Path,
    project_hint: str,
    preview_root: str | Path | None,
    script_type: str = "",
) -> dict:
    width, height = _dimensions(path)
    file_hash = _file_hash(path)
    from_excel = _is_excel_embed(path)
    from_pool = "发布会类" in path.parts and not from_excel
    project_name = _project_name(path, project_hint)
    source_file, source_sheet, source_row = _source_trace(path, from_excel)
    return {
        "image_id": _image_id(path),
        "原图路径": str(path),
        "预览路径": _preview_path(path, root, preview_root),
        "来源文件": source_file,
        "来源 sheet": source_sheet,
        "来源行": source_row,
        "项目名": project_name,
        "活动类型": script_type or ("发布会" if "发布会类" in path.parts else ""),
        "图片角色": "画面示意",
        "尺寸": f"{width}x{height}" if width and height else "",
        "文件大小(bytes)": path.stat().st_size if path.exists() else 0,
        "hash": file_hash,
        "是否低清": bool(width and height and (width < 1280 or height < 720)),
        "是否来自 Excel 嵌图": from_excel,
        "是否来自项目图片池": from_pool,
        "是否来自视频抽帧": _is_video_frame(path),
        "可回查锚点": _anchor(source_file, source_sheet, source_row) or str(path),
    }


def _image_id(path: Path, extra: str = "") -> str:
    raw = "|".join([_file_hash(path), path.name, extra])
    return "img_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _file_hash(path: Path) -> str:
    digest = hashlib.sha1()
    with Path(path).open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dimensions(path: Path) -> tuple[int, int]:
    try:
        with PILImage.open(path) as image:
            return image.size
    except Exception:
        return 0, 0


def _project_name(path: Path, project_hint: str) -> str:
    if project_hint:
        return project_hint
    parts = list(path.parts)
    if "发布会类" in parts:
        index = parts.index("发布会类")
        if index + 1 < len(parts):
            return parts[index + 1]
    return path.parent.name


def _source_trace(path: Path, from_excel: bool) -> tuple[str, str, str]:
    if not from_excel:
        return str(path), "项目图片池", ""
    stem = path.stem
    parts = stem.split("__")
    if len(parts) >= 3:
        return parts[0], parts[1], parts[2].lstrip("R")
    return str(path), "Excel嵌图", ""


def _preview_path(path: Path, root: Path, preview_root: str | Path | None) -> str:
    if not preview_root:
        return str(path)
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    return str(Path(preview_root) / relative)


def _is_excel_embed(path: Path) -> bool:
    text = _normalize(str(path))
    return "extracted" in text or "嵌图" in text or "_assets" in text


def _is_video_frame(path: Path) -> bool:
    text = _normalize(str(path))
    return "video_keyframe" in text or "视频抽帧" in text


def _anchor(source_file: str, source_sheet: str, source_row: str) -> str:
    if source_file and source_sheet and source_row:
        return f"{source_file}#{source_sheet}!R{source_row}"
    return ""


def _normalize(value: str) -> str:
    return "".join(str(value or "").lower().split())
