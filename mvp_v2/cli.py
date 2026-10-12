from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path

from .ai_spec import AiSpecError
from .asset_package import AssetPackageError, load_asset_package
from .client_workbook import ClientWorkbookError, write_client_review_workbook
from .auto_pipeline import (
    AutoPipelineError,
    current_visual_cache_fingerprint,
    project_cache_dir,
    write_auto_pipeline_error_report,
    write_run_report,
)
from .excel_io import (
    BriefError,
    read_briefs,
    read_text_brief_file,
)
from .evolution import record_evolution_event
from .image_assets import read_jsonl
from .loop_runner import LoopIteration, LoopResult, run_generation_loop, run_generation_once
from .pipeline_runtime import (
    finalize_pipeline_run,
    fork_pipeline_run,
    migrate_legacy_release,
    open_pipeline_run,
    publish_file_atomic,
    publish_pipeline_run,
    publish_symlink_atomic,
    resolve_pipeline_run,
    resume_pipeline_run,
)
from .pipeline_stages import AutoPipelineStageError, run_initial_auto_pipeline_stages
from .review_publish_stages import (
    ReviewAttestationError,
    read_completed_quality_review_stage,
    run_human_review_stage,
    run_publish_feedback_stage,
)
from .review_package import ReviewPackageError, validate_review_workbook
from .revision import write_revision_constraints
from .run_contract import sha256_file, sha256_json
from .run_provenance import RunProvenanceError, run_provenance
from .runtime_config import configured_asset_package, load_local_secrets, load_runtime_config
from .run_state import write_json_atomic
from .stage_contract import STAGE_ORDER
from .visual_review import write_visual_review_html_from_workbook
from .source_brand_review import BrandReviewError, load_creative_asset_review, load_source_brand_review
from .user_feedback import (
    find_child_revision_constraints,
    load_revision_constraints,
    record_feedback_status,
)
from .script_type_adapter import category_id_for_script_type, normalize_script_type

_RUNTIME_CONFIG = load_runtime_config()
DEFAULT_SOURCE_ROOT = Path(_RUNTIME_CONFIG["source_root"])
DEFAULT_DATA_ROOT = Path(_RUNTIME_CONFIG["data_root"])
DEFAULT_ASSET_CACHE_ROOT = Path(_RUNTIME_CONFIG["runs_root"])
DEFAULT_OUTPUT_ROOT = Path(_RUNTIME_CONFIG["exports_root"])
DEFAULT_ASSET_PACKAGE = os.environ.get("MPLUS_ASSET_PACKAGE", "")


