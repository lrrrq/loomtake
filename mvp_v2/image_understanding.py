from __future__ import annotations

from dataclasses import dataclass, field
from contextlib import contextmanager
from io import BytesIO
import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from PIL import Image as PILImage

from .brand_policy import brand_verdict_status, expected_brand_keys
from .image_assets import read_jsonl, write_jsonl
from .run_state import write_json_atomic
from .text_providers import TextProviderResponse, _diagnostic_text, _redact_diagnostics, _response_diagnostics, _transport_evidence


class VisionProviderError(RuntimeError):
    pass


ANALYSIS_FIELDS = [
    "image_id",
    "caption",
    "tags",
    "ocr_text",
    "brand_names",
    "people_count",
    "scene_type",
    "shot_type",
    "action_type",
    "quality_flags",
    "negative_flags",
    "confidence",
    "source",
    "review_status",
    "analysis_provider",
    "analysis_model",
    "annotator",
    "annotated_at",
    "source_file",
    "source_record_key",
    "provenance_status",
    "analysis_context",
    "prompt_version",
    "subjects",
    "main_subject",
    "foreground_subjects",
    "background_subjects",
    "camera_angle",
    "camera_movement",
    "uncertainties",
    "observation_basis",
    "analysis_schema_version",
    "confidence_basis",
    "quality",
    "quality_observation",
    "pixel_width",
    "pixel_height",
    "analysis_parse_status",
]

VISION_PROMPT_VERSION = "minimax-event-caption-v7-json-syntax"
PROJECT_IMAGE_VISION_PROMPT_VERSION = "minimax-project-image-v6-json-syntax"

PROJECT_IMAGE_QUALITY_FLAGS = frozenset({
    "slight_blur", "severe_blur", "low_resolution", "overexposed",
    "underexposed", "bad_crop", "watermark", "unreadable_subject",
})
PROJECT_IMAGE_NEGATIVE_FLAGS = frozenset({
    "external_brand", "forbidden_content", "sensitive_content",
})


@dataclass(frozen=True)
class ProjectImageVisionResult:
    """Strict business-only result used by the Formal project image authority."""

    status: str
    caption: str
    tags: tuple[str, ...]
    subjects: tuple[str, ...]
    scene: str
    purpose: str
    quality: str
    brand: str
    evidence: tuple[str, ...]
    quality_flags: tuple[str, ...] = ()
    negative_flags: tuple[str, ...] = ()
    visual_observations: dict = field(default_factory=dict)


def generate_minimax_project_image_analysis(
    image_path: str | Path,
    *,
    api_key: str | None = None,
    command: str = "mmx",
    timeout_seconds: int = 90,
    diagnostics_dir: str | Path | None = None,
) -> ProjectImageVisionResult:
    """Read one server-controlled image with MiniMax and reject loose output."""
    path = Path(image_path).expanduser().resolve()
    if not path.is_file():
        raise VisionProviderError("项目图片不存在")
    key = api_key or os.environ.get("MINIMAX_API_KEY", "")
    prompt = _project_image_prompt()
    record = {"image_sha256": sha256(path.read_bytes()).hexdigest(), "raw_response": "", "stages": [],
        "invocation": {"provider": "minimax-cli", "model_selection": "provider_managed",
            "timeout_seconds": timeout_seconds, "prompt": prompt, "prompt_version": PROJECT_IMAGE_VISION_PROMPT_VERSION}}
    stage = "provider"
    try:
        if not minimax_credentials_available(key, command):
            raise VisionProviderError("MiniMax 视觉能力未登录或未配置")
        with tempfile.TemporaryDirectory(prefix="mplus-project-vision-") as temporary:
            provider_path = _provider_compatible_image_path(path, Path(temporary))
            with _minimax_supported_image_path(provider_path) as vision_path:
                raw = _run_minimax_vision(command, vision_path, key, timeout_seconds, prompt=prompt)
        record.update(raw_response=str(raw), raw_response_sha256=sha256(str(raw).encode("utf-8")).hexdigest(),
            transport=_transport_evidence(raw))
        record["stages"].append({"stage": stage, "status": "succeeded"})
        stage = "parse"
        parsed, diagnostics = _parse_minimax_response(raw)
        record.update(parsed=parsed, parsing=diagnostics)
        if diagnostics["status"] != "parsed":
            code = "VISION_RESPONSE_TRUNCATED" if diagnostics["status"] == "truncated" else "VISION_RESPONSE_PARSE_FAILED"
            raise VisionProviderError(f"{code}: MiniMax 图片理解返回被截断或不是完整合法 JSON，保留原响应供核对")
        record["stages"].append({"stage": stage, "status": "succeeded"})
        stage = "validation"
        record["missing_fields"] = sorted(_PROJECT_IMAGE_REQUIRED_FIELDS - set(parsed))
        record["extra_fields"] = sorted(set(parsed) - (_PROJECT_IMAGE_REQUIRED_FIELDS | {"visual_observations"}))
        result = _project_image_result(parsed)
        if "ocr_text" in parsed:
            record["normalizations"] = [{"source": "ocr_text", "target": "visual_observations.ocr_text"}]
        record["stages"].append({"stage": stage, "status": "succeeded"})
    except Exception as exc:
        record["stages"].append({"stage": stage, "status": "failed"})
        message = _redact_diagnostics(str(exc), secrets=(key,))
        code = message.split(":", 1)[0] if message.startswith("VISION_") else (
            "VISION_PROVIDER_FAILED" if stage == "provider" else "VISION_RESPONSE_VALIDATION_FAILED")
        record.update(status="failed", failure_stage=stage, error={"code": code, "type": type(exc).__name__, "message": message})
        if hasattr(exc, "diagnostics"):
            record["transport"] = _transport_evidence(exc)
            record["raw_response"] = (exc.diagnostics.get("response") or {}).get("raw_response", record["raw_response"])
        safe = _redact_diagnostics(record, secrets=(key,))
        safe["response_redacted"] = safe["raw_response"] != record["raw_response"]
        safe["stored_response_sha256"] = sha256(safe["raw_response"].encode("utf-8")).hexdigest()
        _write_vision_diagnostics(diagnostics_dir, safe)
        if isinstance(exc, VisionProviderError):
            if message != str(exc):
                error = VisionProviderError(message)
                error.diagnostics = safe
                raise error from exc
            exc.diagnostics = safe
            raise
        error = VisionProviderError(f"{code}: {message}")
        error.diagnostics = safe
        raise error from exc
    record["status"] = "succeeded"
    _write_vision_diagnostics(diagnostics_dir, _redact_diagnostics(record, secrets=(key,)))
    return result


