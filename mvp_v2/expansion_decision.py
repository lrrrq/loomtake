from __future__ import annotations

import json
from pathlib import Path

from .acceptance import AcceptanceError, build_project_acceptance
from .blind_input import BlindInputError, verify_blind_input
from .freeze_contract import compute_freeze_batch_digest
from .freeze_contract import FreezeContractError, verify_freeze_manifest
from .quality_contract import compute_release_decision, labor_time_saving_rate
from .run_contract import sha256_file
from .run_state import write_json_atomic


class ExpansionDecisionError(ValueError):
    pass


REQUIRED_PROJECT_FIELDS = (
    "project_code", "role", "evidence_status", "machine_status",
    "hard_fail_count", "warning_count", "human_review_status", "release_decision",
    "passed_iteration", "shot_count", "adjacent_edge_count", "full_film_count",
    "duration_seconds",
    "approved_count", "needs_revision_count", "rejected_count", "major_change_count",
    "high_severity_rejected_count", "source_reference_rate",
    "visual_analysis_coverage", "visual_evidence_rate", "brand_conflict_count",
    "duplicate_image_count",
)
SOURCE_EVIDENCE_KEYS = frozenset({
    "freeze_manifest", "dual_acceptance", "blind_input_seal", "timing_evidence",
})


def build_expansion_evidence(
    project_root: str | Path,
    freeze_manifest: str | Path,
    freeze_manifest_sha256: str,
    blind_input_seal: str | Path,
    blind_input_seal_sha256: str,
    blind_case: tuple[str | Path, str | Path],
    timing_evidence: str | Path,
    timing_evidence_sha256: str,
) -> dict:
    root = Path(project_root).resolve()
    try:
        freeze = verify_freeze_manifest(
            project_root, freeze_manifest, freeze_manifest_sha256
        )
        blind_input = verify_blind_input(
            project_root, blind_input_seal, blind_input_seal_sha256
        )
    except (FreezeContractError, BlindInputError) as exc:
        raise ExpansionDecisionError(str(exc)) from exc
    if freeze.get("status") != "pass":
        raise ExpansionDecisionError("冻结版本校验未通过")
    if blind_input.get("status") != "pass":
        raise ExpansionDecisionError("盲测输入校验未通过")

    manifest = _read_anchored_json(freeze_manifest, freeze_manifest_sha256)
    dual_path = (root / str(manifest.get("dual_acceptance_path") or "")).resolve()
    try:
        dual_path.relative_to(root)
    except ValueError as exc:
        raise ExpansionDecisionError("双项目验收路径必须位于项目根目录") from exc
    dual_sha256 = str(freeze.get("dual_acceptance_sha256") or "")
    dual = _read_anchored_json(dual_path, dual_sha256)
    timing = _read_anchored_json(timing_evidence, timing_evidence_sha256)
    if not (
        dual.get("schema_version") == "dual-project-acceptance/v1"
        and dual.get("status") == "quality_pass"
        and isinstance(dual.get("projects"), list)
        and len(dual["projects"]) == 2
    ):
        raise ExpansionDecisionError("双项目验收证据未通过")
    try:
        blind = build_project_acceptance(*blind_case)
    except AcceptanceError as exc:
        raise ExpansionDecisionError(f"盲测项目现场验收失败: {exc}") from exc
    if not (
        blind.get("schema_version") == "project-acceptance/v1"
        and blind.get("status") == "quality_pass"
        and isinstance(blind.get("project"), dict)
    ):
        raise ExpansionDecisionError("盲测项目验收证据未通过")
    if timing.get("schema_version") != "launch-timing-evidence/v1" or not isinstance(timing.get("projects"), dict):
        raise ExpansionDecisionError("计时证据无效")

    verified_freeze_manifest_sha256 = str(freeze.get("freeze_manifest_sha256") or "")
    if verified_freeze_manifest_sha256 != freeze_manifest_sha256:
        raise ExpansionDecisionError("冻结清单摘要与现场校验不一致")
    if str(freeze.get("dual_acceptance_sha256") or "") != dual_sha256:
        raise ExpansionDecisionError("双项目验收不属于冻结批次")
    expected_freeze_batch = compute_freeze_batch_digest(
        verified_freeze_manifest_sha256, dual_sha256
    )
    if (
        not _is_sha256(verified_freeze_manifest_sha256)
        or str(freeze.get("freeze_batch_digest") or "") != expected_freeze_batch
    ):
        raise ExpansionDecisionError("冻结批次摘要无效")
    benchmark_runs = {
        str(project.get("project_name") or ""): str(project.get("run_id") or "")
        for project in dual["projects"]
    }
    if not all(benchmark_runs) or dict(freeze.get("benchmark_runs") or {}) != benchmark_runs:
        raise ExpansionDecisionError("基准项目 run 与冻结批次不一致")

    blind_code = str(blind_input.get("project_code") or "")
    blind_seal_sha256 = str(blind_input.get("blind_input_seal_sha256") or "")
    blind_project = blind["project"]
    blind_run_id = str(blind_project.get("run_id") or "")
    if not blind_code or str(blind_project.get("project_name") or "") != blind_code:
        raise ExpansionDecisionError("盲测项目身份与输入封存不一致")
    if not blind_run_id:
        raise ExpansionDecisionError("盲测项目 run 缺失")
    if blind_seal_sha256 != blind_input_seal_sha256:
        raise ExpansionDecisionError("盲测输入封存摘要与现场校验不一致")
    if blind_code in benchmark_runs:
        raise ExpansionDecisionError("盲测项目不能复用基准项目身份")

    rows = []
    for project in dual["projects"]:
        code = str(project.get("project_name") or "")
        rows.append(_project_from_acceptance(project, code, "benchmark", timing["projects"]))
    rows.append(_project_from_acceptance(blind_project, blind_code, "blind", timing["projects"]))
    common_rework, common_all_three = _derive_common_issue_counts(rows)
    return {
        "schema_version": "launch-type-expansion-evidence/v1",
        "projects": rows,
        "common_rework_count": common_rework,
        "common_all_three_count": common_all_three,
        "source_evidence_sha256": {
            "blind_input_seal": blind_input_seal_sha256,
            "dual_acceptance": dual_sha256,
            "freeze_manifest": verified_freeze_manifest_sha256,
            "timing_evidence": timing_evidence_sha256,
        },
        "evidence_binding": {
            "freeze_manifest_sha256": verified_freeze_manifest_sha256,
            "freeze_batch_digest": expected_freeze_batch,
            "dual_acceptance_sha256": dual_sha256,
            "blind_input_seal_sha256": blind_seal_sha256,
            "blind_project_code": blind_code,
            "blind_run_id": blind_run_id,
            "benchmark_runs": benchmark_runs,
        },
    }


