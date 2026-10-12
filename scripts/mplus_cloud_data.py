#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from mvp_v2.cloud_migration import (
    CloudMigrationError,
    create_cloud_snapshot,
    restore_cloud_snapshot,
    validate_data_root,
    verify_cloud_snapshot,
)
from mvp_v2.runtime_config import configured_source_commit, load_runtime_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M+ 云服务器数据迁移工具")
    commands = parser.add_subparsers(dest="command", required=True)

    export = commands.add_parser("export", help="创建不可覆盖的自包含数据快照")
    export.add_argument("--archive", required=True)
    export.add_argument("--data-root")
    export.add_argument("--source-root")
    export.add_argument("--asset-package", action="append", default=[])
    export.add_argument("--source-commit")

    verify = commands.add_parser("verify", help="只读校验数据快照")
    verify.add_argument("--archive", required=True)

    restore = commands.add_parser("restore", help="恢复到全新的 data_root")
    restore.add_argument("--archive", required=True)
    restore.add_argument("--target-data-root", required=True)
    restore.add_argument("--source-commit")

    check = commands.add_parser("check", help="只读校验已恢复 data_root")
    check.add_argument("--data-root")

    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            runtime = load_runtime_config()
            asset_packages = tuple(args.asset_package) or _configured_packages(runtime)
            result = create_cloud_snapshot(
                args.data_root or runtime["data_root"],
                args.archive,
                source_root=args.source_root or runtime["source_root"],
                asset_packages=asset_packages,
                source_commit=args.source_commit or configured_source_commit(),
            )
        elif args.command == "verify":
            result = verify_cloud_snapshot(args.archive)
        elif args.command == "restore":
            result = restore_cloud_snapshot(
                args.archive,
                args.target_data_root,
                source_commit=args.source_commit or configured_source_commit(),
            )
        else:
            runtime = load_runtime_config()
            result = validate_data_root(args.data_root or runtime["data_root"])
    except CloudMigrationError as exc:
        print(json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _configured_packages(runtime: dict) -> tuple[str, ...]:
    values = [str(runtime.get("asset_package") or "")]
    values.extend(str(value) for value in dict(runtime.get("asset_packages") or {}).values())
    return tuple(dict.fromkeys(value for value in values if value))


if __name__ == "__main__":
    raise SystemExit(main())
