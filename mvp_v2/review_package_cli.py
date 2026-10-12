from __future__ import annotations

import argparse

from .review_package import ReviewPackageError, export_review_workbook


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出可填写且可追溯的人工复核副本")
    parser.add_argument(
        "--case",
        action="append",
        nargs=2,
        metavar=("PROJECT_CACHE", "OUTPUT_XLSX"),
        help="项目缓存目录和复核副本路径；可重复传入",
    )
    parser.add_argument(
        "--run-case",
        action="append",
        nargs=3,
        metavar=("PROJECT_CACHE", "RUN_ID", "OUTPUT_XLSX"),
        help="从 waiting_human_review run 导出复核副本；可重复传入",
    )
    args = parser.parse_args(argv)
    if not args.case and not args.run_case:
        parser.error("必须提供 --case 或 --run-case")
    try:
        outputs = [
            export_review_workbook(cache, output)
            for cache, output in (args.case or [])
        ]
        outputs.extend(
            export_review_workbook(cache, output, run_id=run_id)
            for cache, run_id, output in (args.run_case or [])
        )
    except ReviewPackageError as exc:
        print(f"复核副本导出失败: {exc}")
        return 1
    for output in outputs:
        print(f"复核副本: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