def main(argv: list[str] | None = None) -> int:
    load_local_secrets()
    parser = argparse.ArgumentParser(description="M+ 多类别脚本生成 MVP v2")
    parser.add_argument("--input", help="项目需求 Brief Excel")
    parser.add_argument("--brief-file", help="项目需求 Brief 文本文件")
    parser.add_argument("--output", required=True, help="输出脚本 Excel")
    parser.add_argument("--client-output", help="人工 quality_pass 后的客户前终审稿路径")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT), help="六类别脚本资料目录")
    parser.add_argument("--asset-package", default=DEFAULT_ASSET_PACKAGE, help="可插拔资产包目录或 asset_package.json；设置后不再依赖固定资料库地址")
    parser.add_argument(
        "--source-view",
        choices=("human", "ai-spec"),
        default="human",
        help="资料读取视图；默认human，ai-spec会先执行同步门禁",
    )
    parser.add_argument("--project-dir", help="覆盖 Brief 中的项目图片/视频目录")
    parser.add_argument("--audit-json", help="输出质量审计 JSON")
    parser.add_argument("--vision-analysis-json", help="外部/人工视觉分析 JSON，用于写回 caption、标签和配图评分")
    parser.add_argument("--image-manifest-jsonl", help="图片资产 manifest JSONL")
    parser.add_argument("--image-analysis-jsonl", help="图片视觉理解 analysis JSONL")
    parser.add_argument("--rematch-visuals", action="store_true", help="根据视觉分析从项目图片池自动替换低分配图")
    parser.add_argument("--visual-review-html", help="输出高清配图复核 HTML；不传值则不生成")
    parser.add_argument("--loop", action="store_true", help="开启自动质量循环")
    parser.add_argument("--max-loops", type=int, default=5, help="最大循环次数")
    parser.add_argument("--auto-pipeline", action="store_true", help="自动完成图片资产、MiniMax 视觉理解、重配、loop 和复核页")
    parser.add_argument("--asset-cache-root", default=str(DEFAULT_ASSET_CACHE_ROOT), help="自动管线资产缓存根目录")
    parser.add_argument("--minimax-command", default="mmx", help="MiniMax 视觉理解命令")
    parser.add_argument("--minimax-timeout-seconds", type=int, default=90, help="单张图片视觉理解超时")
    parser.add_argument("--minimax-workers", type=int, default=4, help="自动管线 MiniMax 并发数")
    parser.add_argument("--vision-model", default="", help="期望的视觉模型；当前 mmx 仍会记录实际 provider-managed 模型")
    parser.add_argument(
        "--quality-profile",
        choices=("draft", "client-review"),
        default="draft",
        help="文本质量模式；client-review 要求两遍受控创作",
    )
    parser.add_argument(
        "--text-model",
        default="MiniMax-M2.7",
        help="ScriptDraft 文本模型",
    )
    parser.add_argument("--writer-provider", default="minimax-cli", choices=("minimax-cli", "minimax-http", "deepseek-http"))
    parser.add_argument("--writer-model", default="")
    parser.add_argument("--editor-provider", default="minimax-cli", choices=("minimax-cli", "minimax-http", "deepseek-http"))
    parser.add_argument("--editor-model", default="")
    parser.add_argument("--revision-constraints", help="界面反馈生成的 revision constraints JSON")
    parser.add_argument("--parent-run-id", default="", help="新版本的父 run ID")
    parser.add_argument("--source-brand-review", help="已完成人工确认的创意库来源品牌审核表")
    parser.add_argument("--resume", metavar="RUN_ID", help="恢复指定 auto pipeline run")
    parser.add_argument(
        "--review-workbook",
        help="导入由指定 waiting run 导出并已填写的人工复核副本",
    )
    parser.add_argument(
        "--review-attestation",
        help="导入绑定当前 waiting run 的独立 AI 质量复核 JSON 凭证",
    )
    recovery_group = parser.add_mutually_exclusive_group()
    recovery_group.add_argument(
        "--from-stage",
        choices=tuple(stage.value for stage in STAGE_ORDER),
        help="失效指定节点及下游后恢复",
    )
    recovery_group.add_argument(
        "--retry-stage",
        choices=tuple(stage.value for stage in STAGE_ORDER),
        help="只重试指定节点",
    )
    args = parser.parse_args(argv)
    if (args.resume or args.from_stage or args.retry_stage) and not args.auto_pipeline:
        parser.error("--resume/--from-stage/--retry-stage 仅支持 --auto-pipeline")
    if args.retry_stage and not args.resume:
        parser.error("--retry-stage 必须与 --resume RUN_ID 一起使用")
    if args.from_stage and not (args.resume or args.parent_run_id):
        parser.error("--from-stage 必须与 --resume RUN_ID 或 --parent-run-id 一起使用")
    if args.resume and args.parent_run_id:
        parser.error("--resume 与 --parent-run-id 不能同时使用")
    if args.review_workbook and not args.resume:
        parser.error("--review-workbook 必须与 --resume RUN_ID 一起使用")
    if args.review_attestation and not args.resume:
        parser.error("--review-attestation 必须与 --resume RUN_ID 一起使用")
    if args.review_workbook and args.review_attestation:
        parser.error("--review-workbook 与 --review-attestation 不能同时使用")
    if args.quality_profile == "client-review" and not args.auto_pipeline:
        parser.error("client-review 仅支持 --auto-pipeline 节点运行")

    try:
        if args.brief_file:
            briefs = [read_text_brief_file(args.brief_file)]
        elif args.input:
            briefs = read_briefs(args.input)
        else:
            parser.error("必须提供 --brief-file 或 --input")
    except (BriefError, OSError) as exc:
        print(f"Brief 读取失败: {exc}")
        return 1
    script_types = {
        normalize_script_type(brief.get("脚本类型") or "发布会")
        for brief in briefs
    }
    if (
        not args.asset_package
        and Path(args.source_root).expanduser() == DEFAULT_SOURCE_ROOT
        and len(script_types) == 1
    ):
        args.asset_package = configured_asset_package(next(iter(script_types)), _RUNTIME_CONFIG)
    if args.asset_package:
        try:
            package = load_asset_package(args.asset_package)
            expected_category_ids = {
                category_id_for_script_type(brief.get("脚本类型") or "发布会")
                for brief in briefs
            }
            if expected_category_ids != {package.get("category_id")}:
                raise AssetPackageError(
                    f"资产包类别 {package.get('category_id')} 与 Brief 类别 "
                    f"{', '.join(sorted(expected_category_ids))} 不一致"
                )
            args.asset_package = str(Path(package["package_root"]).resolve())
            args.source_root = str(package["source_root_path"])
        except (AssetPackageError, OSError, json.JSONDecodeError) as exc:
            print(f"资产包读取失败: {exc}")
            return 1
    if args.source_brand_review:
        try:
            load_source_brand_review(args.source_brand_review)
            load_creative_asset_review(args.source_brand_review)
        except (BrandReviewError, OSError) as exc:
            print(f"创意库品牌审核表读取失败: {exc}")
            return 1
    if args.project_dir:
        project_dir = Path(args.project_dir).expanduser()
        if not project_dir.is_dir():
            print(f"项目素材目录不存在: {project_dir}")
            return 1
        for brief in briefs:
            brief["素材目录或素材文件"] = str(project_dir)
    vision_analysis = _read_json(args.vision_analysis_json) if args.vision_analysis_json else None
    image_manifest_rows = read_jsonl(args.image_manifest_jsonl) if args.image_manifest_jsonl else None
    image_analysis_rows = read_jsonl(args.image_analysis_jsonl) if args.image_analysis_jsonl else None
    auto_assets = None
    review_html_path = args.visual_review_html
    if args.auto_pipeline:
        if len(briefs) != 1:
            print("自动管线一次只接受一个 Brief")
            return 1
        if bool(args.image_manifest_jsonl) != bool(args.image_analysis_jsonl):
            print("自动管线手动覆盖时必须同时提供 --image-manifest-jsonl 和 --image-analysis-jsonl")
            return 1
        try:
            return _run_auto_pipeline(
                args,
                briefs,
                vision_analysis,
                review_html_path or _default_review_html_path(args.output),
            )
        except (AiSpecError, RunProvenanceError) as exc:
            print(f"运行锁定或 AI_规范表读取失败: {exc}")
            return 2
    if args.loop:
        try:
            result = run_generation_loop(
                briefs,
                args.source_root,
                args.output,
                max_loops=args.max_loops,
                audit_json_path=args.audit_json,
                vision_analysis=vision_analysis,
                rematch_visuals=args.rematch_visuals,
                image_manifest_rows=image_manifest_rows,
                image_analysis_rows=image_analysis_rows,
                source_brand_review_path=args.source_brand_review,
                source_view=args.source_view,
            )
        except AiSpecError as exc:
            print(f"AI_规范表读取失败: {exc}")
            return 2
        if result.exit_code == 0:
            final_audit = result.iterations[-1].audit
            review_html = _write_review_html_if_requested(review_html_path, result.output_path)
            print(
                f"质量审计通过: loop={result.passed_iteration}, "
                f"warning={final_audit['warning_count']}, 输出={result.output_path}, 审计={result.audit_path}"
            )
            if review_html:
                print(f"高清配图复核: {review_html}")
        else:
            final_audit = _read_json(str(result.best_failed_audit_path)) if result.best_failed_audit_path else {}
            review_html = _write_review_html_if_requested(review_html_path, result.best_failed_output_path)
            print(
                f"质量审计失败: loops={len(result.iterations)}, "
                f"hard_fail={final_audit.get('hard_fail_count', 0)}, "
                f"best={result.best_failed_output_path}, 审计={result.best_failed_audit_path}"
            )
            if review_html:
                print(f"高清配图复核: {review_html}")
        if auto_assets:
            workbook_path = result.output_path if result.exit_code == 0 else result.best_failed_output_path
            report = write_run_report(
                auto_assets,
                briefs[0],
                result,
                workbook_path,
                review_html,
            )
            print(
                f"运行报告: {auto_assets.report_path}, "
                f"项目素材分析覆盖={_project_media_coverage_text(report)}"
            )
            print(
                f"发布门: machine={report['machine_audit_status']}, "
                f"human={report['human_review_status']}, "
                f"decision={report['release_decision']}"
            )
            return report["exit_code"]
        return result.exit_code

    try:
        iteration = run_generation_once(
            briefs,
            args.source_root,
            args.output,
            audit_json_path=args.audit_json,
            vision_analysis=vision_analysis,
            rematch_visuals=args.rematch_visuals,
            image_manifest_rows=image_manifest_rows,
            image_analysis_rows=image_analysis_rows,
            source_brand_review_path=args.source_brand_review,
            source_view=args.source_view,
        )
    except AiSpecError as exc:
        print(f"AI_规范表读取失败: {exc}")
        return 2
    print(f"写出: {args.output}")
    print(f"审计: {iteration.audit_path}")
    review_html = _write_review_html_if_requested(review_html_path, iteration.output_path)
    if review_html:
        print(f"高清配图复核: {review_html}")
    if iteration.audit["status"] == "fail":
        print(f"质量审计失败: hard_fail={iteration.audit['hard_fail_count']}")
        return 2
    print(f"质量审计通过: warning={iteration.audit['warning_count']}")
    return 0


