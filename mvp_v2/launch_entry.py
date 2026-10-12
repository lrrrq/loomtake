from __future__ import annotations

import argparse
import re
from pathlib import Path

from .cli import (
    DEFAULT_ASSET_CACHE_ROOT,
    DEFAULT_ASSET_PACKAGE,
    DEFAULT_OUTPUT_ROOT,
    DEFAULT_SOURCE_ROOT,
    main as pipeline_main,
)
from .preflight import run_launch_preflight
from .runtime_config import configured_asset_package, load_local_secrets
from .stage_contract import STAGE_ORDER
from .script_type_adapter import normalize_script_type


def _slug(value: str) -> str:
    text = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", str(value or "发布会项目"), flags=re.UNICODE)
    return text.strip("-.") or "发布会项目"


def _brief_summary(brief_path: str | None, brief_file: str | None) -> tuple[str, str]:
    if brief_path:
        from .excel_io import read_briefs

        rows = read_briefs(brief_path)
    else:
        from .excel_io import read_text_brief_file

        rows = [read_text_brief_file(brief_file)]
    brief = rows[0]
    script_type = normalize_script_type(brief.get("脚本类型", ""))
    return str(brief.get("项目名称") or f"{script_type}项目"), script_type


def main(argv: list[str] | None = None) -> int:
    load_local_secrets()
    parser = argparse.ArgumentParser(description="M+ 脚本用户入口：先检查，再运行自动管线")
    brief_group = parser.add_mutually_exclusive_group(required=True)
    brief_group.add_argument("--brief", help="项目需求 Brief Excel")
    brief_group.add_argument("--brief-file", help="项目需求 Brief 文本文件")
    parser.add_argument("--project-dir", help="项目图片/视频目录；不传则使用 Brief 中的素材目录字段")
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT), help="六类别脚本资料目录")
    parser.add_argument("--asset-package", default=DEFAULT_ASSET_PACKAGE, help="可插拔资产包目录或清单")
    parser.add_argument("--output-dir", help="输出目录；不传则使用 ~/.mplus/exports/项目名")
    parser.add_argument("--asset-cache-root", default=str(DEFAULT_ASSET_CACHE_ROOT), help="视觉分析缓存目录")
    parser.add_argument("--minimax-command", default="mmx", help="MiniMax视觉命令")
    parser.add_argument("--max-loops", type=int, default=3, help="自动质量循环次数")
    parser.add_argument(
        "--quality-profile",
        choices=("draft", "client-review"),
        default="draft",
    )
    parser.add_argument("--text-model", default="MiniMax-M2.7")
    parser.add_argument("--writer-provider", default="minimax-cli", choices=("minimax-cli", "minimax-http", "deepseek-http"))
    parser.add_argument("--writer-model", default="")
    parser.add_argument("--editor-provider", default="minimax-cli", choices=("minimax-cli", "minimax-http", "deepseek-http"))
    parser.add_argument("--editor-model", default="")
    parser.add_argument("--vision-model", default="")
    parser.add_argument("--revision-constraints")
    parser.add_argument("--parent-run-id", default="")
    parser.add_argument("--review-workbook", help="已填写的人工复核副本")
    parser.add_argument("--review-attestation", help="独立 AI 质量复核 JSON 凭证")
    parser.add_argument("--allow-cached-analysis", action="store_true", help="无新凭据时显式允许复用已有视觉缓存")
    parser.add_argument("--preflight-only", action="store_true", help="只做运行前检查，不生成脚本")
    parser.add_argument("--resume", metavar="RUN_ID", help="恢复指定 auto pipeline run")
    recovery_group = parser.add_mutually_exclusive_group()
    recovery_group.add_argument(
        "--from-stage",
        choices=tuple(stage.value for stage in STAGE_ORDER),
    )
    recovery_group.add_argument(
        "--retry-stage",
        choices=tuple(stage.value for stage in STAGE_ORDER),
    )
    args = parser.parse_args(argv)
    if args.retry_stage and not args.resume:
        parser.error("--retry-stage 必须与 --resume RUN_ID 一起使用")
    if args.from_stage and not (args.resume or args.parent_run_id):
        parser.error("--from-stage 必须与 --resume RUN_ID 或 --parent-run-id 一起使用")
    if args.resume and args.parent_run_id:
        parser.error("--resume 与 --parent-run-id 不能同时使用")

    try:
        project_name, script_type = _brief_summary(args.brief, args.brief_file)
    except Exception as exc:
        print(f"Brief读取失败: {exc}")
        return 2
    if not args.asset_package and Path(args.source_root).expanduser() == DEFAULT_SOURCE_ROOT:
        args.asset_package = configured_asset_package(script_type)

    output_dir = Path(args.output_dir).expanduser() if args.output_dir else (
        Path(DEFAULT_OUTPUT_ROOT) / _slug(project_name)
    )
    report = run_launch_preflight(
        brief_path=args.brief,
        brief_file=args.brief_file,
        source_root=args.source_root,
        project_dir=args.project_dir,
        output_dir=output_dir,
        asset_cache_root=args.asset_cache_root,
        minimax_command=args.minimax_command,
        allow_cached_analysis=args.allow_cached_analysis,
        quality_profile=args.quality_profile,
        writer_provider=args.writer_provider,
        editor_provider=args.editor_provider,
        asset_package=args.asset_package,
    )
    print(report.render())
    if not report.passed or args.preflight_only:
        return 0 if report.passed else 2

    output = output_dir / f"{_slug(project_name)}-{script_type}脚本.xlsx"
    audit = Path(str(output) + ".audit.json")
    review = output.with_suffix(".visual_review.html")
    client_output = output_dir / f"{_slug(project_name)}-{script_type}脚本-客户前终审.xlsx"
    pipeline_args = [
        "--output", str(output),
        "--source-root", str(Path(args.source_root).expanduser()),
        "--source-view", "ai-spec",
        "--asset-cache-root", str(Path(args.asset_cache_root).expanduser()),
        "--auto-pipeline",
        "--max-loops", str(args.max_loops),
        "--audit-json", str(audit),
        "--visual-review-html", str(review),
        "--minimax-command", args.minimax_command,
        "--quality-profile", args.quality_profile,
        "--text-model", args.text_model,
        "--writer-provider", args.writer_provider,
        "--writer-model", args.writer_model or args.text_model,
        "--editor-provider", args.editor_provider,
        "--editor-model", args.editor_model or args.text_model,
        "--vision-model", args.vision_model,
        "--client-output", str(client_output),
    ]
    if args.brief:
        pipeline_args.extend(["--input", args.brief])
    else:
        pipeline_args.extend(["--brief-file", args.brief_file])
    if args.project_dir:
        pipeline_args.extend(["--project-dir", args.project_dir])
    if args.asset_package:
        pipeline_args.extend(["--asset-package", args.asset_package])
    if args.revision_constraints:
        pipeline_args.extend(["--revision-constraints", args.revision_constraints])
    if args.parent_run_id:
        pipeline_args.extend(["--parent-run-id", args.parent_run_id])
    if args.resume:
        pipeline_args.extend(["--resume", args.resume])
    if args.from_stage:
        pipeline_args.extend(["--from-stage", args.from_stage])
    if args.retry_stage:
        pipeline_args.extend(["--retry-stage", args.retry_stage])
    if args.review_workbook:
        pipeline_args.extend(["--review-workbook", args.review_workbook])
    if args.review_attestation:
        pipeline_args.extend(["--review-attestation", args.review_attestation])
    return pipeline_main(pipeline_args)


if __name__ == "__main__":
    raise SystemExit(main())
