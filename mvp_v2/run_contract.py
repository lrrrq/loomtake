from __future__ import annotations

import hashlib
import json
import platform
import re
import tempfile
from pathlib import Path

import openpyxl
import PIL

from .runtime_config import configured_source_commit

SCHEMA_VERSION = "v0.2.3"
_SENSITIVE_KEY_PARTS = ("key", "token", "secret", "password")
_IGNORED_FILENAMES = {".DS_Store"}


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value):
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def build_baseline_manifest(
    project_root,
    source_paths,
    input_paths=(),
    config=None,
    reject_symlinks=False,
):
    root = Path(project_root).resolve()
    if not root.is_dir():
        raise ValueError(f"project_root must be an existing directory: {project_root}")

    files = _file_records(root, "source", source_paths, reject_symlinks=reject_symlinks)
    files.extend(_file_records(root, "input", input_paths, reject_symlinks=reject_symlinks))
    files.sort(key=lambda row: (row["path"], row["role"]))

    redacted_config = _redact_config({} if config is None else config)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "source_commit": configured_source_commit(),
        "python_version": platform.python_version(),
        "dependencies": {
            "openpyxl": openpyxl.__version__,
            "Pillow": PIL.__version__,
        },
        "files": files,
        "config": redacted_config,
        "config_sha256": sha256_json(redacted_config),
    }
    manifest["baseline_sha256"] = sha256_json({
        "schema_version": manifest["schema_version"],
        "source_commit": manifest["source_commit"],
        "python_version": manifest["python_version"],
        "dependencies": manifest["dependencies"],
        "files": manifest["files"],
        "config": manifest["config"],
        "config_sha256": manifest["config_sha256"],
    })
    return manifest


def write_baseline_manifest(path, manifest):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            suffix=".tmp",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            json.dump(manifest, file, ensure_ascii=False, sort_keys=True, indent=2)
            file.write("\n")
        temporary_path.replace(output)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
    return output


def _file_records(root, role, paths, reject_symlinks=False):
    records = {}
    for raw_path in paths:
        raw = Path(raw_path)
        if reject_symlinks and raw.is_symlink():
            raise ValueError(f"受控范围不允许符号链接: {raw_path}")
        path = _validate_path(root, raw_path)
        candidates = [path] if path.is_file() else sorted(path.rglob("*"))
        for candidate in candidates:
            if candidate.is_symlink():
                if reject_symlinks:
                    raise ValueError(f"受控范围不允许符号链接: {candidate}")
                continue
            if not candidate.is_file() or _ignored(candidate):
                continue
            resolved = _validate_path(root, candidate)
            relative_path = resolved.relative_to(root).as_posix()
            records[(role, relative_path)] = {
                "role": role,
                "path": relative_path,
                "sha256": sha256_file(resolved),
                "size_bytes": resolved.stat().st_size,
            }
    return list(records.values())


def _validate_path(root, path):
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise ValueError(f"path does not exist: {path}")
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f"path is outside project_root: {path}") from error
    return resolved


def _ignored(path):
    return (
        "__pycache__" in path.parts
        or path.name in _IGNORED_FILENAMES
        or path.suffix == ".pyc"
    )


def _redact_config(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(key) else _redact_config(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _is_sensitive_key(key):
    components = re.findall(
        r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|\d+",
        str(key).replace("-", "_").replace("_", " "),
    )
    return bool(components) and components[-1].lower() in _SENSITIVE_KEY_PARTS
