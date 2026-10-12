#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.source_publish import PublishError, source_status


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def weekly_release_report(
    *,
    stable_worktree: Path,
    dev_worktree: Path,
    expected_publish: str = "",
) -> dict:
    stable = stable_worktree.expanduser().resolve()
    dev = dev_worktree.expanduser().resolve()
    if not (stable / ".git").exists() or not (dev / ".git").exists():
        raise PublishError("stable_or_dev_worktree_missing")
    stable_source = _git(stable, "rev-parse", "HEAD")
    dev_source = _git(dev, "rev-parse", "HEAD")
    status = source_status(stable_source)
    ahead = int(_git(dev, "rev-list", "--count", f"{stable_source}..{dev_source}") or 0)
    dev_dirty = bool(_git(
        dev,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "mplus",
        "mvp_v2",
        "scripts",
        "schemas",
        "tests",
    ))
    issues = list(status.get("issues") or [])
    if dev_dirty:
        issues.append("dev_publish_scope_dirty")
    actual_publish = str(dict(status.get("publish") or {}).get("publish_commit") or "")
    if expected_publish and actual_publish != expected_publish.casefold():
        issues.append("remote_publish_head_changed")
    if issues:
        verdict = "BLOCK"
    elif status.get("verdict") == "REVISE":
        verdict = "REVISE"
    else:
        verdict = "PASS"
    return {
        "schema_version": "mplus-weekly-release-check/v1",
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verdict": verdict,
        "stable_source_commit": stable_source,
        "published_source_commit": str(dict(status.get("publish") or {}).get("source_commit") or ""),
        "publish_commit": actual_publish,
        "dev_source_commit": dev_source,
        "dev_commits_ahead": ahead,
        "dev_publish_scope_dirty": dev_dirty,
        "issues": sorted(set(issues)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="M+ 每周 source-only 只读版本检查")
    parser.add_argument("--stable-worktree", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument(
        "--dev-worktree",
        default=str(Path(__file__).resolve().parents[1]),
    )
    parser.add_argument("--expected-publish", default="")
    parser.add_argument(
        "--output",
        default=str(Path.home() / ".mplus" / "reports" / "weekly-release-latest.json"),
    )
    args = parser.parse_args()
    try:
        report = weekly_release_report(
            stable_worktree=Path(args.stable_worktree),
            dev_worktree=Path(args.dev_worktree),
            expected_publish=args.expected_publish,
        )
        output = Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (PublishError, subprocess.CalledProcessError, OSError) as exc:
        print(json.dumps({"verdict": "BLOCK", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps({**report, "output": str(output)}, ensure_ascii=False, sort_keys=True))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