def _run_auto_pipeline(args, briefs: list[dict], vision_analysis, review_html_path: str) -> int:
    brief = briefs[0]
    provenance = (
        run_provenance(
            input_path=args.brief_file or args.input,
            asset_package=args.asset_package,
        )
        if args.asset_package
        else None
    )
    cache_dir = project_cache_dir(brief, args.asset_cache_root)
    if args.resume and not args.revision_constraints:
        recovered_constraints = find_child_revision_constraints(cache_dir, args.resume)
        if recovered_constraints is not None:
            args.revision_constraints = str(recovered_constraints)
    revision_constraints = load_revision_constraints(args.revision_constraints)
    fingerprint = current_visual_cache_fingerprint(
        args.minimax_command
        if not args.vision_model
        else f"{args.minimax_command}:model={args.vision_model}"
    )
    if args.resume:
        runtime_context = resume_pipeline_run(
            args.resume,
            cache_dir,
            from_stage=args.from_stage,
            retry_stage=args.retry_stage,
        )
    elif args.parent_run_id and args.from_stage:
        runtime_context = fork_pipeline_run(
            args.parent_run_id,
            cache_dir,
            brief=brief,
            source_root=args.source_root,
            cache_fingerprint=fingerprint,
            from_stage=args.from_stage,
            provenance=provenance,
        )
    else:
        runtime_context = open_pipeline_run(
            brief=brief,
            cache_dir=cache_dir,
            source_root=args.source_root,
            cache_fingerprint=fingerprint,
            category_id=category_id_for_script_type(brief.get("脚本类型", "")),
            parent_run_id=args.parent_run_id or None,
            provenance=provenance,
        )
    try:
        with runtime_context as runtime:
            if _is_review_only_resume(args):
                return _run_review_only_resume(
                    runtime,
                    args=args,
                    cache_dir=cache_dir,
                    review_html_path=review_html_path,
                )
            try:
                auto_assets = run_initial_auto_pipeline_stages(
                    runtime,
                    brief=brief,
                    config={
                        "source_root": str(Path(args.source_root).resolve()),
                        "source_view": args.source_view,
                        "source_brand_review": args.source_brand_review or "",
                        "manual_manifest": args.image_manifest_jsonl or "",
                        "manual_analysis": args.image_analysis_jsonl or "",
                        "max_loops": args.max_loops,
                        "minimax_command_sha256": sha256_json(args.minimax_command),
                        "minimax_timeout_seconds": args.minimax_timeout_seconds,
                        "minimax_workers": args.minimax_workers,
                        "quality_profile": args.quality_profile,
                        "text_model": args.text_model,
                        "writer_provider": args.writer_provider,
                        "writer_model": args.writer_model or args.text_model,
                        "editor_provider": args.editor_provider,
                        "editor_model": args.editor_model or args.text_model,
                        "vision_model": args.vision_model,
                        "asset_package": args.asset_package or "",
                    },
                    source_root=args.source_root,
                    cache_root=args.asset_cache_root,
                    source_view=args.source_view,
                    api_key=os.environ.get("MINIMAX_API_KEY", ""),
                    minimax_command=args.minimax_command,
                    timeout_seconds=args.minimax_timeout_seconds,
                    workers=args.minimax_workers,
                    vision_model=args.vision_model,
                    source_brand_review_path=args.source_brand_review,
                    brief_ref=args.brief_file or args.input,
                    manual_manifest_path=args.image_manifest_jsonl,
                    manual_analysis_path=args.image_analysis_jsonl,
                    asset_package=args.asset_package or None,
                    supplemental_image_paths=_supplemental_image_paths(revision_constraints),
                )
            except AutoPipelineStageError as exc:
                report_path = write_auto_pipeline_error_report(
                    brief,
                    args.asset_cache_root,
                    exc,
                    runtime_state=runtime,
                )
                finalize_pipeline_run(
                    runtime,
                    status=exc.status,
                    artifacts={"run_report": report_path},
                    metrics={"exit_code": exc.exit_code},
                )
                print(f"自动管线初始化失败: {exc}")
                print(f"运行报告: {report_path}")
                return exc.exit_code
            except AutoPipelineError as exc:
                report_path = write_auto_pipeline_error_report(
                    brief,
                    args.asset_cache_root,
                    exc,
                    runtime_state=runtime,
                )
                finalize_pipeline_run(
                    runtime,
                    status="failed",
                    artifacts={"run_report": report_path},
                    metrics={"exit_code": 1},
                )
                print(f"自动管线初始化失败: {exc}")
                print(f"运行报告: {report_path}")
                return 1

            run_output = runtime.run_dir / Path(args.output).name
            run_audit = (
                runtime.run_dir / Path(args.audit_json).name
                if args.audit_json else None
            )
            result = _load_resumable_generation_result(runtime)
            if result is None:
                try:
                    result = run_generation_loop(
                        briefs,
                        args.source_root,
                        run_output,
                        max_loops=args.max_loops,
                        audit_json_path=run_audit,
                        vision_analysis=vision_analysis,
                        rematch_visuals=True,
                        image_manifest_rows=auto_assets.manifest_rows,
                        image_analysis_rows=auto_assets.analysis_rows,
                        source_brand_review_path=args.source_brand_review,
                        source_view=args.source_view,
                        quality_profile=args.quality_profile,
                        text_model=args.text_model,
                        text_command=args.minimax_command,
                        writer_provider=args.writer_provider,
                        writer_model=args.writer_model or args.text_model,
                        editor_provider=args.editor_provider,
                        editor_model=args.editor_model or args.text_model,
                        initial_constraints=revision_constraints,
                        runtime=runtime,
                    )
                except AutoPipelineStageError as exc:
                    report_path = write_auto_pipeline_error_report(
                        brief,
                        args.asset_cache_root,
                        exc,
                        runtime_state=runtime,
                    )
                    finalize_pipeline_run(
                        runtime,
                        status=exc.status,
                        artifacts={"run_report": report_path},
                        metrics={"exit_code": exc.exit_code},
                    )
                    print(f"自动管线生成失败: {exc}")
                    print(f"运行报告: {report_path}")
                    return exc.exit_code
            run_workbook = result.output_path if result.exit_code == 0 else result.best_failed_output_path
            run_review_path = runtime.run_dir / Path(review_html_path).name
            run_review = _write_review_html_if_requested(str(run_review_path), run_workbook)
            published_result, workbook_path, published_review = _stage_auto_artifacts(
                runtime,
                args,
                result,
                run_review,
                review_html_path,
            )
            run_assets = replace(auto_assets, report_path=runtime.run_dir / "run_report.json")
            report = write_run_report(
                run_assets,
                brief,
                published_result,
                workbook_path,
                published_review,
                runtime_state=runtime,
            )
            record_evolution_event(
                cache_dir,
                run_id=runtime.run_id,
                event="ai_output_created",
                details={
                    "parent_run_id": args.parent_run_id or "",
                    "shot_count": report.get("shot_count"),
                    "machine_audit_status": report.get("machine_audit_status"),
                    "output_sha256": sha256_file(workbook_path) if workbook_path and Path(workbook_path).is_file() else "",
                    "audit_sha256": sha256_file(report["audit_path"]) if report.get("audit_path") and Path(report["audit_path"]).is_file() else "",
                    "revision_constraints_path": args.revision_constraints or "",
                },
            )
            artifacts = {
                "workbook": workbook_path,
                "audit": (
                    published_result.audit_path
                    if published_result.exit_code == 0
                    else published_result.best_failed_audit_path
                ),
                "loop_log": published_result.log_path,
                "visual_review": published_review,
                "run_report": run_assets.report_path,
            }
            if report["machine_audit_status"] != "pass":
                finalize_pipeline_run(
                    runtime,
                    status="blocked",
                    artifacts=artifacts,
                    visual_snapshots={
                        "image_manifest": run_assets.manifest_path,
                        "image_analysis": run_assets.analysis_path,
                    },
                    metrics=_release_metrics(report),
                )
                completed_result = published_result
                completed_report = report
                completed_review = published_review
                continue_pipeline = False
            else:
                continue_pipeline = True

            if continue_pipeline:
                review_exit = _run_quality_review_gate(
                    runtime,
                    args=args,
                    cache_dir=cache_dir,
                    report=report,
                    report_path=run_assets.report_path,
                    artifacts=artifacts,
                    workbook_path=workbook_path,
                    published_review=published_review,
                    review_html_path=review_html_path,
                    visual_snapshots={
                        "image_manifest": run_assets.manifest_path,
                        "image_analysis": run_assets.analysis_path,
                    },
                )
                if review_exit is not None:
                    return review_exit
            completed_result = published_result
            completed_report = report
            completed_review = published_review
    except AiSpecError as exc:
        print(f"AI_规范表读取失败: {exc}")
        return 2
    except RuntimeError as exc:
        print(f"自动管线运行失败: {exc}")
        return 1
    _print_auto_pipeline_result(completed_result, completed_report, completed_review)
    return completed_report["exit_code"]


