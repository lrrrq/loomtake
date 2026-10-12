from __future__ import annotations

import argparse

from .ai_spec import AiSpecError, check_ai_spec, refresh_ai_spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M+ AI_规范表刷新与检查")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in (
        ("refresh", "从人工表刷新AI_规范表"),
        ("check", "检查AI_规范表同步和质量状态"),
    ):
        child = subparsers.add_parser(command, help=help_text)
        child.add_argument("--workbook", required=True, help="含模版、创意库的 M+ 脚本库工作簿")
    args = parser.parse_args(argv)

    try:
        report = refresh_ai_spec(args.workbook) if args.command == "refresh" else check_ai_spec(args.workbook)
    except Exception as exc:
        print(f"AI_规范表{args.command}失败: {exc}")
        return 1

    state = "已刷新" if report.changed else ("已同步" if report.in_sync else "不同步")
    print(
        f"{state}: 模板={report.template_count}, 创意={report.creative_count}, "
        f"ready={report.ready_count}, pending_review={report.pending_review_count}, "
        f"blocked={report.blocked_count}"
    )
    if report.validation_codes:
        print(f"校验: {', '.join(report.validation_codes)}")
    return 0 if report.in_sync and report.usable else 2


if __name__ == "__main__":
    raise SystemExit(main())