_PROJECT_IMAGE_REQUIRED_FIELDS = frozenset({
    "status", "caption", "tags", "subjects", "scene", "purpose",
    "quality", "brand", "evidence", "quality_flags", "negative_flags",
})


def _write_vision_diagnostics(directory: str | Path | None, record: dict) -> None:
    if directory is None:
        return
    root = Path(directory)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / f"{record['image_sha256']}-{uuid.uuid4().hex}.json"
    write_json_atomic(path, {"schema_version": "mplus-model-diagnostics/v1",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), **record})
    path.chmod(0o600)


def _project_image_result(parsed: dict) -> ProjectImageVisionResult:
    # OCR is an existing observation field; retain a complete provider response
    # when this one known field appears at the top level instead of its container.
    if "ocr_text" in parsed:
        ocr = parsed["ocr_text"]
        observations = parsed.get("visual_observations", {})
        if not isinstance(ocr, str) or not isinstance(observations, dict):
            raise VisionProviderError("VISION_RESPONSE_FIELDS_INVALID: MiniMax 图片理解 ocr_text 无效")
        nested_ocr = observations.get("ocr_text", "")
        if nested_ocr not in ("", ocr):
            raise VisionProviderError("VISION_RESPONSE_FIELDS_INVALID: MiniMax 图片理解 OCR 字段冲突")
        parsed = {key: value for key, value in parsed.items() if key != "ocr_text"}
        parsed["visual_observations"] = {**observations, "ocr_text": ocr}
    required = {
        "status", "caption", "tags", "subjects", "scene", "purpose",
        "quality", "brand", "evidence", "quality_flags", "negative_flags",
    }
    if missing := required - set(parsed):
        raise VisionProviderError(f"VISION_RESPONSE_FIELDS_MISSING: MiniMax 图片理解返回字段不完整: {sorted(missing)}")
    if extra := set(parsed) - (required | {"visual_observations"}):
        raise VisionProviderError(f"VISION_RESPONSE_FIELDS_INVALID: MiniMax 图片理解返回字段不完整或含未声明字段: {sorted(extra)}")
    observations = parsed.get("visual_observations", {})
    if not isinstance(observations, dict):
        raise VisionProviderError("MiniMax 图片理解 visual_observations 无效")
    allowed_observations = {
        "main_subject", "foreground_subjects", "background_subjects", "camera_angle",
        "scene_type", "shot_type", "action_type", "uncertainties", "ocr_text",
    }
    if set(observations) - allowed_observations:
        raise VisionProviderError("MiniMax 图片理解 visual_observations 字段无效")
    observations = normalize_visual_analysis(observations)
    status = parsed.get("status")
    if status not in {"pass", "low_confidence", "blocked"}:
        raise VisionProviderError("MiniMax 图片理解 status 无效")
    strings = {}
    for field in ("caption", "scene", "purpose", "quality", "brand"):
        value = parsed.get(field)
        if not isinstance(value, str) or not value.strip():
            raise VisionProviderError(f"MiniMax 图片理解 {field} 无效")
        strings[field] = value.strip()
    description = "；".join(strings[field] for field in ("caption", "scene", "purpose"))
    document_mentions = re.finditer(r"文字资料页|需求(?:说明|资料|文档)页|文档页面|文本页面|文字页面|文件截图|文字截图|演示文稿页面", description)
    if any(not re.search(r"(?:不(?:是|含|属于)|非|没有|未见)[^，；。]{0,3}$", description[:match.start()]) for match in document_mentions):
        if status == "pass":
            status = "low_confidence"
        strings["purpose"] = "文字资料参考；请人工核对用途，不能直接作为场景配图"
    return ProjectImageVisionResult(
        status=status,
        caption=strings["caption"],
        tags=_strict_project_image_list(parsed.get("tags"), "tags"),
        subjects=_strict_project_image_list(parsed.get("subjects"), "subjects"),
        scene=strings["scene"],
        purpose=strings["purpose"],
        quality=strings["quality"],
        brand=strings["brand"],
        evidence=_strict_project_image_list(parsed.get("evidence"), "evidence"),
        quality_flags=_strict_project_image_flags(
            parsed.get("quality_flags"), "quality_flags", PROJECT_IMAGE_QUALITY_FLAGS,
        ),
        negative_flags=_strict_project_image_flags(
            parsed.get("negative_flags"), "negative_flags", PROJECT_IMAGE_NEGATIVE_FLAGS,
        ),
        visual_observations=observations,
    )


def project_image_pixel_evidence(source: str | Path | bytes) -> dict:
    opened = BytesIO(source) if isinstance(source, bytes) else Path(source).expanduser().resolve()
    with PILImage.open(opened) as image:
        width, height = image.size
        grayscale = image.convert("L").resize((9, 8))
        getter = getattr(grayscale, "get_flattened_data", grayscale.getdata)
        pixels = list(getter())
    value = 0
    for y in range(8):
        offset = y * 9
        for x in range(8):
            value = (value << 1) | int(pixels[offset + x] > pixels[offset + x + 1])
    return {
        "pixel_width": int(width),
        "pixel_height": int(height),
        "perceptual_hash": f"{value:016x}",
    }


