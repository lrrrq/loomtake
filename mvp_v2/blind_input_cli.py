from __future__ import annotations

import argparse

from .blind_input import BlindInputError, seal_blind_input, verify_blind_input


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布会第三项目盲测输入封存")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seal = subparsers.add_parser("seal")
    seal.add_argument("--project-root", required=True)
    seal.add_argument("--brief", required=True)
    seal.add_argument("--source-workbook", required=True)
    seal.add_argument("--media-root")
    seal.add_argument("--qualification", required=True)
    seal.add_argument("--output", required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--project-root", required=True)
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--expected-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "seal":
            manifest = seal_blind_input(
                args.project_root,
                args.brief,
                args.media_root,
                args.qualification,
                args.output,
                source_workbook_path=args.source_workbook,
            )
            print(
                f"盲测输入封存: project_code={manifest['project_code']}, "
                f"files={manifest['input_file_count']}, output={args.output}"
            )
            return 0
        report = verify_blind_input(
            args.project_root, args.manifest, args.expected_sha256
        )
    except BlindInputError as exc:
        print(f"盲测输入合同失败: {exc}")
        return 1
    print(f"盲测输入校验: status={report['status']}, project_code={report['project_code']}")
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
