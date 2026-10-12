from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
import json
import re
from pathlib import Path
from typing import Callable

from openpyxl import load_workbook

from .brand_policy import (
    brand_evidence_unverified,
    conflicting_visual_brand_names,
    conflicting_brands,
)
from .excel_io import project_hint_from_brief
from .image_assets import build_image_manifest, build_reviewed_image_manifest, read_jsonl, write_jsonl
from .image_cli import write_minimax_analysis_incremental
from .image_understanding import _minimax_prompt, _parse_minimax_output, minimax_credentials_available
from .review_quality import ReviewWorkbookError, analyze_review_workbooks
from .pipeline_runtime import publish_file_atomic
from .run_contract import sha256_file
from .run_state import cache_fingerprint, write_json_atomic
from .source_brand_review import load_creative_asset_review
from .script_type_adapter import (
    category_id_for_script_type,
    normalize_script_type,
    route_contract_for_script_type,
)
from .video_frames import VideoFrameError, build_project_video_frame_manifest


VISUAL_CACHE_SCHEMA_VERSION = "image-analysis-v1"
VISUAL_CACHE_RULE_VERSION = "event-visual-rules-v0.3"
VISUAL_CACHE_PROMPT_VERSION = "minimax-event-caption-v3-category-context"
VISUAL_CACHE_PROVIDER = "mmx-vision-describe/provider-managed"


class AutoPipelineError(RuntimeError):
    pass


VisionBatchProvider = Callable[..., int]


@dataclass(frozen=True)
class AutoPipelineAssets:
    cache_dir: Path
    manifest_path: Path
    analysis_path: Path
    report_path: Path
    manifest_rows: list[dict]
    analysis_rows: list[dict]
    reused_analysis_count: int
    generated_analysis_count: int
    cache_fingerprint: str


@dataclass(frozen=True)
class AutoPipelineMediaManifest:
    cache_dir: Path
    manifest_path: Path
    report_path: Path
    manifest_rows: list[dict]


