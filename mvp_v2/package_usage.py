from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from .run_state import exclusive_lock


USAGE_SCHEMA_VERSION = "mplus-package-usage/v1"


def append_local_usage(logs_root: str | Path, record: dict) -> Path:
    root = Path(logs_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / "package-usage.jsonl"
    payload = {
        "schema_version": USAGE_SCHEMA_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        **record,
    }
    with exclusive_lock(path.with_suffix(".lock")):
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
    return path


def local_usage_summary(logs_root: str | Path) -> list[dict]:
    path = Path(logs_root).expanduser().resolve() / "package-usage.jsonl"
    if not path.is_file():
        return []
    counts: dict[tuple, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("schema_version") != USAGE_SCHEMA_VERSION:
            continue
        key = (
            str(row.get("requested_ref") or "unknown"),
            str(row.get("package_id") or "unknown"),
            row.get("package_version") if type(row.get("package_version")) is int else None,
            str(row.get("package_sha256") or "unknown"),
            str(row.get("invocation_source") or "local_cli"),
            "local",
            str(row.get("status") or "unknown"),
        )
        counts[key] = counts.get(key, 0) + 1
    fields = (
        "requested_ref", "package_id", "package_version", "package_sha256",
        "invocation_source", "execution_backend", "status",
    )
    return [
        {**dict(zip(fields, key)), "call_count": count}
        for key, count in sorted(counts.items(), key=lambda item: str(item[0]))
    ]
