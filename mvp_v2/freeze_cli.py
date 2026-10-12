from __future__ import annotations

import argparse

from .freeze_contract import (
    FreezeContractError,
    create_freeze_manifest,
    verify_freeze_manifest,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布会第三项目盲测冻结清单")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create = subparsers.add_parser("create")
    create.add_argument("--project-root", required=True)
    create.add_argument("--source", action="append", required=True)
    create.add_argument("--input", action="append", default=[])
    create.add_argument(
        "--case",
        action="append",
        nargs=2,
        metavar=("PROJECT_CACHE", "REVIEW_WORKBOOK"),
        required=True,
    )
    create.add_argument("--output", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--project-root", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            manifest = create_freeze_manifest(
                args.project_root,
                args.source,
                args.input,
                [tuple(case) for case in args.case],
                args.output,
            )
            print(
                f"冻结清单: {args.output}, baseline={manifest['baseline']['baseline_sha256']}, "
                f"dual_acceptance={manifest['dual_acceptance_sha256']}"
            )
            return 0
        report = verify_freeze_manifest(
            args.project_root, args.manifest, args.expected_sha256
        )
    except FreezeContractError as exc:
        print(f"冻结合同失败: {exc}")
        return 1
    print(
        f"冻结校验: status={report['status']}, "
        f"expected={report['expected_baseline_sha256']}, actual={report['actual_baseline_sha256']}, "
        f"batch={report['freeze_batch_digest']}"
    )
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