def import_manual_visual_analysis(
    manifest_rows: list[dict],
    manual_seed: dict | list[dict],
    source_file: str | Path | None = None,
    annotator: str | None = None,
) -> list[dict]:
    meta = _seed_meta(manual_seed)
    seed_by_key = _seed_by_key(manual_seed)
    rows = []
    for asset in manifest_rows:
        seed, source_record_key = _find_seed(asset, seed_by_key)
        if not seed:
            continue
        tags = _tags(seed)
        confidence = _reported_confidence(seed.get("confidence"))
        row_annotator = str(annotator or seed.get("annotator") or meta.get("annotator") or "unknown")
        row_source_file = str(source_file or seed.get("source_file") or meta.get("source_file") or "")
        rows.append(normalize_visual_analysis({
            "image_id": asset.get("image_id", ""),
            "caption": str(seed.get("caption") or seed.get("summary") or seed.get("视觉内容摘要") or ""),
            "tags": tags,
            "ocr_text": str(seed.get("ocr_text") or seed.get("ocr") or ""),
            "brand_names": _list_value(seed.get("brand_names") or _brands_from_tags(tags)),
            "people_count": seed.get("people_count", ""),
            "scene_type": str(seed.get("scene_type") or ""),
            "shot_type": str(seed.get("shot_type") or ""),
            "action_type": seed.get("action_type") or "",
            "quality_flags": seed.get("quality_flags", _quality_flags(tags)),
            "negative_flags": seed.get("negative_flags", _negative_flags(tags)),
            "confidence": confidence,
            "confidence_basis": "manual_self_reported" if confidence is not None else "unknown",
            "source": "manual_seed",
            "review_status": str(seed.get("review_status") or meta.get("review_status") or "unverified"),
            "analysis_provider": "manual",
            "analysis_model": "",
            "annotator": row_annotator,
            "annotated_at": str(seed.get("annotated_at") or meta.get("annotated_at") or ""),
            "source_file": row_source_file,
            "source_record_key": source_record_key,
            "provenance_status": "ok" if row_annotator != "unknown" and row_source_file else "missing_provenance",
            **{field: seed[field] for field in (
                "subjects", "main_subject", "foreground_subjects", "background_subjects",
                "camera_angle", "camera_movement", "uncertainties", "quality",
                "observation_basis", "analysis_schema_version", "confidence_basis",
                "analysis_context", "prompt_version",
            ) if field in seed},
        }))
    return rows


def generate_api_visual_analysis(
    manifest_rows: list[dict],
    api_key: str | None = None,
    model: str = "gpt-4.1-mini",
) -> list[dict]:
    if not api_key:
        raise VisionProviderError("缺少视觉 API key，不能生成 image_analysis.jsonl")
    raise VisionProviderError(f"当前 MVP 只接入 manual_seed 导入；API 视觉理解待接入，model={model}")


def generate_minimax_visual_analysis(
    manifest_rows: list[dict],
    api_key: str | None = None,
    command: str = "mmx",
    model: str = "",
    limit: int | None = None,
    timeout_seconds: int = 90,
) -> list[dict]:
    key = api_key or os.environ.get("MINIMAX_API_KEY", "")
    _reject_secret_in_argv(shlex.split(command), key)
    if not key and not _command_available(command):
        raise VisionProviderError("缺少 MINIMAX_API_KEY，且未检测到可用 mmx CLI")
    if not _command_available(command):
        raise VisionProviderError(_redact_provider_secret(f"未检测到 MiniMax CLI: {command}", key))

    rows = []
    for asset in manifest_rows[:limit]:
        path = Path(str(asset.get("原图路径") or ""))
        if not path.exists():
            continue
        rows.append(generate_minimax_visual_analysis_row(asset, key, command, model, timeout_seconds))
    return rows


def generate_minimax_visual_analysis_row(
    asset: dict,
    api_key: str,
    command: str = "mmx",
    model: str = "",
    timeout_seconds: int = 90,
) -> dict:
    path = Path(str(asset.get("原图路径") or ""))
    with PILImage.open(path) as image:
        pixel_width, pixel_height = image.size
    parsed = {}
    caption = ""
    tags = []
    brand_names = []
    with _minimax_supported_image_path(path) as vision_path:
        for attempt in range(2):
            script_type = _script_type_for_asset(asset)
            prompt = (
                _minimax_prompt(script_type, pixel_size=(pixel_width, pixel_height))
                if attempt == 0
                else _minimax_brand_retry_prompt(script_type, pixel_size=(pixel_width, pixel_height))
            )
            parsed = _parse_minimax_output(_run_minimax_vision(
                command,
                vision_path,
                api_key,
                timeout_seconds,
                prompt=prompt,
            ))
            caption = parsed.get("caption", "")
            tags = _list_value(parsed.get("tags") or _tags_from_caption(caption))
            brand_names = _list_value(parsed.get("brand_names") or _brands_from_tags(tags))
            if str(caption).strip() and tags and brand_verdict_status(brand_names) == "explicit":
                break
    negative_flags = normalize_visual_flags(parsed.get("negative_flags", _negative_flags(tags)))
    review_status = "unreviewed"
    if brand_verdict_status(brand_names) != "explicit":
        review_status = "blocked"
        if "品牌不确定禁用" not in negative_flags:
            negative_flags.append("品牌不确定禁用")
    confidence = _reported_confidence(parsed.get("confidence"))
    uncertainties = _observation_uncertainties(parsed, include_missing=True)
    if parsed.get("_parse_warning"):
        uncertainties.append("识图结果格式异常，恢复的内容待核对")
    if parsed.get("camera_movement"):
        uncertainties.append("单帧无法判断运镜")
    quality_flags = normalize_visual_flags(parsed.get("quality_flags", _quality_flags(tags)))
    return normalize_visual_analysis({
        "image_id": asset.get("image_id", ""),
        "caption": caption,
        "tags": tags,
        "ocr_text": parsed.get("ocr_text") or "",
        "brand_names": brand_names,
        "people_count": parsed.get("people_count", ""),
        "scene_type": str(parsed.get("scene_type") or ""),
        "shot_type": str(parsed.get("shot_type") or ""),
        "action_type": parsed.get("action_type") or "",
        "quality_flags": quality_flags,
        "negative_flags": negative_flags,
        "confidence": confidence,
        "confidence_basis": "model_self_reported" if confidence is not None else "unknown",
        "subjects": _list_value(parsed.get("subjects")),
        "main_subject": str(parsed.get("main_subject") or ""),
        "foreground_subjects": _list_value(parsed.get("foreground_subjects")),
        "background_subjects": _list_value(parsed.get("background_subjects")),
        "camera_angle": str(parsed.get("camera_angle") or ""),
        "camera_movement": "",
        "quality": _pixel_quality_summary(pixel_width, pixel_height, quality_flags),
        "quality_observation": str(parsed.get("quality") or ""),
        "pixel_width": pixel_width,
        "pixel_height": pixel_height,
        "analysis_parse_status": "recovered" if parsed.get("_parse_warning") else "parsed",
        "uncertainties": list(dict.fromkeys(uncertainties)),
        "observation_basis": "single_frame",
        "analysis_schema_version": "visual-observation/v1",
        "source": "minimax",
        "review_status": review_status,
        "analysis_provider": "minimax",
        "analysis_model": "mmx-vision-describe/provider-managed",
        "requested_model": model,
        "model_selection": "provider_managed",
        "annotator": "minimax",
        "annotated_at": "",
        "source_file": "",
        "source_record_key": str(asset.get("image_id") or ""),
        "provenance_status": "api_generated",
        "analysis_context": script_type or "通用活动",
        "prompt_version": VISION_PROMPT_VERSION,
    })


