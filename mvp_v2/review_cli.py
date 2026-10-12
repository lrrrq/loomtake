from __future__ import annotations

import argparse

from .review_quality import ReviewWorkbookError, analyze_review_workbooks, write_review_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布会剪辑脚本人工复核统计")
    parser.add_argument("--input", action="append", required=True, help="已填写剪辑复核 sheet 的输出 Excel；可重复传入")
    parser.add_argument("--output", required=True, help="审核汇总 JSON")
    args = parser.parse_args(argv)

    try:
        report = analyze_review_workbooks(args.input)
        output = write_review_report(args.output, report)
    except ReviewWorkbookError as exc:
        print(f"审核报告生成失败: {exc}")
        return 1

    summary = report["summary"]
    human_review_status = report["human_review_status"]
    print(
        f"审核报告: {output}, status={report['status']}, human_review_status={human_review_status}, "
        f"coverage={summary['review_coverage']:.0%}, pending={summary['pending_shot_count']}。"
        "退出码 0 仅表示人工质量门通过，最终 release 仍需机器与业务门计算。"
    )
    if report["status"] == "invalid":
        return 1
    return 0 if human_review_status == "quality_pass" else 3


if __name__ == "__main__":
    raise SystemExit(main())
