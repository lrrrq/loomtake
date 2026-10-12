from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
import re
from typing import Callable, Mapping

from openpyxl import load_workbook

from .pipeline_stages import AutoPipelineStageError
from .portable_paths import resolve_run_local_path
from .review_quality import (
    ADJACENT_REVIEW_SHEET_NAME,
    FULL_FILM_REVIEW_SHEET_NAME,
    REVIEW_SHEET_NAME,
    ReviewWorkbookError,
    analyze_review_workbooks,
)
from .run_contract import sha256_file, sha256_json
from .run_state import write_json_atomic
from .stage_contract import StageError, StageErrorCategory, StageStatus
from .stage_runtime import finish_pipeline_stage, start_pipeline_stage


@dataclass(frozen=True)
class HumanReviewArtifact:
    workbook_path: Path
    audit_path: Path
    report_path: Path
    review_html_path: Path | None
    human_review_status: str
    quality_review_status: str
    reviewer_kind: str
    stage_status: str
    report: dict


@dataclass(frozen=True)
class PublishFeedbackArtifact:
    path: Path
    action: str
    feedback: dict


class ReviewAttestationError(ValueError):
    pass


def read_completed_quality_review_stage(
    state,
    *,
    machine_audit_path: str | Path,
    workbook_path: str | Path,
    review_html_path: str | Path | None = None,
) -> HumanReviewArtifact:
    manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
    record = manifest.get("stages", {}).get("human_review") or {}
    if record.get("status") != StageStatus.SUCCEEDED.value:
        raise RuntimeError("发布重试缺少已通过的质量复核节点")
    audit_path = Path(machine_audit_path)
    workbook_path = Path(workbook_path)
    html_path = Path(review_html_path) if review_html_path else None
    artifact = _read_human_review_artifact(
        state,
        record,
        workbook_path=workbook_path,
        audit_path=audit_path,
        review_html_path=html_path,
    )
    attestation_path = resolve_run_local_path(
        state.run_dir,
        artifact.report.get("review_attestation_path") or "",
    )
    actual_output_sha256 = sha256_json({
        "report": sha256_file(artifact.report_path),
        "workbook": sha256_file(workbook_path),
        "review_html": (
            sha256_file(html_path)
            if html_path is not None and html_path.is_file()
            else None
        ),
        "review_attestation": (
            sha256_file(attestation_path)
            if attestation_path.is_file()
            else None
        ),
    })
    if actual_output_sha256 != record.get("output_sha256"):
        raise RuntimeError("已通过的质量复核节点产物完整性校验失败")
    return artifact