def audit_image_analysis_provenance(analysis_rows: list[dict]) -> list[dict]:
    checks = []
    for row in analysis_rows:
        if row.get("source") == "manual_seed" and (
            not row.get("source_file") or str(row.get("annotator") or "") in {"", "unknown"}
        ):
            checks.append({
                "检查项": "image_analysis_missing_provenance",
                "级别": "warning",
                "状态": "warning",
                "关联镜号": "",
                "详情": f"图片 {row.get('image_id', '')} 的人工视觉标注缺少标注人或来源文件",
                "修复建议": "补充 annotator、source_file、annotated_at；或改用 MiniMax/API 生成后人工复核",
                "audit_domain": "media_readiness",
            })
        if row.get("source") == "manual_seed" and row.get("review_status") != "reviewed":
            checks.append({
                "检查项": "image_analysis_unverified_manual_seed",
                "级别": "warning",
                "状态": "warning",
                "关联镜号": "",
                "详情": f"图片 {row.get('image_id', '')} 的视觉标注来自未复核预标注，不应视为真值",
                "修复建议": "用 MiniMax/API 重新读图，或由人工复核后把 review_status 改为 reviewed",
                "audit_domain": "media_readiness",
            })
        if not row.get("caption") or not row.get("tags"):
            checks.append({
                "检查项": "image_analysis_missing_caption_tags",
                "级别": "warning",
                "状态": "warning",
                "关联镜号": "",
                "详情": f"图片 {row.get('image_id', '')} 缺少 caption 或 tags",
                "修复建议": "重新生成视觉理解或补充人工标注",
                "audit_domain": "media_readiness",
            })
        if (
            brand_verdict_status(row.get("brand_names")) != "explicit"
            and not analysis_asset_unusable(row)
            and str(row.get("review_status") or "") != "blocked"
        ):
            checks.append({
                "检查项": "image_analysis_missing_brand_verdict",
                "级别": "warning",
                "状态": "warning",
                "关联镜号": "",
                "详情": f"图片 {row.get('image_id', '')} 品牌识别结论不确定，已禁止参与自动配图",
                "修复建议": "重新读图；无品牌时显式返回 brand_names=[无]，或人工复核品牌结论",
                "audit_domain": "media_readiness",
            })
    return checks


def analysis_asset_unusable(row: dict) -> bool:
    text = " ".join(
        " ".join(normalize_visual_flags(row.get(key)))
        for key in ("quality_flags", "negative_flags")
    ).casefold()
    return any(flag in text for flag in (
        "严重模糊", "虚焦", "无法辨认", "对焦不准", "画质模糊", "画面模糊",
        "分辨率较低", "低分辨率", "细节不清晰", "噪点明显",
        "severely_blurred", "out_of_focus", "unreadable_subject", "low_resolution",
        "severe_blur", "heavy_noise", "高风险", "禁用", "forbidden_content",
    ))


_FLAG_ALIASES = {
    "is_blurry": "slight_blur", "blurry": "slight_blur", "blur": "slight_blur",
    "模糊": "slight_blur", "轻微模糊": "slight_blur", "轻微运动模糊": "slight_blur",
    "严重模糊": "severe_blur", "severely_blurred": "severe_blur",
    "画质模糊": "severe_blur", "画面模糊": "severe_blur",
    "虚焦": "out_of_focus", "对焦不准": "out_of_focus",
    "is_low_resolution": "low_resolution", "low_res": "low_resolution",
    "低分辨率": "low_resolution", "低清": "low_resolution", "分辨率较低": "low_resolution",
    "is_overexposed": "overexposed", "过曝": "overexposed", "过度曝光": "overexposed",
    "is_underexposed": "underexposed", "欠曝": "underexposed", "曝光不足": "underexposed",
    "is_watermarked": "watermark", "has_watermark": "watermark", "水印": "watermark",
    "裁切不当": "bad_crop", "无法辨认": "unreadable_subject",
    "主体无法辨认": "unreadable_subject", "严重噪点": "heavy_noise",
    "外部品牌": "external_brand", "敏感内容": "sensitive_content",
    "禁用内容": "forbidden_content",
}
_TRUE_FLAGS = {"true", "yes", "1", "是", "有", "存在"}
_FALSE_FLAGS = {"false", "no", "0", "否", "无", "没有", "none", "null", "", "未知", "unknown"}
_UNKNOWN_OBSERVATIONS = {"unknown", "未知", "不确定", "无法判断", "null", "none"}
_OBSERVATION_LABELS = {
    "subjects": "可见主体", "main_subject": "主主体", "foreground_subjects": "前景",
    "background_subjects": "背景", "scene_type": "场景", "shot_type": "景别",
    "action_type": "可见动作", "camera_angle": "机位",
}


