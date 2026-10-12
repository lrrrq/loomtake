from __future__ import annotations

import json
from pathlib import Path

from .run_contract import (
    build_baseline_manifest,
    sha256_file,
    write_baseline_manifest,
)


BLIND_INPUT_SCHEMA_VERSION = "launch-blind-input/v2"
LEGACY_BLIND_INPUT_SCHEMA_VERSION = "launch-blind-input/v1"
REQUIRED_QUALIFICATION_FLAGS = (
    "authorized_for_use",
    "original_brief",
    "approved_source_documents",
    "not_used_in_development",
)


class BlindInputError(ValueError):
    pass


def seal_blind_input(
    project_root: str | Path,
    brief_path: str | Path,
    media_root: str | Path | None,
    qualification_path: str | Path,
    output_path: str | Path,
    *,
    source_workbook_path: str | Path | None = None,
) -> dict:
    root = Path(project_root).resolve()
    brief = Path(brief_path).resolve()
    media = Path(media_root).resolve() if media_root else None
    source_workbook = (
        Path(source_workbook_path).resolve() if source_workbook_path else None
    )
    qualification_file = Path(qualification_path).resolve()
    if not brief.is_file():
        raise BlindInputError("盲测 Brief 必须是文件")
    if source_workbook is None or not source_workbook.is_file():
        raise BlindInputError("盲测已审核源工作簿必须是文件")
    if media is not None and not media.is_dir():
        raise BlindInputError("盲测素材必须是目录")
    qualification = _read_json(qualification_file)
    missing = [key for key in REQUIRED_QUALIFICATION_FLAGS if qualification.get(key) is not True]
    if missing:
        raise BlindInputError("盲测资格声明未通过: " + ", ".join(missing))
    for key in ("project_code", "custodian", "confirmed_at"):
        if not str(qualification.get(key) or "").strip():
            raise BlindInputError(f"盲测资格声明缺少 {key}")
    try:
        input_paths = [brief, source_workbook]
        if media is not None:
            input_paths.append(media)
        baseline = build_baseline_manifest(
            root,
            [],
            input_paths,
            config={"qualification": qualification},
            reject_symlinks=True,
        )
    except ValueError as exc:
        raise BlindInputError(str(exc)) from exc
    manifest = {
        "schema_version": BLIND_INPUT_SCHEMA_VERSION,
        "project_code": str(qualification["project_code"]),
        "brief_scope": _relative(root, brief),
        "source_workbook_scope": _relative(root, source_workbook),
        "media_scope": _relative(root, media) if media is not None else None,
        "qualification_path": _relative(root, qualification_file),
        "qualification_sha256": sha256_file(qualification_file),
        "input_file_count": len(baseline["files"]),
        "input_baseline": baseline,
    }
    write_baseline_manifest(output_path, manifest)
    return manifest


def verify_blind_input(
    project_root: str | Path,
    manifest_path: str | Path,
    expected_manifest_sha256: str,
) -> dict:
    root = Path(project_root).resolve()
    manifest_file = Path(manifest_path).resolve()
    if not expected_manifest_sha256 or sha256_file(manifest_file) != expected_manifest_sha256:
        raise BlindInputError("盲测封存清单与外部摘要不一致")
    sealed = _read_json(manifest_file)
    schema_version = sealed.get("schema_version")
    if schema_version not in {
        BLIND_INPUT_SCHEMA_VERSION,
        LEGACY_BLIND_INPUT_SCHEMA_VERSION,
    }:
        raise BlindInputError("盲测输入封存清单版本无效")
    project_code = str(sealed.get("project_code") or "").strip()
    if not project_code:
        raise BlindInputError("盲测输入封存清单缺少 project_code")
    baseline = sealed.get("input_baseline")
    if not isinstance(baseline, dict):
        raise BlindInputError("盲测输入封存清单缺少 input_baseline")
    qualification_path = root / str(sealed.get("qualification_path") or "")
    qualification_changed = (
        not qualification_path.is_file()
        or sha256_file(qualification_path) != sealed.get("qualification_sha256")
    )
    try:
        if schema_version == LEGACY_BLIND_INPUT_SCHEMA_VERSION:
            input_paths = [
                root / sealed["brief_scope"],
                root / sealed["media_scope"],
            ]
        else:
            input_paths = [
                root / sealed["brief_scope"],
                root / sealed["source_workbook_scope"],
            ]
            if sealed.get("media_scope"):
                input_paths.append(root / sealed["media_scope"])
        current = build_baseline_manifest(
            root,
            [],
            input_paths,
            config=baseline.get("config") or {},
            reject_symlinks=True,
        )
    except (KeyError, ValueError) as exc:
        raise BlindInputError(str(exc)) from exc
    frozen_files = _files_by_path(baseline.get("files") or [])
    current_files = _files_by_path(current.get("files") or [])
    added = sorted(current_files.keys() - frozen_files.keys())
    removed = sorted(frozen_files.keys() - current_files.keys())
    changed = sorted(
        path
        for path in frozen_files.keys() & current_files.keys()
        if frozen_files[path] != current_files[path]
    )
    status = "pass" if current == baseline and not qualification_changed else "mismatch"
    return {
        "schema_version": "launch-blind-input-verification/v2",
        "status": status,
        "project_code": project_code,
        "blind_input_seal_sha256": expected_manifest_sha256,
        "blind_input_baseline_sha256": str(baseline.get("baseline_sha256") or ""),
        "expected_baseline_sha256": str(baseline.get("baseline_sha256") or ""),
        "actual_baseline_sha256": str(current.get("baseline_sha256") or ""),
        "added_paths": added,
        "removed_paths": removed,
        "changed_paths": changed,
        "qualification_changed": qualification_changed,
    }


def _files_by_path(rows: list[dict]) -> dict[str, tuple[str, int]]:
    return {
        str(row.get("path") or ""): (
            str(row.get("sha256") or ""),
            int(row.get("size_bytes") or 0),
        )
        for row in rows
    }


def _relative(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError as exc:
        raise BlindInputError(f"盲测输入必须位于封存根目录: {path}") from exc


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BlindInputError(f"JSON 不可读取: {path}") from exc
    if not isinstance(value, dict):
        raise BlindInputError(f"JSON 必须是对象: {path}")
    return value