def run_human_review_stage(
    state,
    *,
    machine_audit_path: str | Path,
    workbook_path: str | Path,
    review_html_path: str | Path | None = None,
    review_attestation_path: str | Path | None = None,
) -> HumanReviewArtifact:
    stage_id = "human_review"
    audit_path = Path(machine_audit_path)
    workbook_path = Path(workbook_path)
    html_path = Path(review_html_path) if review_html_path else None
    attestation_path = Path(review_attestation_path) if review_attestation_path else None
    attestation = None
    if attestation_path is not None:
        attestation = _validate_independent_review_attestation(
            state,
            attestation_path=attestation_path,
            workbook_path=workbook_path,
            audit_path=audit_path,
        )
    input_refs = [str(audit_path.resolve()), str(workbook_path.resolve())]
    if html_path is not None:
        input_refs.append(str(html_path.resolve()))
    if attestation_path is not None:
        input_refs.append(str(attestation_path.resolve()))
    input_sha256 = sha256_json({
        "machine_audit": _file_sha256(audit_path),
        "workbook": _file_sha256(workbook_path),
        "review_html": _file_sha256(html_path) if html_path is not None else None,
        "review_attestation": _file_sha256(attestation_path) if attestation_path is not None else None,
    })

    existing = _existing_stage_record(state, stage_id)
    if (
        existing.get("input_sha256") == input_sha256
        and existing.get("status") in {
            StageStatus.SUCCEEDED.value,
            StageStatus.SUCCEEDED_WITH_WARNINGS.value,
            StageStatus.WAITING_HUMAN_REVIEW.value,
            StageStatus.BLOCKED.value,
        }
    ):
        return _read_human_review_artifact(
            state,
            existing,
            workbook_path=workbook_path,
            audit_path=audit_path,
            review_html_path=html_path,
        )

    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=input_refs,
    )
    if record.get("status") in {
        StageStatus.SUCCEEDED.value,
        StageStatus.SUCCEEDED_WITH_WARNINGS.value,
    }:
        return _read_human_review_artifact(
            state,
            record,
            workbook_path=workbook_path,
            audit_path=audit_path,
            review_html_path=html_path,
        )

    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "machine_audit_read_failed",
            f"机器审计产物读取失败: {exc}",
            retryable=True,
        )
    if audit.get("status") != "pass":
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.DEPENDENCY,
            "machine_gate_not_passed",
            "机器审计门未通过，不能进入质量复核结论",
        )

    if attestation is not None:
        report_path = state.run_dir / "quality_review_report.json"
        normalized_attestation_path = state.run_dir / "independent_review_attestation.json"
        write_json_atomic(normalized_attestation_path, attestation)
        quality_status = _attestation_quality_status(attestation["verdict"])
        human_status = "not_performed"
        reviewer_kind = "independent_ai"
        report = {
            "schema_version": "mplus-quality-review/v1",
            "status": "complete",
            "reviewer_kind": reviewer_kind,
            "quality_review_status": quality_status,
            "human_review_status": human_status,
            "projects": [{
                "project_name": attestation["project_name"],
                "run_id": attestation["run_id"],
                "verdict": attestation["verdict"],
                "counts": attestation["counts"],
                "blocking_issues": attestation["blocking_issues"],
                "warnings": attestation["warnings"],
            }],
            "summary": {
                "blocking_issue_count": len(attestation["blocking_issues"]),
                "warning_count": len(attestation["warnings"]),
                **attestation["counts"],
            },
            "rule_change_candidates": [],
            "contract_errors": [],
            "reviewer_thread_id": attestation["reviewer_thread_id"],
            "reviewer_output_sha256": attestation["reviewer_output_sha256"],
            "reviewed_at": attestation["reviewed_at"],
            "user_authorization": attestation["user_authorization"],
            "review_attestation_path": str(normalized_attestation_path.resolve()),
            "review_attestation_sha256": sha256_file(normalized_attestation_path),
            "review_attestation_source_sha256": sha256_file(attestation_path),
        }
    else:
        report_path = state.run_dir / "human_review_report.json"
        try:
            report = analyze_review_workbooks([workbook_path])
        except ReviewWorkbookError as exc:
            report = {
                "schema_version": "mplus-human-review/v1",
                "status": "invalid",
                "human_review_status": "invalid",
                "projects": [],
                "summary": {},
                "rule_change_candidates": [],
                "contract_errors": [str(exc)],
            }
        except OSError as exc:
            _abort(
                state,
                stage_id,
                StageStatus.FAILED,
                StageErrorCategory.SYSTEM,
                "human_review_read_failed",
                f"人工复核产物读取失败: {exc}",
                retryable=True,
            )
        human_status = str(report.get("human_review_status") or "invalid")
        quality_status = human_status
        reviewer_kind = "human"
        report["schema_version"] = "mplus-human-review/v1"

    contract_errors = list(report.get("contract_errors") or [])
    stage_status = _quality_stage_status(quality_status)
    report.update({
        "machine_audit_path": str(audit_path.resolve()),
        "machine_audit_sha256": sha256_file(audit_path),
        "review_workbook_path": str(workbook_path.resolve()),
        "review_workbook_sha256": sha256_file(workbook_path),
        "review_html_path": str(html_path.resolve()) if html_path is not None else "",
        "human_review_status": human_status,
        "quality_review_status": quality_status,
        "reviewer_kind": reviewer_kind,
        "stage_status": stage_status.value,
        "contract_errors": contract_errors,
    })
    try:
        write_json_atomic(report_path, report)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "human_review_report_write_failed",
            f"人工复核报告写出失败: {exc}",
            retryable=True,
        )

    output_refs = [str(report_path.resolve()), str(workbook_path.resolve())]
    if html_path is not None and html_path.is_file():
        output_refs.append(str(html_path.resolve()))
    if attestation is not None:
        output_refs.append(report["review_attestation_path"])
    output_sha256 = sha256_json({
        "report": sha256_file(report_path),
        "workbook": sha256_file(workbook_path),
        "review_html": sha256_file(html_path) if html_path is not None and html_path.is_file() else None,
        "review_attestation": report.get("review_attestation_sha256"),
    })
    error = None
    if stage_status == StageStatus.BLOCKED:
        error = StageError(
            category=StageErrorCategory.CONTRACT,
            code="quality_review_contract_blocked",
            message=_quality_block_message(quality_status, reviewer_kind, contract_errors),
            retryable=False,
        )
    finish_pipeline_stage(
        state,
        stage_id,
        stage_status,
        output_sha256=output_sha256,
        output_refs=output_refs,
        error=error,
    )
    return HumanReviewArtifact(
        workbook_path=workbook_path,
        audit_path=audit_path,
        report_path=report_path,
        review_html_path=html_path,
        human_review_status=human_status,
        quality_review_status=quality_status,
        reviewer_kind=reviewer_kind,
        stage_status=stage_status.value,
        report=report,
    )


