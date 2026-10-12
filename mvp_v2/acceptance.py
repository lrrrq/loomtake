from __future__ import annotations

import json
from pathlib import Path

from .pipeline_runtime import MANIFEST_FILENAME, resolve_current_release, resolve_pipeline_run
from .review_quality import ReviewWorkbookError, analyze_review_workbooks
from .review_package import ReviewPackageError, validate_review_workbook
from .run_contract import sha256_file
from .run_state import write_json_atomic
from .launch_contract import contract_counts_valid


class AcceptanceError(ValueError):
    pass


def build_dual_project_acceptance(cases: list[tuple[str | Path, str | Path]]) -> dict:
    if len(cases) != 2:
        raise AcceptanceError("双项目验收必须提供且只提供两个项目案例")
    normalized = [(Path(cache).resolve(), Path(review).resolve()) for cache, review in cases]
    if len({cache for cache, _ in normalized}) != 2:
        raise AcceptanceError("双项目验收不能重复使用同一项目缓存目录")

    projects = [_project_evidence(cache, review) for cache, review in normalized]
    if len({row["project_name"] for row in projects}) != 2:
        raise AcceptanceError("双项目验收的项目名称必须互不相同")

    try:
        human_report = analyze_review_workbooks([row["workbook"] for row in projects])
    except ReviewWorkbookError as exc:
        raise AcceptanceError(str(exc)) from exc
    for project, review in zip(projects, human_report["projects"]):
        _merge_review_evidence(project, review)

    review_contract = _review_contract(human_report)
    machine_pass = all(row["machine_ready"] for row in projects)
    contract_ready = all(_project_contract_ready(row) for row in projects)
    human_status = _joint_human_status(human_report.get("projects") or [])
    if not machine_pass:
        status = "machine_blocked"
    elif not contract_ready or human_status == "invalid":
        status = "contract_blocked"
    elif human_status == "quality_pass":
        status = "quality_pass"
    elif human_status == "quality_fail":
        status = "quality_fail"
    else:
        status = "ready_for_review"

    return {
        "schema_version": "dual-project-acceptance/v1",
        "status": status,
        "project_count": len(projects),
        "machine_status": "pass" if machine_pass else "fail",
        "human_review_status": human_status,
        "release_decision": "stage2_pass" if status == "quality_pass" else "blocked",
        "review_contract": review_contract,
        "projects": projects,
        "review_summary": human_report["summary"],
        "rule_change_candidates": human_report["rule_change_candidates"],
    }


def build_project_acceptance(cache_dir: str | Path, review_path: str | Path) -> dict:
    project = _project_evidence(Path(cache_dir).resolve(), Path(review_path).resolve())
    try:
        human_report = analyze_review_workbooks([project["workbook"]])
    except ReviewWorkbookError as exc:
        raise AcceptanceError(str(exc)) from exc
    review = human_report["projects"][0]
    _merge_review_evidence(project, review)
    contract = _review_contract(human_report)
    contract_ready = _project_contract_ready(project)
    human_status = project["human_review_status"]
    if not project["machine_ready"]:
        status = "machine_blocked"
    elif not contract_ready or human_status == "invalid":
        status = "contract_blocked"
    elif human_status == "quality_pass":
        status = "quality_pass"
    elif human_status == "quality_fail":
        status = "quality_fail"
    else:
        status = "ready_for_review"
    return {
        "schema_version": "project-acceptance/v1",
        "status": status,
        "machine_status": "pass" if project["machine_ready"] else "fail",
        "human_review_status": human_status,
        "review_contract": contract,
        "project": project,
    }


def write_acceptance_report(path: str | Path, report: dict) -> Path:
    output = Path(path)
    write_json_atomic(output, report)
    return output


