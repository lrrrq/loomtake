#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from mvp_v2.project_area import (
    PROJECT_ROOT,
    broken_root_symlinks,
    inventory_project_root,
    project_root_issues,
    read_records,
    remove_registered_broken_symlinks,
    write_records,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="M+ 项目根目录治理检查")
    parser.add_argument("--root", default=str(PROJECT_ROOT))
    subparsers = parser.add_subparsers(dest="command", required=True)

    inventory = subparsers.add_parser("inventory")
    inventory.add_argument("--output", required=True)

    broken = subparsers.add_parser("broken-links")
    broken.add_argument("--output", required=True)

    check = subparsers.add_parser("check")
    check.add_argument("--final", action="store_true")

    remove = subparsers.add_parser("remove-broken-links")
    remove.add_argument("--manifest", required=True)

    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    if args.command == "inventory":
        path = write_records(args.output, inventory_project_root(root))
        print(path)
        return 0
    if args.command == "broken-links":
        path = write_records(args.output, broken_root_symlinks(root))
        print(path)
        return 0
    if args.command == "remove-broken-links":
        removed = remove_registered_broken_symlinks(root, read_records(args.manifest))
        print(json.dumps({"removed": len(removed), "paths": removed}, ensure_ascii=False))
        return 0
    issues = project_root_issues(root, final=args.final)
    print(json.dumps({"status": "pass" if not issues else "fail", "issues": issues}, ensure_ascii=False))
    return 0 if not issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