def _observation_uncertainties(row: dict, *, include_missing: bool = False) -> list[str]:
    uncertainties = _list_value(row.get("uncertainties"))
    for field, label in _OBSERVATION_LABELS.items():
        if field not in row:
            if include_missing:
                uncertainties.append(f"{label}信息未记录，待核对")
            continue
        value = _structured_value(row[field])
        values = value if isinstance(value, (list, tuple)) else [value]
        if any(item is None or str(item).strip().casefold() in _UNKNOWN_OBSERVATIONS for item in values):
            uncertainties.append(f"{label}未知，待核对")
    return list(dict.fromkeys(uncertainties))


def _structured_value(value):
    """Decode old JSON/repr arrays, including the former split-dict writer output."""
    if isinstance(value, (list, tuple)) and value and str(value[0]).startswith("{"):
        joined = " ".join(str(item) for item in value)
        value = re.sub(r"(?<=[\w}\]])\s+(?=['\"][^'\"]+['\"]\s*:)", ", ", joined)
    if isinstance(value, str) and value.strip().startswith(("[", "{", "(")):
        text = value.strip()
        for parser in (json.loads, ast.literal_eval):
            try:
                parsed = parser(text)
            except (ValueError, SyntaxError, TypeError, RecursionError):
                continue
            if isinstance(parsed, (dict, list, tuple)):
                return parsed
    return value


def normalize_visual_flags(value) -> list[str]:
    """Normalize observations, not their eligibility or severity policy."""
    value = _structured_value(value)
    flags: list[str] = []
    if isinstance(value, dict):
        for name, finding in value.items():
            state = str(finding).strip().casefold()
            if finding is None or state in _FALSE_FLAGS:
                continue
            if state in _TRUE_FLAGS:
                flags.extend(normalize_visual_flags(str(name)))
            else:
                flags.extend(normalize_visual_flags(finding))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            flags.extend(normalize_visual_flags(item))
    elif isinstance(value, str):
        for item in re.split(r"[；;，,、\n]+", value):
            text = item.strip()
            key = re.sub(r"[\s-]+", "_", text.casefold())
            if key and key not in _FALSE_FLAGS | _TRUE_FLAGS:
                flags.append(_FLAG_ALIASES.get(key, key if key in PROJECT_IMAGE_QUALITY_FLAGS | PROJECT_IMAGE_NEGATIVE_FLAGS else text))
    return list(dict.fromkeys(flags))


def normalize_visual_analysis(row: dict) -> dict:
    """Read legacy analysis without changing its provenance or inventing missing evidence."""
    result = dict(row)
    uncertainties = _observation_uncertainties(row)
    if uncertainties:
        result["uncertainties"] = uncertainties
    for field in ("quality_flags", "negative_flags"):
        if field in result:
            result[field] = normalize_visual_flags(result[field])
    for field in ("tags", "brand_names", "subjects", "foreground_subjects", "background_subjects", "uncertainties"):
        if field in result:
            result[field] = _list_value(result[field])
    for field in ("main_subject", "scene_type", "shot_type", "action_type", "camera_angle", "ocr_text"):
        if field in result:
            value = _structured_value(result[field])
            result[field] = "、".join(_list_value(value)) if isinstance(value, (list, tuple)) else str(value or "")
            if field != "ocr_text" and result[field].strip().casefold() in _FALSE_FLAGS:
                result[field] = ""
    return result


def _reported_confidence(value):
    return value if type(value) in (int, float) and 0 <= value <= 1 else None


def _pixel_quality_summary(width: int, height: int, flags: list[str]) -> str:
    labels = {
        "slight_blur": "轻微模糊", "severe_blur": "严重模糊", "low_resolution": "分辨率较低",
        "overexposed": "过曝", "underexposed": "欠曝", "bad_crop": "裁切不完整",
        "watermark": "有水印", "unreadable_subject": "主体难以辨认",
    }
    findings = "、".join(labels.get(flag, flag) for flag in flags[:3]) or "画质待核对"
    return f"{width}×{height} 像素；{findings}"


def load_manual_seed(path: str | Path) -> dict | list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_image_analysis_jsonl(path: str | Path, rows: list[dict]) -> None:
    write_jsonl(path, rows)


def read_image_analysis_jsonl(path: str | Path) -> list[dict]:
    return read_jsonl(path)


def _seed_meta(seed: dict | list[dict]) -> dict:
    if isinstance(seed, dict) and isinstance(seed.get("_meta"), dict):
        return dict(seed["_meta"])
    return {}


def _seed_by_key(seed: dict | list[dict]) -> dict[str, dict]:
    if isinstance(seed, list):
        rows = seed
    else:
        rows = []
        for key, value in seed.items():
            if str(key).startswith("_"):
                continue
            item = dict(value)
            item.setdefault("_key", str(key))
            rows.append(item)
    result = {}
    for item in rows:
        keys = [
            item.get("_key"),
            item.get("image_id"),
            item.get("原图路径"),
            item.get("path"),
            item.get("filename"),
        ]
        for key in keys:
            if key:
                result[str(key)] = dict(item)
    return result


def _find_seed(asset: dict, seed_by_key: dict[str, dict]) -> tuple[dict | None, str]:
    path = Path(str(asset.get("原图路径") or ""))
    keys = [
        asset.get("image_id"),
        asset.get("原图路径"),
        path.name,
        path.stem,
        asset.get("可回查锚点"),
    ]
    for key in keys:
        if key and str(key) in seed_by_key:
            return dict(seed_by_key[str(key)]), str(key)
    return None, ""


def _tags(item: dict) -> list[str]:
    return _list_value(item.get("tags") or item.get("视觉标签") or [])


def _list_value(value) -> list[str]:
    value = _structured_value(value)
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value)
    separators = ["；", ";", "，", ",", "、", "\n", " "]
    parts = [text]
    for separator in separators:
        next_parts = []
        for part in parts:
            next_parts.extend(part.split(separator))
        parts = next_parts
    return [part.strip() for part in parts if part.strip()]


def _brands_from_tags(tags: list[str]) -> list[str]:
    return sorted(expected_brand_keys(" ".join(tags)))


def _quality_flags(tags: list[str]) -> list[str]:
    flags = []
    text = " ".join(tags)
    if any(term in text for term in ("低清", "虚焦", "模糊", "不清晰")):
        flags.append("低清/虚焦")
    return flags


