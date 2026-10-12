from __future__ import annotations

import hashlib
import math
import re
import subprocess
from pathlib import Path

from .image_assets import build_video_frame_asset


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}


class VideoFrameError(RuntimeError):
    pass


def evenly_spaced_timecodes(duration_seconds: float, sample_count: int) -> list[float]:
    duration = float(duration_seconds or 0)
    count = int(sample_count or 0)
    if duration <= 0 or count <= 0:
        return []
    margin = min(0.2, duration * 0.05)
    start = margin
    end = max(start, duration - margin)
    if count == 1 or math.isclose(start, end):
        return [round(duration / 2, 3)]
    step = (end - start) / (count - 1)
    return [round(start + step * index, 3) for index in range(count)]


def discover_project_videos(
    media_root: str | Path,
    launch_root: str | Path,
    aliases: tuple[str, ...] | list[str] = (),
) -> list[Path]:
    media = Path(media_root)
    launch = Path(launch_root)
    primary = sorted(
        path.absolute()
        for path in media.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )
    videos = list(primary)
    seen = set(primary)
    normalized_aliases = {
        _normalize(alias)
        for alias in aliases
        if len(_normalize(alias)) >= 2 and _normalize(alias) not in {"发布会", "开场短片"}
    }
    finished = launch / "成片"
    if finished.exists() and normalized_aliases:
        for path in finished.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            name = _normalize(path.stem)
            if any(alias in name or name in alias for alias in normalized_aliases):
                candidate = path.absolute()
                if candidate not in seen:
                    videos.append(candidate)
                    seen.add(candidate)
    return videos


def frame_records_to_manifest(
    video_path: str | Path,
    frame_records: list[dict],
    project_name: str = "",
) -> list[dict]:
    return [
        build_video_frame_asset(
            video_path,
            record["frame_path"],
            str(record["timecode"]),
            int(record["frame_index"]),
            project_name=project_name,
        )
        for record in frame_records
    ]


def extract_video_keyframes(
    video_path: str | Path,
    output_root: str | Path,
    sample_count: int = 12,
    swiftc_command: str = "/usr/bin/swiftc",
) -> list[dict]:
    video = Path(video_path).resolve()
    duration = _video_duration_seconds(video)
    times = evenly_spaced_timecodes(duration, sample_count)
    if not times:
        raise VideoFrameError(f"视频时长无效，不能抽帧: {video}")
    frame_dir = Path(output_root) / _video_cache_key(video)
    frame_dir.mkdir(parents=True, exist_ok=True)
    records = []
    missing_args = []
    for index, seconds in enumerate(times, start=1):
        frame_path = frame_dir / f"{_safe_stem(video.stem)}__t{round(seconds * 1000):09d}__f{index:03d}.png"
        records.append({
            "frame_path": str(frame_path),
            "timecode": _format_timecode(seconds),
            "frame_index": index,
        })
        if not frame_path.exists():
            missing_args.extend([f"{seconds:.3f}", str(frame_path)])
    if missing_args:
        helper = Path(__file__).with_name("tools") / "extract_video_frames.swift"
        binary = _compiled_helper(helper, output_root, swiftc_command)
        result = subprocess.run(
            [str(binary), str(video), *missing_args],
            capture_output=True,
            text=True,
            check=False,
            timeout=max(120, 20 * len(times)),
        )
        if result.returncode != 0:
            message = result.stderr.strip() or result.stdout.strip() or "未知错误"
            raise VideoFrameError(f"视频抽帧失败: {video}: {message}")
    missing = [record["frame_path"] for record in records if not Path(record["frame_path"]).is_file()]
    if missing:
        raise VideoFrameError(f"视频抽帧产物缺失: {missing[:3]}")
    return records


def _compiled_helper(helper: Path, output_root: str | Path, swiftc_command: str) -> Path:
    digest = hashlib.sha1(helper.read_bytes()).hexdigest()[:12]
    tools_dir = Path(output_root) / ".tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    binary = tools_dir / f"extract_video_frames-{digest}"
    if binary.exists():
        return binary
    result = subprocess.run(
        [
            swiftc_command,
            "-framework", "AVFoundation",
            "-framework", "AppKit",
            str(helper),
            "-o", str(binary),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "未知错误"
        raise VideoFrameError(f"视频抽帧工具编译失败: {message}")
    return binary


def build_project_video_frame_manifest(
    media_root: str | Path,
    launch_root: str | Path,
    cache_root: str | Path,
    aliases: tuple[str, ...] | list[str],
    project_name: str,
    sample_count: int = 12,
) -> list[dict]:
    rows = []
    for video in discover_project_videos(media_root, launch_root, aliases):
        records = extract_video_keyframes(video, Path(cache_root) / "video_keyframes", sample_count)
        rows.extend(frame_records_to_manifest(video, records, project_name=project_name))
    return rows


def _video_duration_seconds(video_path: Path) -> float:
    result = subprocess.run(
        ["/usr/bin/mdls", "-name", "kMDItemDurationSeconds", "-raw", str(video_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    try:
        duration = float(result.stdout.strip())
    except (TypeError, ValueError) as exc:
        raise VideoFrameError(f"无法读取视频时长: {video_path}") from exc
    return duration


def _format_timecode(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def _video_cache_key(path: Path) -> str:
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:12]
    return f"{_safe_stem(path.stem)}-{digest}"


def _safe_stem(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", value).strip("-.") or "video"


def _normalize(value: str) -> str:
    return "".join(str(value or "").lower().split())