def decide_type_expansion(
    projects: list[dict],
    common_rework_count: int,
    common_all_three_count: int,
) -> dict:
    if len(projects) != 3 or len({str(row.get("project_code") or "") for row in projects}) != 3:
        raise ExpansionDecisionError("类型扩展决策必须提供三个互不相同的发布会项目")
    for row in projects:
        _validate_project(row)
    blind = [row for row in projects if row.get("role") == "blind"]
    if len(blind) != 1:
        raise ExpansionDecisionError("三个项目中必须且只能有一个 blind 项目")
    if common_rework_count < 0 or common_all_three_count < 0:
        raise ExpansionDecisionError("共性返工计数不能为负数")

    total_modified = sum(
        _count(row, "needs_revision_count") + _count(row, "rejected_count")
        for row in projects
    )
    modification_rate = total_modified / 54
    blocking = []
    for row in projects:
        code = str(row.get("project_code") or "")
        if row.get("evidence_status") != "pass":
            blocking.append(f"{code}: 证据完整性未通过")
        if (
            row.get("machine_status") != "pass"
            or _count(row, "hard_fail_count") > 0
            or _count(row, "warning_count") > 0
        ):
            blocking.append(f"{code}: 机器硬门未通过")
        if _count(row, "high_severity_rejected_count") > 0:
            blocking.append(f"{code}: 存在高严重度不通过问题")
        if (
            float(row["source_reference_rate"]) != 1.0
            or float(row["visual_analysis_coverage"]) != 1.0
            or float(row["visual_evidence_rate"]) != 1.0
            or _count(row, "brand_conflict_count") > 0
            or _count(row, "duplicate_image_count") > 0
        ):
            blocking.append(f"{code}: 来源、视觉、品牌或重复图完整性未通过")
    quality_fail_count = sum(row.get("human_review_status") == "quality_fail" for row in projects)
    if quality_fail_count >= 2:
        blocking.append("两个及以上项目人工质量门失败")
    if modification_rate > 0.25:
        blocking.append("三项目总体修改率超过 25%")
    if common_rework_count >= 5:
        blocking.append("跨项目共性返工候选达到 5 条")
    if common_all_three_count >= 2:
        blocking.append("至少两个问题在三个项目中同时复现")

    entry_conditions = [
        all(row.get("release_decision") == "release_pass" for row in projects),
        all(row.get("human_review_status") == "quality_pass" for row in projects),
        _count(blind[0], "passed_iteration") == 1,
        all(
            row.get("role") != "benchmark" or _count(row, "passed_iteration") <= 2
            for row in projects
        ),
        modification_rate <= 0.10,
        all(
            _count(row, "needs_revision_count") + _count(row, "rejected_count") <= 2
            for row in projects
        ),
        all(_count(row, "rejected_count") == 0 for row in projects),
        all(_count(row, "major_change_count") == 0 for row in projects),
        common_rework_count == 0,
        common_all_three_count == 0,
    ]
    if blocking:
        decision = "stop_expansion"
        reasons = ["存在完整性、机器或跨项目规模红线"]
    elif all(entry_conditions):
        decision = "enter_exhibition"
        reasons = ["三个发布会项目均达到冻结版本质量与业务门"]
    else:
        decision = "continue_launch"
        reasons = ["未满足进入展会的全部质量条件"]
    return {
        "schema_version": "launch-type-expansion-decision/v1",
        "decision": decision,
        "project_count": 3,
        "overall_modification_rate": round(modification_rate, 6),
        "common_rework_count": common_rework_count,
        "common_all_three_count": common_all_three_count,
        "blocking_reasons": blocking,
        "reasons": reasons,
        "projects": projects,
    }


