from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path

from .ai_spec import AiSpecError, load_ai_spec_shots
from .asset_package import (
    AssetPackageError,
    asset_package_source_record,
    package_media_for_brief,
    package_script_catalog,
    source_in_frozen_library,
)
from .auto_pipeline import (
    AutoPipelineAssets,
    AutoPipelineError,
    AutoPipelineMediaManifest,
    analyze_auto_pipeline_media_manifest,
    build_auto_pipeline_media_manifest,
    current_visual_cache_fingerprint,
    VisionBatchProvider,
    project_cache_dir,
    resolve_project_media_root,
)
from .excel_io import (
    load_creative_variants,
    load_template_slots,
    project_hint_from_brief,
)
from .script_type_adapter import (
    category_id_for_script_type,
    normalize_script_type,
    resolve_script_type_workbook,
    route_contract_for_script_type,
)
from .run_contract import _redact_config, sha256_file, sha256_json
from .run_state import write_json_atomic
from .image_assets import build_supplemental_image_asset, read_jsonl, write_jsonl
from .image_understanding import VisionProviderError
from .pipeline_runtime import publish_file_atomic
from .portable_paths import resolve_run_local_path
from .stage_contract import StageError, StageErrorCategory, StageStatus
from .stage_runtime import (
    finish_pipeline_stage,
    reused_fork_stage_output,
    start_pipeline_stage,
)
from .video_frames import VideoFrameError
from .project_image_authority import ProjectImagePlanningReadiness


_SUCCESS_STATUSES = {
    StageStatus.SUCCEEDED.value,
    StageStatus.SUCCEEDED_WITH_WARNINGS.value,
}


class AutoPipelineStageError(AutoPipelineError):
    def __init__(self, stage_id: str, status: str, error: StageError, exit_code: int = 1):
        super().__init__(error.message)
        self.stage_id = stage_id
        self.status = status
        self.error = error
        self.exit_code = exit_code


@dataclass(frozen=True)
class SourceCatalog:
    path: Path
    rows: list[dict]
    template_count: int
    creative_count: int