def snapshot_visual_assets(
    assets: AutoPipelineAssets,
    run_dir: str | Path,
) -> AutoPipelineAssets:
    target_dir = Path(run_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = publish_file_atomic(
        assets.manifest_path, target_dir / "image_manifest.jsonl"
    )
    analysis_path = publish_file_atomic(
        assets.analysis_path, target_dir / "image_analysis.minimax.jsonl"
    )
    return replace(
        assets,
        manifest_path=manifest_path,
        analysis_path=analysis_path,
    )


def current_visual_cache_fingerprint(minimax_command: str = "mmx") -> str:
    return cache_fingerprint(
        schema=VISUAL_CACHE_SCHEMA_VERSION,
        rule=VISUAL_CACHE_RULE_VERSION,
        prompt=f"{VISUAL_CACHE_PROMPT_VERSION}:{_minimax_prompt()}",
        provider=f"{VISUAL_CACHE_PROVIDER}:command={minimax_command}",
    )


def project_cache_dir(brief: dict, cache_root: str | Path) -> Path:
    hint = project_key_from_brief(brief)
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", hint, flags=re.UNICODE).strip("-.")
    return Path(cache_root) / (slug or "发布会项目")


def project_key_from_brief(brief: dict) -> str:
    material_value = str(brief.get("素材目录或素材文件") or "").strip()
    material_ref = Path(material_value) if material_value else None
    if (
        material_ref is not None
        and material_ref.exists()
        and material_ref.is_dir()
        and material_ref.name != "发布会类"
    ):
        return material_ref.name
    area = str(brief.get("项目区域文件名") or "")
    bracket = re.search(r"【([^】]+)】", area)
    if bracket:
        return bracket.group(1).strip()
    area_stem = Path(area).stem.strip()
    if area_stem and area_stem not in {"发布会", "发布会类"}:
        return area_stem
    return str(brief.get("品牌或客户") or brief.get("项目名称") or "发布会项目").strip()


def resolve_project_media_root(brief: dict, source_root: str | Path) -> Path:
    material_ref = Path(str(brief.get("素材目录或素材文件") or ""))
    project_key = project_key_from_brief(brief)
    if material_ref.exists() and material_ref.is_dir() and material_ref.name == project_key:
        return material_ref
    launch_root = Path(source_root) / "发布会类"
    exact = launch_root / project_key
    if exact.exists() and exact.is_dir():
        return exact
    hint = re.sub(r"\s+", "", project_hint_from_brief(brief).lower())
    if launch_root.exists():
        matches = [
            path for path in launch_root.iterdir()
            if path.is_dir() and re.sub(r"\s+", "", path.name.lower()) in hint
        ]
        if matches:
            return sorted(matches, key=lambda path: len(path.name), reverse=True)[0]
    raise AutoPipelineError(f"未找到项目图片目录: project={project_key}, source_root={source_root}")


def prepare_auto_pipeline_assets(
    brief: dict,
    source_root: str | Path,
    cache_root: str | Path,
    api_key: str,
    minimax_command: str = "mmx",
    timeout_seconds: int = 90,
    workers: int = 4,
    source_brand_review_path: str | Path | None = None,
) -> AutoPipelineAssets:
    try:
        media_manifest = build_auto_pipeline_media_manifest(
            brief,
            source_root,
            cache_root,
            source_brand_review_path=source_brand_review_path,
        )
    except VideoFrameError as exc:
        raise AutoPipelineError(str(exc)) from exc
    return analyze_auto_pipeline_media_manifest(
        media_manifest,
        api_key=api_key,
        minimax_command=minimax_command,
        timeout_seconds=timeout_seconds,
        workers=workers,
    )


def build_auto_pipeline_media_manifest(
    brief: dict,
    source_root: str | Path,
    cache_root: str | Path,
    source_brand_review_path: str | Path | None = None,
) -> AutoPipelineMediaManifest:
    cache_dir = project_cache_dir(brief, cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "image_manifest.jsonl"
    report_path = cache_dir / "run_report.json"
    project_hint = project_key_from_brief(brief)
    project_manifest = []
    try:
        media_root = resolve_project_media_root(brief, source_root)
    except AutoPipelineError:
        media_root = None
    if media_root is not None:
        project_manifest = build_image_manifest(
            media_root,
            project_hint=project_hint,
            script_type=normalize_script_type(brief.get("脚本类型") or "发布会"),
        )
        project_manifest.extend(build_project_video_frame_manifest(
            media_root=media_root,
            launch_root=Path(source_root) / "发布会类",
            cache_root=cache_dir,
            aliases=(project_hint, str(brief.get("品牌或客户") or "")),
            project_name=project_hint,
        ))
    creative_manifest = []
    if source_brand_review_path:
        creative_manifest = build_reviewed_image_manifest(
            source_root,
            load_creative_asset_review(source_brand_review_path),
            script_type=normalize_script_type(brief.get("脚本类型") or "发布会"),
        )
    manifest_by_id = {
        str(row.get("image_id") or ""): row
        for row in [*project_manifest, *creative_manifest]
        if row.get("image_id")
    }
    manifest_rows = sorted(
        manifest_by_id.values(),
        key=lambda row: (str(row.get("项目名") or ""), str(row.get("原图路径") or "")),
    )
    write_jsonl(manifest_path, manifest_rows)
    return AutoPipelineMediaManifest(
        cache_dir=cache_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        manifest_rows=manifest_rows,
    )


def analyze_auto_pipeline_media_manifest(
    media_manifest: AutoPipelineMediaManifest,
    *,
    api_key: str,
    minimax_command: str = "mmx",
    timeout_seconds: int = 90,
    workers: int = 4,
    model: str = "",
    seed_analysis_rows: list[dict] | None = None,
    vision_provider: VisionBatchProvider | None = None,
) -> AutoPipelineAssets:
    cache_dir = media_manifest.cache_dir
    manifest_path = media_manifest.manifest_path
    manifest_rows = media_manifest.manifest_rows
    analysis_path = cache_dir / "image_analysis.minimax.jsonl"
    cache_meta_path = cache_dir / "cache_meta.json"
    manifest_ids = {str(row.get("image_id") or "") for row in manifest_rows}
    fingerprint = current_visual_cache_fingerprint(
        minimax_command if not model else f"{minimax_command}:model={model}"
    )
    cache_metadata = _read_json(cache_meta_path)
    metadata_exists = cache_meta_path.exists()
    cache_mismatch = metadata_exists and cache_metadata.get("fingerprint") != fingerprint
    cache_integrity_mismatch = (
        metadata_exists
        and not cache_mismatch
        and not _analysis_cache_content_matches(cache_metadata, analysis_path)
    )
    cached_rows = (
        read_jsonl(analysis_path)
        if metadata_exists and not cache_mismatch and not cache_integrity_mismatch
        else []
    )
    seed_rows = list(seed_analysis_rows or [])
    existing_rows = [*cached_rows, *seed_rows]
    has_credentials = vision_provider is not None or minimax_credentials_available(
        api_key, minimax_command
    )
    if cache_mismatch or cache_integrity_mismatch:
        existing_rows = seed_rows
    valid_rows = _valid_minimax_rows(existing_rows, manifest_ids)
    covered_ids = {str(row.get("image_id") or "") for row in valid_rows}
    pending_count = len(manifest_ids - covered_ids)
    if pending_count and not has_credentials:
        cache_reason = (
            "缓存指纹已变化；" if cache_mismatch
            else "缓存完整性校验失败；" if cache_integrity_mismatch
            else ""
        )
        raise AutoPipelineError(
            f"{cache_reason}缺少 MiniMax 凭据（MINIMAX_API_KEY 或 mmx auth login），"
            f"仍有 {pending_count}/{len(manifest_ids)} 张图片待视觉分析；"
            f"缓存目录={cache_dir}"
        )
    write_jsonl(analysis_path, valid_rows)

    generated_count = 0
    if pending_count:
        provider = vision_provider or write_minimax_analysis_incremental
        try:
            generated_count = provider(
                manifest_rows,
                analysis_path,
                api_key=api_key,
                command=minimax_command,
                model=model,
                timeout_seconds=timeout_seconds,
                workers=workers,
            )
        except BaseException:
            partial_rows = _valid_minimax_rows(read_jsonl(analysis_path), manifest_ids)
            write_jsonl(analysis_path, partial_rows)
            _write_visual_cache_metadata(
                cache_meta_path,
                manifest_path,
                analysis_path,
                fingerprint,
                minimax_command,
                status="partial",
            )
            raise
    analysis_rows = _valid_minimax_rows(read_jsonl(analysis_path), manifest_ids)
    write_jsonl(analysis_path, analysis_rows)
    analysis_ids = {row["image_id"] for row in analysis_rows}
    if analysis_ids != manifest_ids:
        _write_visual_cache_metadata(
            cache_meta_path,
            manifest_path,
            analysis_path,
            fingerprint,
            minimax_command,
            status="partial",
        )
        missing_ids = sorted(manifest_ids - analysis_ids)
        raise AutoPipelineError(
            "MiniMax 视觉分析未覆盖当前 manifest 全部图片；"
            f"缺失 image_id={','.join(missing_ids)}"
        )
    _write_visual_cache_metadata(
        cache_meta_path,
        manifest_path,
        analysis_path,
        fingerprint,
        minimax_command,
        status="complete",
    )
    return AutoPipelineAssets(
        cache_dir=cache_dir,
        manifest_path=manifest_path,
        analysis_path=analysis_path,
        report_path=media_manifest.report_path,
        manifest_rows=manifest_rows,
        analysis_rows=analysis_rows,
        reused_analysis_count=len(analysis_rows) - generated_count,
        generated_analysis_count=generated_count,
        cache_fingerprint=fingerprint,
    )


def _write_visual_cache_metadata(
    cache_meta_path: Path,
    manifest_path: Path,
    analysis_path: Path,
    fingerprint: str,
    minimax_command: str,
    status: str,
) -> None:
    metadata = {
        "fingerprint": fingerprint,
        "schema_version": VISUAL_CACHE_SCHEMA_VERSION,
        "rule_version": VISUAL_CACHE_RULE_VERSION,
        "prompt_version": VISUAL_CACHE_PROMPT_VERSION,
        "provider": VISUAL_CACHE_PROVIDER,
        "manifest_sha256": sha256_file(manifest_path),
        "analysis_sha256": sha256_file(analysis_path),
        "status": status,
        "command_identity": minimax_command,
    }
    write_json_atomic(cache_meta_path, metadata)


def write_run_report(
    assets: AutoPipelineAssets,
    brief: dict,
    result,
    workbook_path: str | Path | None,
    review_html_path: str | Path | None,
    runtime_state=None,
) -> dict:
    audit_path = result.audit_path if result.exit_code == 0 else result.best_failed_audit_path
    audit = _read_audit(audit_path)
    machine_audit_status = "pass" if audit.get("status") == "pass" else "fail"
    machine_audit_stage_status = (
        "blocked" if machine_audit_status == "fail"
        else "succeeded_with_warnings" if audit.get("warning_count", 0)
        else "succeeded"
    )
    script_document_audit = audit.get("script_document_audit") or {
        "status": machine_audit_status,
        "hard_fail_count": audit.get("hard_fail_count", 0),
        "warning_count": audit.get("warning_count", 0),
    }
    media_readiness = dict(audit.get("media_readiness") or {})
    source_reference_rows = [
        row for row in assets.manifest_rows
        if row.get("asset_role") == "source_reference"
    ]
    project_media_rows = [
        row for row in assets.manifest_rows
        if row.get("asset_role") != "source_reference"
    ]
    analysis_ids = {
        str(row.get("image_id") or "") for row in assets.analysis_rows
        if row.get("image_id")
    }
    analyzed_project_media_count = sum(
        str(row.get("image_id") or "") in analysis_ids for row in project_media_rows
    )
    if not project_media_rows and source_reference_rows:
        media_readiness["status"] = "not_provided"
    elif (
        not project_media_rows
        or analyzed_project_media_count < len(project_media_rows)
    ):
        media_readiness["status"] = "not_ready"
    else:
        media_readiness.setdefault("status", "not_assessed")
    media_readiness.update({
        "manifest_count": len(project_media_rows),
        "analysis_count": analyzed_project_media_count,
        "source_reference_count": len(source_reference_rows),
    })
    human_review_status, pending_human_review_count = _human_review_status(workbook_path)
    release_decision = "blocked"
    release_exit_code = 2 if machine_audit_status == "fail" else 3
    script_type = normalize_script_type(brief.get("脚本类型") or "发布会")
    report = {
        "project_name": brief.get("项目名称", ""),
        "project_hint": project_key_from_brief(brief),
        "script_type": script_type,
        "category_id": category_id_for_script_type(script_type),
        "route_contract": route_contract_for_script_type(script_type),
        "machine_audit_status": machine_audit_status,
        "machine_audit_stage_status": machine_audit_stage_status,
        "script_document_audit": script_document_audit,
        "media_readiness": media_readiness,
        "media_readiness_status": media_readiness["status"],
        "human_review_status": human_review_status,
        "release_decision": release_decision,
        "generation_exit_code": result.exit_code,
        "status": release_decision,
        "exit_code": release_exit_code,
        "output_path": str(workbook_path or ""),
        "audit_path": str(audit_path or ""),
        "visual_review_html": str(review_html_path or ""),
        "manifest_path": str(assets.manifest_path),
        "analysis_path": str(assets.analysis_path),
        "manifest_sha256": sha256_file(assets.manifest_path),
        "analysis_sha256": sha256_file(assets.analysis_path),
        "manifest_count": len(assets.manifest_rows),
        "analysis_count": len(assets.analysis_rows),
        "visual_analysis_coverage": _ratio(len(assets.analysis_rows), len(assets.manifest_rows)),
        "source_reference_manifest_count": len(source_reference_rows),
        "project_media_manifest_count": len(project_media_rows),
        "project_media_analysis_count": analyzed_project_media_count,
        "project_media_analysis_coverage": _ratio(
            analyzed_project_media_count, len(project_media_rows)
        ),
        "reused_analysis_count": assets.reused_analysis_count,
        "generated_analysis_count": assets.generated_analysis_count,
        "loop_count": len(result.iterations),
        "passed_iteration": result.passed_iteration,
        "hard_fail_count": audit.get("hard_fail_count", 0),
        "warning_count": audit.get("warning_count", 0),
        "score": audit.get("score", 0),
        "blocking_items": audit.get("blocking_items", []),
        "pending_human_review_count": pending_human_review_count,
        "cache_fingerprint": assets.cache_fingerprint,
        "run_report_path": str(assets.report_path),
    }
    if runtime_state is not None:
        report.update(_runtime_report_fields(runtime_state, workbook_path))
    if workbook_path and Path(workbook_path).exists():
        report.update(_workbook_metrics(workbook_path, brief))
    _write_report(assets.report_path, report)
    return report


def write_auto_pipeline_error_report(
    brief: dict,
    cache_root: str | Path,
    error: AutoPipelineError,
    runtime_state=None,
) -> Path:
    cache_dir = project_cache_dir(brief, cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    report_path = (
        runtime_state.run_dir / "run_report.json"
        if runtime_state is not None
        else cache_dir / "run_report.json"
    )
    script_type = normalize_script_type(brief.get("脚本类型") or "发布会")
    report = {
        "project_name": brief.get("项目名称", ""),
        "project_hint": project_key_from_brief(brief),
        "script_type": script_type,
        "category_id": category_id_for_script_type(script_type),
        "route_contract": route_contract_for_script_type(script_type),
        "machine_audit_status": "fail",
        "machine_audit_stage_status": "blocked",
        "script_document_audit": {
            "status": "fail",
            "hard_fail_count": 0,
            "warning_count": 0,
        },
        "media_readiness": {
            "status": "not_assessed",
            "manifest_count": 0,
            "analysis_count": 0,
        },
        "media_readiness_status": "not_assessed",
        "human_review_status": "invalid",
        "release_decision": "blocked",
        "generation_exit_code": 1,
        "status": "blocked",
        "exit_code": 1,
        "output_path": "",
        "audit_path": "",
        "visual_review_html": "",
        "manifest_path": str(cache_dir / "image_manifest.jsonl"),
        "analysis_path": str(cache_dir / "image_analysis.minimax.jsonl"),
        "manifest_sha256": "",
        "analysis_sha256": "",
        "manifest_count": 0,
        "analysis_count": 0,
        "visual_analysis_coverage": 0.0,
        "reused_analysis_count": 0,
        "generated_analysis_count": 0,
        "loop_count": 0,
        "passed_iteration": None,
        "hard_fail_count": 0,
        "warning_count": 0,
        "score": 0,
        "blocking_items": [],
        "pending_human_review_count": 0,
        "error": str(error),
        "cache_fingerprint": current_visual_cache_fingerprint(),
        "run_report_path": str(report_path),
    }
    if runtime_state is not None:
        report.update(_runtime_report_fields(runtime_state, None))
    _write_report(report_path, report)
    return report_path


def assets_from_manual_paths(
    brief: dict,
    cache_root: str | Path,
    manifest_path: str | Path,
    analysis_path: str | Path,
) -> AutoPipelineAssets:
    manifest_rows = read_jsonl(manifest_path)
    analysis_rows = read_jsonl(analysis_path)
    if not manifest_rows or not analysis_rows:
        raise AutoPipelineError("手动 manifest/analysis 为空或不可读取")
    cache_dir = project_cache_dir(brief, cache_root)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return AutoPipelineAssets(
        cache_dir=cache_dir,
        manifest_path=Path(manifest_path),
        analysis_path=Path(analysis_path),
        report_path=cache_dir / "run_report.json",
        manifest_rows=manifest_rows,
        analysis_rows=analysis_rows,
        reused_analysis_count=len(analysis_rows),
        generated_analysis_count=0,
        cache_fingerprint=current_visual_cache_fingerprint(),
    )


def _valid_minimax_rows(rows: list[dict], manifest_ids: set[str]) -> list[dict]:
    by_id = {}
    for row in rows:
        item = dict(row)
        raw_caption = str(item.get("caption") or "")
        if '"caption"' in raw_caption and '"brand_names"' in raw_caption:
            reparsed = _parse_minimax_output(raw_caption)
            for field in ("caption", "tags", "brand_names", "ocr_text", "quality_flags", "negative_flags"):
                if reparsed.get(field) not in (None, "", []):
                    item[field] = reparsed[field]
        image_id = str(item.get("image_id") or "")
        if image_id not in manifest_ids:
            continue
        if item.get("source") != "minimax":
            continue
        if not item.get("brand_names"):
            item["brand_names"] = ["未知"]
        if (
            not str(item.get("caption") or "").strip()
            or not item.get("tags")
        ):
            continue
        item["analysis_provider"] = "minimax"
        item["analysis_model"] = "mmx-vision-describe/provider-managed"
        item["requested_model"] = ""
        item["model_selection"] = "provider_managed"
        item["provenance_status"] = "api_generated"
        by_id[image_id] = item
    return [by_id[key] for key in sorted(by_id)]


def _read_audit(path: str | Path | None) -> dict:
    if not path:
        return {}
    return _read_json(path)


def _analysis_cache_content_matches(metadata: dict, analysis_path: Path) -> bool:
    expected_analysis = str(metadata.get("analysis_sha256") or "")
    if not expected_analysis:
        return False
    if not analysis_path.is_file():
        return False
    return sha256_file(analysis_path) == expected_analysis


def _read_json(path: str | Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_report(path: Path, report: dict) -> None:
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _legacy_minimax_rows(cache_root: Path, manifest_ids: set[str]) -> list[dict]:
    rows = []
    for path in sorted(cache_root.glob("image_analysis*.jsonl")):
        rows.extend(read_jsonl(path))
    return _valid_minimax_rows(rows, manifest_ids)


def _workbook_metrics(path: str | Path, brief: dict) -> dict:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        details = _sheet_rows(wb["抽调明细"]) if "抽调明细" in wb.sheetnames else []
        visuals = _sheet_rows(wb["视觉资产"]) if "视觉资产" in wb.sheetnames else []
        adjacent = _sheet_rows(wb["相邻镜头复核"]) if "相邻镜头复核" in wb.sheetnames else []
        full_film = _sheet_rows(wb["整片复核"]) if "整片复核" in wb.sheetnames else []
    finally:
        wb.close()
    traced = sum(
        bool(
            (row.get("采用来源文件") and row.get("采用来源 sheet") and row.get("采用来源行"))
            or (row.get("替换来源文件") and row.get("替换来源 sheet") and row.get("替换来源行"))
            or (row.get("模板来源文件") and row.get("模板来源 sheet") and row.get("模板来源行"))
        )
        for row in details
    )
    total_duration = sum(
        _integer(row.get("时长(秒)"))
        for row in details
    )
    bound_visuals = sum(
        bool(row.get("image_id") and row.get("原图路径"))
        for row in visuals
    )
    traceable_visuals = sum(
        bool(
            row.get("image_id")
            and row.get("原图路径")
            and row.get("来源文件")
            and row.get("来源 sheet")
            and row.get("来源行")
        )
        for row in visuals
    )
    expected_brand = str(brief.get("品牌或客户") or brief.get("项目名称") or "")
    brand_conflicts = 0
    uses: dict[str, set[int]] = {}
    for row in visuals:
        observed = " ".join([
            str(row.get("视觉内容摘要") or ""),
            str(row.get("视觉标签") or ""),
        ])
        brand_conflicts += bool(
            conflicting_brands(expected_brand, observed)
            or conflicting_visual_brand_names(expected_brand, row.get("brand_names"))
            or brand_evidence_unverified(
                expected_brand, row.get("brand_names"), row.get("ocr_text")
            )
        )
        image_id = str(row.get("image_id") or "")
        if image_id:
            try:
                slot = int(row.get("slot_index") or 0)
            except (TypeError, ValueError):
                slot = 0
            uses.setdefault(image_id, set()).add(slot)
    duplicate_count = sum(
        1 for slots in uses.values()
        if len(slots) > 1 and slots != {3, 10}
    )
    return {
        "shot_count": len(details),
        "total_duration_seconds": total_duration,
        "adjacent_edge_count": len(adjacent),
        "full_film_review_count": len(full_film),
        "source_triplet_count": traced,
        "visual_asset_count": bound_visuals,
        "visual_slot_count": len(visuals),
        "visual_gap_count": max(0, len(visuals) - bound_visuals),
        "visual_traceable_asset_count": traceable_visuals,
        "source_reference_rate": _ratio(traced, len(details)),
        "visual_evidence_rate": _ratio(bound_visuals, len(visuals)),
        "visual_traceability_rate": _ratio(traceable_visuals, bound_visuals),
        "duplicate_image_count": duplicate_count,
        "brand_conflict_count": brand_conflicts,
    }


def _integer(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _human_review_status(path: str | Path | None) -> tuple[str, int]:
    if not path or not Path(path).exists():
        return "invalid", 0
    try:
        project = analyze_review_workbooks([path])["projects"][0]
    except ReviewWorkbookError:
        return "invalid", 0
    if project["status"] == "invalid":
        return "invalid", project["pending_shot_count"]
    return project["human_review_status"], project["pending_shot_count"]


def _sheet_rows(ws) -> list[dict]:
    from .excel_io import read_managed_sheet_rows

    return [row for row in read_managed_sheet_rows(ws.parent, ws.title)
            if any(value not in (None, "") for value in row.values())]


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _runtime_report_fields(runtime_state, workbook_path: str | Path | None) -> dict:
    fields = {
        "run_id": runtime_state.run_id,
        "run_dir": str(runtime_state.run_dir),
        "run_manifest_path": str(runtime_state.manifest_path),
        "run_ledger_path": str(runtime_state.ledger_path),
    }
    if workbook_path and Path(workbook_path).exists():
        fields["output_sha256"] = sha256_file(workbook_path)
    else:
        fields["output_sha256"] = ""
    return fields