def write_expansion_decision(path: str | Path, decision: dict) -> Path:
    output = Path(path)
    write_json_atomic(output, decision)
    return output


def read_expansion_evidence(path: str | Path) -> tuple[list[dict], int, int]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpansionDecisionError(f"扩展决策证据不可读取: {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("projects"), list):
        raise ExpansionDecisionError("扩展决策证据必须包含 projects 数组")
    if value.get("schema_version") != "launch-type-expansion-evidence/v1":
        raise ExpansionDecisionError("扩展决策证据 schema 无效")
    source_hashes = value.get("source_evidence_sha256")
    if not isinstance(source_hashes, dict) or set(source_hashes) != SOURCE_EVIDENCE_KEYS:
        raise ExpansionDecisionError("扩展决策证据缺少四类来源摘要")
    if any(not _is_sha256(source_hashes[key]) for key in SOURCE_EVIDENCE_KEYS):
        raise ExpansionDecisionError("扩展决策来源摘要无效")
    return (
        value["projects"],
        int(value.get("common_rework_count") or 0),
        int(value.get("common_all_three_count") or 0),
    )


def _count(row: dict, key: str) -> int:
    try:
        return int(row.get(key) or 0)
    except (TypeError, ValueError) as exc:
        raise ExpansionDecisionError(f"{key} 必须是整数") from exc


def _validate_project(row: dict) -> None:
    for key in REQUIRED_PROJECT_FIELDS:
        if key not in row:
            raise ExpansionDecisionError(f"项目证据缺少 {key}")
    count_keys = (
        "hard_fail_count", "warning_count", "passed_iteration", "shot_count",
        "adjacent_edge_count", "full_film_count", "duration_seconds", "approved_count",
        "needs_revision_count", "rejected_count", "major_change_count",
        "high_severity_rejected_count", "brand_conflict_count", "duplicate_image_count",
    )
    counts = {key: _count(row, key) for key in count_keys}
    if any(value < 0 for value in counts.values()):
        raise ExpansionDecisionError("项目证据计数不能为负数")
    if counts["passed_iteration"] < 1:
        raise ExpansionDecisionError("passed_iteration 必须大于 0")
    shot_count = counts["shot_count"]
    if (
        shot_count < 1
        or counts["adjacent_edge_count"] != shot_count - 1
        or counts["full_film_count"] != 1
        or counts["duration_seconds"] != 60
        or counts["approved_count"] + counts["needs_revision_count"] + counts["rejected_count"] != shot_count
    ):
        raise ExpansionDecisionError("每项目必须满足 N 镜、N-1 边、1 整片且审核计数合计为 N")
    for key in ("source_reference_rate", "visual_analysis_coverage", "visual_evidence_rate"):
        try:
            value = float(row[key])
        except (TypeError, ValueError) as exc:
            raise ExpansionDecisionError(f"{key} 必须是 0 到 1 的数值") from exc
        if not 0 <= value <= 1:
            raise ExpansionDecisionError(f"{key} 必须是 0 到 1 的数值")


def _is_sha256(value) -> bool:
    text = str(value or "")
    return len(text) == 64 and all(character in "0123456789abcdef" for character in text.lower())


def _read_anchored_json(path: str | Path, expected_sha256: str) -> dict:
    source = Path(path)
    if not _is_sha256(expected_sha256) or sha256_file(source) != expected_sha256:
        raise ExpansionDecisionError(f"扩展证据来源与外部摘要不一致: {source}")
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExpansionDecisionError(f"扩展证据来源不可读取: {source}") from exc
    if not isinstance(value, dict):
        raise ExpansionDecisionError(f"扩展证据来源必须是对象: {source}")
    return value


def _project_from_acceptance(project: dict, code: str, role: str, timings: dict) -> dict:
    if not code or code not in timings:
        raise ExpansionDecisionError(f"项目缺少计时证据: {code or 'unknown'}")
    metrics = project.get("quality_metrics") or {}
    gate = project.get("stage1_editorial_gate") or {}
    timing = timings[code]
    try:
        labor_rate = labor_time_saving_rate(
            float(timing["manual_active_minutes"]),
            float(timing["assisted_active_minutes"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ExpansionDecisionError(f"项目计时证据无效: {code}") from exc
    machine_status = str(project.get("machine_status") or "")
    warning_count = int(project.get("warning_count") or 0)
    human_status = str(project.get("human_review_status") or "")
    discussability = float(metrics.get("discussability_rate") or 0)
    issues = list(project.get("issues") or [])
    return {
        "project_code": code,
        "role": role,
        "evidence_status": "pass",
        "machine_status": machine_status,
        "hard_fail_count": int(project.get("hard_fail_count") or 0),
        "warning_count": warning_count,
        "human_review_status": human_status,
        "release_decision": compute_release_decision(
            machine_status, warning_count, human_status, discussability, labor_rate
        ),
        "passed_iteration": int(project.get("passed_iteration") or 0),
        "shot_count": int((gate.get("per_shot") or {}).get("row_count") or 0),
        "adjacent_edge_count": int((gate.get("adjacent_pairs") or {}).get("edge_count") or 0),
        "full_film_count": int((gate.get("full_film") or {}).get("row_count") or 0),
        "duration_seconds": int(project.get("duration_seconds") or 0),
        "approved_count": int(metrics.get("approved_shot_count") or 0),
        "needs_revision_count": int(metrics.get("needs_revision_shot_count") or 0),
        "rejected_count": int(metrics.get("rejected_shot_count") or 0),
        "major_change_count": int(metrics.get("major_change_count") or 0),
        "high_severity_rejected_count": sum(
            str(issue.get("severity") or "") == "高" and str(issue.get("status") or "") != "通过"
            for issue in issues
        ),
        "source_reference_rate": project.get("source_reference_rate"),
        "visual_analysis_coverage": project.get("visual_analysis_coverage"),
        "visual_evidence_rate": project.get("visual_evidence_rate"),
        "brand_conflict_count": int(project.get("brand_conflict_count") or 0),
        "duplicate_image_count": int(project.get("duplicate_image_count") or 0),
        "issues": issues,
        "labor_time_saving_rate": round(labor_rate, 6),
    }


def _derive_common_issue_counts(projects: list[dict]) -> tuple[int, int]:
    occurrences: dict[tuple[str, str, str], set[str]] = {}
    for project in projects:
        for issue in project.get("issues") or []:
            key = (
                str(issue.get("slot_index") or ""),
                str(issue.get("dimension") or ""),
                str(issue.get("issue_code") or ""),
            )
            if any(key):
                occurrences.setdefault(key, set()).add(project["project_code"])
    return (
        sum(len(projects_by_issue) >= 2 for projects_by_issue in occurrences.values()),
        sum(len(projects_by_issue) == 3 for projects_by_issue in occurrences.values()),
    )