def _is_review_only_resume(args) -> bool:
    return bool(
        args.resume
        and (args.review_workbook or args.review_attestation)
        and not args.from_stage
        and args.retry_stage in {None, "human_review", "publish_feedback"}
    )


def _run_review_only_resume(runtime, *, args, cache_dir: Path, review_html_path: str) -> int:
    allowed_statuses = (
        {"running"}
        if args.retry_stage in {"human_review", "publish_feedback"}
        else {"waiting_human_review"}
    )
    artifacts = resolve_pipeline_run(
        cache_dir,
        runtime.run_id,
        allowed_statuses=allowed_statuses,
        artifact_names={"workbook", "audit", "loop_log", "visual_review", "run_report"},
    )
    result = _load_resumable_generation_result(runtime)
    if result is None:
        raise RuntimeError("waiting run 缺少可复用的机器审核产物")
    report_path = artifacts["run_report"]
    report = _read_json(str(report_path))
    review_exit = _run_quality_review_gate(
        runtime,
        args=args,
        cache_dir=cache_dir,
        report=report,
        report_path=report_path,
        artifacts=artifacts,
        workbook_path=artifacts["workbook"],
        published_review=artifacts["visual_review"],
        review_html_path=review_html_path,
        visual_snapshots={
            "image_manifest": Path(report["manifest_path"]),
            "image_analysis": Path(report["analysis_path"]),
        },
    )
    if review_exit is not None:
        return review_exit
    _print_auto_pipeline_result(result, report, artifacts["visual_review"])
    return int(report["exit_code"])