def run_publish_feedback_stage(
    state,
    *,
    machine_audit_path: str | Path,
    human_review_report_path: str | Path,
    release_artifacts: Mapping[str, str | Path],
    publisher: Callable[[Path, dict], None],
) -> PublishFeedbackArtifact:
    stage_id = "publish_feedback"
    audit_path = Path(machine_audit_path)
    human_report_path = Path(human_review_report_path)
    artifact_paths = {name: Path(path) for name, path in release_artifacts.items()}
    input_refs = [
        str(audit_path.resolve()),
        str(human_report_path.resolve()),
        *(str(path.resolve()) for path in artifact_paths.values()),
    ]
    input_sha256 = sha256_json({
        "machine_audit": _file_sha256(audit_path),
        "human_review": _file_sha256(human_report_path),
        "release_artifacts": {
            name: _file_sha256(path)
            for name, path in sorted(artifact_paths.items())
        },
    })
    record = start_pipeline_stage(
        state,
        stage_id,
        input_sha256=input_sha256,
        input_refs=input_refs,
    )
    reused = _reused_output(state, record)
    if reused is not None:
        feedback = json.loads(reused.read_text(encoding="utf-8"))
        return PublishFeedbackArtifact(reused, str(feedback["action"]), feedback)

    try:
        audit = json.loads(audit_path.read_text(encoding="utf-8"))
        human_report = json.loads(human_report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "publish_feedback_input_read_failed",
            f"发布反馈输入读取失败: {exc}",
            retryable=True,
        )
    if audit.get("status") != "pass":
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.DEPENDENCY,
            "machine_gate_not_passed",
            "机器审计门未通过，不能生成发布反馈",
        )
    quality_status = str(
        human_report.get("quality_review_status")
        or human_report.get("human_review_status")
        or "invalid"
    )
    reviewer_kind = str(human_report.get("reviewer_kind") or "human")
    if reviewer_kind not in {"human", "independent_ai"}:
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.CONTRACT,
            "quality_reviewer_kind_invalid",
            f"质量复核身份无效: {reviewer_kind}",
        )
    if quality_status != "quality_pass":
        _abort(
            state,
            stage_id,
            StageStatus.BLOCKED,
            StageErrorCategory.DEPENDENCY,
            "quality_gate_not_passed",
            "质量复核门未通过，不能生成发布反馈",
        )
    if reviewer_kind == "independent_ai":
        attestation_ref = Path(str(human_report.get("review_attestation_path") or ""))
        if (
            human_report.get("human_review_status") != "not_performed"
            or not attestation_ref.is_file()
            or sha256_file(attestation_ref) != human_report.get("review_attestation_sha256")
        ):
            _abort(
                state,
                stage_id,
                StageStatus.BLOCKED,
                StageErrorCategory.CONTRACT,
                "independent_review_evidence_invalid",
                "独立 AI 复核证据缺失或不一致",
            )

    candidates = list(human_report.get("rule_change_candidates") or [])
    action = "candidate_review_required" if candidates else "no_op"
    feedback = {
        "schema_version": "mplus-publish-feedback/v1",
        "action": action,
        "automatic_rule_update": False,
        "human_confirmation_required": bool(candidates),
        "rule_change_candidates": candidates,
        "quality_review_status": quality_status,
        "reviewer_kind": reviewer_kind,
        "machine_audit_ref": str(audit_path.resolve()),
        "quality_review_ref": str(human_report_path.resolve()),
        "human_review_ref": str(human_report_path.resolve()) if reviewer_kind == "human" else "",
        "release_artifact_refs": {
            name: str(path.resolve())
            for name, path in sorted(artifact_paths.items())
        },
    }
    output_path = state.run_dir / "publish_feedback.json"
    try:
        write_json_atomic(output_path, feedback)
    except OSError as exc:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "publish_feedback_write_failed",
            f"发布反馈写出失败: {exc}",
            retryable=True,
        )
    completion = {
        "stage_id": stage_id,
        "output_sha256": sha256_file(output_path),
        "output_refs": (str(output_path.resolve()),),
    }
    try:
        publisher(output_path, completion)
    except Exception as exc:
        failed_record = _existing_stage_record(state, stage_id)
        if failed_record.get("status") == StageStatus.FAILED.value:
            payload = failed_record.get("error") or {}
            error = StageError(
                category=StageErrorCategory(str(payload.get("category") or "system")),
                code=str(payload.get("code") or "publish_feedback_publish_failed"),
                message=str(payload.get("message") or exc),
                retryable=bool(payload.get("retryable")),
            )
            raise AutoPipelineStageError(stage_id, StageStatus.FAILED.value, error) from exc
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "publish_feedback_publish_failed",
            f"正式 bundle 原子发布失败: {exc}",
            retryable=True,
        )
    completed_record = _existing_stage_record(state, stage_id)
    if completed_record.get("status") != StageStatus.SUCCEEDED.value:
        _abort(
            state,
            stage_id,
            StageStatus.FAILED,
            StageErrorCategory.SYSTEM,
            "publish_feedback_publisher_contract_invalid",
            "publisher 返回后 publish_feedback 未完成",
            retryable=True,
        )
    return PublishFeedbackArtifact(output_path, action, feedback)