def run_formal_context_media_stage(
    state,
    *,
    input_freeze_path: str | Path,
    readiness: ProjectImagePlanningReadiness,
) -> AutoPipelineMediaManifest:
    """Persist the formal image authority as the normal media-manifest stage.

    This is deliberately separate from ``run_media_manifest_stage``: formal
    generation must never discover images by walking the legacy source root.
    """
    if not isinstance(readiness, ProjectImagePlanningReadiness):
        raise TypeError("正式图片上下文只接受 ProjectImagePoolService 返回对象")
    stage_id = "media_manifest"
    frozen = reused_fork_stage_output(state, stage_id, "image_manifest.jsonl")
    if frozen is not None:
        return AutoPipelineMediaManifest(
            cache_dir=state.run_dir,
            manifest_path=frozen,
            report_path=state.run_dir / "formal-image-context.report.json",
            manifest_rows=read_jsonl(frozen),
        )
    rows = _formal_context_rows(readiness)
    input_sha256 = sha256_json({
        "input_freeze_sha256": sha256_file(input_freeze_path),
        "formal_image_context": rows,
    })
    record = start_pipeline_stage(
        state, stage_id, input_sha256=input_sha256,
        input_refs=(str(Path(input_freeze_path).resolve()),),
    )
    reused = _reused_output(state, record)
    if reused is not None:
        return AutoPipelineMediaManifest(
            cache_dir=state.run_dir,
            manifest_path=reused,
            report_path=state.run_dir / "formal-image-context.report.json",
            manifest_rows=read_jsonl(reused),
        )
    output_path = state.run_dir / "image_manifest.jsonl"
    write_jsonl(output_path, rows)
    finish_pipeline_stage(
        state, stage_id, StageStatus.SUCCEEDED,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return AutoPipelineMediaManifest(
        cache_dir=state.run_dir, manifest_path=output_path,
        report_path=state.run_dir / "formal-image-context.report.json",
        manifest_rows=rows,
    )


def run_formal_context_visual_stage(
    state,
    *,
    media_manifest: AutoPipelineMediaManifest,
) -> AutoPipelineAssets:
    """Persist verified manual image analyses without invoking a vision provider."""
    stage_id = "visual_analysis"
    frozen = reused_fork_stage_output(state, stage_id, "image_analysis.minimax.jsonl")
    if frozen is not None:
        rows = read_jsonl(frozen)
        return AutoPipelineAssets(
            cache_dir=media_manifest.cache_dir, manifest_path=media_manifest.manifest_path,
            analysis_path=frozen, report_path=media_manifest.report_path,
            manifest_rows=media_manifest.manifest_rows, analysis_rows=rows,
            reused_analysis_count=len(rows), generated_analysis_count=0,
            cache_fingerprint=str(state.cache_fingerprint or ""),
        )
    rows = [
        {key: value for key, value in row.items() if key not in {"analysis_ref", "analysis_sha256"}}
        for row in media_manifest.manifest_rows
    ]
    input_sha256 = sha256_json({
        "media_manifest_sha256": sha256_file(media_manifest.manifest_path),
        "formal_manual_analysis": rows,
    })
    record = start_pipeline_stage(
        state, stage_id, input_sha256=input_sha256,
        input_refs=(str(media_manifest.manifest_path.resolve()),),
    )
    reused = _reused_output(state, record)
    if reused is not None:
        rows = read_jsonl(reused)
        return AutoPipelineAssets(
            cache_dir=media_manifest.cache_dir, manifest_path=media_manifest.manifest_path,
            analysis_path=reused, report_path=media_manifest.report_path,
            manifest_rows=media_manifest.manifest_rows, analysis_rows=rows,
            reused_analysis_count=len(rows), generated_analysis_count=0,
            cache_fingerprint=str(state.cache_fingerprint or ""),
        )
    output_path = state.run_dir / "image_analysis.minimax.jsonl"
    write_jsonl(output_path, rows)
    finish_pipeline_stage(
        state, stage_id, StageStatus.SUCCEEDED,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return AutoPipelineAssets(
        cache_dir=media_manifest.cache_dir, manifest_path=media_manifest.manifest_path,
        analysis_path=output_path, report_path=media_manifest.report_path,
        manifest_rows=media_manifest.manifest_rows, analysis_rows=rows,
        reused_analysis_count=0, generated_analysis_count=0,
        cache_fingerprint=str(state.cache_fingerprint or ""),
    )


def _formal_context_rows(readiness: ProjectImagePlanningReadiness) -> list[dict]:
    """Project image rows contain business context, never an OS path or source bytes."""
    rows: list[dict] = []
    for image in readiness.images:
        if image.current_analysis is None or image.business_summary is None:
            raise ValueError("正式图片上下文含未完成分析")
        if image.current_analysis.status != "pass":
            raise ValueError("正式图片上下文含非通过图片")
        summary = image.business_summary
        rows.append({
            "image_id": image.image_id,
            "image_sha256": image.image_sha256,
            "analysis_ref": image.current_analysis.evidence.ref,
            "analysis_sha256": image.current_analysis.evidence.sha256,
            "caption": summary.caption,
            "tags": list(summary.tags),
            "subjects": list(summary.subjects),
            "scene": summary.scene,
            "purpose": summary.purpose,
            "quality": summary.quality,
            "brand": summary.brand,
            "context_role": "candidate_only_journey6_selects_final_per_shot",
        })
    return rows


def run_initial_auto_pipeline_stages(
    state,
    *,
    brief: dict,
    config: dict,
    source_root: str | Path,
    cache_root: str | Path,
    source_view: str,
    api_key: str,
    minimax_command: str = "mmx",
    timeout_seconds: int = 90,
    workers: int = 4,
    vision_model: str = "",
    source_brand_review_path: str | Path | None = None,
    brief_ref: str | Path | None = None,
    manual_manifest_path: str | Path | None = None,
    manual_analysis_path: str | Path | None = None,
    asset_package: str | Path | None = None,
    supplemental_image_paths: list[str] | None = None,
    vision_provider: VisionBatchProvider | None = None,
) -> AutoPipelineAssets:
    if bool(manual_manifest_path) != bool(manual_analysis_path):
        raise ValueError("自动管线手动覆盖时必须同时提供 manifest 和 analysis")
    freeze_path = run_input_freeze_stage(
        state,
        brief=brief,
        config=config,
        source_root=source_root,
        brief_ref=brief_ref,
        asset_package=asset_package,
    )
    run_source_catalog_stage(
        state,
        input_freeze_path=freeze_path,
        brief=brief,
        source_root=source_root,
        source_view=source_view,
        source_brand_review_path=source_brand_review_path,
        asset_package=asset_package,
    )
    media_manifest = run_media_manifest_stage(
        state,
        input_freeze_path=freeze_path,
        brief=brief,
        source_root=source_root,
        cache_root=cache_root,
        source_brand_review_path=source_brand_review_path,
        manual_manifest_path=manual_manifest_path,
        asset_package=asset_package,
        supplemental_image_paths=supplemental_image_paths,
    )
    return run_visual_analysis_stage(
        state,
        media_manifest=media_manifest,
        api_key=api_key,
        minimax_command=minimax_command,
        timeout_seconds=timeout_seconds,
        workers=workers,
        vision_model=vision_model,
        manual_analysis_path=manual_analysis_path,
        asset_package=asset_package,
        vision_provider=vision_provider,
    )


def run_input_freeze_stage(
    state,
    *,
    brief: dict,
    config: dict,
    source_root: str | Path,
    brief_ref: str | Path | None = None,
    asset_package: str | Path | None = None,
    asset_package_evidence: dict | None = None,
) -> Path:
    stage_id = "input_freeze"
    frozen = reused_fork_stage_output(state, stage_id, "input_freeze.json")
    if frozen is not None:
        return frozen
    script_type = normalize_script_type(brief.get("脚本类型") or "发布会")
    expected_category_id = category_id_for_script_type(script_type)
    workbook_path = resolve_script_type_workbook(source_root, script_type)
    workbook_record = {"path": str(workbook_path.resolve()), "sha256": None, "missing": True}
    workbook_error = None
    if asset_package:
        try:
            package_source = asset_package_source_record(
                asset_package,
                expected_category_id=expected_category_id,
            )
            workbook_path = Path(package_source["source_workbook_path"])
            workbook_record = {
                "path": package_source["source_workbook_path"],
                "sha256": package_source["source_workbook_sha256"],
                "asset_package_id": package_source["package_id"],
                "index_fingerprint": package_source["index_fingerprint"],
            }
        except AssetPackageError as exc:
            workbook_error = exc
    elif workbook_path.is_file():
        try:
            workbook_record = {
                "path": str(workbook_path.resolve()),
                "sha256": sha256_file(workbook_path),
            }
        except OSError as exc:
            workbook_error = exc

    redacted_brief = _redact_config(brief)
    redacted_config = _redact_config(config)
    input_sha256 = sha256_json({
        "category_id": expected_category_id,
        "brief": redacted_brief,
        "config": redacted_config,
        "source_workbook": workbook_record,
        "formal_asset_package": asset_package_evidence,
    })
    input_refs = [str(brief_ref)] if brief_ref else ["brief://redacted"]
    input_refs.append(workbook_record["path"])
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=input_refs,
    )
    reused = _reused_output(state, record)
    if reused is not None:
        return reused
    if state.category_id != expected_category_id:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "category_route_mismatch",
            f"运行类别 {state.category_id} 与 Brief 类型 {script_type}/{expected_category_id} 不一致",
        )
    if not isinstance(brief, dict) or not brief:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.INPUT,
            "brief_missing",
            "缺少可冻结的 Brief",
        )
    if workbook_error is not None:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "source_workbook_hash_failed",
            f"来源工作簿 hash 读取失败: {workbook_error}",
            retryable=True,
        )

    output_path = state.run_dir / "input_freeze.json"
    payload = {
        "schema_version": "mplus-input-freeze/v1",
        "category_id": state.category_id,
        "script_type": script_type,
        "route_contract": route_contract_for_script_type(script_type),
        "brief": redacted_brief,
        "config": redacted_config,
        "source_workbook": workbook_record,
        "formal_asset_package": asset_package_evidence,
    }
    try:
        write_json_atomic(output_path, payload)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "input_freeze_write_failed",
            f"InputFreeze 写出失败: {exc}",
            retryable=True,
        )
    finish_pipeline_stage(
        state,
        stage_id,
        StageStatus.SUCCEEDED,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return output_path


def run_source_catalog_stage(
    state,
    *,
    input_freeze_path: str | Path,
    brief: dict,
    source_root: str | Path,
    source_view: str,
    source_brand_review_path: str | Path | None = None,
    asset_package: str | Path | None = None,
    asset_package_evidence: dict | None = None,
    library_packages: tuple[str | Path, ...] | list[str | Path] = (),
) -> SourceCatalog:
    stage_id = "source_catalog"
    frozen = reused_fork_stage_output(state, stage_id, "source_catalog.json")
    if frozen is not None:
        return _read_source_catalog(frozen)
    script_type = normalize_script_type(brief.get("脚本类型") or "发布会")
    workbook_path = resolve_script_type_workbook(source_root, script_type)
    review_path = Path(source_brand_review_path) if source_brand_review_path else None
    package_source = None
    if asset_package:
        try:
            package_source = asset_package_source_record(
                asset_package,
                expected_category_id=state.category_id,
            )
            workbook_path = Path(package_source["source_workbook_path"])
        except AssetPackageError as exc:
            _abort(
                state,
                stage_id,
                StageStatus.BLOCKED,
                StageErrorCategory.CONTRACT,
                "asset_package_source_stale",
                str(exc),
            )
    workbook_sha256 = (
        package_source["source_workbook_sha256"]
        if package_source
        else sha256_file(workbook_path) if workbook_path.is_file() else None
    )
    review_sha256 = sha256_file(review_path) if review_path and review_path.is_file() else None
    input_sha256 = sha256_json({
        "input_freeze_sha256": sha256_file(input_freeze_path),
        "source_view": source_view,
        "project_hint": project_hint_from_brief(brief),
        "source_workbook_sha256": workbook_sha256,
        "source_brand_review_sha256": review_sha256,
        "asset_package_index": (
            package_source["index_fingerprint"] if package_source else None
        ),
        "formal_asset_package": asset_package_evidence,
    })
    input_refs = [str(Path(input_freeze_path).resolve()), str(workbook_path.resolve())]
    if review_path:
        input_refs.append(str(review_path.resolve()))
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=input_refs,
    )
    reused = _reused_output(state, record)
    if reused is not None:
        return _read_source_catalog(reused)
    if not workbook_path.is_file():
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.INPUT,
            "source_workbook_missing",
            f"来源工作簿不存在: {workbook_path}",
        )

    try:
        project_hint = project_hint_from_brief(brief)
        if asset_package:
            template, creative, package_source = package_script_catalog(
                asset_package,
                expected_category_id=state.category_id,
                source_view=source_view,
                library_paths=library_packages,
            )
        elif source_view == "ai-spec":
            template, creative = load_ai_spec_shots(
                source_root,
                brand_review_path=source_brand_review_path,
                project_hint=project_hint,
                script_type=script_type,
                workbook_path=workbook_path,
            )
        elif source_view == "human":
            template = load_template_slots(
                source_root,
                project_hint=project_hint,
                script_type=script_type,
                workbook_path=workbook_path,
            )
            creative = load_creative_variants(
                source_root,
                project_hint=project_hint,
                brand_review_path=source_brand_review_path,
                script_type=script_type,
                workbook_path=workbook_path,
            )
        else:
            _abort(
                state,
                stage_id,
                StageStatus.BLOCKED,
                StageErrorCategory.CONTRACT,
                "source_view_invalid",
                message=f"不支持的 source_view: {source_view}",
            )
    except AiSpecError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "ai_spec_not_ready",
            str(exc),
            exit_code=2,
        )
    except AssetPackageError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "asset_package_source_unavailable",
            str(exc),
            exit_code=2,
        )
    except AutoPipelineStageError:
        raise
    except Exception as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "source_catalog_read_failed",
            f"来源工作簿读取失败: {exc}",
            retryable=True,
        )

    if not template:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "source_template_empty",
            "来源工作簿未读取到可用模版记录",
        )
    serialized_rows = [
        {"record_type": record_type, **asdict(shot)}
        for record_type, shots in (("template", template), ("creative", creative))
        for shot in shots
    ]
    cross_category_rows = [
        row
        for row in serialized_rows
        if normalize_script_type(row.get("script_type", "")) != script_type
        and not (row['record_type'] == 'creative' and source_in_frozen_library(row, asset_package_evidence))
    ]
    if cross_category_rows:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "cross_category_source_forbidden",
            f"{script_type}的模板必须同类；跨类创意必须来自本次冻结的已登记共享库",
        )
    if any(
        not row.get("source_file")
        or not row.get("source_sheet")
        or not row.get("source_row")
        for row in serialized_rows
    ):
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "source_trace_incomplete",
            "来源记录缺少来源文件、来源 Sheet 或来源行",
        )

    output_path = state.run_dir / "source_catalog.json"
    payload = {
        "schema_version": "mplus-source-catalog/v1",
        "category_id": state.category_id,
        "script_type": script_type,
        "route_contract": route_contract_for_script_type(script_type),
        "source_view": source_view,
        "source_workbook": {
            "path": str(workbook_path.resolve()),
            "sha256": workbook_sha256,
        },
        "read_mode": "asset_package_index" if asset_package else "xlsx",
        "asset_package": (
            {
                "package_id": package_source["package_id"],
                "index_fingerprint": package_source["index_fingerprint"],
            }
            if package_source else None
        ),
        "formal_asset_package": asset_package_evidence,
        "record_count": len(serialized_rows),
        "template_count": len(template),
        "creative_count": len(creative),
        "source_refs": [
            {
                "source_file": row["source_file"],
                "source_sheet": row["source_sheet"],
                "source_row": row["source_row"],
            }
            for row in serialized_rows
        ],
        "records": serialized_rows,
    }
    try:
        write_json_atomic(output_path, payload)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "source_catalog_write_failed",
            f"SourceCatalog 写出失败: {exc}",
            retryable=True,
        )
    finish_pipeline_stage(
        state,
        stage_id,
        StageStatus.SUCCEEDED,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return SourceCatalog(
        path=output_path,
        rows=serialized_rows,
        template_count=len(template),
        creative_count=len(creative),
    )


def run_media_manifest_stage(
    state,
    *,
    input_freeze_path: str | Path,
    brief: dict,
    source_root: str | Path,
    cache_root: str | Path,
    source_brand_review_path: str | Path | None = None,
    manual_manifest_path: str | Path | None = None,
    asset_package: str | Path | None = None,
    supplemental_image_paths: list[str] | None = None,
) -> AutoPipelineMediaManifest:
    stage_id = "media_manifest"
    cache_dir = project_cache_dir(brief, cache_root)
    frozen = reused_fork_stage_output(state, stage_id, "image_manifest.jsonl")
    if frozen is not None:
        return AutoPipelineMediaManifest(
            cache_dir=cache_dir,
            manifest_path=frozen,
            report_path=cache_dir / "run_report.json",
            manifest_rows=read_jsonl(frozen),
        )
    manual_path = Path(manual_manifest_path) if manual_manifest_path else None
    review_path = Path(source_brand_review_path) if source_brand_review_path else None
    media_root = None
    resolve_error = None
    if manual_path is None:
        try:
            media_root = resolve_project_media_root(brief, source_root)
        except AutoPipelineError as exc:
            resolve_error = exc
    package_manifest_rows: list[dict] = []
    package_error: AssetPackageError | None = None
    if asset_package:
        try:
            package_manifest_rows, _ = package_media_for_brief(asset_package, brief)
        except AssetPackageError as exc:
            package_error = exc
    input_sha256 = sha256_json({
        "input_freeze_sha256": sha256_file(input_freeze_path),
        "manual_manifest_sha256": (
            sha256_file(manual_path) if manual_path and manual_path.is_file() else None
        ),
        "media_files": _directory_file_hashes(media_root) if media_root else [],
        "source_brand_review_sha256": (
            sha256_file(review_path) if review_path and review_path.is_file() else None
        ),
        "asset_package": str(asset_package or ""),
        "asset_package_manifest_sha256": (
            f"error:{sha256_json(str(package_error))}"
            if package_error is not None
            else sha256_json(package_manifest_rows)
            if asset_package
            else None
        ),
        "supplemental_images": [
            {"path": str(Path(value).expanduser().resolve()), "sha256": sha256_file(Path(value).expanduser())}
            for value in (supplemental_image_paths or [])
            if Path(value).expanduser().is_file()
        ],
    })
    input_refs = [str(Path(input_freeze_path).resolve())]
    if manual_path:
        input_refs.append(str(manual_path.resolve()))
    elif media_root:
        input_refs.append(str(media_root.resolve()))
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=input_refs,
    )
    reused = _reused_output(state, record)
    if reused is not None:
        return AutoPipelineMediaManifest(
            cache_dir=cache_dir,
            manifest_path=reused,
            report_path=cache_dir / "run_report.json",
            manifest_rows=read_jsonl(reused),
        )
    if package_error is not None:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "asset_package_category_mismatch",
            str(package_error),
        )
    try:
        if manual_path is not None:
            manifest_rows = read_jsonl(manual_path)
            if not manifest_rows:
                _abort(
                    state,
                    stage_id,
                    StageStatus.BLOCKED,
                    StageErrorCategory.INPUT,
                    "manual_manifest_empty",
                    "手动 manifest 为空或不可读取",
                )
            built = AutoPipelineMediaManifest(
                cache_dir=cache_dir,
                manifest_path=manual_path,
                report_path=cache_dir / "run_report.json",
                manifest_rows=manifest_rows,
            )
        else:
            built = build_auto_pipeline_media_manifest(
                brief,
                source_root,
                cache_root,
                source_brand_review_path=source_brand_review_path,
            )
        manifest_rows = list(built.manifest_rows)
        if package_manifest_rows:
            manifest_rows = _merge_manifest_rows(manifest_rows, package_manifest_rows)
        supplements = [
            build_supplemental_image_asset(
                value,
                str(brief.get("项目名称") or ""),
                normalize_script_type(brief.get("脚本类型") or "发布会"),
            )
            for value in (supplemental_image_paths or [])
        ]
        # 同一路径已存在于项目池或资产包时，保留其稳定 ID 与已有分析；
        # 补充图片只负责加入真正的新路径，不能把已索引图片变回待识别图片。
        manifest_rows = _merge_manifest_rows(supplements, manifest_rows)
        cache_manifest_path = cache_dir / "image_manifest.jsonl"
        write_jsonl(cache_manifest_path, manifest_rows)
        output_path = publish_file_atomic(
            cache_manifest_path,
            state.run_dir / "image_manifest.jsonl",
        )
        built = replace(built, manifest_path=output_path, manifest_rows=manifest_rows)
    except AutoPipelineStageError:
        raise
    except VideoFrameError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.EXECUTION,
            "video_manifest_failed",
            str(exc),
            retryable=True,
        )
    except AutoPipelineError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.INPUT,
            "media_manifest_empty",
            str(exc),
        )
    except AssetPackageError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "asset_package_category_mismatch",
            str(exc),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "media_manifest_failed",
            f"媒体 manifest 建立失败: {exc}",
            retryable=True,
        )
    stage_status = (
        StageStatus.SUCCEEDED_WITH_WARNINGS
        if resolve_error is not None or not built.manifest_rows
        else StageStatus.SUCCEEDED
    )
    finish_pipeline_stage(
        state,
        stage_id,
        stage_status,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return replace(built, manifest_path=output_path)


def run_visual_analysis_stage(
    state,
    *,
    media_manifest: AutoPipelineMediaManifest,
    api_key: str,
    minimax_command: str = "mmx",
    timeout_seconds: int = 90,
    workers: int = 4,
    vision_model: str = "",
    manual_analysis_path: str | Path | None = None,
    asset_package: str | Path | None = None,
    vision_provider: VisionBatchProvider | None = None,
) -> AutoPipelineAssets:
    stage_id = "visual_analysis"
    frozen = reused_fork_stage_output(
        state,
        stage_id,
        "image_analysis.minimax.jsonl",
    )
    if frozen is not None:
        rows = read_jsonl(frozen)
        return AutoPipelineAssets(
            cache_dir=media_manifest.cache_dir,
            manifest_path=media_manifest.manifest_path,
            analysis_path=frozen,
            report_path=media_manifest.report_path,
            manifest_rows=media_manifest.manifest_rows,
            analysis_rows=rows,
            reused_analysis_count=len(rows),
            generated_analysis_count=0,
            cache_fingerprint=str(state.cache_fingerprint or ""),
        )
    manual_path = Path(manual_analysis_path) if manual_analysis_path else None
    package_analysis = []
    package_error = None
    if asset_package:
        try:
            package_analysis = package_media_for_brief(
                asset_package,
                {},
                expected_category_id=state.category_id,
            )[1]
        except AssetPackageError as exc:
            package_error = exc
    fingerprint = current_visual_cache_fingerprint(
        minimax_command if not vision_model else f"{minimax_command}:model={vision_model}"
    )
    input_sha256 = sha256_json({
        "media_manifest_sha256": sha256_file(media_manifest.manifest_path),
        "cache_fingerprint": fingerprint,
        "manual_analysis_sha256": (
            sha256_file(manual_path) if manual_path and manual_path.is_file() else None
        ),
        "requested_model": vision_model,
        "asset_package_analysis": sha256_json(package_analysis) if package_analysis else None,
    })
    input_refs = [str(media_manifest.manifest_path.resolve())]
    if manual_path:
        input_refs.append(str(manual_path.resolve()))
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=input_refs,
    )
    reused = _reused_output(state, record)
    if reused is not None:
        rows = read_jsonl(reused)
        return AutoPipelineAssets(
            cache_dir=media_manifest.cache_dir,
            manifest_path=media_manifest.manifest_path,
            analysis_path=reused,
            report_path=media_manifest.report_path,
            manifest_rows=media_manifest.manifest_rows,
            analysis_rows=rows,
            reused_analysis_count=len(rows),
            generated_analysis_count=0,
            cache_fingerprint=fingerprint,
        )
    if package_error is not None:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "asset_package_category_mismatch",
            str(package_error),
        )
    warning = False
    cache_analysis = media_manifest.cache_dir / "image_analysis.minimax.jsonl"
    initial_analysis_count = len(read_jsonl(cache_analysis))
    manifest_ids = {
        str(row.get("image_id") or "")
        for row in media_manifest.manifest_rows
        if row.get("image_id")
    }
    seeded_analysis = _merge_analysis_rows(
        read_jsonl(cache_analysis),
        package_analysis,
        allowed_ids=manifest_ids,
    )
    analysis_manifest_rows = [
        row for row in media_manifest.manifest_rows
        if row.get("analysis_required", True) is not False
    ]
    model_cache_dir = media_manifest.cache_dir / "model_visual_analysis"
    try:
        if manual_path is not None:
            analysis_rows = read_jsonl(manual_path)
            if not analysis_rows:
                _abort(
                    state,
                    stage_id,
                    StageStatus.BLOCKED,
                    StageErrorCategory.INPUT,
                    "manual_analysis_empty",
                    "手动 analysis 为空或不可读取",
                )
            assets = AutoPipelineAssets(
                cache_dir=media_manifest.cache_dir,
                manifest_path=media_manifest.manifest_path,
                analysis_path=manual_path,
                report_path=media_manifest.report_path,
                manifest_rows=media_manifest.manifest_rows,
                analysis_rows=analysis_rows,
                reused_analysis_count=len(analysis_rows),
                generated_analysis_count=0,
                cache_fingerprint=fingerprint,
            )
        elif not analysis_manifest_rows:
            write_jsonl(cache_analysis, seeded_analysis)
            assets = AutoPipelineAssets(
                cache_dir=media_manifest.cache_dir,
                manifest_path=media_manifest.manifest_path,
                analysis_path=cache_analysis,
                report_path=media_manifest.report_path,
                manifest_rows=media_manifest.manifest_rows,
                analysis_rows=seeded_analysis,
                reused_analysis_count=len(seeded_analysis),
                generated_analysis_count=0,
                cache_fingerprint=fingerprint,
            )
        else:
            model_cache_dir.mkdir(parents=True, exist_ok=True)
            model_manifest_path = model_cache_dir / "image_manifest.jsonl"
            write_jsonl(model_manifest_path, analysis_manifest_rows)
            cache_manifest = replace(
                media_manifest,
                cache_dir=model_cache_dir,
                manifest_path=model_manifest_path,
                manifest_rows=analysis_manifest_rows,
            )
            model_assets = analyze_auto_pipeline_media_manifest(
                cache_manifest,
                api_key=api_key,
                minimax_command=minimax_command,
                timeout_seconds=timeout_seconds,
                workers=workers,
                model=vision_model,
                seed_analysis_rows=seeded_analysis,
                vision_provider=vision_provider,
            )
            combined_analysis = _merge_analysis_rows(
                seeded_analysis,
                model_assets.analysis_rows,
                allowed_ids=manifest_ids,
            )
            write_jsonl(cache_analysis, combined_analysis)
            assets = AutoPipelineAssets(
                cache_dir=media_manifest.cache_dir,
                manifest_path=media_manifest.manifest_path,
                analysis_path=cache_analysis,
                report_path=media_manifest.report_path,
                manifest_rows=media_manifest.manifest_rows,
                analysis_rows=combined_analysis,
                reused_analysis_count=max(
                    0, len(combined_analysis) - model_assets.generated_analysis_count
                ),
                generated_analysis_count=model_assets.generated_analysis_count,
                cache_fingerprint=fingerprint,
            )
        output_path = publish_file_atomic(
            assets.analysis_path,
            state.run_dir / "image_analysis.minimax.jsonl",
        )
    except AutoPipelineStageError:
        raise
    except (AutoPipelineError, VisionProviderError) as exc:
        warning = True
        analysis_rows = _merge_analysis_rows(
            _merge_analysis_rows(
                seeded_analysis,
                read_jsonl(cache_analysis),
                allowed_ids=manifest_ids,
            ),
            read_jsonl(model_cache_dir / "image_analysis.minimax.jsonl"),
            allowed_ids=manifest_ids,
        )
        write_jsonl(cache_analysis, analysis_rows)
        assets = AutoPipelineAssets(
            cache_dir=media_manifest.cache_dir,
            manifest_path=media_manifest.manifest_path,
            analysis_path=cache_analysis,
            report_path=media_manifest.report_path,
            manifest_rows=media_manifest.manifest_rows,
            analysis_rows=analysis_rows,
            reused_analysis_count=min(initial_analysis_count, len(analysis_rows)),
            generated_analysis_count=max(0, len(analysis_rows) - initial_analysis_count),
            cache_fingerprint=fingerprint,
        )
        output_path = publish_file_atomic(
            assets.analysis_path,
            state.run_dir / "image_analysis.minimax.jsonl",
        )
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "visual_analysis_io_failed",
            f"视觉分析 IO 失败: {exc}",
            retryable=True,
        )
    except Exception as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.EXECUTION,
            "visual_analysis_failed",
            f"视觉分析执行失败: {exc}",
            retryable=True,
        )
    stage_status = (
        StageStatus.SUCCEEDED_WITH_WARNINGS
        if warning or len(assets.analysis_rows) < len(assets.manifest_rows)
        else StageStatus.SUCCEEDED
    )
    finish_pipeline_stage(
        state,
        stage_id,
        stage_status,
        output_sha256=sha256_file(output_path),
        output_refs=(str(output_path.resolve()),),
    )
    return replace(
        assets,
        manifest_path=media_manifest.manifest_path,
        analysis_path=output_path,
    )