def _run_quality_review_gate(
    runtime,
    *,
    args,
    cache_dir: Path,
    report: dict,
    report_path: Path,
    artifacts: dict,
    workbook_path: Path,
    published_review: Path | None,
    review_html_path: str,
    visual_snapshots: dict,
) -> int | None:
    human_workbook = workbook_path
    if args.review_workbook:
        if args.retry_stage != "publish_feedback":
            try:
                validate_review_workbook(
                    cache_dir,
                    args.review_workbook,
                    run_id=runtime.run_id,
                )
            except ReviewPackageError as exc:
                print(f"人工复核副本校验失败: {exc}")
                return 1
        human_workbook = Path(args.review_workbook).resolve()
    elif args.resume and not args.from_stage and not args.retry_stage:
        resolve_pipeline_run(
            cache_dir,
            runtime.run_id,
            allowed_statuses={"waiting_human_review"},
            artifact_names={"workbook", "audit"},
        )
    try:
        if args.retry_stage == "publish_feedback":
            human = read_completed_quality_review_stage(
                runtime,
                machine_audit_path=artifacts["audit"],
                workbook_path=human_workbook,
                review_html_path=published_review,
            )
        else:
            human = run_human_review_stage(
                runtime,
                machine_audit_path=artifacts["audit"],
                workbook_path=human_workbook,
                review_html_path=published_review,
                review_attestation_path=args.review_attestation,
            )
    except ReviewAttestationError as exc:
        print(f"独立 AI 复核凭证校验失败: {exc}")
        return 1
    except RuntimeError as exc:
        if args.retry_stage != "publish_feedback":
            raise
        print(f"发布节点重试失败: {exc}")
        return 1
    _apply_human_review_to_report(report, human)
    if args.retry_stage != "publish_feedback":
        record_evolution_event(
            cache_dir,
            run_id=runtime.run_id,
            event=(
                "independent_ai_review_evaluated"
                if human.reviewer_kind == "independent_ai"
                else "human_review_evaluated"
            ),
            details={
                "human_review_status": human.human_review_status,
                "quality_review_status": human.quality_review_status,
                "reviewer_kind": human.reviewer_kind,
                "review_workbook_sha256": human.report.get("review_workbook_sha256", ""),
                "review_attestation_sha256": human.report.get("review_attestation_sha256", ""),
                "summary": human.report.get("summary") or {},
                "contract_errors": human.report.get("contract_errors") or [],
            },
        )
    write_json_atomic(report_path, report)
    if human.stage_status != "succeeded":
        if args.review_workbook and human.human_review_status not in {"pending", "invalid"}:
            revision_path = write_revision_constraints(
                human_workbook,
                runtime.run_dir / "revision_constraints.json",
                parent_run_id=runtime.run_id,
            )
            report["revision_constraints_path"] = str(revision_path.resolve())
            write_json_atomic(report_path, report)
        finalize_pipeline_run(
            runtime,
            status=human.stage_status,
            artifacts=artifacts,
            visual_snapshots=visual_snapshots,
            metrics=_release_metrics(report),
        )
        return None

    if args.quality_profile == "client-review":
        reviewed_copy = publish_file_atomic(
            human_workbook,
            runtime.run_dir / "reviewed_workbook.xlsx",
        )
        client_name = Path(
            args.client_output
            or _default_client_output_path(args.output)
        ).name
        client_workbook = runtime.run_dir / client_name
        try:
            write_client_review_workbook(
                workbook_path,
                client_workbook,
                _pending_confirmations(runtime),
            )
        except ClientWorkbookError as exc:
            print(f"客户前终审稿生成失败: {exc}")
            return 1
        artifacts.update({
            "review_workbook": reviewed_copy,
            "human_review_report": human.report_path,
            "client_workbook": client_workbook,
        })
        report["client_workbook_path"] = str(client_workbook.resolve())
        write_json_atomic(report_path, report)

    def publish_feedback(_feedback_path, completion):
        _prepare_public_release_links(
            cache_dir,
            args,
            review_html_path,
            artifacts,
        )
        publish_pipeline_run(
            runtime,
            status="succeeded",
            artifacts=artifacts,
            visual_snapshots=visual_snapshots,
            metrics=_release_metrics(report),
            final_stage=completion,
        )

    try:
        run_publish_feedback_stage(
            runtime,
            machine_audit_path=artifacts["audit"],
            human_review_report_path=human.report_path,
            release_artifacts=artifacts,
            publisher=publish_feedback,
        )
        record_feedback_status(
            args.revision_constraints,
            "resolved",
            child_run_id=runtime.run_id,
            resolution_note=(
                "机器审计与经授权的独立 AI 质量复核通过，已原子发布"
                if human.reviewer_kind == "independent_ai"
                else "机器审计与人工质量审核通过，已原子发布"
            ),
        )
    except AutoPipelineStageError as exc:
        report.update({
            "status": "failed",
            "release_decision": "blocked",
            "exit_code": exc.exit_code,
            "error": exc.error.to_dict(),
        })
        write_json_atomic(report_path, report)
        finalize_pipeline_run(
            runtime,
            status="failed",
            artifacts=artifacts,
            visual_snapshots=visual_snapshots,
            metrics=_release_metrics(report),
        )
        print(f"自动管线发布失败: {exc}")
        print(f"运行报告: {report_path}")
        return exc.exit_code
    return None