def _negative_flags(tags: list[str]) -> list[str]:
    flags = []
    text = " ".join(tags)
    if any(term in text for term in ("禁用", "敏感", "错品牌")):
        flags.append("高风险")
    return flags


def _command_available(command: str) -> bool:
    executable = shlex.split(command)[0] if command else ""
    return bool(executable and shutil.which(executable))


def minimax_credentials_available(api_key: str, command: str = "mmx") -> bool:
    if api_key:
        return True
    args = shlex.split(command)
    if not args or not _command_available(command):
        return False
    try:
        completed = subprocess.run(
            args + ["auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _run_minimax_vision(
    command: str,
    image_path: Path,
    api_key: str,
    timeout_seconds: int = 90,
    prompt: str | None = None,
) -> str:
    args = shlex.split(command)
    _reject_secret_in_argv(args, api_key)
    uses_mmx_parameters = len(args) == 1 and Path(args[0]).name == "mmx"
    if uses_mmx_parameters:
        args = args + [
            "--non-interactive",
            "--output",
            "json",
            "--timeout",
            str(timeout_seconds),
            "vision",
            "describe",
            "--image",
            str(image_path),
            "--prompt",
            prompt or _minimax_prompt(),
        ]
    env = dict(os.environ)
    if api_key:
        env["MINIMAX_API_KEY"] = api_key
    request_evidence = {"operation": "custom_command", "parameters_available": False,
        "process_timeout_seconds": timeout_seconds + 10}
    if uses_mmx_parameters:
        request_evidence.update(provider="minimax-cli", operation="vision.describe", parameters_available=True,
            model_selection="provider_managed", timeout_seconds=timeout_seconds,
            prompt_sha256=sha256((prompt or _minimax_prompt()).encode("utf-8")).hexdigest())
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_seconds + 10,
        )
    except Exception as exc:
        error = VisionProviderError(_redact_provider_secret(_subprocess_failure_message(exc), api_key))
        response = _response_diagnostics(getattr(exc, "stdout", "") or "", secrets=(api_key,))
        error.diagnostics = {"provider": "minimax-cli" if uses_mmx_parameters else "custom-command",
            "request": request_evidence, "response": response,
            "attempts": [{"attempt": 1, "status": "failed", "error_type": type(exc).__name__,
                "error": str(error), "response": response,
                "stderr": _redact_diagnostics(_diagnostic_text(getattr(exc, "stderr", "") or ""), secrets=(api_key,))}]}
        raise error from exc
    diagnostics = {"provider": "minimax-cli" if uses_mmx_parameters else "custom-command", "request": request_evidence,
        "response": _response_diagnostics(result.stdout, secrets=(api_key,)),
        "attempts": [{"attempt": 1, "return_code": result.returncode, "status": "succeeded" if result.returncode == 0 else "failed",
            "stderr": _redact_diagnostics(result.stderr or "", secrets=(api_key,))}]}
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "MiniMax 视觉理解命令执行失败"
        error = VisionProviderError(_redact_provider_secret(message, api_key))
        error.diagnostics = diagnostics
        raise error
    return TextProviderResponse(result.stdout.strip(), diagnostics=diagnostics)


@contextmanager
def _minimax_supported_image_path(path: Path):
    if path.suffix.lower() != ".gif":
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="mplus-vision-frame-") as temporary:
        target = Path(temporary) / "first-frame.png"
        with PILImage.open(path) as image:
            image.seek(0)
            image.convert("RGB").save(target, format="PNG")
        yield target


def _minimax_prompt(script_type: str = "", *, pixel_size: tuple[int, int] | None = None) -> str:
    context = f"{script_type}脚本" if script_type else "活动脚本"
    return (
        f"请用中文识别这张{context}参考图，重点描述画面中实际可见的"
        "主体、场景、动作、人物关系、活动领域和可复用镜头功能；"
        "不得因活动类别上下文猜测画面中不存在的内容。返回JSON，字段包括"
        "caption,tags,brand_names,ocr_text,people_count,scene_type,shot_type,"
        "action_type,quality_flags,negative_flags,subjects,main_subject,foreground_subjects,"
        "background_subjects,camera_angle,quality,uncertainties,confidence。"
        "main_subject 是最主要的可见主体；前景、背景与主体分别记录，不能把背景物体当作主主体。"
        "先区分现场、广告牌、屏幕内容、画中画与白边。广告牌或屏幕里的人物、动作属于画中内容，"
        "不等于现场真人或现场动作；有拼接图时分区描述，不编造连续剧情。"
        "subjects、foreground_subjects、background_subjects、uncertainties、tags 使用字符串数组；"
        "scene_type、shot_type、action_type、camera_angle 使用字符串。"
        "这里只观察单帧，不得推断运镜、完整动作过程、人物身份或不可见内容；"
        "俯拍只说明观察角度，不能证明航拍、无人机或飞行机位；没有明确证据只写高位俯拍或平视，"
        "不能因看见大量人群或完整舞台就写航拍。"
        "action_type 写清哪个可见主体正在做什么，不能把背景人物的动作移给主主体；"
        "无法从单帧判断的动作留空，不能把观看、交谈或站立猜成演讲、领奖或产品发布。"
        "上述观察字段均须返回；无法判断的字段留空，并在 uncertainties 逐项说明原因。"
        "空数组不证明画面没有该主体，空字符串不证明不存在动作或机位。"
        "只转写清楚可辨的文字，不要猜写品牌、Logo或词语；模糊残字留空或注明部分可辨。"
        "brand_names 与可见Logo、ocr_text一致；已记录品牌Logo时不能又返回无品牌。"
        "图中看不清品牌时返回未知，不根据活动类别、场景或相似标志补出品牌。"
        "机位与姿态未知时留空，不用unknown、none代替空字符串。"
        "quality 只写实际可见画质，quality_flags/negative_flags 使用字符串数组，不输出布尔字典；"
        "禁止凭感觉声称1080p、4K或推测图片像素；图片分辨率由文件元数据提供。"
        "无异常返回空数组，quality_flags 优先使用 slight_blur,severe_blur,low_resolution,"
        "overexposed,underexposed,bad_crop,watermark,unreadable_subject。"
        "confidence 为 0–1 的自评确信程度，不代表人工审核或匹配分；无法评估返回 null。"
        "brand_names不得为空数组；"
        "画面中确实无品牌时返回[\"无\"]，无法判断时返回[\"未知\"]。"
        "只输出一个严格合法的JSON对象，不要Markdown代码块或说明文字。"
        "字符串内部的引语使用中文引号“”，或对ASCII双引号做JSON转义；"
        "caption、OCR与数组元素中均不得出现未转义的ASCII双引号。输出前检查JSON能完整解析。"
        + (f"文件实际像素：{pixel_size[0]}×{pixel_size[1]}，含全部白边和拼接区域。" if pixel_size else "")
    )