def _project_evidence(cache_dir: Path, review_path: Path) -> dict:
    candidate = cache_dir.resolve()
    selected_run_id = candidate.name if (candidate / MANIFEST_FILENAME).is_file() and candidate.parent.name == "runs" else None
    cache_root = candidate.parent.parent if selected_run_id else candidate
    try:
        release = (
            resolve_pipeline_run(
                cache_root,
                selected_run_id,
                allowed_statuses={"blocked", "waiting_human_review", "succeeded"},
            )
            if selected_run_id
            else resolve_current_release(cache_root)
        )
    except RuntimeError as exc:
        raise AcceptanceError(f"项目 run 无法验证: {candidate}: {exc}") from exc
    run_dir = next(iter(release.values())).parent
    manifest = _read_json(run_dir / MANIFEST_FILENAME)
    report = _read_json(release["run_report"])
    audit = _read_json(release["audit"])
    run_id = run_dir.name
    if str(manifest.get("run_id") or "") != run_id:
        raise AcceptanceError(f"run manifest 的 run_id 不一致: {cache_dir}")
    if str(report.get("run_id") or "") != run_id:
        raise AcceptanceError(f"run report 的 run_id 不一致: {cache_dir}")
    manifest_project = str(manifest.get("project_name") or "").strip()
    report_project = str(report.get("project_name") or "").strip()
    if not manifest_project or report_project != manifest_project:
        raise AcceptanceError(f"run report 与 run manifest 的项目名称不一致: {cache_dir}")
    try:
        review_provenance = validate_review_workbook(
            cache_root, review_path, release_snapshot=release
        )
    except ReviewPackageError as exc:
        raise AcceptanceError(str(exc)) from exc
    _require_report_path(report, "output_path", release["workbook"])
    _require_report_path(report, "audit_path", release["audit"])
    _require_report_path(report, "visual_review_html", release["visual_review"])
    visual_hashes = _validate_visual_snapshot_evidence(
        manifest, report, run_dir, cache_root
    )

    report_machine = str(report.get("machine_audit_status") or "")
    audit_status = str(audit.get("status") or "")
    hard_fail_count = int(audit.get("hard_fail_count") or 0)
    warning_count = int(audit.get("warning_count") or 0)
    manifest_fingerprint = str(manifest.get("cache_fingerprint") or "").strip()
    report_fingerprint = str(report.get("cache_fingerprint") or "").strip()
    if not manifest_fingerprint or report_fingerprint != manifest_fingerprint:
        raise AcceptanceError(f"run report 缺少 cache_fingerprint: {cache_dir}")
    coverage = {
        key: _required_rate(report, key, cache_dir)
        for key in ("source_reference_rate", "visual_analysis_coverage", "visual_evidence_rate")
    }
    brand_conflict_count = _required_nonnegative_count(report, "brand_conflict_count", cache_dir)
    duplicate_image_count = _required_nonnegative_count(report, "duplicate_image_count", cache_dir)
    if hard_fail_count != int(report.get("hard_fail_count") or 0):
        raise AcceptanceError(f"audit 与 run report 的 hard_fail_count 不一致: {cache_dir}")
    if warning_count != int(report.get("warning_count") or 0):
        raise AcceptanceError(f"audit 与 run report 的 warning_count 不一致: {cache_dir}")
    document_audit = audit.get("script_document_audit") or {}
    document_status = str(document_audit.get("status") or audit_status)
    machine_ready = (
        report_machine == "pass"
        and audit_status == "pass"
        and document_status == "pass"
        and hard_fail_count == 0
        and coverage["source_reference_rate"] == 1.0
    )
    artifact_hashes = {
        name: record["sha256"]
        for name, record in (manifest.get("artifacts") or {}).items()
    }
    return {
        "project_name": report_project,
        "cache_dir": str(cache_root),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "workbook": str(review_path),
        "source_workbook": str(release["workbook"]),
        "source_workbook_sha256": review_provenance["source_workbook_sha256"],
        "machine_status": report_machine,
        "machine_ready": machine_ready,
        "hard_fail_count": hard_fail_count,
        "warning_count": warning_count,
        "manifest_sha256": visual_hashes["manifest_sha256"],
        "analysis_sha256": visual_hashes["analysis_sha256"],
        "cache_fingerprint": manifest_fingerprint,
        "loop_count": int(report.get("loop_count") or 0),
        "passed_iteration": report.get("passed_iteration"),
        **coverage,
        "brand_conflict_count": brand_conflict_count,
        "duplicate_image_count": duplicate_image_count,
        "artifact_hashes": artifact_hashes,
    }


def _review_contract(report: dict) -> dict:
    projects = report.get("projects") or []
    return {
        "shot_count": sum(
            int(project["stage1_editorial_gate"]["per_shot"]["row_count"])
            for project in projects
        ),
        "adjacent_edge_count": sum(
            int(project["stage1_editorial_gate"]["adjacent_pairs"]["edge_count"])
            for project in projects
        ),
        "full_film_count": sum(
            int(project["stage1_editorial_gate"]["full_film"]["row_count"])
            for project in projects
        ),
    }