def _load_resumable_generation_result(runtime) -> LoopResult | None:
    manifest = _read_json(str(runtime.manifest_path))
    machine = manifest.get("stages", {}).get("machine_audit", {})
    if machine.get("status") not in {"succeeded", "succeeded_with_warnings"}:
        return None
    report_path = runtime.run_dir / "run_report.json"
    if not report_path.is_file():
        parent_run_id = str(manifest.get("parent_run_id") or "").strip()
        if parent_run_id and Path(parent_run_id).name == parent_run_id:
            report_path = runtime.run_dir.parent / parent_run_id / "run_report.json"
    if not report_path.is_file():
        return None
    report = _read_json(str(report_path))
    output = Path(report.get("output_path") or "")
    audit_path = Path(report.get("audit_path") or "")
    if not output.is_file() or not audit_path.is_file():
        return None
    audit = _read_json(str(audit_path))
    loop_log_record = manifest.get("artifacts", {}).get("loop_log", {})
    loop_log = Path(loop_log_record.get("path") or str(output) + ".loop.json")
    return LoopResult(
        exit_code=int(report.get("generation_exit_code") or 0),
        output_path=output,
        audit_path=audit_path,
        log_path=loop_log,
        iterations=[LoopIteration(1, output, audit_path, audit)],
        passed_iteration=report.get("passed_iteration") or 1,
    )


