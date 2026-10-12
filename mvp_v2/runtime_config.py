from __future__ import annotations

import json
import os
from pathlib import Path
import re


LOCAL_SECRET_NAMES = {
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "MINIMAX_API_KEY",
    "ARK_API_KEY",
    "ZHIPU_API_KEY",
}


DEFAULT_DATA_ROOT = "~/.mplus"
PROFILE_ENV = "MPLUS_PROFILE"
EXECUTION_BACKEND_ENV = "MPLUS_EXECUTION_BACKEND"
VALID_PROFILES = {"stable", "dev", "test"}
VALID_EXECUTION_BACKENDS = {"local", "cloud"}
SOURCE_COMMIT_ENV = "MPLUS_SOURCE_COMMIT"
_SOURCE_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{40}$")
RUNTIME_DIRECTORY_NAMES = {
    "runs_root": "runs",
    "cache_root": "cache",
    "exports_root": "exports",
    "logs_root": "logs",
    "packages_root": "packages",
}


def configured_profile(environ: dict | None = None) -> str:
    environment = os.environ if environ is None else environ
    profile = str(environment.get(PROFILE_ENV) or "stable").strip().casefold()
    if profile not in VALID_PROFILES:
        raise ValueError(f"{PROFILE_ENV} 必须是 stable、dev 或 test")
    return profile


def profile_root(profile: str | None = None) -> Path:
    normalized = profile or configured_profile()
    root = Path(DEFAULT_DATA_ROOT).expanduser()
    return root if normalized == "stable" else root / "profiles" / normalized


def default_profile_port(profile: str | None = None) -> int:
    return 8766 if (profile or configured_profile()) == "dev" else 8765


def load_runtime_config() -> dict:
    profile = configured_profile()
    base_root = profile_root(profile)
    config_path = Path(
        os.environ.get("MPLUS_CONFIG") or base_root / "config.json"
    ).expanduser()
    payload = {}
    if config_path.is_file():
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
    data_root = Path(
        os.environ.get("MPLUS_DATA_ROOT")
        or payload.get("data_root")
        or (base_root if profile == "stable" else base_root / "data")
    ).expanduser().resolve()
    source_root = Path(
        os.environ.get("MPLUS_SOURCE_ROOT")
        or payload.get("source_root")
        or data_root / "sources"
    ).expanduser().resolve()
    asset_package = os.environ.get("MPLUS_ASSET_PACKAGE") or payload.get("asset_package") or ""
    execution_backend = str(
        os.environ.get(EXECUTION_BACKEND_ENV)
        or payload.get("execution_backend")
        or "local"
    ).strip().casefold()
    if execution_backend not in VALID_EXECUTION_BACKENDS:
        raise ValueError(
            f"{EXECUTION_BACKEND_ENV}/execution_backend 必须是 local 或 cloud"
        )
    default_receipt = data_root / "legacy-upload-receipt.json"
    default_legacy_catalog = (
        default_receipt
        if default_receipt.is_file()
        else data_root / "legacy-upload-plan.json"
    )
    return {
        **payload,
        "profile": profile,
        "profile_root": str(base_root.resolve()),
        "config_path": str(config_path),
        "data_root": str(data_root),
        "source_root": str(source_root),
        **{
            key: str(data_root / dirname)
            for key, dirname in RUNTIME_DIRECTORY_NAMES.items()
        },
        "legacy_mounts_path": str(data_root / "legacy-mounts.json"),
        "package_registry_path": str(
            Path(
                os.environ.get("MPLUS_PACKAGE_REGISTRY")
                or payload.get("package_registry_path")
                or data_root / "package_registry.json"
            ).expanduser().resolve()
        ),
        "legacy_catalog_path": str(
            Path(
                os.environ.get("MPLUS_LEGACY_CATALOG")
                or payload.get("legacy_catalog_path")
                or default_legacy_catalog
            ).expanduser().resolve()
        ),
        "asset_package": str(asset_package),
        "execution_backend": execution_backend,
        "default_port": default_profile_port(profile),
        "source_commit": configured_source_commit(),
    }


def configured_source_commit(environ: dict | None = None) -> str:
    environment = os.environ if environ is None else environ
    value = str(environment.get(SOURCE_COMMIT_ENV) or "").strip()
    if not value:
        return "unavailable"
    if not _SOURCE_COMMIT_PATTERN.fullmatch(value):
        raise ValueError(f"{SOURCE_COMMIT_ENV} 必须是完整的 40 位 Git SHA")
    return value.casefold()


def load_legacy_mounts(config: dict | None = None) -> list[dict]:
    runtime = config or load_runtime_config()
    path = Path(runtime["legacy_mounts_path"])
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    mounts = []
    for raw in payload.get("mounts") or []:
        item = dict(raw)
        if item.get("mode") != "read_only":
            continue
        original = Path(str(item.get("original_path") or "")).expanduser()
        target = Path(str(item.get("path") or "")).expanduser()
        if not original.is_absolute() or not target.is_absolute() or not target.is_dir():
            continue
        mounts.append({
            **item,
            "original_path": str(original.resolve()),
            "path": str(target.resolve()),
        })
    return mounts


def legacy_roots(kind: str, config: dict | None = None) -> list[Path]:
    return [
        Path(item["path"])
        for item in load_legacy_mounts(config)
        if item.get("kind") == kind
    ]


def configured_asset_package(script_type: str, config: dict | None = None) -> str:
    runtime = config or load_runtime_config()
    normalized = str(script_type or "").strip()
    registry_path = Path(str(runtime.get("package_registry_path") or ""))
    if registry_path.is_file():
        try:
            from .package_registry import LocalPackageRegistry
            from .script_type_adapter import category_id_for_script_type

            category_id = category_id_for_script_type(normalized)
            registry = LocalPackageRegistry(registry_path)
            if (registry.list()["aliases"].get(category_id) or {}).get("formal"):
                return str(registry.resolve_formal(category_id)["local_path"])
        except (OSError, RuntimeError, ValueError):
            return ""
    environment_override = os.environ.get("MPLUS_ASSET_PACKAGE")
    if environment_override:
        return str(environment_override)
    configured = dict(runtime.get("asset_packages") or {}).get(normalized)
    if not configured and normalized == "发布会":
        configured = runtime.get("asset_package")
    return str(configured or "")


def remap_legacy_path(value: str | Path, config: dict | None = None) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or path.exists():
        return path
    path = Path(os.path.realpath(path))
    for mount in load_legacy_mounts(config):
        original = Path(mount["original_path"])
        try:
            relative = path.relative_to(original)
        except ValueError:
            continue
        return Path(mount["path"]) / relative
    return path


def load_local_secrets(path: str | Path | None = None) -> dict:
    secret_path = Path(
        path
        or os.environ.get("MPLUS_SECRETS")
        or profile_root() / "secrets.env"
    ).expanduser()
    loaded = []
    ignored = []
    if not secret_path.is_file():
        return {"path": str(secret_path), "loaded": loaded, "ignored": ignored}
    try:
        lines = secret_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"path": str(secret_path), "loaded": loaded, "ignored": ignored}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            ignored.append("malformed")
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if name not in LOCAL_SECRET_NAMES:
            ignored.append(name or "unnamed")
            continue
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        if value and not os.environ.get(name):
            os.environ[name] = value
            loaded.append(name)
    return {
        "path": str(secret_path),
        "loaded": sorted(loaded),
        "ignored": sorted(ignored),
    }
