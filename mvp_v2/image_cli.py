from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path

from .script_type_adapter import SCRIPT_TYPE_METADATA
from .image_assets import build_image_manifest, read_jsonl, write_jsonl
from .image_retrieval import search_images_for_slot
from .image_understanding import (
    VisionProviderError,
    generate_minimax_visual_analysis,
    generate_minimax_visual_analysis_row,
    import_manual_visual_analysis,
    load_manual_seed,
    write_image_analysis_jsonl,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MVP v2 图片资产/理解/检索工具")
    subparsers = parser.add_subparsers(dest="action", required=True)

    build = subparsers.add_parser("build-manifest", help="扫描图片并生成 image_manifest.jsonl")
    build.add_argument("--source-root", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--project-hint", default="")
    build.add_argument("--script-type", default="", help="图片所属脚本类别")

    manual = subparsers.add_parser("import-manual-analysis", help="把人工视觉标注导入 image_analysis.jsonl")
    manual.add_argument("--manifest", required=True)
    manual.add_argument("--manual-json", required=True)
    manual.add_argument("--output", required=True)
    manual.add_argument("--annotator", default="")

    minimax = subparsers.add_parser("generate-minimax-analysis", help="调用 MiniMax/mmx 生成 image_analysis.jsonl")
    minimax.add_argument("--manifest", required=True)
    minimax.add_argument("--output", required=True)
    minimax.add_argument(
        "--model",
        default="",
        help="仅记录调用方期望；mmx vision describe 当前不支持指定视觉模型",
    )
    minimax.add_argument("--command", dest="minimax_command", default="mmx")
    minimax.add_argument("--limit", type=int)
    minimax.add_argument("--timeout-seconds", type=int, default=90)
    minimax.add_argument("--workers", type=int, default=1)

    search = subparsers.add_parser("search", help="按槽位文本检索图片候选")
    search.add_argument("--manifest", required=True)
    search.add_argument("--analysis", required=True)
    search.add_argument("--slot-text", required=True)
    search.add_argument("--brief-text", default="", help="项目名称（旧参数兼容）")
    search.add_argument(
        "--script-type",
        choices=tuple(item.display_name for item in SCRIPT_TYPE_METADATA),
        default="发布会",
    )
    search.add_argument("--shot-function", default="", help="AI_规范表镜头功能")
    search.add_argument("--visual-subject", default="", help="本镜主要画面主体")
    search.add_argument("--industry", default="", help="项目行业或活动领域")
    search.add_argument("--tone", default="", help="Brief 风格调性")
    search.add_argument("--required-content", default="", help="Brief 必须内容")
    search.add_argument("--forbidden-content", default="", help="Brief 禁忌内容")
    search.add_argument("--top-k", type=int, default=5)

    args = parser.parse_args(argv)
    if args.action == "build-manifest":
        rows = build_image_manifest(
            args.source_root,
            project_hint=args.project_hint,
            script_type=args.script_type,
        )
        write_jsonl(args.output, rows)
        print(f"写出图片资产清单: {args.output}, count={len(rows)}")
        return 0
    if args.action == "import-manual-analysis":
        manifest = read_jsonl(args.manifest)
        seed = load_manual_seed(args.manual_json)
        rows = import_manual_visual_analysis(
            manifest,
            seed,
            source_file=args.manual_json,
            annotator=args.annotator or None,
        )
        write_image_analysis_jsonl(args.output, rows)
        print(f"写出视觉理解清单: {args.output}, count={len(rows)}")
        return 0
    if args.action == "generate-minimax-analysis":
        manifest = read_jsonl(args.manifest)
        rows = write_minimax_analysis_incremental(
            manifest,
            args.output,
            api_key=os.environ.get("MINIMAX_API_KEY", ""),
            command=args.minimax_command,
            model=args.model,
            limit=args.limit,
            timeout_seconds=args.timeout_seconds,
            workers=args.workers,
        )
        print(f"写出 MiniMax 视觉理解清单: {args.output},新增={rows}, model={args.model}")
        return 0
    if args.action == "search":
        manifest = read_jsonl(args.manifest)
        analysis = read_jsonl(args.analysis)
        result = search_images_for_slot(
            {
                "画面描述": args.slot_text,
                "镜头功能": args.shot_function,
                "画面主体": args.visual_subject,
            },
            {
                "项目名称": args.brief_text,
                "脚本类型": args.script_type,
                "行业或活动类别": args.industry,
                "风格调性": args.tone,
                "必须出现的内容": args.required_content,
                "禁忌或不能出现的内容": args.forbidden_content,
            },
            manifest,
            analysis,
            top_k=args.top_k,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 1


def write_minimax_analysis_incremental(
    manifest_rows: list[dict],
    output: str | Path,
    api_key: str,
    command: str = "mmx",
    model: str = "",
    limit: int | None = None,
    timeout_seconds: int = 90,
    workers: int = 1,
) -> int:
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_jsonl(output_path)
    done_ids = {str(row.get("image_id") or "") for row in existing}
    pending = [row for row in manifest_rows if str(row.get("image_id") or "") not in done_ids]
    if limit is not None:
        pending = pending[:limit]
    written = 0
    workers = max(1, int(workers or 1))
    with output_path.open("a", encoding="utf-8") as file:
        if workers == 1:
            for index, asset in enumerate(pending, start=1):
                print(f"[{index}/{len(pending)}] MiniMax 读图 {_asset_label(asset)}", flush=True)
                row = _generate_minimax_row(asset, api_key, command, model, timeout_seconds)
                _append_analysis_row(file, row)
                written += 1
            return written

        errors = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _generate_minimax_row,
                    asset,
                    api_key,
                    command,
                    model,
                    timeout_seconds,
                ): (index, asset)
                for index, asset in enumerate(pending, start=1)
            }
            for future in as_completed(futures):
                index, asset = futures[future]
                try:
                    row = future.result()
                except Exception as exc:
                    errors.append((asset, exc))
                    print(f"[{index}/{len(pending)}] MiniMax 失败 {_asset_label(asset)}: {exc}", flush=True)
                    continue
                _append_analysis_row(file, row)
                written += 1
                print(f"[{written}/{len(pending)}] MiniMax 完成 {_asset_label(asset)}", flush=True)
        if errors:
            asset, exc = errors[0]
            raise VisionProviderError(
                f"MiniMax 批量读图部分失败: failed={len(errors)}, success={written}, "
                f"first={_asset_label(asset)}: {exc}"
            ) from exc
    return written


def _generate_minimax_row(
    asset: dict,
    api_key: str,
    command: str,
    model: str,
    timeout_seconds: int,
) -> dict:
    try:
        return generate_minimax_visual_analysis_row(
            asset,
            api_key,
            command=command,
            model=model,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        raise VisionProviderError(f"MiniMax 读图失败 {_asset_label(asset)}: {exc}") from exc


def _append_analysis_row(file, row: dict) -> None:
    file.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    file.flush()


def _asset_label(asset: dict) -> str:
    return f"{asset.get('image_id', '')} {asset.get('原图路径', '')}"


if __name__ == "__main__":
    raise SystemExit(main())