def _apply_human_review_to_report(report: dict, human) -> None:
    report["human_review_status"] = human.human_review_status
    report["quality_review_status"] = human.quality_review_status
    report["reviewer_kind"] = human.reviewer_kind
    report["human_review_report_path"] = str(human.report_path)
    report["quality_review_report_path"] = str(human.report_path)
    report["pending_human_review_count"] = sum(
        int(project.get("pending_shot_count") or 0)
        for project in human.report.get("projects", [])
    )
    if human.stage_status == "succeeded":
        report.pop("error", None)
        report.update({
            "release_decision": "release_pass",
            "status": "release_pass",
            "exit_code": 0,
        })
    else:
        report.update({
            "release_decision": "blocked",
            "status": human.stage_status,
            "exit_code": 3,
        })


def _release_metrics(report: dict) -> dict:
    return {
        "machine_audit_status": report["machine_audit_status"],
        "machine_audit_stage_status": report.get("machine_audit_stage_status", ""),
        "media_readiness_status": report.get("media_readiness_status", "not_assessed"),
        "human_review_status": report["human_review_status"],
        "quality_review_status": report.get("quality_review_status", report["human_review_status"]),
        "reviewer_kind": report.get("reviewer_kind", "human"),
        "exit_code": report["exit_code"],
    }


