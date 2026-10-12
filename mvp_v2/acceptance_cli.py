from __future__ import annotations

import argparse

from .acceptance import AcceptanceError, build_dual_project_acceptance, write_acceptance_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布会双项目 Stage 2 联合验收")
    parser.add_argument(
        "--case",
        action="append",
        nargs=2,
        metavar=("PROJECT_CACHE", "REVIEW_WORKBOOK"),
        required=True,
        help="项目缓存目录和对应复核副本；必须传入两次",
    )
    parser.add_argument("--output", required=True, help="联合验收 JSON")
    args = parser.parse_args(argv)
    try:
        report = build_dual_project_acceptance([tuple(case) for case in args.case])
        output = write_acceptance_report(args.output, report)
    except AcceptanceError as exc:
        print(f"双项目验收失败: {exc}")
        return 1
    print(
        f"双项目验收: status={report['status']}, machine={report['machine_status']}, "
        f"human={report['human_review_status']}, output={output}"
    )
    if report["status"] == "quality_pass":
        return 0
    if report["status"] in {"machine_blocked", "contract_blocked"}:
        return 2
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
