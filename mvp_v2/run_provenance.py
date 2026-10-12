from __future__ import annotations

import json
import os
from pathlib import Path

from . import __version__
from .asset_package import load_asset_package
from .package_registry import PackageRegistryError, build_package_record
from .run_contract import sha256_file
from .runtime_config import configured_source_commit


PROVENANCE_SCHEMA_VERSION = "mplus-run-provenance/v1"
LOCKED_PROVENANCE_ENV = "MPLUS_LOCKED_RUN_PROVENANCE"


class RunProvenanceError(RuntimeError):
    pass


def run_provenance(
    *,
    input_path: str | Path,
    asset_package: str | Path,
    environ: dict | None = None,
) -> dict:
    environment = os.environ if environ is None else environ
    source = Path(input_path).expanduser().resolve()
    if not source.is_file():
        raise RunProvenanceError("运行输入文件不存在")
    try:
        package = load_asset_package(asset_package)
        package_record = build_package_record(asset_package)
    except (OSError, ValueError, PackageRegistryError) as exc:
        raise RunProvenanceError(f"资产包完整性校验失败: {exc}") from exc
    manifest_sha = sha256_file(package["manifest_path"])
    provenance = {
        "schema_version": PROVENANCE_SCHEMA_VERSION,
        "runner_version": __version__,
        "source_commit": configured_source_commit(environment),
        "package_id": str(package.get("package_id") or ""),
        "package_version": package.get("package_version"),
        "package_sha256": package_record["package_sha256"],
        "package_manifest_sha256": manifest_sha,
        "package_schema_version": str(package.get("schema_version") or ""),
        "input_ref": str(source),
        "input_sha256": sha256_file(source),
    }
    raw_locked = str(environment.get(LOCKED_PROVENANCE_ENV) or "").strip()
    if raw_locked:
        try:
            locked = json.loads(raw_locked)
        except json.JSONDecodeError as exc:
            raise RunProvenanceError(f"{LOCKED_PROVENANCE_ENV} 不是有效 JSON") from exc
        _validate_locked(locked)
        expected = {
            "runner_version": __version__,
            "source_commit": provenance["source_commit"],
            "package_id": provenance["package_id"],
            "package_version": provenance["package_version"],
            "package_sha256": provenance["package_sha256"],
            "package_schema_version": provenance["package_schema_version"],
            "input_sha256": provenance["input_sha256"],
        }
        for field, value in expected.items():
            if locked.get(field) != value:
                raise RunProvenanceError(f"云端锁定运行字段不一致: {field}")
        return {
            **provenance,
            **locked,
            "package_manifest_sha256": manifest_sha,
            "materialized_input_path": str(source),
            "materialized_package_path": str(Path(package["package_root"]).resolve()),
        }
    return provenance


def _validate_locked(value: object) -> None:
    if not isinstance(value, dict):
        raise RunProvenanceError("云端锁定运行记录必须是 JSON 对象")
    required = {
        "schema_version",
        "cloud_run_id",
        "runner_version",
        "source_commit",
        "package_id",
        "package_version",
        "package_sha256",
        "package_schema_version",
        "input_ref",
        "input_sha256",
    }
    if not required.issubset(value):
        raise RunProvenanceError("云端锁定运行记录字段不完整")
    for field in ("package_sha256", "input_sha256"):
        digest = str(value.get(field) or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise RunProvenanceError(f"云端锁定运行 {field} 无效")