def _minimax_brand_retry_prompt(script_type: str = "", *, pixel_size: tuple[int, int] | None = None) -> str:
    return (
        _minimax_prompt(script_type, pixel_size=pixel_size)
        + "这是二次品牌判定：请重点重新检查logo、设备铭牌、屏幕文字和OCR；"
        "能识别时返回具体品牌，画面确实没有可识别品牌时返回[\"无\"]，"
        "证据不足时仍返回[\"未知\"]，不得猜测。"
    )


def _project_image_prompt() -> str:
    return (
        "请用中文理解这张项目资料图片，只返回严格 JSON 对象，不要 Markdown。"
        "字符串内部的引语使用中文引号“”，或对ASCII双引号做JSON转义；"
        "caption、OCR与数组元素中均不得出现未转义的ASCII双引号。"
        "只输出一个严格合法的JSON对象，不要Markdown代码块或说明文字，输出前检查JSON能完整解析。"
        "必须包含 status,caption,tags,subjects,scene,purpose,quality,brand,evidence,quality_flags,negative_flags。"
        "本次观察必须包含 visual_observations 对象，逐项返回 main_subject,foreground_subjects,background_subjects,"
        "camera_angle,scene_type,shot_type,action_type,uncertainties,ocr_text。"
        "其中foreground_subjects、background_subjects、uncertainties为字符串数组，其余为字符串；"
        "未知留空，并在 uncertainties 逐项说明原因；空值不证明画面不存在对应内容。"
        "主主体与前景、背景分开，不把背景物体当主主体。"
        "action_type 写清哪个可见主体正在做什么，不把背景人物的动作移给主主体；"
        "单帧无法判断动作时留空，不把观看、交谈或站立猜成演讲、领奖或产品发布。"
        "广告牌、屏幕、画中画中的人物和动作不等于现场真人与现场动作，必须注明画中内容；"
        "拼接图分区描述，不编造连续剧情。俯拍不能证明航拍或无人机，单帧不得推断运镜。"
        "只转写清楚的文字，不要猜写品牌或词语；认不清品牌应说明未知，不能同时说看见品牌和没有品牌。"
        "status 只能是 pass、low_confidence、blocked：画面信息清晰且所有结论都有可见依据时为 pass；"
        "无法可靠判断主体、场景、用途、品牌或文字时为 low_confidence；"
        "图片损坏、无法理解或明显不应使用时为 blocked。"
        "文字资料页、需求说明页、文档页面或文件截图应注明资料用途，并设为low_confidence等待人工核对；"
        "文字清楚不代表可用作场景配图。产品照片或现场照片上带品牌文字不属于文字资料页。"
        "tags、subjects、evidence 必须是非空字符串数组；其他字段必须是非空字符串。"
        "quality_flags 必须是数组，只能使用 slight_blur,severe_blur,low_resolution,overexposed,underexposed,bad_crop,watermark,unreadable_subject；没有则返回空数组。"
        "negative_flags 必须是数组，只能使用 external_brand,forbidden_content,sensitive_content；没有则返回空数组。"
        "brand 可以明确写‘画面中未见品牌标识’，但不得猜测不可见内容。"
    )


def _strict_project_image_flags(value, field: str, allowed: frozenset[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or item not in allowed for item in value
    ):
        raise VisionProviderError(f"MiniMax 图片理解 {field} 无效")
    return tuple(dict.fromkeys(value))


def _provider_compatible_image_path(source: Path, temporary: Path) -> Path:
    content = source.read_bytes()
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix = ".png"
    elif content.startswith(b"\xff\xd8\xff"):
        suffix = ".jpg"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        suffix = ".gif"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        suffix = ".webp"
    else:
        raise VisionProviderError("项目图片格式不受 MiniMax 视觉路由支持")
    target = temporary / f"image{suffix}"
    target.write_bytes(content)
    return target


def _strict_project_image_list(value: object, field: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
    ):
        raise VisionProviderError(f"MiniMax 图片理解 {field} 无效")
    return tuple(item.strip() for item in value)


def _parse_minimax_output(raw: str) -> dict:
    # Preserve the legacy parser adapter for cached analysis repair. Strict
    # project analysis consumes the separate diagnostics tuple below.
    parsed, diagnostics = _parse_minimax_response(raw)
    if diagnostics["status"] != "parsed":
        text = str(raw or "").strip()
        outer = _loads_json(text)
        content = outer.get("content") if outer else None
        body = _strip_json_fence(content if isinstance(content, str) else text)
        parsed = {"caption": body, "tags": _tags_from_caption(body), **parsed,
            "_parse_warning": "partial_json_recovery"}
    return parsed


def _parse_minimax_response(raw: str) -> tuple[dict, dict]:
    text = str(raw or "").strip()
    metadata = _response_diagnostics(text)
    diagnostics = {"status": "parsed", "finish_reason": metadata["finish_reason"], "usage": metadata["usage"]}
    outer = _loads_json(text)
    content = outer.get("content") if outer is not None else None
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content
            if isinstance(item, dict) and item.get("type") == "text")
    body = _strip_json_fence(content if isinstance(content, str) else text)
    parsed = _loads_json(body)
    if parsed is None:
        diagnostics["status"] = "invalid"
        try:
            json.loads(body)
        except json.JSONDecodeError as exc:
            diagnostics["json_error"] = {"message": exc.msg, "line": exc.lineno, "column": exc.colno, "position": exc.pos}
        if _incomplete_json(body):
            diagnostics.update(status="truncated", truncation_evidence="unclosed_json_at_end")
        parsed = _salvage_minimax_json(body)
        diagnostics["recovered_fields"] = sorted(parsed)
    if diagnostics["finish_reason"] in {"length", "max_tokens"}:
        diagnostics.update(status="truncated", truncation_evidence="provider_finish_reason")
    return parsed, diagnostics