def _existing_stage_record(state, stage_id: str) -> dict:
    manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
    return dict(manifest["stages"][stage_id])


def _read_human_review_artifact(
    state,
    record: dict,
    *,
    workbook_path: Path,
    audit_path: Path,
    review_html_path: Path | None,
) -> HumanReviewArtifact:
    refs = record.get("output_refs") or []
    if not refs:
        raise RuntimeError("human_review 已有状态但缺少 output_refs")
    report_path = resolve_run_local_path(state.run_dir, refs[0])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return HumanReviewArtifact(
        workbook_path=workbook_path,
        audit_path=audit_path,
        report_path=report_path,
        review_html_path=review_html_path,
        human_review_status=str(report.get("human_review_status") or "invalid"),
        quality_review_status=str(
            report.get("quality_review_status")
            or report.get("human_review_status")
            or "invalid"
        ),
        reviewer_kind=str(report.get("reviewer_kind") or "human"),
        stage_status=str(record.get("status") or ""),
        report=report,
    )


def _reused_output(state, record: dict) -> Path | None:
    if record.get("status") not in {
        StageStatus.SUCCEEDED.value,
        StageStatus.SUCCEEDED_WITH_WARNINGS.value,
    }:
        return None
    refs = record.get("output_refs") or []
    if not refs:
        raise RuntimeError(f"{record.get('stage_id')} 已成功但缺少 output_refs")
    return resolve_run_local_path(state.run_dir, refs[0])


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    return sha256_file(path)


