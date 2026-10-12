from __future__ import annotations

import os
from pathlib import Path
import subprocess

from . import __version__
from .runtime_config import load_runtime_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _git(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def current_source_commit() -> str:
    configured = str(load_runtime_config().get("source_commit") or "")
    return configured if configured != "unavailable" else _git("rev-parse", "HEAD")


def package_versions(runtime: dict) -> list[dict]:
    from .package_registry import LocalPackageRegistry

    payload = LocalPackageRegistry(runtime["package_registry_path"]).list()
    packages = dict(payload.get("packages") or {})
    rows = []
    for category_id, aliases in sorted(dict(payload.get("aliases") or {}).items()):
        key = str(dict(aliases or {}).get("formal") or "")
        record = dict(packages.get(key) or {})
        rows.append({
            "category_id": category_id,
            "formal_ref": key,
            "package_id": record.get("package_id"),
            "package_version": record.get("package_version"),
            "package_sha256": record.get("package_sha256"),
        })
    return rows


def version_report(*, include_remote: bool = False) -> dict:
    runtime = load_runtime_config()
    source_commit = current_source_commit()
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    result = {
        "schema_version": "mplus-version-report/v1",
        "runner_version": __version__,
        "source_commit": source_commit,
        "source_dirty": dirty,
        "branch": _git("branch", "--show-current"),
        "profile": runtime["profile"],
        "execution_backend": runtime["execution_backend"],
        "config_path": runtime["config_path"],
        "data_root": runtime["data_root"],
        "default_port": runtime["default_port"],
        "container_image_digest": str(
            os.environ.get("MPLUS_CONTAINER_IMAGE_DIGEST") or "unavailable"
        ),
        "cloud_schema_version": _cloud_schema_version(),
        "formal_packages": package_versions(runtime),
    }
    if include_remote:
        from scripts.source_publish import source_status

        result["publish"] = source_status(source_commit)
        if dirty:
            result["publish"] = {
                **result["publish"],
                "status": "blocked",
                "verdict": "BLOCK",
                "issues": sorted(set(
                    list(result["publish"].get("issues") or [])
                    + ["local_publish_scope_dirty"]
                )),
            }
    return result


def _cloud_schema_version() -> str:
    try:
        from .cloud_runtime import POSTGRES_SCHEMA_VERSION
    except (ImportError, AttributeError):
        return "unversioned"
    return str(POSTGRES_SCHEMA_VERSION)