def _project_contract_ready(project: dict) -> bool:
    gate = project.get("stage1_editorial_gate") or {}
    return contract_counts_valid(
        int((gate.get("per_shot") or {}).get("row_count") or 0),
        int((gate.get("adjacent_pairs") or {}).get("edge_count") or 0),
        int((gate.get("full_film") or {}).get("row_count") or 0),
    )


def _joint_human_status(projects: list[dict]) -> str:
    statuses = {str(project.get("human_review_status") or "invalid") for project in projects}
    for status in ("invalid", "quality_fail", "pending", "quality_pass"):
        if status in statuses:
            return status
    return "invalid"


def _merge_review_evidence(project: dict, review: dict) -> None:
    project.update({
        "human_review_status": review["human_review_status"],
        "review_coverage": review["review_coverage"],
        "quality_metrics": review["quality_metrics"],
        "manual_modification_rate": review["manual_modification_rate"],
        "image_acceptance_rate": review["image_acceptance_rate"],
        "duration_seconds": review["duration_seconds"],
        "stage1_editorial_gate": review["stage1_editorial_gate"],
        "issues": review["issues"],
    })


def _require_report_path(report: dict, key: str, expected: Path) -> None:
    actual = Path(str(report.get(key) or "")).resolve()
    if actual != expected.resolve():
        raise AcceptanceError(f"run report 的 {key} 不属于当前 run")


def _validate_visual_snapshot_evidence(
    manifest: dict,
    report: dict,
    run_dir: Path,
    cache_dir: Path,
) -> dict[str, str]:
    snapshots = manifest.get("visual_snapshots")
    expected = {
        "image_manifest": ("manifest_path", "manifest_sha256", "视觉资产清单"),
        "image_analysis": ("analysis_path", "analysis_sha256", "视觉分析"),
    }
    if not isinstance(snapshots, dict) or set(snapshots) != set(expected):
        raise AcceptanceError(f"run manifest 缺少完整视觉快照: {cache_dir}")

    verified = {}
    for name, (path_key, hash_key, label) in expected.items():
        record = snapshots.get(name)
        if not isinstance(record, dict) or not {"path", "sha256", "size_bytes"} <= set(record):
            raise AcceptanceError(f"{label}快照完整性记录缺失: {cache_dir}")
        raw_path = Path(str(record["path"]))
        snapshot_path = raw_path.resolve()
        if raw_path.is_symlink() or snapshot_path.parent != run_dir or not snapshot_path.is_file():
            raise AcceptanceError(f"{label}快照不属于当前 run: {cache_dir}")
        report_path = Path(str(report.get(path_key) or "")).resolve()
        if report_path != snapshot_path:
            raise AcceptanceError(f"run report 的 {path_key} 不属于当前 run")
        actual_size = snapshot_path.stat().st_size
        actual_sha256 = sha256_file(snapshot_path)
        if record.get("size_bytes") != actual_size or record.get("sha256") != actual_sha256:
            raise AcceptanceError(f"{label}快照完整性校验失败: {cache_dir}")
        if str(report.get(hash_key) or "") != actual_sha256:
            raise AcceptanceError(f"{label}快照 SHA 与真实文件不一致: {cache_dir}")
        verified[hash_key] = actual_sha256
    return verified


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"JSON 证据不可读取: {path}") from exc
    if not isinstance(value, dict):
        raise AcceptanceError(f"JSON 证据必须是对象: {path}")
    return value


def _required_rate(report: dict, key: str, cache_dir: Path) -> float:
    if key not in report:
        raise AcceptanceError(f"run report 缺少 {key}: {cache_dir}")
    try:
        value = float(report[key])
    except (TypeError, ValueError) as exc:
        raise AcceptanceError(f"run report 的 {key} 无效: {cache_dir}") from exc
    if not 0 <= value <= 1:
        raise AcceptanceError(f"run report 的 {key} 无效: {cache_dir}")
    return value


def _required_nonnegative_count(report: dict, key: str, cache_dir: Path) -> int:
    if key not in report:
        raise AcceptanceError(f"run report 缺少 {key}: {cache_dir}")
    try:
        value = int(report[key])
    except (TypeError, ValueError) as exc:
        raise AcceptanceError(f"run report 的 {key} 无效: {cache_dir}") from exc
    if value < 0:
        raise AcceptanceError(f"run report 的 {key} 无效: {cache_dir}")
    return value
