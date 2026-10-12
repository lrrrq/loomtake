#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from mvp_v2.project_migration import execute_cutover, run_rollback_drill


def main() -> int:
    parser = argparse.ArgumentParser(description="M+ 项目业务目录安全迁移")
    parser.add_argument("--project-root", default=str(PROJECT_DIR))
    parser.add_argument("--data-root", default="~/.mplus")
    parser.add_argument("--cutover-id")
    parser.add_argument("--approved-by")
    parser.add_argument("--observation-days", type=int, default=14)
    parser.add_argument("--rollback-drill", action="store_true")
    parser.add_argument("--source-manifest")
    parser.add_argument("--target-manifest")
    parser.add_argument("--incremental-manifest")
    parser.add_argument("--rto-limit-seconds", type=float, default=60 * 60)
    args = parser.parse_args()
    if args.rollback_drill:
        missing = [
            name
            for name in ("source_manifest", "target_manifest", "incremental_manifest")
            if not getattr(args, name)
        ]
        if missing:
            parser.error(
                "rollback drill 需要 --source-manifest、--target-manifest 和 --incremental-manifest"
            )
        summary = run_rollback_drill(
            args.source_manifest,
            args.target_manifest,
            args.incremental_manifest,
            rto_limit_seconds=args.rto_limit_seconds,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["status"] == "ready_for_live_switch_drill" else 2
    if not args.cutover_id:
        parser.error("cutover 模式需要 --cutover-id")
    if not args.approved_by:
        parser.error("cutover 模式需要 --approved-by")
    summary = execute_cutover(
        args.project_root,
        args.data_root,
        args.cutover_id,
        approved_by=args.approved_by,
        observation_days=args.observation_days,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