def _quality_stage_status(quality_status: str) -> StageStatus:
    if quality_status == "pending":
        return StageStatus.WAITING_HUMAN_REVIEW
    if quality_status == "quality_pass":
        return StageStatus.SUCCEEDED
    return StageStatus.BLOCKED


def _quality_block_message(
    quality_status: str,
    reviewer_kind: str,
    contract_errors: list[str],
) -> str:
    label = "独立 AI 复核" if reviewer_kind == "independent_ai" else "人工复核"
    if contract_errors:
        return f"{label}合同无效: {'; '.join(contract_errors)}"
    if quality_status == "quality_fail":
        return f"{label}完整，但质量结论未通过"
    return f"{label}合同无效: {quality_status}"


def _validate_independent_review_attestation(
    state,
    *,
    attestation_path: Path,
    workbook_path: Path,
    audit_path: Path,
) -> dict:
    try:
        attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewAttestationError(f"独立 AI 复核凭证读取失败: {exc}") from exc
    if not isinstance(attestation, dict):
        raise ReviewAttestationError("独立 AI 复核凭证必须是 JSON 对象")

    errors = []
    if attestation.get("schema_version") != "mplus-quality-review-attestation/v1":
        errors.append("schema_version 无效")
    if attestation.get("reviewer_kind") != "independent_ai":
        errors.append("reviewer_kind 必须为 independent_ai")
    if str(attestation.get("run_id") or "") != state.run_id:
        errors.append("run_id 与当前运行不一致")
    manifest = json.loads(state.manifest_path.read_text(encoding="utf-8"))
    project_name = str(manifest.get("project_name") or "")
    if str(attestation.get("project_name") or "") != project_name:
        errors.append("project_name 与当前运行不一致")
    if attestation.get("source_workbook_sha256") != sha256_file(workbook_path):
        errors.append("源工作簿哈希不一致")
    if attestation.get("machine_audit_sha256") != sha256_file(audit_path):
        errors.append("机器审计哈希不一致")
    if attestation.get("source_manifest_sha256") != sha256_file(state.manifest_path):
        errors.append("run manifest 哈希不一致")

    reviewer_thread_id = str(attestation.get("reviewer_thread_id") or "").strip()
    if not reviewer_thread_id:
        errors.append("缺少 reviewer_thread_id")
    output_hash = str(attestation.get("reviewer_output_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", output_hash):
        errors.append("reviewer_output_sha256 无效")
    if not _valid_iso_datetime(str(attestation.get("reviewed_at") or "")):
        errors.append("reviewed_at 无效")

    authorization = attestation.get("user_authorization")
    if not isinstance(authorization, dict) or authorization.get("authorized") is not True:
        errors.append("缺少用户明确授权")
    elif not str(authorization.get("authorized_at") or "").strip():
        errors.append("用户授权日期缺失")

    verdict = str(attestation.get("verdict") or "")
    if verdict not in {"quality_pass", "needs_revision", "reject"}:
        errors.append("verdict 无效")
    blocking_issues = attestation.get("blocking_issues")
    warnings = attestation.get("warnings")
    if not isinstance(blocking_issues, list):
        errors.append("blocking_issues 必须是数组")
        blocking_issues = []
    if not isinstance(warnings, list):
        errors.append("warnings 必须是数组")
        warnings = []
    if verdict == "quality_pass" and blocking_issues:
        errors.append("quality_pass 不允许存在 blocking_issues")
    if verdict in {"needs_revision", "reject"} and not blocking_issues:
        errors.append(f"{verdict} 必须给出 blocking_issues")

    actual_counts = _workbook_contract_counts(workbook_path)
    if attestation.get("counts") != actual_counts:
        errors.append(f"动态计数不一致: expected={actual_counts}")
    if actual_counts["shot_count"] < 1:
        errors.append("镜数必须大于 0")
    if actual_counts["adjacent_edge_count"] != actual_counts["shot_count"] - 1:
        errors.append("相邻关系必须为 N-1")
    if actual_counts["full_film_count"] != 1:
        errors.append("整片复核必须为 1")
    if actual_counts["source_triplet_count"] != actual_counts["shot_count"]:
        errors.append("来源三元组必须为 N/N")
    if errors:
        raise ReviewAttestationError("; ".join(errors))
    return attestation


def _workbook_contract_counts(path: Path) -> dict:
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return {
            "shot_count": _nonempty_row_count(workbook, REVIEW_SHEET_NAME, "镜号"),
            "adjacent_edge_count": _nonempty_row_count(
                workbook, ADJACENT_REVIEW_SHEET_NAME, "边"
            ),
            "full_film_count": _nonempty_row_count(
                workbook, FULL_FILM_REVIEW_SHEET_NAME, "项目名称"
            ),
            "source_triplet_count": _source_triplet_count(workbook),
        }
    finally:
        workbook.close()


def _nonempty_row_count(workbook, sheet_name: str, key: str) -> int:
    from .excel_io import read_managed_sheet_rows

    return sum(row.get(key) not in (None, "") for row in read_managed_sheet_rows(workbook, sheet_name))


def _source_triplet_count(workbook) -> int:
    from .excel_io import read_managed_sheet_rows

    columns = ("采用来源文件", "采用来源 sheet", "采用来源行")
    return sum(
        all(row.get(column) not in (None, "") for column in columns)
        for row in read_managed_sheet_rows(workbook, "抽调明细")
    )


def _attestation_quality_status(verdict: str) -> str:
    return "quality_pass" if verdict == "quality_pass" else "quality_fail"


def _valid_iso_datetime(value: str) -> bool:
    if not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _invalid_review_dates(path: Path) -> list[str]:
    from .excel_io import read_managed_sheet_rows

    workbook = load_workbook(path, read_only=True, data_only=True)
    errors = []
    try:
        for sheet_name in (
            REVIEW_SHEET_NAME,
            ADJACENT_REVIEW_SHEET_NAME,
            FULL_FILM_REVIEW_SHEET_NAME,
        ):
            if sheet_name not in workbook.sheetnames:
                continue
            for excel_row, row in enumerate(read_managed_sheet_rows(workbook, sheet_name), start=2):
                if not any(value not in (None, "") for value in row.values()):
                    continue
                value = row.get("审核日期")
                if value in (None, ""):
                    continue
                if not _valid_review_date(value):
                    errors.append(f"{sheet_name}!审核日期{excel_row} 无效")
    finally:
        workbook.close()
    return errors


def _valid_review_date(value) -> bool:
    if isinstance(value, (datetime, date)):
        return True
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            datetime.strptime(text, pattern)
            return True
        except ValueError:
            continue
    return False


def _abort(
    state,
    stage_id: str,
    status: StageStatus,
    category: StageErrorCategory,
    code: str,
    message: str,
    *,
    retryable: bool = False,
) -> None:
    error = StageError(
        category=category,
        code=code,
        message=message,
        retryable=retryable,
    )
    finish_pipeline_stage(state, stage_id, status, error=error)
    raise AutoPipelineStageError(stage_id, status.value, error)