def _merge_manifest_rows(primary: list[dict], packaged: list[dict]) -> list[dict]:
    def key(row: dict) -> str:
        raw_path = str(row.get("原图路径") or "").strip()
        if raw_path:
            return f"path:{Path(raw_path).expanduser().resolve()}"
        image_id = str(row.get("image_id") or "").strip()
        if image_id:
            return f"id:{image_id}"
        return f"row:{sha256_json(row)}"

    by_key = {key(row): dict(row) for row in primary}
    for row in packaged:
        by_key[key(row)] = dict(row)
    return sorted(
        by_key.values(),
        key=lambda row: (
            str(row.get("项目名") or ""),
            str(row.get("原图路径") or ""),
            str(row.get("image_id") or ""),
        ),
    )


def _merge_analysis_rows(
    primary: list[dict],
    packaged: list[dict],
    *,
    allowed_ids: set[str],
) -> list[dict]:
    by_id = {
        str(row.get("image_id") or ""): dict(row)
        for row in primary
        if str(row.get("image_id") or "") in allowed_ids
    }
    for row in packaged:
        image_id = str(row.get("image_id") or "")
        if image_id in allowed_ids:
            by_id[image_id] = dict(row)
    return [by_id[image_id] for image_id in sorted(by_id)]


def _read_source_catalog(path: Path) -> SourceCatalog:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return SourceCatalog(
        path=path,
        rows=list(payload["records"]),
        template_count=int(payload["template_count"]),
        creative_count=int(payload["creative_count"]),
    )


def _directory_file_hashes(root: Path | None) -> list[dict]:
    if root is None:
        return []
    return [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file() and "_minimax_frames" not in path.parts
    ]


def _reused_output(state, record: dict) -> Path | None:
    if record.get("status") not in _SUCCESS_STATUSES:
        return None
    refs = record.get("output_refs") or []
    if not refs:
        raise RuntimeError(f"{record.get('stage_id')} 已成功但缺少 output_refs")
    return resolve_run_local_path(state.run_dir, refs[0])


def _abort(
    state,
    stage_id: str,
    status: StageStatus,
    category: StageErrorCategory,
    code: str,
    message: str,
    *,
    retryable: bool = False,
    exit_code: int = 1,
) -> None:
    error = StageError(
        category=category,
        code=code,
        message=message,
        retryable=retryable,
    )
    finish_pipeline_stage(state, stage_id, status, error=error)
    raise AutoPipelineStageError(
        stage_id,
        status.value,
        error,
        exit_code=exit_code,
    )