def _incomplete_json(text: str) -> bool:
    if not text.lstrip().startswith(("{", "[")):
        return False
    containers, in_string, escaped = [], False, False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
        elif character == '"':
            in_string = True
        elif character in "{[":
            containers.append(character)
        elif character in "}]":
            if not containers or containers.pop() != ("{" if character == "}" else "["):
                return False
    return in_string or bool(containers)


def _salvage_minimax_json(text: str) -> dict:
    cleaned = _strip_json_fence(text)
    result = {}
    caption_match = re.search(
        r'"caption"\s*:\s*"([\s\S]*)"\s*,\s*"tags"\s*:',
        cleaned,
    )
    if caption_match:
        result["caption"] = caption_match.group(1)
    decoder = json.JSONDecoder()
    for key in ANALYSIS_FIELDS:
        match = re.search(rf'(?<!\\)"{key}"\s*:\s*', cleaned)
        if not match:
            continue
        try:
            value, end = decoder.raw_decode(cleaned[match.end():])
        except (TypeError, ValueError):
            continue
        tail = cleaned[match.end() + end:].lstrip()
        if not tail or tail[0] in ',}"':
            result[key] = value
    return result


def _loads_json(text: str) -> dict | None:
    text = _strip_json_comments(text)
    try:
        value = json.loads(text)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _strip_json_comments(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        quote_open = False
        escaped = False
        cut_at = None
        for index in range(len(line) - 1):
            char = line[index]
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                quote_open = not quote_open
                continue
            if not quote_open and line[index:index + 2] == "//":
                cut_at = index
                break
        lines.append(line[:cut_at] if cut_at is not None else line)
    return "\n".join(lines)


def _strip_json_fence(text: str) -> str:
    stripped = str(text or "").strip()
    if stripped.startswith("```"):
        if "\n" not in stripped:
            stripped = stripped.replace("\\n", "\n")
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    # Escaped quotes inside a valid JSON string are data, not another transport layer.
    # Only old provider output with escaped object keys needs that extra decoding.
    for _ in range(2):
        if not re.match(r'^\{\s*\\+"', stripped):
            break
        stripped = stripped.replace("\\n", "\n").replace('\\"', '"').strip()
    return stripped


def _tags_from_caption(caption: str) -> list[str]:
    tags = sorted(expected_brand_keys(caption))
    rules = {
        "logo": ("logo", "LOGO", "标识"),
        "品牌标识": ("品牌", "标识"),
        "主会场": ("主会场", "发布会现场", "舞台"),
        "大屏": ("大屏",),
        "人群": ("人群", "观众", "多人"),
        "合影": ("合影",),
        "打卡": ("打卡",),
        "启动": ("启动",),
        "展会": ("展会", "展馆", "展台", "展项"),
        "年会": ("年会", "年度盛典", "员工活动", "团队颁奖"),
        "赛事": ("赛事", "比赛", "赛道", "运动员", "选手"),
        "音乐节": ("音乐节", "歌手演出", "乐队演出", "现场演唱"),
        "明星活动": ("明星活动", "艺人亮相", "明星出场", "粉丝应援"),
        "产品互动": ("产品互动", "试用产品", "上手体验"),
        "观众应援": ("观众应援", "应援棒", "灯牌", "齐声呐喊"),
        "虚焦": ("虚焦", "模糊", "不清晰"),
    }
    for tag, terms in rules.items():
        if any(term in caption for term in terms):
            tags.append(tag)
    return tags


def _script_type_for_asset(asset: dict) -> str:
    explicit = str(asset.get("活动类型") or asset.get("script_type") or "").strip()
    if explicit:
        return explicit
    text = " ".join(str(asset.get(key) or "") for key in (
        "来源文件", "原图路径", "项目名",
    ))
    for script_type in ("明星活动", "音乐节", "发布会", "展会", "年会", "赛事"):
        if script_type in text:
            return script_type
    return ""


def _subprocess_failure_message(exc: Exception) -> str:
    parts = []
    for value in (getattr(exc, "stderr", None), getattr(exc, "stdout", None), str(exc)):
        text = str(value or "").strip()
        if text and text not in parts:
            parts.append(text)
    return " | ".join(parts) or "MiniMax 视觉理解命令执行失败"


def _reject_secret_in_argv(args: list[str], api_key: str) -> None:
    if api_key and any(api_key in arg for arg in args):
        raise VisionProviderError("自定义 MiniMax command 的 argv 禁止包含凭据参数: [REDACTED]")
    if any(_is_credential_argument(arg) for arg in args):
        raise VisionProviderError("自定义 MiniMax command 的 argv 禁止包含凭据参数: [REDACTED]")


def _is_credential_argument(argument: str) -> bool:
    name, separator, payload = str(argument or "").partition("=")
    return _contains_credential_marker(name.lstrip("-")) or bool(
        separator and _contains_credential_marker(payload)
    )


def _contains_credential_marker(value: str) -> bool:
    normalized = "".join(char.lower() for char in value if char.isalnum())
    words = []
    current = ""
    for char in value:
        if not char.isalnum():
            if current:
                words.append(current.lower())
                current = ""
            continue
        if char.isupper() and current and current[-1].islower():
            words.append(current.lower())
            current = char
        else:
            current += char
    if current:
        words.append(current.lower())
    return (
        "apikey" in normalized
        or any(marker in normalized for marker in ("secret", "password", "authorization", "credential"))
        or normalized.endswith("token")
        or "tokenfile" in normalized
        or "authfile" in normalized
        or any(marker in words for marker in ("token", "auth"))
    )


def _redact_provider_secret(text: str, api_key: str) -> str:
    redacted = str(text or "")
    if api_key:
        redacted = redacted.replace(api_key, "[REDACTED]")
    return redacted
