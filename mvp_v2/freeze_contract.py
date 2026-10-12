from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .acceptance import (
    AcceptanceError,
    build_dual_project_acceptance,
    write_acceptance_report,
)
from .auto_pipeline import (
    VISUAL_CACHE_PROVIDER,
    current_visual_cache_fingerprint,
)
from .quality_contract import QualityThresholds
from .run_contract import (
    build_baseline_manifest,
    sha256_file,
    sha256_json,
    write_baseline_manifest,
)


FREEZE_SCHEMA_VERSION = "launch-blind-freeze/v1"
REQUIRED_SOURCE_SCOPES = ("mvp_v2", "tests")


class FreezeContractError(ValueError):
    pass


def create_freeze_manifest(
    project_root: str | Path,
    source_paths: list[str | Path],
    input_paths: list[str | Path],
    cases: list[tuple[str | Path, str | Path]],
    output_path: str | Path,
    config: dict | None = None,
) -> dict:
    root = Path(project_root).resolve()
    required_sources = [root / value for value in REQUIRED_SOURCE_SCOPES]
    requested_sources = [Path(path).resolve() for path in source_paths]
    if set(requested_sources) != set(required_sources):
        raise FreezeContractError("冻结 source scope 必须完整覆盖 mvp_v2 和 tests")
    sources = required_sources
    if not input_paths:
        raise FreezeContractError("冻结公共输入不能为空")
    inputs = [Path(path).resolve() for path in input_paths]
    freeze_config = default_freeze_config() if config is None else dict(config)
    try:
        public_baseline = build_baseline_manifest(
            root, sources, inputs, config=freeze_config, reject_symlinks=True
        )
        if not any(row.get("role") == "input" for row in public_baseline.get("files") or []):
            raise FreezeContractError("冻结公共输入不能为空")
        acceptance = build_dual_project_acceptance(cases)
    except AcceptanceError as exc:
        raise FreezeContractError(f"双项目验收失败: {exc}") from exc
    except ValueError as exc:
        raise FreezeContractError(str(exc)) from exc
    if acceptance.get("status") != "quality_pass":
        raise FreezeContractError("双项目人工质量门未通过，不能冻结盲测版本")

    output = Path(output_path).resolve()
    acceptance_path = output.with_name(f"{output.stem}.dual_acceptance.json")
    _relative(root, acceptance_path)
    write_acceptance_report(acceptance_path, acceptance)
    inputs.append(acceptance_path)
    try:
        baseline = build_baseline_manifest(
            root, sources, inputs, config=freeze_config, reject_symlinks=True
        )
    except ValueError as exc:
        raise FreezeContractError(str(exc)) from exc
    manifest = {
        "schema_version": FREEZE_SCHEMA_VERSION,
        "source_scopes": [_relative(root, path) for path in sources],
        "input_scopes": [_relative(root, path) for path in inputs],
        "dual_acceptance_path": _relative(root, acceptance_path),
        "dual_acceptance_sha256": sha256_file(acceptance_path),
        "benchmark_runs": _benchmark_runs(acceptance),
        "baseline": baseline,
    }
    write_baseline_manifest(output_path, manifest)
    return manifest


def verify_freeze_manifest(
    project_root: str | Path,
    manifest_path: str | Path,
    expected_manifest_sha256: str,
) -> dict:
    root = Path(project_root).resolve()
    manifest_file = Path(manifest_path).resolve()
    if not expected_manifest_sha256 or sha256_file(manifest_file) != expected_manifest_sha256:
        raise FreezeContractError("冻结清单与外部摘要不一致")
    frozen = _read_json(manifest_file)
    if frozen.get("schema_version") != FREEZE_SCHEMA_VERSION:
        raise FreezeContractError("冻结清单版本无效")
    baseline = frozen.get("baseline")
    if not isinstance(baseline, dict):
        raise FreezeContractError("冻结清单缺少 baseline")
    if tuple(frozen.get("source_scopes") or []) != REQUIRED_SOURCE_SCOPES:
        raise FreezeContractError("冻结 source scope 不完整")
    dual_acceptance_path = root / str(frozen.get("dual_acceptance_path") or "")
    try:
        actual_dual_acceptance_sha256 = sha256_file(dual_acceptance_path)
    except OSError:
        actual_dual_acceptance_sha256 = ""
    expected_dual_acceptance_sha256 = str(frozen.get("dual_acceptance_sha256") or "")
    source_paths = [root / value for value in REQUIRED_SOURCE_SCOPES]
    input_paths = [root / value for value in frozen.get("input_scopes") or []]
    try:
        current = build_baseline_manifest(
            root,
            source_paths,
            input_paths,
            config=baseline.get("config") or {},
            reject_symlinks=True,
        )
    except ValueError as exc:
        raise FreezeContractError(str(exc)) from exc
    frozen_files = _files_by_key(baseline.get("files") or [])
    current_files = _files_by_key(current.get("files") or [])
    added = sorted(path for path in current_files if path not in frozen_files)
    removed = sorted(path for path in frozen_files if path not in current_files)
    changed = sorted(
        path
        for path in frozen_files.keys() & current_files.keys()
        if frozen_files[path] != current_files[path]
    )
    status = (
        "pass"
        if current == baseline
        and actual_dual_acceptance_sha256 == expected_dual_acceptance_sha256
        else "mismatch"
    )
    freeze_manifest_sha256 = sha256_file(manifest_file)
    return {
        "schema_version": "launch-blind-freeze-verification/v1",
        "status": status,
        "freeze_manifest_sha256": freeze_manifest_sha256,
        "dual_acceptance_sha256": expected_dual_acceptance_sha256,
        "freeze_batch_digest": compute_freeze_batch_digest(
            freeze_manifest_sha256, expected_dual_acceptance_sha256
        ),
        "benchmark_runs": dict(frozen.get("benchmark_runs") or {}),
        "expected_baseline_sha256": str(baseline.get("baseline_sha256") or ""),
        "actual_baseline_sha256": str(current.get("baseline_sha256") or ""),
        "added_paths": added,
        "removed_paths": removed,
        "changed_paths": changed,
        "environment_or_config_changed": bool(
            status == "mismatch" and not (added or removed or changed)
        ),
    }


def compute_freeze_batch_digest(
    freeze_manifest_sha256: str,
    dual_acceptance_sha256: str,
) -> str:
    return sha256_json({
        "freeze_manifest_sha256": freeze_manifest_sha256,
        "dual_acceptance_sha256": dual_acceptance_sha256,
    })


def default_freeze_config() -> dict:
    return {
        "quality_thresholds": asdict(QualityThresholds()),
        "provider": VISUAL_CACHE_PROVIDER,
        "model_label": "mmx-vision-describe/provider-managed",
        "visual_cache_fingerprint": current_visual_cache_fingerprint(),
        "max_generation_loops": 5,
    }


def _files_by_key(rows: list[dict]) -> dict[str, tuple[str, int]]:
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
        raise FreezeContractError(f"冻结范围必须位于项目根目录: {path}") from exc


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FreezeContractError(f"JSON 不可读取: {path}") from exc
    if not isinstance(value, dict):
        raise FreezeContractError(f"JSON 必须是对象: {path}")
    return value


def _benchmark_runs(acceptance: dict) -> dict[str, str]:
    return {
        str(project.get("project_name") or ""): str(project.get("run_id") or "")
        for project in acceptance.get("projects") or []
    }

