#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from mvp_v2.cloud_runtime import CloudRuntimeError
from mvp_v2.cloud_storage import CloudStorageError
from mvp_v2.cloud_worker import CloudWorker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M+ PostgreSQL 租约云端 Worker")
    parser.add_argument("--worker-id", default=socket.gethostname())
    parser.add_argument("--package-cache", default="/srv/mplus/package-cache")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args(argv)
    try:
        worker = CloudWorker.from_environment(
            worker_id=args.worker_id,
            package_cache=args.package_cache,
        )
        worker.repository.initialize()
        if args.once:
            result = worker.run_once()
            print(json.dumps(result or {"status": "idle"}, ensure_ascii=False, sort_keys=True))
            return 0
        worker.serve_forever(poll_seconds=args.poll_seconds)
    except (CloudRuntimeError, CloudStorageError) as exc:
        print(json.dumps({"status": "blocked", "message": str(exc)}, ensure_ascii=False))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