def _stage_auto_artifacts(runtime, args, result, run_review, review_html_path: str):
    output = runtime.run_dir / Path(args.output).name
    audit = runtime.run_dir / (
        Path(args.audit_json).name if args.audit_json else Path(str(output) + ".audit.json").name
    )
    loop_log = runtime.run_dir / Path(str(output) + ".loop.json").name
    published_review = _publish_if_present(run_review, runtime.run_dir / Path(review_html_path).name)
    published_log = _publish_if_present(result.log_path, loop_log)
    if result.exit_code == 0:
        workbook = _publish_if_present(result.output_path, output)
        published_audit = _publish_if_present(result.audit_path, audit)
        published_result = replace(
            result,
            output_path=workbook,
            audit_path=published_audit,
            log_path=published_log,
        )
        return published_result, workbook, published_review

    best_output = runtime.run_dir / f"{Path(args.output).stem}.best_failed{Path(args.output).suffix}"
    best_audit = runtime.run_dir / f"{Path(args.output).stem}.best_failed.audit.json"
    workbook = _publish_if_present(result.best_failed_output_path, best_output)
    published_audit = _publish_if_present(result.best_failed_audit_path, best_audit)
    published_result = replace(
        result,
        log_path=published_log,
        best_failed_output_path=workbook,
        best_failed_audit_path=published_audit,
    )
    return published_result, workbook, published_review


def _publish_if_present(source: str | Path | None, target: Path) -> Path | None:
    if not source or not Path(source).exists():
        return None
    if Path(source).resolve() == target.resolve():
        return target
    return publish_file_atomic(source, target)


def _prepare_public_release_links(cache_dir: Path, args, review_html_path: str, artifacts: dict) -> None:
    output = Path(args.output)
    public_paths = {
        "workbook": output,
        "audit": Path(args.audit_json) if args.audit_json else Path(str(output) + ".audit.json"),
        "loop_log": Path(str(output) + ".loop.json"),
        "visual_review": Path(review_html_path),
        "run_report": cache_dir / "run_report.json",
    }
    base_artifacts = {name: artifacts[name] for name in public_paths}
    migrated = migrate_legacy_release(cache_dir, public_paths, base_artifacts)
    link_artifacts = migrated or base_artifacts
    for name, public_path in public_paths.items():
        target = Path(os.path.abspath(link_artifacts[name]))
        if public_path.is_symlink() and public_path.readlink() == target:
            continue
        publish_symlink_atomic(target, public_path)
    if "client_workbook" in artifacts:
        client_public = Path(
            args.client_output
            or _default_client_output_path(args.output)
        )
        target = Path(os.path.abspath(artifacts["client_workbook"]))
        publish_symlink_atomic(target, client_public)


def _print_auto_pipeline_result(result, report: dict, review_html: Path | None) -> None:
    if result.exit_code == 0:
        audit = result.iterations[-1].audit
        print(
            f"质量审计通过: loop={result.passed_iteration}, "
            f"warning={audit['warning_count']}, 输出={result.output_path}, 审计={result.audit_path}"
        )
    else:
        audit = _read_json(str(result.best_failed_audit_path)) if result.best_failed_audit_path else {}
        print(
            f"质量审计失败: loops={len(result.iterations)}, "
            f"hard_fail={audit.get('hard_fail_count', 0)}, "
            f"best={result.best_failed_output_path}, 审计={result.best_failed_audit_path}"
        )
    if review_html:
        print(f"高清配图复核: {review_html}")
    print(
        f"运行报告: {report['run_report_path']}, "
        f"项目素材分析覆盖={_project_media_coverage_text(report)}, "
        f"脚本参考图={report.get('visual_asset_count', 0)}"
    )
    print(
        f"发布门: machine={report['machine_audit_status']}, "
        f"human={report['human_review_status']}, decision={report['release_decision']}"
    )


def _read_json(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _project_media_coverage_text(report: dict) -> str:
    if int(report.get("project_media_manifest_count") or 0) == 0:
        return "未提供（不阻断脚本审核）"
    return f"{float(report.get('project_media_analysis_coverage') or 0):.0%}"


def _supplemental_image_paths(constraints: dict) -> list[str]:
    return list(dict.fromkeys(
        str(item.get("supplemental_image_path") or "").strip()
        for item in constraints.get("user_feedback", [])
        if str(item.get("supplemental_image_path") or "").strip()
    ))


def _write_review_html_if_requested(path: str | None, workbook_path: str | Path | None) -> Path | None:
    if not path or not workbook_path:
        return None
    return write_visual_review_html_from_workbook(workbook_path, path)


def _default_client_output_path(output: str | Path) -> str:
    path = Path(output)
    return str(path.with_name(f"{path.stem}-客户前终审{path.suffix}"))


def _pending_confirmations(runtime) -> list[dict]:
    path = runtime.run_dir / "visual_binding.json"
    if not path.is_file():
        return []
    try:
        return list(_read_json(str(path)).get("pending_confirmations") or [])
    except (OSError, json.JSONDecodeError):
        return []


def _default_review_html_path(output: str | Path) -> str:
    path = Path(output)
    return str(path.with_suffix(".visual_review.html"))


if __name__ == "__main__":
    raise SystemExit(main())
