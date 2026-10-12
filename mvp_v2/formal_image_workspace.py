"""Formal per-shot image confirmation and provider-free child versions.

The employee boundary exposes only business image choices.  Storage paths,
artifact references, hashes and actor identifiers remain server-side.  Every
save is a complete immutable N-shot decision snapshot and creates one child
version; it never publishes or writes CURRENT.
"""

from __future__ import annotations

import base64

from copy import deepcopy
from dataclasses import dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable

from openpyxl import load_workbook
from openpyxl.drawing.image import Image as ExcelImage

from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .audit import add_audit_checks, audit_output_workbook_format
from .excel_io import (
    ADJACENT_REVIEW_SHEET_NAME,
    scrub_workbook_paths,
    write_template_output_workbook,
)
from .generation_stages import (
    read_script_draft,
    read_script_plan,
    read_visual_binding,
    run_machine_audit_stage,
    run_workbook_render_stage,
)
from .formal_image_candidates import (
    allocate_diverse_defaults,
    build_match_explanation,
    candidate_snapshot_digest,
    perceptual_cluster_ids,
    search_project_candidates,
)
from .identity_service import require_permission
from .loop_runner import audit_generation_content
from .mutation_policy import require_active_project
from .models import parse_positive_seconds_strict, validate_script_brief
from .pipeline_runtime import finalize_pipeline_run, fork_pipeline_run
from .project_image_authority import (
    ArtifactEvidence,
    ProjectImageDeliveryAsset,
    ProjectImageDeliveryPool,
    ProjectImagePoolService,
)
from .project_service import ProjectService
from .revision_service import build_revision_report, compare_versions, version_shots_from_artifacts
from .run_contract import sha256_file, sha256_json
from .run_state import write_json_atomic
from .script_type_adapter import normalize_script_type, resolve_script_type_workbook
from .stage_contract import StageStatus
from .stage_runtime import finish_pipeline_stage, start_pipeline_stage
from .task_service import TaskService
from .version_delivery import VersionDeliveryContext, VersionDeliveryService, resolve_version_shot_ids
from .version_review_repository import open_script_draft_id, require_no_unsaved_script_draft
from .visual_function import (
    annotate_visual_function_fields,
    ALLOWED_FUNCTIONS_BY_SLOT,
    EXPECTED_FUNCTION_BY_SLOT,
    desired_visual_functions,
)
from .visual_assets import apply_visual_analysis
from .asset_package import load_asset_package
from .package_registry import LocalPackageRegistry, PackageRegistryError
from .script_type_adapter import category_id_for_script_type
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    check_version,
    enqueue_outbox,
    new_id,
    utc_now,
)


@dataclass(frozen=True)
class ImageShotSelection:
    shot_no: str
    image_key: str = ""
    confirmed: bool = False
    decision_kind: str = "image_confirmed"
    temporary_reason: str = ""
    candidate_snapshot_digest: str = ""
    query: str = ""
    reuse_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.shot_no, str) or not self.shot_no.strip():
            raise WorkspaceServiceError("INVALID_INPUT", "镜号不能为空")
        if self.confirmed is not True:
            raise WorkspaceServiceError("IMAGE_CONFIRMATION_REQUIRED", "每镜图片都需要人工确认")
        if self.decision_kind == "image_confirmed":
            if not isinstance(self.image_key, str) or not self.image_key.strip():
                raise WorkspaceServiceError("INVALID_INPUT", "图片选择不能为空")
            if str(self.temporary_reason or "").strip():
                raise WorkspaceServiceError("INVALID_INPUT", "已确认图片不能同时填写暂准理由")
        elif self.decision_kind == "temporary_text_basis":
            if str(self.image_key or "").strip():
                raise WorkspaceServiceError("INVALID_INPUT", "暂准文字依据不得包含图片身份")
            if not str(self.temporary_reason or "").strip():
                raise WorkspaceServiceError("INVALID_INPUT", "暂准文字依据必须填写理由")
        else:
            raise WorkspaceServiceError("INVALID_INPUT", "图片决定类型无效")
        if self.decision_kind == "temporary_text_basis" and str(self.reuse_reason or "").strip():
            raise WorkspaceServiceError("INVALID_INPUT", "暂准文字依据不得填写图片复用理由")


@dataclass(frozen=True)
class FormalImageWorkspaceServices:
    store: WorkspaceStore
    projects: ProjectService
    delivery: VersionDeliveryService
    artifacts: RunArtifactResolver
    images: ProjectImagePoolService


class ImageVersionMaterializer:
    def __init__(
        self,
        *,
        artifacts: RunArtifactResolver,
        runs_root: str | Path,
        source_root: str | Path,
        source_root_resolver: Callable[[str], Path] | None = None,
        package_registry: LocalPackageRegistry | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.runs_root = Path(runs_root).expanduser().resolve()
        self.source_root = Path(source_root).expanduser().resolve()
        self.source_root_resolver = source_root_resolver
        self.package_registry = package_registry

    def materialize(
        self,
        *,
        context: VersionDeliveryContext,
        child_version_id: str,
        snapshot_id: str,
        decisions: list[dict[str, Any]],
        request_sha256: str,
        created_by_actor_id: str,
        created_at: str,
    ) -> dict[str, str]:
        plan_ref = self.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        )
        draft_ref = self.artifacts.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256,
        )
        binding_ref = self.artifacts.resolve_stage_artifact(
            context.run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256,
        )
        try:
            parent_workbook = self.artifacts.resolve_artifact(context.run_id, "workbook").path
        except ArtifactResolutionError as exc:
            if exc.code != "NOT_FOUND":
                raise
            parent_workbook = None
        plan = read_script_plan(plan_ref.path)
        package_evidence = plan.constraints.get("formal_asset_package")
        if package_evidence and self.package_registry is not None:
            source_root = self._source_root_for(plan.brief.get("脚本类型") or "发布会", package_evidence)
        else:
            try:
                source_root = self.artifacts.resolve_template_workbook(
                    context.run_id, expected_manifest_sha256=context.manifest.sha256,
                )
            except ArtifactResolutionError as exc:
                raise WorkspaceServiceError(
                    "ASSET_PACKAGE_DRIFT", "已保存版本的来源工作簿缺失或内容已变化，请恢复原版本来源后重试",
                ) from exc
        draft = read_script_draft(draft_ref.path)
        parent_binding = read_visual_binding(binding_ref.path)
        shot_nos = [_shot_no(row) for row in draft.script_rows]
        if (
            not shot_nos
            or len(set(shot_nos)) != len(shot_nos)
            or shot_nos != [_shot_no(row) for row in parent_binding.script_rows]
            or shot_nos != [str(item["shot_no"]) for item in decisions]
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "逐镜图片决定与当前脚本不一致")
        child_bindings = [self._binding(decision, index) for index, decision in enumerate(decisions, start=1)]
        temporary_count = sum(
            item["decision_kind"] == "temporary_text_basis" for item in decisions
        )
        report = self._revision_report(
            context=context,
            child_version_id=child_version_id,
            script_rows=draft.script_rows,
            detail_rows=draft.detail_rows,
            parent_bindings=parent_binding.bindings,
            child_bindings=child_bindings,
            request_sha256=request_sha256,
            identities=resolve_version_shot_ids(self.artifacts, context, draft.script_rows),
        )
        paths = {
            str(decision["shot_no"]): Path(decision["controlled_path"]).resolve()
            for decision in decisions
            if decision["decision_kind"] == "image_confirmed"
        }
        cache_root = self.runs_root / context.project_id
        with fork_pipeline_run(
            context.run_id,
            cache_root,
            brief=plan.brief,
            source_root=source_root,
            cache_fingerprint=sha256_json({
                "route": "formal-manual-image-decisions",
                "parent_manifest_sha256": context.manifest.sha256,
                "request_sha256": request_sha256,
                "image_decisions": [
                    {
                        "shot_no": item["shot_no"],
                        "decision_kind": item["decision_kind"],
                        "image_sha256": item.get("image_sha256") or "",
                        "analysis_sha256": item.get("analysis_sha256") or "",
                        "temporary_reason": item.get("temporary_reason") or "",
                    }
                    for item in decisions
                ],
            }),
            from_stage="visual_binding",
        ) as state:
            binding_path = self._write_binding_stage(
                state,
                parent=binding_ref.path,
                script_plan_path=state.run_dir / "script_plan.json",
                script_draft_path=state.run_dir / "script_draft.json",
                child_bindings=child_bindings,
                decisions=decisions,
                request_sha256=request_sha256,
            )
            decision_path = state.run_dir / "image_decisions.json"
            write_json_atomic(decision_path, {
                "schema_version": "mplus-version-image-decisions/v3",
                "snapshot_id": snapshot_id,
                "workspace_id": context.workspace_id,
                "project_id": context.project_id,
                "base_version_id": context.version_id,
                "materialized_version_id": child_version_id,
                "run_id": state.run_id,
                "shot_count": len(decisions),
                "confirmed_count": len(decisions) - temporary_count,
                "image_confirmed_count": len(decisions) - temporary_count,
                "temporary_text_basis_count": temporary_count,
                "request_sha256": request_sha256,
                "created_by_actor_id": created_by_actor_id,
                "created_at": created_at,
                "decisions": [self._artifact_decision(item) for item in decisions],
            })
            excluded_image_ids = list(dict.fromkeys(
                str(item.get("excluded_image_id") or "")
                for item in decisions if item.get("excluded_image_id")
            ))
            preferred_image_ids = list(dict.fromkeys(
                str(item.get("image_id") or "")
                for item in decisions
                if item.get("excluded_image_id") and item.get("image_id")
            ))
            feedback_path = None
            if excluded_image_ids:
                feedback_path = state.run_dir / "image_feedback.json"
                write_json_atomic(feedback_path, {
                    "schema_version": "mplus-formal-image-feedback/v2",
                    "project_id": context.project_id,
                    "base_version_id": context.version_id,
                    "child_version_id": child_version_id,
                    "image_feedback": {
                        "excluded_image_ids": excluded_image_ids,
                        "preferred_image_ids": preferred_image_ids,
                        "forced_image_id": "",
                        "shot_policies": [
                            {
                                "shot_no": str(item["shot_no"]),
                                "excluded_image_ids": [str(item["excluded_image_id"])],
                                "preferred_image_ids": [str(item["image_id"])],
                                "forced_image_id": "",
                            }
                            for item in decisions
                            if item.get("excluded_image_id") and item.get("image_id")
                        ],
                    },
                    "created_by_actor_id": created_by_actor_id,
                    "created_at": created_at,
                })
            run_report = state.run_dir / "run_report.json"
            write_json_atomic(run_report, {
                "schema_version": "mplus-run-report/v1",
                "run_id": state.run_id,
                "parent_run_id": context.run_id,
                "machine_audit_status": "not_run" if temporary_count else "pass",
                "human_review_status": "blocked" if temporary_count else "pending",
                "image_decision_status": "temporary_text_basis" if temporary_count else "all_shots_confirmed",
                "release_decision": "blocked",
                "status": "images_pending" if temporary_count else "waiting_human_review",
                "provider_calls": 0,
            })
            revision_report_path = state.run_dir / "revision_report.json"
            write_json_atomic(revision_report_path, report)
            artifacts = {
                "run_report": run_report,
                "revision_report": revision_report_path,
                "image_decisions": decision_path,
            }
            if feedback_path is not None:
                artifacts["image_feedback"] = feedback_path
            if not temporary_count:
                binding = read_visual_binding(binding_path)
                renderer, auditor = self._workbook_functions(
                    binding, paths, source_root,
                    template_workbook_path=source_root,
                    parent_workbook_path=parent_workbook,
                )
                rendered = run_workbook_render_stage(
                    state, visual_binding_path=binding_path, renderer=renderer,
                )
                final_workbook = state.run_dir / "formal-script.xlsx"
                audit_path = state.run_dir / "formal-script.audit.json"
                audited = run_machine_audit_stage(
                    state,
                    visual_binding_path=binding_path,
                    workbook_render_path=rendered.path,
                    output_path=final_workbook,
                    audit_path=audit_path,
                    auditor=auditor,
                    renderer=renderer,
                )
                if audited.audit.get("status") != "pass":
                    raise WorkspaceServiceError(
                        "IMAGE_VERSION_AUDIT_FAILED", "逐镜图片版本未通过机器校验",
                    )
                _verify_workbook(final_workbook, decisions, paths)
                report_payload = _read_json(run_report, "运行报告")
                report_payload.update({
                    "machine_audit_status": audited.audit["status"],
                    "machine_audit": audited.audit,
                })
                write_json_atomic(run_report, report_payload)
                artifacts.update({"workbook": audited.output_path, "audit": audited.audit_path})
            finalize_pipeline_run(
                state,
                "completed",
                artifacts,
                metrics={
                    "provider_calls": 0,
                    "image_decision_count": len(decisions),
                    "manual_image_confirmation": True,
                },
            )
            run_id = state.run_id
        manifest = self.artifacts.resolve_manifest(run_id)
        snapshot = self.artifacts.resolve_artifact(run_id, "image_decisions")
        revision = self.artifacts.resolve_artifact(run_id, "revision_report")
        feedback = (
            self.artifacts.resolve_artifact(run_id, "image_feedback")
            if any(item.get("excluded_image_id") for item in decisions) else None
        )
        return {
            "run_id": run_id,
            "manifest_ref": manifest.ref,
            "manifest_sha256": manifest.sha256,
            "artifact_ref": (
                self.artifacts.resolve_artifact(run_id, "workbook").ref
                if not temporary_count else snapshot.ref
            ),
            "content_sha256": (
                self.artifacts.resolve_artifact(run_id, "workbook").sha256
                if not temporary_count else snapshot.sha256
            ),
            "snapshot_ref": snapshot.ref,
            "snapshot_sha256": snapshot.sha256,
            "revision_report_ref": revision.ref,
            "revision_report_sha256": revision.sha256,
            "feedback_ref": feedback.ref if feedback else "",
            "feedback_sha256": feedback.sha256 if feedback else "",
        }

    @staticmethod
    def _binding(decision: dict[str, Any], index: int) -> dict[str, Any]:
        if decision["decision_kind"] == "temporary_text_basis":
            return {
                "slot_index": index,
                "shot_no": str(decision["shot_no"]),
                "image_id": "",
                "image_sha256": "",
                "original_path": "",
                "decision": "暂准文字依据",
                "resolution_status": "unresolved",
                "document_basis_available": True,
                "recommended_action": "补充或确认当前镜头图片后再审核交付",
                "basis": str(decision["temporary_reason"]),
                "decision_kind": "temporary_text_basis",
                "decision_id": str(decision["decision_id"]),
                "decided_by_actor_id": str(decision["decided_by_actor_id"]),
                "decided_at": str(decision["decided_at"]),
            }
        return {
            "slot_index": index,
            "shot_no": str(decision["shot_no"]),
            "image_id": str(decision["image_id"]),
            "image_sha256": str(decision["image_sha256"]),
            "original_path": "",
            "decision": "人工确认",
            "resolution_status": "resolved",
            "document_basis_available": True,
            "recommended_action": "已确认当前项目图片",
            "basis": "项目经理逐镜人工确认",
            "analysis_version_id": str(decision["analysis_version_id"]),
            "analysis_ref": str(decision["analysis_ref"]),
            "analysis_sha256": str(decision["analysis_sha256"]),
            "decision_id": str(decision["decision_id"]),
            "decided_by_actor_id": str(decision["decided_by_actor_id"]),
            "decided_at": str(decision["decided_at"]),
            "source_kind": str(decision["source_kind"]),
            "source_key": str(decision["source_key"]),
            "source_location": deepcopy(decision["source_location"]),
            "decision_kind": "image_confirmed",
        }

    @staticmethod
    def _artifact_decision(decision: dict[str, Any]) -> dict[str, Any]:
        common = {
            key: deepcopy(decision[key])
            for key in (
                "decision_id", "shot_no", "decision_kind",
                "decided_by_actor_id", "decided_at",
            )
        }
        if decision["decision_kind"] == "temporary_text_basis":
            return {**common, "temporary_reason": decision["temporary_reason"]}
        return {
            **common,
            **{
                key: deepcopy(decision[key])
                for key in (
                    "image_id", "image_sha256", "analysis_version_id",
                    "analysis_ref", "analysis_sha256", "source_kind",
                    "source_key", "source_location", "reuse_reason",
                )
            },
        }

    @staticmethod
    def _write_binding_stage(
        state,
        *,
        parent: Path,
        script_plan_path: Path,
        script_draft_path: Path,
        child_bindings: list[dict[str, Any]],
        decisions: list[dict[str, Any]],
        request_sha256: str,
        working_script_rows: list[dict] | None = None,
        working_detail_rows: list[dict] | None = None,
    ) -> Path:
        payload = _read_json(parent, "父 VisualBinding")
        if working_script_rows is not None:
            child_plan = _read_json(script_plan_path, "子 ScriptPlan")
            for key in ("decisions", "template_slots", "traceability", "constraints"):
                payload[key] = deepcopy(child_plan.get(key) or ([] if key != "constraints" else {}))
            payload["shot_count"] = len(working_script_rows)
            payload["script_rows"] = deepcopy(working_script_rows)
            payload["detail_rows"] = deepcopy(working_detail_rows)
        script_rows = list(payload.get("script_rows") or [])
        detail_rows = list(payload.get("detail_rows") or [])
        if len(script_rows) != len(decisions) or len(detail_rows) != len(decisions):
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "逐镜图片决定数量与脚本不一致")
        visual_rows = []
        analysis_rows = []
        analysis_by_id = {}
        for index, decision in enumerate(decisions, start=1):
            if decision["decision_kind"] == "temporary_text_basis":
                visual_rows.append({
                    "visual_asset_id": "",
                    "image_id": "",
                    "media_id": "",
                    "content_sha256": "",
                    "slot_index": index,
                    "镜号": decision["shot_no"],
                    "图片角色": "暂准文字依据",
                    "来源文件": "",
                    "来源 sheet": "",
                    "来源行": "",
                    "绑定方式": "人工暂准",
                    "视觉读取状态": "待补图",
                    "配图置信度": "未完成",
                    "配图判断": "待补图",
                    "视觉内容摘要": decision["temporary_reason"],
                    "视觉功能标签": "",
                    "质量指标": "",
                    "审核结果": "阻断交付",
                })
                continue
            source = decision["source_location"]
            source_key = str(decision.get("source_key") or "")
            media_id = (
                source_key.rsplit(":", 1)[-1]
                if source_key.startswith("package:") and ":" in source_key
                else str(decision["image_id"])
            )
            visual_rows.append({
                "visual_asset_id": decision["image_id"],
                "image_id": decision["image_id"],
                "media_id": media_id,
                "content_sha256": decision["image_sha256"],
                "slot_index": index,
                "镜号": decision["shot_no"],
                "图片角色": "逐镜确认图",
                "来源文件": source["file"],
                "来源 sheet": source.get("sheet", ""),
                "来源行": source.get("row", ""),
                "绑定方式": "人工确认",
                "视觉读取状态": "待核对",
                "配图置信度": "待核对",
                "配图判断": "待核对",
                "视觉内容摘要": decision["summary"],
                "视觉功能标签": "、".join(decision["tags"]),
                "质量指标": decision["quality"],
                "审核结果": "待复核",
            })
            analysis = deepcopy(decision.get("analysis") or {})
            origin = str(analysis.get("analysis_origin") or "")
            manual = origin == "manual" or analysis.get("schema_version") == "mplus-project-image-analysis/v1"
            analysis_row = {
                **analysis,
                "image_id": decision["image_id"],
                "caption": decision["summary"], "tags": list(decision["tags"]),
                "brand_names": [str(analysis["brand"])] if analysis.get("brand") else [],
                "source": "manual_seed" if manual else str(analysis.get("provider") or origin or "未记录分析来源"),
                "source_file": source["file"],
                "annotator": str(analysis.get("reviewed_by_actor_id") or ""),
                "review_status": "reviewed" if manual and analysis.get("reviewed_by_actor_id") else "",
            }
            analysis_rows.append(analysis_row)
            analysis_by_id[str(decision["image_id"])] = analysis_row
            visual_rows[-1]["quality_flags"] = list(analysis.get("quality_flags") or [])
        for detail, asset in zip(detail_rows, visual_rows):
            for key in ("视觉读取状态", "配图置信度", "视觉内容摘要", "视觉标签", "视觉匹配分", "配图判断",
                        "视觉功能标签", "主视觉功能", "槽位允许功能", "功能适配判断", "功能重复组"):
                detail[key] = asset.get(key, "")
            detail.update({
                "image_id": asset["image_id"], "视觉资产ID": asset["visual_asset_id"], "原图路径": "",
                "图片绑定方式": asset["绑定方式"], "图片绑定说明": "本次逐镜图片决定",
                "视觉质量指标": asset.get("质量指标", ""),
            })
        apply_visual_analysis(script_rows, detail_rows, visual_rows, analysis_by_id)
        annotate_visual_function_fields(detail_rows, visual_rows, str(payload.get("brief", {}).get("脚本类型") or "发布会"))
        start_pipeline_stage(
            state,
            "visual_binding",
            input_sha256=sha256_json({
                "parent_visual_binding_sha256": sha256_file(parent),
                "script_draft_sha256": sha256_file(script_draft_path),
                "request_sha256": request_sha256,
                "decisions": [
                    {
                        "shot_no": item["shot_no"],
                        "decision_kind": item["decision_kind"],
                        "image_sha256": item.get("image_sha256") or "",
                        "analysis_sha256": item.get("analysis_sha256") or "",
                        "temporary_reason": item.get("temporary_reason") or "",
                    }
                    for item in decisions
                ],
            }),
            input_refs=(str(script_draft_path.resolve()), str(parent.resolve())),
        )
        output = state.run_dir / "visual_binding.json"
        write_json_atomic(output, {
            **payload,
            "script_plan_ref": str(script_plan_path.resolve()),
            "script_draft_ref": str(script_draft_path.resolve()),
            "visual_asset_rows": visual_rows,
            "detail_rows": detail_rows,
            "image_analysis_rows": analysis_rows,
            "bindings": child_bindings,
            "image_decision_status": (
                "temporary_text_basis"
                if any(item["decision_kind"] == "temporary_text_basis" for item in decisions)
                else "all_shots_confirmed"
            ),
            "image_decision_request_sha256": request_sha256,
        })
        finish_pipeline_stage(
            state,
            "visual_binding",
            StageStatus.SUCCEEDED,
            output_sha256=sha256_file(output),
            output_refs=(str(output.resolve()),),
        )
        return output

    @staticmethod
    def _revision_report(
        *,
        context: VersionDeliveryContext,
        child_version_id: str,
        script_rows: list[dict],
        detail_rows: list[dict],
        parent_bindings: list[dict],
        child_bindings: list[dict],
        request_sha256: str,
        identities: dict[str, str] | None = None,
    ) -> dict:
        identities = identities or {
            _shot_no(row): f"shot_{sha256_json({'project_id': context.project_id, 'version_id': context.version_id, 'shot_no': _shot_no(row)})[:24]}"
            for row in script_rows
        }
        parent_shots = version_shots_from_artifacts(
            script_rows, detail_rows, bindings=parent_bindings, shot_ids_by_no=identities,
        )
        child_shots = version_shots_from_artifacts(
            script_rows, detail_rows, bindings=child_bindings, shot_ids_by_no=identities,
        )
        diff = compare_versions(
            project_id=context.project_id,
            parent_version_id=context.version_id,
            child_version_id=child_version_id,
            parent_shots=parent_shots,
            child_shots=child_shots,
        )
        changed = {
            (str(shot.get("child_shot_no") or ""), str(field.get("field") or ""))
            for shot in diff.get("shot_diffs") or []
            for field in shot.get("field_diffs") or []
            if field.get("status") != "unchanged"
        }
        image_fields = {
            "image_id", "original_path", "decision", "resolution_status", "basis",
        }
        if not changed or any(field not in image_fields for _, field in changed):
            raise WorkspaceServiceError("IMAGE_VERSION_SCOPE_INVALID", "图片子版本包含非图片变化或没有变化")
        locked = [
            {"shot_no": shot["shot_no"], "field": field}
            for shot in child_shots
            for field in shot.get("fields") or {}
            if field not in image_fields
        ]
        report = build_revision_report(
            diff=diff,
            original_feedback="逐镜人工确认项目图片",
            structured_intent={
                "scope": "manual_per_shot_image_decisions",
                "request_sha256": request_sha256,
                "confirmed_shot_nos": [_shot_no(row) for row in script_rows],
            },
            expected_impact={
                "shot_nos": sorted({shot for shot, _ in changed}, key=_shot_sort_key),
                "fields": sorted({field for _, field in changed}),
                "earliest_restart_stage": "visual_binding",
            },
            locked_fields=locked,
            rerun_nodes=["visual_binding", "workbook_render", "machine_audit"],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "waiting_human_review"},
            publication={"status": "unpublished"},
        )
        if report.get("completion", {}).get("status") != "waiting_human_review":
            raise WorkspaceServiceError("IMAGE_VERSION_SCOPE_INVALID", "图片子版本影响范围校验失败")
        return {**report, "version_diff": diff, "request_sha256": request_sha256}

    def _workbook_functions(
        self,
        binding,
        paths: dict[str, Path],
        source_root: Path,
        *,
        template_workbook_path: Path | None = None,
        parent_workbook_path: Path | None = None,
    ):
        script_type = normalize_script_type(binding.brief.get("脚本类型") or "发布会")
        template = template_workbook_path or resolve_script_type_workbook(source_root, script_type)
        validate_script_brief(binding.brief)
        duration_seconds = sum(
            parse_positive_seconds_strict(row.get("时长(秒)", "")) or 0
            for row in binding.script_rows
        )

        def renderer(current, target, checks):
            rows = []
            visual_rows = []
            for row in current.script_rows:
                shot_no = _shot_no(row)
                rows.append({**dict(row), "画面示意": str(paths[shot_no]), "效果示意": ""})
            for row in current.visual_asset_rows:
                shot_no = str(row.get("镜号") or row.get("slot_index") or "").strip()
                visual_rows.append({**dict(row), "原图路径": str(paths[shot_no])})
            write_template_output_workbook(
                target,
                rows,
                current.detail_rows,
                checks,
                template_workbook_path=template,
                visual_asset_rows=visual_rows,
                overall_summary=current.overall_summary,
                script_type=script_type,
                duration_seconds=duration_seconds,
                parent_workbook_path=parent_workbook_path,
                parent_shot_nos=[_shot_no(row) for row in current.script_rows],
            )
            _replace_main_images(target, rows, paths)
            scrub_workbook_paths(target, set(paths.values()))

        def auditor(current, workbook_path):
            checks = audit_output_workbook_format(
                workbook_path,
                expected_image_count=len(current.script_rows),
                template_workbook_path=parent_workbook_path or template,
                script_type=script_type,
                duration_seconds=duration_seconds,
            )
            return add_audit_checks(audit_generation_content(current, revision=True), checks)

        return renderer, auditor

    def _source_root_for(self, script_type: str, package_evidence: dict | None = None) -> Path:
        if package_evidence and self.package_registry is not None:
            try:
                record = self.package_registry.resolve_evidence(category_id_for_script_type(script_type), package_evidence)
                return Path(load_asset_package(record["local_path"])["source_workbook_path"])
            except (PackageRegistryError, ValueError, OSError) as exc:
                raise WorkspaceServiceError("ASSET_PACKAGE_DRIFT", "已保存脚本的模板不可用，请检查模板库") from exc
        if self.source_root_resolver is None:
            return self.source_root
        return Path(self.source_root_resolver(script_type)).expanduser().resolve()


class FormalImageWorkspaceService:
    def __init__(
        self,
        services: FormalImageWorkspaceServices,
        *,
        runs_root: str | Path,
        source_root: str | Path,
        source_root_resolver: Callable[[str], Path] | None = None,
        package_registry: LocalPackageRegistry | None = None,
        semantic_policy=None,
    ) -> None:
        self.services = services
        self.semantic_policy = semantic_policy
        self.semantic_cache_root = services.store.database_path.parent / 'image-semantic-matching'
        self.materializer = ImageVersionMaterializer(
            artifacts=services.artifacts,
            runs_root=runs_root,
            source_root=source_root,
            source_root_resolver=source_root_resolver,
            package_registry=package_registry,
        )
        self.services.store.initialize()

    def _progress_script(self, connection, context: VersionDeliveryContext) -> tuple[int, dict[str, dict]]:
        """Read the existing draft head; image progress never creates a text draft."""
        binding = read_visual_binding(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        identities = resolve_version_shot_ids(self.services.artifacts, context, binding.script_rows)
        draft_id = open_script_draft_id(connection, workspace_id=context.workspace_id,
                                      project_id=context.project_id, base_version_id=context.version_id)
        revision = 0
        if draft_id:
            head = connection.execute(
                """SELECT d.current_revision, s.shots_json FROM version_review_drafts d
                JOIN version_review_draft_heads h ON h.draft_id=d.draft_id
                JOIN version_review_draft_states s ON s.state_id=h.current_state_id
                WHERE d.draft_id=?""", (draft_id,),
            ).fetchone()
            if head is None:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "当前文字草稿缺少保存记录")
            revision, shots = int(head["current_revision"]), json.loads(head["shots_json"])
        else:
            shots = version_shots_from_artifacts(binding.script_rows, binding.detail_rows,
                                                shot_ids_by_no=identities)
        result = {}
        for shot in shots:
            identity = str(shot.get("shot_id") or identities.get(str(shot.get("origin_shot_no") or shot["shot_no"])) or "")
            if identity:
                result[identity] = shot
        return revision, result

    def working_image_context(self, context, plan, draft, binding=None):
        """Project the canonical saved draft onto image retrieval, without writes.

        Immutable artifact caches remain immutable. Sources and template context
        follow explicit origin identities when shots move, split or are added.
        """
        with self.services.store.connection() as connection:
            revision, shots = self._progress_script(connection, context)
        if not revision:
            return revision, shots, plan, draft, binding
        parent_rows = {_shot_no(row): row for row in draft.script_rows}
        parent_details = formal_image_details_by_script_shot(draft.script_rows, draft.detail_rows)
        parent_plans = {_shot_no(row): planned for row, planned in zip(draft.script_rows, plan.shot_plans)}
        parent_bindings = {str(row.get('shot_no') or row.get('slot_index') or ''): row
                           for row in binding.bindings} if binding else {}
        rows, details, plans, bindings = [], [], [], []
        for identity, shot in shots.items():
            no = str(shot['shot_no'])
            origin = str(shot.get('origin_shot_no') or no)
            rows.append({**deepcopy(parent_rows.get(origin, {})), **deepcopy(shot['fields']),
                         '镜号': no, 'shot_id': identity})
            detail = deepcopy(shot.get('source_detail_override') or parent_details.get(origin, {}))
            detail['source_refs'] = deepcopy(shot.get('source_refs') or [])
            details.append(detail)
            plans.append(deepcopy(parent_plans.get(origin, {})))
            if origin in parent_bindings:
                bindings.append({**deepcopy(parent_bindings[origin]), 'shot_no': no})
        return (revision, shots, replace(plan, shot_plans=plans),
                replace(draft, script_rows=rows, detail_rows=details),
                replace(binding, bindings=bindings) if binding else None)

    @staticmethod
    def _progress_basis(shot: dict) -> str:
        # Same picture-reconfirmation fields as the established revision core.
        fields = shot.get("fields") or {}
        return sha256_json({
            "fields": {key: str(fields.get(key) or "") for key in ("类型", "景别", "运镜", "画面描述")},
            "sources": [{key: str(value.get(key) or "") for key in ("source_file", "source_sheet", "source_row")}
                        for value in shot.get("source_refs") or []],
        })

    def _progress_project_references(self, auth_token: str, context: VersionDeliveryContext,
                                    *, similarity_items: list[dict] | None = None, historical: bool = False) -> dict[str, dict]:
        plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        brief_id, snapshot_id = self._brief_binding(context, plan.confirmed_brief_input, require_current=not historical)
        readiness = self.services.images.resolve_for_planning(
            auth_token, workspace_id=context.workspace_id, project_id=context.project_id,
            brief_version_id=brief_id, brief_snapshot_id=snapshot_id,
            expected_project_lock_version=context.project_lock_version, historical=historical,
        )
        public = {item["image_key"]: item for item in self.services.images.employee_readiness(readiness)["images"]}
        if similarity_items is not None:
            pool = self.services.images.resolve_for_delivery(auth_token, workspace_id=context.workspace_id,
                project_id=context.project_id, brief_version_id=brief_id, brief_snapshot_id=snapshot_id,
                expected_project_lock_version=context.project_lock_version, historical=historical)
            similarity_items.extend({"image_id": "project:" + asset.image_id,
                "path": asset.controlled_path, "perceptual_hash": asset.business_summary.perceptual_hash}
                for asset in pool.assets)
        references = {}
        for asset in readiness.images:
            item = public[asset.image_id]
            summary = asset.business_summary
            media_ids = sorted({source.source_document_id.rsplit(":", 1)[-1] for source in asset.source_links
                                if source.source_document_id.startswith("package:")})
            candidate = {
                "image_key": asset.image_id, "content_sha256": asset.image_sha256,
                "media_id": media_ids[0] if len(media_ids) == 1 else "",
                "caption": summary.caption if summary else "图片内容待识别",
                "actual_caption": summary.caption if summary and getattr(summary, "visual_analysis_available", None) is not False else "",
                "template_usage": str(getattr(summary, "source_semantic_text", "") or ""),
                "sources": item["sources"], "source_category": "创意库" if media_ids else "项目素材",
                "preview_url": f"/api/formal/projects/{context.project_id}/images/{asset.image_id}/preview?workspace_id={context.workspace_id}&expected_project_version={context.project_lock_version}",
            }
            references[asset.image_id] = {
                "kind": "project", "image_key": asset.image_id, "content_sha256": asset.image_sha256,
                "media_ids": media_ids, "sources": item["sources"], "candidate": candidate,
                "analysis_status": item["status"],
                "analysis_key": asset.current_analysis.analysis_version_id if asset.current_analysis else "",
            }
        return references

    @staticmethod
    def _same_progress_identity(left: dict, right: dict) -> bool:
        keys = ("kind", "image_key", "candidate_key", "content_sha256", "media_ids", "media_id", "sources",
                "package_id", "package_version", "package_manifest_sha256")
        return all(left.get(key) == right.get(key) for key in keys)

    def prepare_working_version(self, auth_token: str, *, workspace_id: str, project_id: str,
                                expected_project_version: int, expected_script_revision: int,
                                expected_progress_revision: int | None = None) -> dict:
        """Resolve final draft choices through the existing image and source authorities."""
        _, context = self._current_context(auth_token, workspace_id, project_id, expected_project_version)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有脚本版本")
        plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256).path)
        original = read_script_draft(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256).path)
        revision, shots, plan, draft, _ = self.working_image_context(context, plan, original)
        if revision != expected_script_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "文字草稿已变化，请重新查看修改")
        progress = self.read_progress(auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=expected_project_version)
        if expected_progress_revision is not None and progress["revision"] != expected_progress_revision:
            raise WorkspaceServiceError("IMAGE_PROGRESS_CONFLICT", "选图进度已变化，请重新查看修改")
        parent_ids = resolve_version_shot_ids(self.services.artifacts, context, original.script_rows)
        original_shots = version_shots_from_artifacts(original.script_rows, original.detail_rows, shot_ids_by_no=parent_ids)
        original_by_id = {item["shot_id"]: item for item in original_shots}
        prior, pending, _ = self._effective_snapshot(context, list(parent_ids))
        prior_by_id = {parent_ids[no]: item for no, item in prior.items() if no in parent_ids}
        entries = {item["shot_id"]: item for item in progress["selections"] if item["selection_active"]}
        brief_id, brief_snapshot = self._brief_binding(context, plan.confirmed_brief_input)
        pool = self.services.images.resolve_for_delivery(auth_token, workspace_id=workspace_id,
            project_id=project_id, brief_version_id=brief_id, brief_snapshot_id=brief_snapshot,
            expected_project_lock_version=expected_project_version)
        assets = {item.image_id: item for item in pool.assets}
        normalized, issues, preserved, changed_ids, comparisons = [], [], set(), [], []
        for identity, shot in shots.items():
            no, entry, old = str(shot["shot_no"]), entries.get(identity), prior_by_id.get(identity)
            origin = str(shot.get("origin_shot_no") or no)
            basis_changed = identity not in original_by_id or self._progress_basis(shot) != self._progress_basis(original_by_id[identity])
            key = str((entry or {}).get("image_key") or (old or {}).get("image_id") or "")
            reason = ""
            if entry:
                if entry.get("unavailable"):
                    reason = "所选图片已不可用"
                elif entry.get("decision_kind") == "temporary_text_basis" or not key and not entry.get("library_candidate_key"):
                    reason = "尚未配图"
                elif entry.get("library_candidate_key"):
                    reason = "暂选图片待确认"
                elif not entry.get("checked") or entry.get("needs_recheck"):
                    reason = "配图待核对"
            elif not old or old.get("decision_kind") != "image_confirmed":
                reason = "尚未确认配图"
            elif basis_changed or origin in pending:
                reason = "画面依据已变化，请重新核对配图"
            asset = assets.get(key)
            if not reason and asset is None:
                reason = "所选图片尚不可用，请检查图片分析"
            if reason:
                issues.append({"shot_id": identity, "shot_no": no, "reason": reason})
                continue
            keep = not entry and old is not None
            if keep and (old.get("image_sha256") != asset.image_sha256
                         or old.get("analysis_version_id") != asset.analysis.analysis_version_id):
                issues.append({"shot_id": identity, "shot_no": no, "reason": "图片分析已变化，请重新核对"})
                continue
            query, digest = str((entry or {}).get("query") or ""), ""
            if not keep:
                searched = self.search_shot(auth_token, workspace_id=workspace_id, project_id=project_id,
                    shot_no=no, query=query, base_version_id=context.version_id,
                    expected_project_version=expected_project_version)
                if key not in {item["image_key"] for item in searched["candidates"]}:
                    issues.append({"shot_id": identity, "shot_no": no, "reason": "所选图片不在当前有效候选中，请重新核对"})
                    continue
                digest = searched["candidate_snapshot_digest"]
                changed_ids.append(identity)
            else:
                preserved.add(no)
            normalized.append({"shot_no": no, "shot_id": identity, "decision_kind": "image_confirmed",
                "image_key": key, "temporary_reason": "", "query": query,
                "candidate_snapshot_digest": digest,
                "reuse_reason": str((entry if entry is not None else old).get("reuse_reason") or "")})
            if not keep:
                comparisons.append({"shot_id": identity, "shot_no": no,
                    "before_image_key": str((old or {}).get("image_id") or ""), "after_image_key": key,
                    "before_caption": "原确认图" if old else "原版本参考图／未确认",
                    "after_caption": asset.business_summary.caption,
                    "reuse_reason": normalized[-1]["reuse_reason"],
                    "review_impact": "重新核对本镜配图；整片确认需重做"})
        if issues:
            error = WorkspaceServiceError("IMAGE_CONFIRMATION_REQUIRED", "配图尚未全部确认，已保存进度；请先处理待办再保存新版本")
            error.issues = issues
            raise error
        self._require_reuse_reasons(pool, normalized, preserved_shot_nos=preserved)
        decisions = self._decisions(context, draft.script_rows, pool, normalized, auth_token)
        # Carrying unchanged frozen image decisions is part of editing a child
        # version. Only newly confirmed choices require an image reviewer.
        permission = "review.record" if changed_ids else "project.update"
        actor_id, now = self._actor(auth_token, context, permission), utc_now()
        for decision, selection in zip(decisions, normalized):
            old = prior_by_id.get(selection["shot_id"])
            if selection["shot_no"] in preserved and old:
                decision.update({key: deepcopy(value) for key, value in old.items()
                    if key not in {"shot_no", "confirmation_scope"}})
            decision["decision_id"] = new_id("image_decision")
            decision.setdefault("decided_by_actor_id", actor_id)
            decision.setdefault("decided_at", now)
        snapshot_digest = sha256_json({"base_version_id": context.version_id,
            "project_version": expected_project_version, "script_revision": revision,
            "progress_revision": progress["revision"], "selections": normalized,
            "evidence": [{k: d.get(k) for k in ("shot_no", "image_sha256", "analysis_version_id",
                "analysis_sha256", "source_kind", "source_key", "source_location")} for d in decisions]})
        return {"working_draft": True, "source_version_id": context.version_id,
            "script_revision": revision, "progress_revision": progress["revision"],
            "snapshot_digest": snapshot_digest, "candidate_snapshot_sha256": snapshot_digest,
            "shot_count": len(decisions), "selections": normalized, "decisions": decisions,
            "changed_shot_ids": changed_ids, "comparisons": comparisons,
            "payload": {"schema_version": "mplus-version-image-decisions/v3",
                "workspace_id": workspace_id, "project_id": project_id,
                "decisions": [self.materializer._artifact_decision(item) for item in decisions]},
            "paths_by_shot": {d["shot_no"]: d["controlled_path"] for d in decisions}}

    def _progress_model(self, context, script_revision, shots, row, *, references=None, library_service=None, auth_token="", library_references=None) -> dict:
        raw = json.loads(row["selections_json"]) if row else {}
        source = read_script_draft(self.services.artifacts.resolve_stage_artifact(
            context.run_id, 'script_draft', expected_manifest_sha256=context.manifest.sha256).path)
        original_ids = resolve_version_shot_ids(self.services.artifacts, context, source.script_rows)
        saved, pending, _ = self._effective_snapshot(context, list(original_ids))
        original = {item['shot_id']: item for item in version_shots_from_artifacts(
            source.script_rows, source.detail_rows, shot_ids_by_no=original_ids)}
        original_nos = {identity: no for no, identity in original_ids.items()}
        selections, projection = [], []
        selected_ids, check_ids = set(), set()
        for identity, shot in shots.items():
            origin_no = original_nos.get(identity, '')
            decision = saved.get(origin_no) or {}
            basis_changed = identity not in original or self._progress_basis(original[identity]) != self._progress_basis(shot)
            confirmed_saved = bool(decision.get('image_id') and decision.get('decision_kind') == 'image_confirmed')
            saved_recheck = basis_changed or origin_no in pending
            state = 'saved' if confirmed_saved and not saved_recheck else 'selected' if confirmed_saved else 'missing'
            projected = {'shot_id': identity, 'shot_no': str(shot['shot_no']), 'status': state,
                'needs_recheck': bool(confirmed_saved and saved_recheck), 'unavailable': False}
            projection.append(projected)
            if decision.get("image_id") and decision.get("decision_kind") != "temporary_text_basis":
                selected_ids.add(identity)
                if saved_recheck:
                    check_ids.add(identity)
            entry = raw.get(identity)
            if entry is None:
                continue
            ref = deepcopy(entry.get("reference") or {})
            unavailable = False
            if ref.get("kind") == "project" and references is not None:
                current = references.get(ref.get("image_key"))
                unavailable = current is None or not self._same_progress_identity(ref, current)
                if not unavailable:
                    ref["candidate"] = deepcopy(current["candidate"])
            elif ref.get("kind") == "library" and library_service is not None:
                try:
                    current = library_references.get(ref["candidate_key"]) if library_references is not None else library_service.progress_reference(
                        auth_token, workspace_id=context.workspace_id, project_id=context.project_id,
                        expected_project_version=context.project_lock_version, candidate_key=ref["candidate_key"],
                    )
                    unavailable = current is None or not self._same_progress_identity(ref, current)
                except WorkspaceServiceError:
                    unavailable = True
            current_reference = (references or {}).get(ref.get("image_key")) if ref.get("kind") == "project" else None
            analysis_changed = current_reference is not None and ref.get("analysis_key", "") != current_reference.get("analysis_key", "")
            needs_recheck = bool(entry.get("checked") and (entry.get("checked_basis") != self._progress_basis(shot) or analysis_changed))
            checked = bool(entry.get("checked") and not needs_recheck and not unavailable)
            if entry.get("selection_active", True):
                selected_ids.discard(identity)
                check_ids.discard(identity)
                if ref:
                    selected_ids.add(identity)
                    if not checked or ref.get("kind") == "library":
                        check_ids.add(identity)
            if entry.get('selection_active', True):
                projected.update(status='confirmed' if checked and ref.get('kind') == 'project' else 'selected' if ref else 'missing',
                    unavailable=unavailable, needs_recheck=needs_recheck)
            selections.append({
                "shot_id": identity, "shot_no": str(shot["shot_no"]),
                "selection_active": entry.get("selection_active", True),
                "image_key": ref.get("image_key", ""), "library_candidate_key": ref.get("candidate_key", ""),
                "source_kind": ref.get("kind", ""), "candidate": deepcopy(ref.get("candidate")),
                "checked": checked, "needs_recheck": needs_recheck, "unavailable": unavailable,
                "decision_kind": entry.get("decision_kind", "image_confirmed"),
                "temporary_reason": entry.get("temporary_reason", ""), "reuse_reason": entry.get("reuse_reason", ""),
                "query": entry.get("query", ""),
            })
        return {
            "base_version_id": context.version_id, "project_version": context.project_lock_version,
            "revision": int(row["revision"]) if row else 0, "script_revision": script_revision,
            "saved_at": row["saved_at"] if row else "", "selections": selections,
            "shot_count": len(shots), "selected_count": len(selected_ids),
            "pending_count": len(shots) - len(selected_ids), "needs_check_count": len(check_ids),
            "shots": projection,
            "status_counts": {status: sum(item['status'] == status for item in projection)
                for status in ('missing', 'selected', 'confirmed', 'saved')},
            "image_bases": {identity: self._progress_basis(shot) for identity, shot in shots.items()},
            "review_eligible": False,
        }

    def read_progress(self, auth_token: str, *, workspace_id: str, project_id: str,
                      expected_project_version: int | None = None, library_service=None) -> dict:
        _, context = self._current_context(auth_token, workspace_id, project_id, expected_project_version)
        if context is None:
            return {"revision": 0, "selections": [], "saved_at": "", "shot_count": 0,
                    "selected_count": 0, "pending_count": 0, "needs_check_count": 0, "review_eligible": False}
        references = self._progress_project_references(auth_token, context, historical=True)
        with self.services.store.connection() as connection:
            script_revision, shots = self._progress_script(connection, context)
            row = connection.execute(
                "SELECT * FROM formal_image_progress WHERE workspace_id=? AND project_id=? AND base_version_id=?",
                (workspace_id, project_id, context.version_id),
            ).fetchone()
        library_references = None
        if library_service is not None and row and hasattr(library_service, 'progress_references'):
            keys = {item.get('reference', {}).get('candidate_key') for item in json.loads(row['selections_json']).values()
                    if item.get('reference') and item['reference'].get('kind') == 'library'}
            if keys:
                try:
                    library_references = library_service.progress_references(auth_token, workspace_id=workspace_id,
                        project_id=project_id, expected_project_version=context.project_lock_version, candidate_keys=list(keys))
                except WorkspaceServiceError as exc:
                    if exc.code in {'PROJECT_VERSION_CONFLICT', 'CROSS_PROJECT_DENIED', 'FORBIDDEN'}:
                        raise
                    library_references = {}
        result = self._progress_model(context, script_revision, shots, row, references=references,
            library_service=library_service, auth_token=auth_token, library_references=library_references)
        return self._with_creative_references(result, auth_token, context, shots, script_revision)

    def _with_creative_references(self, result, auth_token, context, shots, script_revision):
        workspace_id, project_id = context.workspace_id, context.project_id
        plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256).path)
        binding = plan.constraints.get("creative_draft_binding") or {}
        result['creative_references'] = []
        if binding.get("image_references"):
            from .creative_images import public_references
            visual = read_visual_binding(self.services.artifacts.resolve_stage_artifact(
                context.run_id, 'visual_binding', expected_manifest_sha256=context.manifest.sha256).path)
            identities = resolve_version_shot_ids(self.services.artifacts, context, visual.script_rows)
            mappings = {}
            for planned in plan.shot_plans:
                block_id = (planned.get('creative_block') or {}).get('block_id')
                identity = identities.get(str(planned['shot_no']))
                if block_id and identity in shots:
                    mappings.setdefault(block_id, []).append(identity)
            projected = public_references({'draft': {'shots': [{'shot_id': block} for block in mappings]},
                'image_references': binding['image_references']}, workspace_id=workspace_id,
                project_id=project_id, project_version=context.project_lock_version, version_id=context.version_id)
            result['creative_references'] = [{**ref, 'shot_id': mappings[ref['block_id']][0]}
                for ref in projected if len(mappings[ref['block_id']]) == 1]
            with self.services.store.connection() as connection:
                parent = connection.execute("SELECT parent_version_id FROM project_versions WHERE version_id=?", (context.version_id,)).fetchone()
                adopted = connection.execute("SELECT 1 FROM formal_image_progress_requests WHERE project_id=? AND idempotency_key=?",
                    (project_id, "creative-images:" + binding["payload_sha256"])).fetchone()
            if parent["parent_version_id"] is None and not adopted:
                result["creative_reference_transfer"] = {"version_id": context.version_id,
                    "count": len(binding["image_references"]), "can_transfer": result["revision"] == 0 and script_revision == 0,
                    "message": "初稿参考图仍保留，接入后需要完成图片分析与核对。" if result["revision"] == 0 and script_revision == 0
                        else "当前稿已有修改，初稿参考图仍保留，请按镜头重新选择，避免覆盖当前选图。"}
        return result

    def preview_progress_confirmation(self, auth_token: str, *, workspace_id: str, project_id: str,
            base_version_id: str, expected_project_version: int, expected_revision: int,
            expected_script_revision: int, selections: list[dict], library_service=None) -> dict:
        """Inspect the explicitly selected scope using the existing candidate authorities."""
        model = self.read_progress(auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=expected_project_version, library_service=library_service)
        if model['base_version_id'] != base_version_id:
            raise WorkspaceServiceError('PROJECT_VERSION_CONFLICT', '当前版本已变化，选择已保留')
        if model['revision'] != expected_revision:
            raise WorkspaceServiceError('IMAGE_PROGRESS_CONFLICT', '选图进度已变化，请重新核对')
        if model['script_revision'] != expected_script_revision:
            raise WorkspaceServiceError('DRAFT_REVISION_CONFLICT', '文字稿已变化，请重新核对')
        _, context = self._current_context(auth_token, workspace_id, project_id, expected_project_version)
        similarity_items = []
        refs = self._progress_project_references(auth_token, context, similarity_items=similarity_items)
        shots = {s['shot_id']: s for s in self.read_current(auth_token, workspace_id=workspace_id,
            project_id=project_id, expected_project_version=expected_project_version)['shots']}
        if not selections or len(selections) > 200 or len({s['shot_id'] for s in selections}) != len(selections):
            raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '请选择不重复的镜头范围')
        effective = {s['shot_id']: {'image_key': s.get('selected_image_key', ''),
            'library_candidate_key': '', 'reuse_reason': s.get('reuse_reason', '')} for s in shots.values()}
        effective.update({s['shot_id']: s for s in model['selections'] if s['selection_active']})
        effective.update({s['shot_id']: s for s in selections})
        references = {}
        library_keys = {item.get('library_candidate_key') for item in effective.values() if item.get('library_candidate_key')}
        library_refs = None
        if library_keys and library_service and hasattr(library_service, 'progress_references'):
            library_refs = library_service.progress_references(auth_token, workspace_id=workspace_id,
                project_id=project_id, expected_project_version=expected_project_version, candidate_keys=list(library_keys),
                similarity_items=similarity_items)
        for identity, selection in effective.items():
            key = selection.get('image_key') or ''
            lib = selection.get('library_candidate_key') or ''
            ref = refs.get(key)
            if lib and library_service:
                try:
                    ref = library_refs.get(lib) if library_refs is not None else library_service.progress_reference(auth_token, workspace_id=workspace_id,
                        project_id=project_id, expected_project_version=expected_project_version, candidate_key=lib)
                except WorkspaceServiceError:
                    ref = None
            references[identity] = ref
        clusters = perceptual_cluster_ids(similarity_items)
        def reference_cluster(selection, ref):
            library_key = selection.get('library_candidate_key')
            identity = 'library:' + library_key if library_key else 'project:' + str(selection.get('image_key') or '')
            return clusters.get(identity) or ref.get('content_sha256') or ref.get('image_sha256')

        counts, same_pixels = {}, {}
        for identity, ref in references.items():
            if ref:
                cluster = reference_cluster(effective[identity], ref)
                if cluster:
                    counts[cluster] = counts.get(cluster, 0) + 1
                content_sha = ref.get('content_sha256') or ref.get('image_sha256')
                if content_sha:
                    same_pixels[content_sha] = same_pixels.get(content_sha, 0) + 1
        items = []
        for selection in selections:
            identity = selection['shot_id']
            if identity not in shots:
                raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '范围包含不属于当前稿的镜头')
            shot, ref = shots[identity], references.get(identity)
            reason = ''
            if selection.get('decision_kind') == 'temporary_text_basis' or not (selection.get('image_key') or selection.get('library_candidate_key')):
                reason = '尚未配图'
            elif not ref:
                reason = '所选图片已不可用，请重新选择'
            else:
                cluster = reference_cluster(selection, ref)
                content_sha = ref.get('content_sha256') or ref.get('image_sha256')
                if (counts.get(cluster, 0) > 1 or same_pixels.get(content_sha, 0) > 1) and not str(selection.get('reuse_reason') or '').strip():
                    reason = '同图或近似图片用于多镜，请补充复用说明'
                if not reason and selection.get('image_key'):
                    result = self.search_shot(auth_token, workspace_id=workspace_id, project_id=project_id,
                        base_version_id=base_version_id, expected_project_version=expected_project_version,
                        shot_no=str(shot['shot_no']), query=str(selection.get('query') or ''))
                    if selection['image_key'] not in {c['image_key'] for c in result['candidates']}:
                        reason = '候选依据已变化，请重新选择'
            items.append({'shot_id': identity, 'shot_no': str(shot['shot_no']), 'processable': not reason,
                'reason': reason, 'selection': deepcopy(selection), 'reference': ref})
        digest = sha256_json({'base_version_id': base_version_id, 'project_version': expected_project_version,
            'revision': expected_revision, 'script_revision': expected_script_revision, 'items': items})
        return {'items': items, 'processable_count': sum(item['processable'] for item in items),
            'pending_count': sum(not item['processable'] for item in items), 'preview_digest': digest}

    def confirm_progress(self, auth_token: str, *, workspace_id: str, project_id: str,
            base_version_id: str, expected_project_version: int, expected_revision: int,
            expected_script_revision: int, selections: list[dict], preview_digest: str,
            accepted_shot_ids: list[str], idempotency_key: str, library_service=None, confirm_action=None) -> dict:
        request = {'base_version_id': base_version_id, 'expected_project_version': expected_project_version,
            'expected_revision': expected_revision, 'expected_script_revision': expected_script_revision,
            'selections': selections, 'preview_digest': preview_digest, 'accepted_shot_ids': accepted_shot_ids}
        digest = sha256_json(request)
        receipt_key = 'confirm:' + idempotency_key
        with self.services.store.connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id,
                project_id=project_id, permission='project.update')
            row = connection.execute('SELECT request_sha256,response_json FROM formal_image_progress_requests WHERE actor_id=? AND project_id=? AND idempotency_key=?',
                (actor['actor_id'], project_id, receipt_key)).fetchone()
            if row:
                if row['request_sha256'] != digest:
                    raise WorkspaceServiceError('IDEMPOTENCY_CONFLICT', '同一确认请求不能用于不同内容')
                return json.loads(row['response_json'])
        preview = self.preview_progress_confirmation(auth_token, workspace_id=workspace_id, project_id=project_id,
            base_version_id=base_version_id, expected_project_version=expected_project_version,
            expected_revision=expected_revision, expected_script_revision=expected_script_revision,
            selections=selections, library_service=library_service)
        if preview['preview_digest'] != preview_digest:
            raise WorkspaceServiceError('IMAGE_CANDIDATE_INVALID', '图片依据已变化，请重新查看确认范围')
        ready = {item['shot_id']: item for item in preview['items'] if item['processable']}
        if not accepted_shot_ids or len(set(accepted_shot_ids)) != len(accepted_shot_ids) or not set(accepted_shot_ids) <= set(ready):
            raise WorkspaceServiceError('IMAGE_SCOPE_MISMATCH', '确认范围必须来自本次可处理镜头')
        chosen = [ready[identity] for identity in accepted_shot_ids]
        library = [item for item in chosen if item['selection'].get('library_candidate_key')]
        adopted = {}
        if library:
            proposal = library_service.propose(auth_token, agent_session_id='', workspace_id=workspace_id,
                project_id=project_id, expected_project_version=expected_project_version,
                expected_script_revision=expected_script_revision,
                selections=[{'shot_no': item['shot_no'], 'candidate_key': item['selection']['library_candidate_key']} for item in library],
                allowed_shot_ids=[item['shot_id'] for item in library], idempotency_key='adopt:' + idempotency_key)
            response = confirm_action(auth_token, proposal['task_id'], proposal_digest=proposal['response']['proposal_digest'])['response']
            if response.get('status') != 'completed':
                raise WorkspaceServiceError(response.get('error_code') or 'IMAGE_ADOPTION_PENDING', response.get('summary') or '采用结果待核实，请重试原请求')
            adopted = {item['shot_id']: item['image_key'] for item in response['adopted_selections']}
        updates = []
        for item in chosen:
            selection = deepcopy(item['selection'])
            if selection.get('library_candidate_key'):
                selection.update(image_key=adopted[item['shot_id']], library_candidate_key='', query='')
            selection.update(checked=True, selection_active=True)
            updates.append(selection)
        result = {'confirmed_count': len(updates), 'pending_count': len(selections) - len(updates),
            'items': [{'shot_id': item['shot_id'], 'shot_no': item['shot_no'],
                'status': 'confirmed' if item['shot_id'] in accepted_shot_ids else 'pending', 'reason': item['reason']} for item in preview['items']]}
        return self.save_progress(auth_token, workspace_id=workspace_id, project_id=project_id,
            base_version_id=base_version_id, expected_project_version=expected_project_version,
            expected_revision=expected_revision, expected_script_revision=expected_script_revision,
            selections=updates, idempotency_key=receipt_key, library_service=library_service,
            confirmation_request_digest=digest, confirmation_result=result)

    def save_progress(self, auth_token: str, *, workspace_id: str, project_id: str,
                      base_version_id: str, expected_project_version: int, expected_revision: int,
                      expected_script_revision: int, selections: list[dict], idempotency_key: str,
                      library_service=None, assistant_protection: bool = False,
                      expected_image_bases: dict | None = None, confirmation_request_digest: str | None = None,
                      confirmation_result: dict | None = None, discard_all: bool = False) -> dict:
        """Persist incomplete user input atomically, without a formal image decision."""
        if not idempotency_key or len(idempotency_key) > 160 or len(selections) > 200:
            raise WorkspaceServiceError("INVALID_INPUT", "保存进度的请求无效")
        if discard_all and selections:
            raise WorkspaceServiceError("INVALID_INPUT", "放弃全部进度不能同时提交选图修改")
        digest = sha256_json({"base_version_id": base_version_id, "project_version": expected_project_version,
                             "revision": expected_revision, "script_revision": expected_script_revision,
                             "selections": selections, **({"discard_all": True} if discard_all else {}),
                             **({"image_bases": expected_image_bases} if expected_image_bases is not None else {})})
        if confirmation_request_digest is not None:
            digest = confirmation_request_digest
        # Receipts precede freshness checks so a lost response can be recovered
        # even after another successful save or a version transition.
        with self.services.store.connection() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id,
                                       project_id=project_id, permission="project.update")
            replay = connection.execute(
                "SELECT request_sha256, response_json FROM formal_image_progress_requests WHERE actor_id=? AND project_id=? AND idempotency_key=?",
                (actor["actor_id"], project_id, idempotency_key),
            ).fetchone()
            if replay:
                if replay["request_sha256"] != digest:
                    raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "同一保存请求不能用于不同内容")
                return json.loads(replay["response_json"])
        _, context = self._current_context(auth_token, workspace_id, project_id, expected_project_version)
        if context is None or context.version_id != base_version_id:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "当前脚本版本已变化，原选图输入仍保留")
        references = {} if discard_all else self._progress_project_references(auth_token, context)
        library_refs = {}
        library_keys = {str(item.get('library_candidate_key') or '') for item in selections} - {''}
        if library_keys:
            if library_service is None:
                raise WorkspaceServiceError('ASSET_PACKAGE_UNAVAILABLE', '创意库暂时无法核对，已选输入仍保留')
            if hasattr(library_service, 'progress_references'):
                library_refs = library_service.progress_references(auth_token, workspace_id=workspace_id,
                    project_id=project_id, expected_project_version=expected_project_version, candidate_keys=list(library_keys))
            else:
                library_refs = {key: library_service.progress_reference(auth_token, workspace_id=workspace_id,
                    project_id=project_id, expected_project_version=expected_project_version, candidate_key=key) for key in library_keys}
        with self.services.store.transaction() as connection:
            actor = require_permission(connection, auth_token, workspace_id=workspace_id,
                                       project_id=project_id, permission="project.update")
            replay = connection.execute(
                "SELECT request_sha256, response_json FROM formal_image_progress_requests WHERE actor_id=? AND project_id=? AND idempotency_key=?",
                (actor["actor_id"], project_id, idempotency_key),
            ).fetchone()
            if replay:
                if replay["request_sha256"] != digest:
                    raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "同一保存请求不能用于不同内容")
                return json.loads(replay["response_json"])
            project = connection.execute("SELECT * FROM projects WHERE project_id=?", (project_id,)).fetchone()
            require_active_project(project)
            if project["current_version_id"] != base_version_id or int(project["lock_version"]) != expected_project_version:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "保存过程中项目版本已变化")
            script_revision, shots = self._progress_script(connection, context)
            if script_revision != expected_script_revision:
                if discard_all:
                    raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "文字草稿已变化，请重新核对放弃范围")
                changed = [str(selection.get('shot_id') or '') for selection in selections
                           if not expected_image_bases or selection.get('shot_id') not in shots
                           or expected_image_bases.get(selection.get('shot_id')) != self._progress_basis(shots[selection['shot_id']])]
                if changed:
                    labels = '、'.join(str(shots[s]['shot_no']) for s in changed if s in shots)
                    raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", f"第 {labels or '相关'} 镜的画面依据已变化，原选图和结果仍保留，请重新核对")
            row = connection.execute(
                "SELECT * FROM formal_image_progress WHERE workspace_id=? AND project_id=? AND base_version_id=?",
                (workspace_id, project_id, base_version_id),
            ).fetchone()
            revision = int(row["revision"]) if row else 0
            if revision != expected_revision:
                raise WorkspaceServiceError("IMAGE_PROGRESS_CONFLICT", "另一处已更新选图进度，当前输入仍保留")
            values = json.loads(row["selections_json"]) if row else {}
            original = deepcopy(values)
            if discard_all:
                values = {}
            protected = set()
            if assistant_protection:
                protected = {r['shot_id'] for r in connection.execute(
                    'SELECT shot_id FROM formal_script_assistant_locks WHERE workspace_id=? AND project_id=?',
                    (workspace_id, project_id),
                )}
            seen = set()
            for selection in selections:
                identity = str(selection.get("shot_id") or "")
                if identity in protected:
                    raise WorkspaceServiceError('ASSISTANT_LOCKED', '本次范围中有镜头已保护，请重新生成配图建议')
                if identity not in shots or identity in seen:
                    raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "保存进度的镜头范围不属于当前稿或存在重复")
                seen.add(identity)
                if selection.get("remove") is True:
                    values.pop(identity, None)
                    continue
                image_key, library_key = str(selection.get("image_key") or ""), str(selection.get("library_candidate_key") or "")
                kind = str(selection.get("decision_kind") or "image_confirmed")
                if kind not in {"image_confirmed", "temporary_text_basis"} or (image_key and library_key):
                    raise WorkspaceServiceError("INVALID_INPUT", "选图进度的图片来源无效")
                reference = references.get(image_key) if image_key else library_refs.get(library_key)
                if (image_key or library_key) and reference is None:
                    raise WorkspaceServiceError("IMAGE_CANDIDATE_INVALID", "所选图片不属于当前项目")
                if kind == "temporary_text_basis":
                    reference = None
                checked = selection.get("checked") is True
                item = {"reference": deepcopy(reference), "checked": checked,
                        "checked_basis": self._progress_basis(shots[identity]) if checked else "",
                        "decision_kind": kind, "selection_active": selection.get("selection_active", True)}
                if not item["selection_active"]:
                    item.update(reference=None, checked=False, checked_basis="", decision_kind="image_confirmed")
                for field, limit in (("temporary_reason", 500), ("reuse_reason", 500), ("query", 300)):
                    value = str(selection.get(field) or "")
                    if len(value) > limit:
                        raise WorkspaceServiceError("INVALID_INPUT", "选图进度文字过长")
                    item[field] = value
                values[identity] = item
            changed = values != original
            now = utc_now()
            saved_at = now if changed else row["saved_at"] if row else ""
            if changed:
                revision += 1
                connection.execute(
                    """INSERT INTO formal_image_progress VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_id, project_id, base_version_id) DO UPDATE SET
                    revision=excluded.revision, selections_json=excluded.selections_json,
                    saved_by_actor_id=excluded.saved_by_actor_id, saved_at=excluded.saved_at""",
                    (workspace_id, project_id, base_version_id, revision,
                     json.dumps(values, ensure_ascii=False, sort_keys=True), actor["actor_id"], saved_at),
                )
            result = self._progress_model(context, script_revision, shots, {
                "revision": revision, "selections_json": json.dumps(values), "saved_at": saved_at,
            })
            result = self._with_creative_references(result, auth_token, context, shots, script_revision)
            if confirmation_result is not None:
                result = {**result, **confirmation_result}
            connection.execute("INSERT INTO formal_image_progress_requests VALUES (?, ?, ?, ?, ?, ?)",
                               (actor["actor_id"], project_id, idempotency_key, digest,
                                json.dumps(result, ensure_ascii=False, sort_keys=True), now))
        return result

    def inherit_progress(self, connection, *, workspace_id: str, project_id: str,
                         parent_version_id: str, child_version_id: str, materialized: dict,
                         image_commit: bool = False, submitted_shot_ids=None) -> None:
        """Project pending input in the caller's existing version-commit transaction."""
        row = connection.execute(
            "SELECT * FROM formal_image_progress WHERE workspace_id=? AND project_id=? AND base_version_id=?",
            (workspace_id, project_id, parent_version_id),
        ).fetchone()
        if row is None:
            return
        parent = json.loads(row["selections_json"])
        if image_commit:
            submitted = set(parent) if submitted_shot_ids is None else set(submitted_shot_ids)
            inherited = {key: value for key, value in parent.items() if key not in submitted}
        else:
            resolved = self.services.artifacts.resolve_artifact(materialized["run_id"], "revision_report")
            if resolved.ref != materialized["revision_report_ref"] or resolved.sha256 != materialized["revision_report_sha256"]:
                raise WorkspaceServiceError("ARTIFACT_INVALID", "保存版本的镜头映射证据不一致")
            report = _read_json(resolved.path, "版本差异报告")
            identities = {
                str(item["child_shot_id"])
                for item in (report.get("version_diff") or {}).get("shot_diffs") or []
                if item.get("child_shot_id") and item.get("parent_shot_ids") == [item["child_shot_id"]]
            }
            inherited = {key: value for key, value in parent.items() if key in identities}
        # Keep the parent record for recovery. A new version gets its own CAS
        # revision; submitted input cannot reappear through browser cache restore.
        connection.execute("INSERT INTO formal_image_progress VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (workspace_id, project_id, child_version_id, int(row["revision"]),
                            json.dumps(inherited, ensure_ascii=False, sort_keys=True),
                            row["saved_by_actor_id"], row["saved_at"]))

    def read_current(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int | None = None,
        include_working_draft: bool = True,
        version_id: str | None = None,
    ) -> dict:
        if version_id is not None:
            include_working_draft = False
        project, context = self._current_context(
            auth_token, workspace_id, project_id, expected_project_version, version_id=version_id,
        )
        if context is None:
            return {"state": "empty", "message": "当前项目还没有脚本版本"}
        plan_ref = self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        )
        draft_ref = self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256,
        )
        plan = read_script_plan(plan_ref.path)
        draft = read_script_draft(draft_ref.path)
        brief_version_id, brief_snapshot_id = self._brief_binding(context, plan.confirmed_brief_input, require_current=False)
        pool = self.services.images.resolve_for_delivery(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            brief_version_id=brief_version_id,
            brief_snapshot_id=brief_snapshot_id,
            expected_project_lock_version=context.project_lock_version, historical=True,
        )
        shot_ids = resolve_version_shot_ids(self.services.artifacts, context, draft.script_rows)
        shot_nos = [_shot_no(row) for row in draft.script_rows]
        if not shot_nos or len(set(shot_nos)) != len(shot_nos):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "当前脚本镜号不完整或重复")
        existing, pending_reconfirmation, exact_snapshot = self._effective_snapshot(
            context, shot_nos,
        )
        script_revision = self._is_script_revision_version(context)
        if include_working_draft:
            working_revision, working_shots, plan, draft, _ = self.working_image_context(context, plan, draft)
        else:
            working_revision, working_shots = 0, {}
        origin_by_no = {str(shot['shot_no']): str(shot.get('origin_shot_no') or shot['shot_no'])
                        for shot in working_shots.values()}
        if working_revision:
            existing = {no: existing[origin] for no, origin in origin_by_no.items() if origin in existing}
            pending_reconfirmation = {no for no, origin in origin_by_no.items() if origin in pending_reconfirmation}
            shot_ids = {str(shot['shot_no']): identity for identity, shot in working_shots.items()}
            shot_nos = list(shot_ids)
            exact_snapshot = False
        visible_pending = pending_reconfirmation if script_revision else set()
        assets = {item.image_id: item for item in pool.assets}
        if existing and any(
            item.get("decision_kind") == "image_confirmed"
            and item.get("image_id") not in assets
            for item in existing.values()
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "已保存图片决定不属于当前图片池")
        feedback = self._image_feedback_policy(context)
        ranked_by_shot = self._ranked_selections(
            draft.script_rows, draft.detail_rows, plan.brief, pool.assets,
            shot_plans=plan.shot_plans,
            excluded_visuals=feedback["excluded"],
            preferred_visuals=feedback["preferred"],
            forced_visual=feedback["forced"],
            feedback_by_shot=feedback["by_shot"],
        )
        allocation = allocate_diverse_defaults(
            ranked_by_shot,
            pinned_by_shot={
                shot_no: str(item.get("image_id") or "")
                for shot_no, item in (existing or {}).items()
                if item.get("decision_kind") == "image_confirmed" and item.get("image_id")
            },
        )
        defaults = allocation["selected_by_shot"]
        ready = bool(pool.assets) and len(defaults) == len(shot_nos)
        details_by_shot = formal_image_details_by_script_shot(
            draft.script_rows, draft.detail_rows,
        )
        reference_bytes = {}
        try:
            frozen_workbook = self.services.artifacts.resolve_artifact(
                context.run_id, "workbook",
            )
        except ArtifactResolutionError as exc:
            if exc.code != "NOT_FOUND":
                raise
        else:
            from .excel_io import read_script_image_bytes
            reference_bytes = read_script_image_bytes(frozen_workbook.path)
        shots = []
        for row in draft.script_rows:
            shot_no = _shot_no(row)
            detail = details_by_shot.get(shot_no, {})
            saved = existing.get(shot_no) if existing else None
            selected = (
                saved.get("image_id", "")
                if saved and saved.get("decision_kind") != "temporary_text_basis"
                else defaults.get(shot_no, "") if not saved else ""
            )
            ranked = ranked_by_shot.get(shot_no, {})
            assets_by_id = {item.image_id: item for item in pool.assets}
            candidates = [
                self._employee_asset(
                    assets_by_id[item["image_id"]], item,
                    ranked.get("candidates") or [],
                )
                for item in ranked.get("candidates") or [] if item["image_id"] in assets_by_id
            ]
            current_image = None
            if saved and saved.get("decision_kind") == "image_confirmed":
                current_image = self._employee_asset(assets[str(saved["image_id"])])
                frozen_analysis = self.services.images.artifacts.read_analysis(ArtifactEvidence(
                    str(saved["analysis_ref"]), str(saved["analysis_sha256"]),
                ))
                current_image.update({
                    "actual_caption": "" if frozen_analysis.get("visual_analysis_available") is False else str(frozen_analysis.get("caption") or ""),
                    "template_usage": str(frozen_analysis.get("source_semantic_text") or ""),
                })
            reference_image = None
            raw_image = reference_bytes.get(origin_by_no.get(shot_no, shot_no))
            if raw_image:
                from io import BytesIO
                from PIL import Image
                with Image.open(BytesIO(raw_image)) as image:
                    mime = Image.MIME.get(image.format, "image/png")
                reference_image = {
                    "inline_preview": f"data:{mime};base64,{base64.b64encode(raw_image).decode('ascii')}",
                    "actual_caption": "",
                    "template_usage": str(detail.get("采用来源证据") or detail.get("槽位功能") or ""),
                    "sources": [{"file": str(detail.get("采用来源文件") or ""),
                                 "sheet": str(detail.get("采用来源 sheet") or ""),
                                 "row": detail.get("采用来源行") or ""}],
                }
            shots.append({
                "shot_no": shot_no,
                "shot_id": shot_ids[shot_no],
                "visual": str(row.get("画面描述") or ""),
                "logical_slot": int(detail.get("逻辑槽位") or detail.get("slot_index") or 0),
                "shot_function": str(detail.get("槽位功能") or ""),
                "selected_image_key": selected,
                "current_image": current_image,
                "reference_image": reference_image,
                "confirmed": bool(saved),
                "confirmation_scope": str((saved or {}).get("confirmation_scope") or "pending"),
                "image_reconfirmation_required": shot_no in visible_pending,
                "decision_kind": str((saved or {}).get("decision_kind") or "image_confirmed"),
                "temporary_reason": str((saved or {}).get("temporary_reason") or ""),
                "reuse_reason": str((saved or {}).get("reuse_reason") or ""),
                "candidates": candidates,
                "candidate_snapshot_digest": candidate_snapshot_digest(
                    project_id=project_id, version_id=context.version_id,
                    shot_no=shot_no, query="", result=ranked,
                ),
                "rejected_count": len(ranked.get("rejected") or []),
                "gap_reason": str(ranked.get("gap_reason") or ""),
            })
        versions = self.services.projects.list_project_versions(auth_token, project_id)
        version_no = next(
            (index for index, item in enumerate(versions, start=1) if item["version_id"] == context.version_id),
            len(versions),
        )
        temporary_count = sum(
            item.get("decision_kind") == "temporary_text_basis"
            for item in existing.values()
        ) if existing else 0
        result = {
            "state": (
                "temporary_text_basis" if temporary_count
                else "confirmed" if exact_snapshot and existing
                else "needs_image_reconfirmation" if script_revision
                else "ready" if ready else "blocked"
            ),
            "project_name": str(project.get("name") or "当前项目"),
            "version_label": f"版本 {version_no}",
            "base_version_id": context.version_id,
            "project_version": context.project_lock_version,
            "shot_count": len(shot_nos),
            "available_image_count": len(pool.assets),
            "usable_image_count": len(pool.assets),
            "pending_image_count": pool.pending_count,
            "low_confidence_image_count": pool.low_confidence_count,
            "blocked_image_count": pool.blocked_count,
            "failed_image_count": pool.failed_count,
            "unavailable_asset_warning": self._unavailable_asset_warning(pool),
            "shortage_count": max(0, len(shot_nos) - len(pool.assets)),
            "reuse_required": bool(pool.assets) and len(pool.assets) < len(shot_nos),
            "ready_to_save": ready or bool(existing),
            "review_eligible": exact_snapshot and bool(existing) and temporary_count == 0,
            "delivery_eligible": exact_snapshot and bool(existing) and temporary_count == 0,
            "pending_reconfirmation_shot_nos": sorted(
                visible_pending, key=lambda value: int(value),
            ),
            "requires_exact_snapshot_save": not exact_snapshot and bool(existing),
            "message": (
                "逐镜图片已确认，可继续审核"
                if exact_snapshot and existing and temporary_count == 0
                else "部分镜头暂准文字依据；版本已保存，但不能审核或交付"
                if temporary_count
                else "未受影响镜头已继承；请重选受影响镜头并保存完整图片版本"
                if existing
                else "请逐镜检查默认配图并确认"
                if ready
                else f"已有 {len(pool.assets)} 张图片可用；部分镜头暂无默认推荐，请逐镜选图并确认"
                if pool.assets
                else "当前没有可用项目图片；可先采用类别默认图片"
            ),
            "shots": shots,
            "history": self._employee_history(auth_token, project_id, versions),
            "production_certification": "BLOCK",
        }
        result["input_basis_changed"] = project.get("current_brief_snapshot_id") != brief_snapshot_id
        result["frozen_brief_snapshot_id"] = brief_snapshot_id
        if result["input_basis_changed"]:
            result["message"] = "已有新确认需求；此处保留旧版依据。可查看旧稿，或放弃全部草稿后按新需求重新规划。"
        if not include_working_draft:
            return result
        progress = self.read_progress(auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=expected_project_version)
        result['progress'] = progress
        projection = {item['shot_id']: item for item in progress['shots']}
        for shot in result['shots']:
            shot['progress_status'] = projection[shot['shot_id']]['status']
        if any(item['selection_active'] for item in progress['selections']):
            result.update(state='needs_image_reconfirmation', review_eligible=False, delivery_eligible=False,
                message='当前配图草稿待保存为新版本，请核对全稿后统一保存')
        if result['input_basis_changed']:
            result['message'] = '已有新确认需求；旧稿和当前配图输入仍保留。可查看旧稿，或放弃全部草稿后按新需求重新规划。'
        return result

    def resolve_script_inheritance(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
    ) -> dict[str, Any]:
        """Resolve one fully confirmed image snapshot through canonical lineage."""
        _, context = self._current_context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        if context is None:
            raise WorkspaceServiceError("DELIVERY_NOT_READY", "当前项目还没有可交付版本")
        model = self.read_current(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=expected_project_version,
        )
        shot_nos = [str(item.get("shot_no") or "") for item in model.get("shots") or []]
        decisions, pending, exact = self._effective_snapshot(context, shot_nos)
        if (
            not decisions
            or pending
            or len(decisions) != len(shot_nos)
            or any(item.get("decision_kind") != "image_confirmed" for item in decisions.values())
        ):
            raise WorkspaceServiceError("DELIVERY_NOT_READY", "逐镜图片决定尚未达到交付状态")
        source_versions = (
            {context.version_id}
            if exact
            else {
                str(item.get("inherited_from_version_id") or "")
                for item in decisions.values()
            }
        )
        if len(source_versions) != 1 or "" in source_versions:
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "继承图片决定的版本来源不一致")
        source_version_id = next(iter(source_versions))
        with self.services.store.connection() as connection:
            row = connection.execute(
                """SELECT s.*, v.run_id AS source_run_id
                FROM version_image_decision_snapshots_v3 s
                JOIN project_versions v ON v.version_id=s.materialized_version_id
                WHERE s.workspace_id=? AND s.project_id=?
                  AND s.materialized_version_id=?""",
                (workspace_id, project_id, source_version_id),
            ).fetchone()
        if (
            row is None
            or int(row["shot_count"]) != len(shot_nos)
            or int(row["image_confirmed_count"]) != len(shot_nos)
            or int(row["temporary_text_basis_count"]) != 0
            or str(row["run_id"]) != str(row["source_run_id"])
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "已确认图片 snapshot 不完整")
        try:
            artifact = self.services.artifacts.resolve_artifact(
                str(row["source_run_id"]), "image_decisions",
            )
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError(exc.code, exc.message) from exc
        if artifact.ref != row["snapshot_ref"] or artifact.sha256 != row["snapshot_sha256"]:
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片决定 artifact 与 snapshot 不一致")
        payload = _read_json(artifact.path, "图片决定快照")
        plan_ref = self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        )
        plan = read_script_plan(plan_ref.path)
        brief_version_id, brief_snapshot_id = self._brief_binding(
            context, plan.confirmed_brief_input,
        )
        pool = self.services.images.resolve_for_delivery(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            brief_version_id=brief_version_id,
            brief_snapshot_id=brief_snapshot_id,
            expected_project_lock_version=context.project_lock_version,
        )
        assets = {item.image_id: item for item in pool.assets}
        paths_by_shot = {}
        for shot_no, decision in decisions.items():
            asset = assets.get(str(decision.get("image_id") or ""))
            if asset is None or asset.image_sha256 != decision.get("image_sha256"):
                raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片决定与当前可用图片不一致")
            paths_by_shot[shot_no] = str(asset.controlled_path)
        return {
            "snapshot_id": str(row["snapshot_id"]),
            "source_version_id": source_version_id,
            "source_run_id": str(row["source_run_id"]),
            "snapshot_ref": str(row["snapshot_ref"]),
            "snapshot_sha256": str(row["snapshot_sha256"]),
            "candidate_snapshot_sha256": str(row["candidate_snapshot_sha256"]),
            "shot_count": int(row["shot_count"]),
            "scope": "exact" if exact else "inherited",
            "payload": payload,
            "paths_by_shot": paths_by_shot,
        }

    @staticmethod
    def build_script_child_snapshot(
        evidence: dict[str, Any],
        *,
        context: VersionDeliveryContext,
        child_version_id: str,
        child_run_id: str,
        request_sha256: str,
        created_by_actor_id: str,
        origin_to_child: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        source = deepcopy(evidence.get("payload") or {})
        decisions = list(source.get("decisions") or [])
        shot_count = int(evidence.get("shot_count") or 0)
        if (
            source.get("schema_version") != "mplus-version-image-decisions/v3"
            or shot_count <= 0
            or len(decisions) != shot_count
            or any(item.get("decision_kind") != "image_confirmed" for item in decisions)
            or not created_by_actor_id
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "继承图片决定不完整")
        if origin_to_child is not None:
            by_shot = {str(item.get("shot_no") or ""): item for item in decisions}
            if (
                not origin_to_child
                or len(by_shot) != len(decisions)
                or not set(origin_to_child).issubset(by_shot)
                or list(origin_to_child.values()) != [str(index) for index in range(1, len(origin_to_child) + 1)]
            ):
                raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "继承图片决定的镜头映射不完整")
            decisions = [
                {**by_shot[origin_no], "shot_no": child_no}
                for origin_no, child_no in origin_to_child.items()
            ]
            shot_count = len(decisions)
        created_at = utc_now()
        return {
            **source,
            "base_version_id": context.version_id,
            "materialized_version_id": child_version_id,
            "run_id": child_run_id,
            "snapshot_id": new_id("image_decision_snapshot"),
            "request_sha256": request_sha256,
            "created_by_actor_id": created_by_actor_id,
            "created_at": created_at,
            "shot_count": shot_count,
            "confirmed_count": shot_count,
            "image_confirmed_count": shot_count,
            "temporary_text_basis_count": 0,
            "decisions": [
                {**item, "decision_id": new_id("image_decision")}
                for item in decisions
            ],
        }

    @staticmethod
    def bind_script_child_snapshot(
        connection,
        *,
        workspace_id: str,
        project_id: str,
        parent_version_id: str,
        child_version_id: str,
        materialized: dict[str, Any],
        actor_id: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> None:
        inherited = materialized.get("inherited_image_payload")
        if inherited is None:
            return
        decisions = list(inherited.get("decisions") or [])
        shot_count = int(inherited.get("shot_count") or 0)
        if (
            inherited.get("base_version_id") != parent_version_id
            or inherited.get("materialized_version_id") != child_version_id
            or inherited.get("run_id") != materialized["run_id"]
            or len(decisions) != shot_count
            or any(item.get("decision_kind") != "image_confirmed" for item in decisions)
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "继承图片决定与脚本子版本不一致")
        connection.execute(
            """INSERT INTO version_image_decision_snapshots_v3 VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )""",
            (
                inherited["snapshot_id"], workspace_id, project_id,
                parent_version_id, child_version_id, materialized["run_id"],
                shot_count, shot_count, 0,
                materialized["image_decision_ref"],
                materialized["image_decision_sha256"], request_sha256,
                materialized["candidate_snapshot_sha256"],
                f"{idempotency_key}:inherit-images", actor_id, inherited["created_at"],
            ),
        )
        for decision in decisions:
            connection.execute(
                """INSERT INTO version_image_decisions_v3 VALUES (
                ?, ?, ?, 'image_confirmed', ?, ?, ?, ?, ?, ?, NULL, ?, ?
                )""",
                (
                    decision["decision_id"], inherited["snapshot_id"],
                    str(decision["shot_no"]), decision["image_id"],
                    decision["image_sha256"], decision["analysis_version_id"],
                    decision["source_kind"], decision["source_key"],
                    json.dumps(
                        decision["source_location"], ensure_ascii=False, sort_keys=True,
                    ),
                    decision["decided_by_actor_id"], decision["decided_at"],
                ),
            )
        source_completion = connection.execute(
            """SELECT generation_mode FROM version_completion_states
            WHERE project_id=? AND version_id=?""",
            (
                project_id,
                str(materialized.get("image_source_version_id") or parent_version_id),
            ),
        ).fetchone()
        connection.execute(
            """INSERT INTO version_completion_states(
            completion_state_id, workspace_id, project_id, version_id,
            generation_mode, image_state, review_eligible, delivery_eligible,
            evidence_ref, evidence_sha256, created_by_actor_id, created_at
            ) VALUES (?, ?, ?, ?, ?, 'images_confirmed', 1, 1, ?, ?, ?, ?)""",
            (
                new_id("completion_state"), workspace_id, project_id, child_version_id,
                str(source_completion["generation_mode"]) if source_completion else "image_backed",
                materialized["image_decision_ref"],
                materialized["image_decision_sha256"], actor_id, inherited["created_at"],
            ),
        )

    @staticmethod
    def _unavailable_asset_warning(pool: ProjectImageDeliveryPool) -> str:
        attention = (
            pool.pending_count + pool.low_confidence_count
            + pool.blocked_count + pool.failed_count
        )
        if not attention:
            return ""
        return (
            f"有 {attention} 张项目图片待处理或不可用；已通过的 {len(pool.assets)} 张仍可继续匹配，"
            "注意项不会阻断其他图片。"
        )

    def _is_script_revision_version(self, context: VersionDeliveryContext) -> bool:
        with self.services.store.connection() as connection:
            return connection.execute(
                """SELECT 1 FROM version_review_draft_submissions
                WHERE workspace_id=? AND project_id=? AND materialized_version_id=?""",
                (context.workspace_id, context.project_id, context.version_id),
            ).fetchone() is not None

    def search_shot(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        shot_no: str,
        query: str,
        base_version_id: str,
        expected_project_version: int,
        source_revision_task_id: str | None = None,
        proposal_task_id: str | None = None,
    ) -> dict:
        project, context = self._current_context(
            auth_token, workspace_id, project_id, expected_project_version,
            reuse_immutable=True,
        )
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有脚本版本")
        if context.version_id != str(base_version_id or ""):
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "候选检索基线版本已变化")
        plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        draft = read_script_draft(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        script_revision, working_shots, plan, draft, _ = self.working_image_context(context, plan, draft)
        row_index = next((
            index for index, item in enumerate(draft.script_rows)
            if _shot_no(item) == str(shot_no)
        ), None)
        row = draft.script_rows[row_index] if row_index is not None else None
        if row is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前版本不存在该镜头")
        if proposal_task_id:
            from .creative_workbench import text_projection
            from .formal_script_workspace import _EDITABLE_FIELDS
            from .task_service import TaskService
            changes = text_projection(TaskService(self.services.store), auth_token,
                project_id=project_id, project_version=expected_project_version, revision=script_revision,
                task_id=proposal_task_id, stage='formal')
            identities = {str(value['shot_no']): identity for identity, value in working_shots.items()}
            for change in changes:
                if change['block_id'] == identities.get(str(shot_no)):
                    row[_EDITABLE_FIELDS[change['field']]] = change['after']
        detail = formal_image_details_by_script_shot(
            draft.script_rows, draft.detail_rows,
        ).get(
            str(shot_no), {},
        )
        brief_version_id, brief_snapshot_id = self._brief_binding(context, plan.confirmed_brief_input)
        pool = self.services.images.resolve_for_delivery(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            brief_version_id=brief_version_id, brief_snapshot_id=brief_snapshot_id,
            expected_project_lock_version=context.project_lock_version,
        )
        feedback = self._image_feedback_policy(context)
        shot_feedback = feedback["by_shot"].get(str(shot_no), {})
        source_excluded: tuple[str, ...] = ()
        if source_revision_task_id:
            source_parameters = self._history_image_revision_parameters(
                auth_token,
                task_id=source_revision_task_id,
                workspace_id=workspace_id,
                project_id=project_id,
                base_version_id=context.version_id,
                expected_project_version=expected_project_version,
                shot_no=str(shot_no),
                context=context,
            )
            source_excluded = (str(source_parameters["image_key"]),)
        planned = (
            plan.shot_plans[row_index]
            if row_index is not None and row_index < len(plan.shot_plans) else {}
        )
        slot = formal_image_search_slot(
            row=row,
            detail=detail,
            planned=planned,
            fallback_index=(row_index + 1) if row_index is not None else 1,
        )
        normalized_query = str(query or "").strip()
        if normalized_query:
            slot["画面描述"] = "；".join((str(row.get("画面描述") or ""), normalized_query))
        result = search_project_candidates(
            # Selection snapshots cover the complete safe project pool. A
            # recommendation limit must not make an explicitly adopted
            # reference (or a saved decision) impossible to confirm.
            slot=slot, brief=plan.brief, assets=pool.assets, top_k=None,
            excluded_visuals=(
                *feedback["excluded"], *shot_feedback.get("excluded", ()), *source_excluded,
            ),
            preferred_visuals=(*feedback["preferred"], *shot_feedback.get("preferred", ())),
            forced_visual=str(shot_feedback.get("forced") or feedback["forced"]),
            semantic_policy=self.semantic_policy, semantic_cache_root=self.semantic_cache_root,
        )
        digest = candidate_snapshot_digest(
            project_id=project_id, version_id=context.version_id,
            shot_no=str(shot_no), query=normalized_query, result=result,
        )
        assets = {item.image_id: item for item in pool.assets}
        existing, _, _ = self._effective_snapshot(
            context, [_shot_no(item) for item in draft.script_rows],
        )
        cluster_ids = perceptual_cluster_ids([{
            "image_id": item.image_id,
            "path": str(item.controlled_path),
            "perceptual_hash": item.business_summary.perceptual_hash,
        } for item in pool.assets])
        reuse_counts: dict[str, int] = {}
        for item in existing.values():
            image_id = str(item.get("image_id") or "")
            cluster_id = cluster_ids.get(image_id, image_id)
            reuse_counts[cluster_id] = reuse_counts.get(cluster_id, 0) + 1
        candidates = []
        for match in result.get("candidates") or []:
            asset = assets.get(str(match.get("image_id") or ""))
            if asset is None:
                continue
            public = self._employee_asset(asset, match, result.get("candidates") or [])
            public["content_sha256"] = asset.image_sha256
            cluster_id = str(public.get("perceptual_cluster_id") or cluster_ids.get(asset.image_id, asset.image_id))
            public["reuse_count"] = reuse_counts.get(cluster_id, 0)
            public["match_explanation"]["reuse"] = (
                f"当前版本已有 {public['reuse_count']} 镜使用同一画面或近重复画面"
                if public["reuse_count"] else "当前版本尚未复用该画面"
            )
            public["manual_confirmation_required"] = True
            candidates.append(public)
        saved = existing.get(str(shot_no)) or {}
        current_image = None
        current_image_id = str(saved.get("image_id") or "")
        if current_image_id:
            current_asset = assets.get(current_image_id)
            if current_asset is None:
                raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "当前图片已经不属于项目权威图片池")
            current_image = self._employee_asset(current_asset)
            current_image["content_sha256"] = current_asset.image_sha256
            current_image["perceptual_cluster_id"] = cluster_ids.get(current_image_id, current_image_id)
            current_image["reuse_count"] = reuse_counts.get(current_image["perceptual_cluster_id"], 0)
            current_image["manual_confirmation_required"] = False
        return {
            "state": "ready" if candidates else "gap",
            "project_version": project["project_version"],
            "base_version_id": context.version_id,
            "script_revision": script_revision,
            "shot_id": next((identity for identity, item in working_shots.items() if str(item['shot_no']) == str(shot_no)), ''),
            "basis_label": "当前已保存工作稿",
            "shot_no": str(shot_no),
            "shot_context": {
                "visual": str(row.get("画面描述") or ""),
                "structure": str(row.get("结构") or detail.get("模板结构") or ""),
                "function": str(detail.get("槽位功能") or ""),
                "brief_prohibitions": str(plan.brief.get("禁忌或不能出现的内容") or ""),
            },
            "current_image": current_image,
            "candidate_snapshot_digest": digest,
            'semantic_assessment': result.get('semantic_assessment'),
            "candidates": candidates,
            "rejected": [
                {"image_key": str(item.get("image_id") or ""), "reason": str(item.get("reason") or "")}
                for item in result.get("rejected") or []
            ],
            "gap_reason": str(result.get("gap_reason") or ""),
            "temporary_text_basis_available": True,
            "supplement_upload_available": True,
            "feedback_applied": {
                "excluded_count": len(feedback["excluded"]) + len(shot_feedback.get("excluded", ())),
                "preferred_count": len(feedback["preferred"]) + len(shot_feedback.get("preferred", ())),
                "forced": bool(shot_feedback.get("forced") or feedback["forced"]),
            },
        }

    def record_image_feedback(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        base_version_id: str,
        shot_no: str,
        image_key: str,
        reason: str,
        expected_project_version: int,
    ) -> dict:
        """Write one immutable exclusion into the existing feedback authority."""
        reason = str(reason or "").strip()
        if not reason:
            raise WorkspaceServiceError("INVALID_INPUT", "图片不合适反馈必须填写原因")
        project, context = self._current_context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        if context is None or context.version_id != str(base_version_id or ""):
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "图片反馈基线版本已变化")
        snapshot = self._read_snapshot(context) or {}
        current = snapshot.get(str(shot_no)) or {}
        if current.get("decision_kind") != "image_confirmed":
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "目标镜头当前没有已确认图片")
        if str(current.get("image_id") or "") != str(image_key or ""):
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "目标镜头当前图片已变化")
        payload = {
            "schema_version": "mplus-formal-image-feedback/v2",
            "image_feedback": {
                "shot_policies": [{
                    "shot_no": str(shot_no),
                    "excluded_image_ids": [str(image_key)],
                    "preferred_image_ids": [],
                    "forced_image_id": "",
                    "reason": reason,
                }],
            },
        }
        batch = self.services.images.artifacts.stage_feedback(payload)
        created_at = utc_now()
        feedback_id = new_id("formal_feedback")
        duplicate = False
        try:
            with self.services.store.transaction() as connection:
                locked = connection.execute(
                    "SELECT * FROM projects WHERE project_id=?", (project_id,),
                ).fetchone()
                if locked is None:
                    raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
                if locked["workspace_id"] != workspace_id:
                    raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
                require_active_project(locked)
                check_version(
                    int(locked["lock_version"]), expected_project_version,
                    "PROJECT_VERSION_CONFLICT",
                )
                if str(locked["current_version_id"] or "") != context.version_id:
                    raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "当前脚本版本已变化")
                actor = require_permission(
                    connection, auth_token, workspace_id=workspace_id,
                    project_id=project_id, permission="review.record",
                )
                prior = connection.execute(
                    """SELECT feedback_id FROM formal_version_feedback_records
                    WHERE project_id=? AND base_version_id=? AND feedback_kind='image'
                      AND shot_no=? AND payload_sha256=? AND created_by_actor_id=?""",
                    (project_id, context.version_id, str(shot_no), batch.evidence.sha256, actor["actor_id"]),
                ).fetchone()
                if prior is not None:
                    feedback_id = str(prior["feedback_id"])
                    duplicate = True
                else:
                    batch.promote()
                    connection.execute(
                        """INSERT INTO formal_version_feedback_records(
                        feedback_id, workspace_id, project_id, base_version_id, child_version_id,
                        feedback_kind, shot_no, payload_ref, payload_sha256,
                        created_by_actor_id, created_at)
                        VALUES (?, ?, ?, ?, NULL, 'image', ?, ?, ?, ?, ?)""",
                        (
                            feedback_id, workspace_id, project_id, context.version_id,
                            str(shot_no), batch.evidence.ref, batch.evidence.sha256,
                            actor["actor_id"], created_at,
                        ),
                    )
                    updated = connection.execute(
                        """UPDATE projects SET lock_version=lock_version+1, updated_at=?
                        WHERE project_id=? AND lock_version=?""",
                        (created_at, project_id, expected_project_version),
                    )
                    if updated.rowcount != 1:
                        raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目状态在反馈写入时变化")
        except Exception:
            batch.rollback()
            raise
        batch.commit()
        return {
            "type": "formal_image_feedback_completed",
            "status": "completed",
            "feedback_id": feedback_id,
            "shot_no": str(shot_no),
            "excluded_image_key": str(image_key),
            "project_version": expected_project_version if duplicate else expected_project_version + 1,
            "idempotent_replay": duplicate,
            "summary": f"已记录第 {shot_no} 镜图片排除反馈；下一次权威检索将消费该反馈。",
        }

    def supplement_local_image(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        filename: str,
        content: bytes,
    ) -> dict:
        project, context = self._current_context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        if context is None:
            brief_version_id, brief_snapshot_id = self.services.images.current_confirmed_brief_binding(
                auth_token, workspace_id=workspace_id, project_id=project_id,
                expected_project_lock_version=project["project_version"],
            )
        else:
            plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
                context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
            ).path)
            brief_version_id, brief_snapshot_id = self._brief_binding(
                context, plan.confirmed_brief_input,
            )
        image_id = self.services.images.adopt_local_upload(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            brief_version_id=brief_version_id,
            brief_snapshot_id=brief_snapshot_id,
            expected_project_lock_version=project["project_version"],
            filename=filename,
            content=content,
        )
        return {
            "status": "uploaded",
            "image_key": image_id,
            "project_version": project["project_version"],
            "requires_visual_analysis": True,
            "brief_reupload_required": False,
        }

    def preview_version(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        base_version_id: str,
        expected_project_version: int,
        selections: tuple[ImageShotSelection, ...],
        idempotency_key: str,
        source_revision_task_id: str | None = None,
        allowed_shot_ids: list[str] | tuple[str, ...] | None = None,
    ) -> dict:
        if not selections:
            raise WorkspaceServiceError("INVALID_INPUT", "逐镜图片决定不能为空")
        if any(item.decision_kind != "image_confirmed" or not item.image_key for item in selections):
            # Product rule: incomplete images remain progress; historical versions remain readable.
            raise WorkspaceServiceError("IMAGE_CONFIRMATION_REQUIRED", "配图尚未全部确认，请保存进度；补齐后再统一保存新版本")
        project, context = self._current_context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        if context is None or context.version_id != base_version_id:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "图片决定基线版本已变化")
        with self.services.store.connection() as connection:
            progress_revision = self._progress_revision(connection, context)
            require_no_unsaved_script_draft(connection, workspace_id=workspace_id,
                                           project_id=project_id, base_version_id=base_version_id)
        source_revision_shot_no = ""
        if source_revision_task_id:
            source_parameters = self._history_image_revision_parameters(
                auth_token,
                task_id=source_revision_task_id,
                workspace_id=workspace_id,
                project_id=project_id,
                base_version_id=base_version_id,
                expected_project_version=expected_project_version,
                shot_no=None,
                context=context,
            )
            source_revision_shot_no = str(source_parameters["shot_no"])
        draft = read_script_draft(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        shot_nos = [_shot_no(item) for item in draft.script_rows]
        inherited, pending_reconfirmation, _ = self._effective_snapshot(context, shot_nos)
        shot_ids = resolve_version_shot_ids(self.services.artifacts, context, draft.script_rows)
        submitted_nos = [item.shot_no for item in selections]
        preserved_nos: set[str] = set()
        if allowed_shot_ids is not None:
            allowed = set(allowed_shot_ids)
            if (not allowed or len(allowed) != len(allowed_shot_ids)
                    or not allowed <= set(shot_ids.values()) or len(set(submitted_nos)) != len(submitted_nos)
                    or any(shot_ids.get(shot_no) not in allowed for shot_no in submitted_nos)):
                raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "图片选择超出本次明确镜头范围，请重新选择")
            # Omitted shots retain a verified saved decision. Defaults never enter
            # the submitted set and cannot supply missing human confirmations.
            preserved_nos = set(shot_nos) - set(submitted_nos)
            if any(shot_no not in inherited or shot_no in pending_reconfirmation for shot_no in preserved_nos):
                raise WorkspaceServiceError("IMAGE_DECISION_CONFIRMATION_REQUIRED", "其他镜头仍有待确认配图，请明确选择需要处理的镜头")
            supplied = {item.shot_no: item for item in selections}
            selections = tuple(supplied[shot_no] if shot_no in supplied else ImageShotSelection(
                shot_no, str(inherited[shot_no].get("image_id") or ""), True,
                decision_kind=str(inherited[shot_no].get("decision_kind") or "image_confirmed"),
                temporary_reason=str(inherited[shot_no].get("temporary_reason") or ""),
                reuse_reason=str(inherited[shot_no].get("reuse_reason") or ""),
            ) for shot_no in shot_nos)
        if [item.shot_no for item in selections] != shot_nos:
            raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "必须按当前脚本顺序提交全部逐镜决定")
        normalized = []
        for item in selections:
            if item.shot_no in preserved_nos:
                normalized.append({
                    "shot_no": item.shot_no, "decision_kind": item.decision_kind,
                    "image_key": item.image_key, "temporary_reason": item.temporary_reason,
                    "candidate_snapshot_digest": "", "query": "", "reuse_reason": item.reuse_reason,
                    "perceptual_cluster_id": item.image_key, "confirmed": True, "preserved": True,
                })
                continue
            if item.decision_kind == "image_confirmed":
                previous = inherited.get(item.shot_no) or {}
                carry_forward = (
                    previous.get("confirmation_scope") == "inherited"
                    and item.shot_no not in pending_reconfirmation
                    and str(previous.get("image_id") or "") == item.image_key
                )
                if carry_forward:
                    normalized.append({
                        "shot_no": item.shot_no,
                        "decision_kind": item.decision_kind,
                        "image_key": item.image_key,
                        "temporary_reason": "",
                        "candidate_snapshot_digest": item.candidate_snapshot_digest,
                        "query": item.query,
                        "reuse_reason": item.reuse_reason.strip(),
                        "perceptual_cluster_id": item.image_key,
                        "confirmed": True,
                        "confirmation_scope": "inherited",
                    })
                    continue
                searched = self.search_shot(
                    auth_token, workspace_id=workspace_id, project_id=project_id,
                    shot_no=item.shot_no, query=item.query,
                    base_version_id=base_version_id,
                    expected_project_version=expected_project_version,
                    source_revision_task_id=(
                        source_revision_task_id
                        if item.shot_no == source_revision_shot_no else None
                    ),
                )
                if item.candidate_snapshot_digest != searched["candidate_snapshot_digest"]:
                    raise WorkspaceServiceError(
                        "IMAGE_CANDIDATE_SNAPSHOT_DRIFT", "候选、视觉分析或反馈已变化，请重新检索",
                    )
                if item.image_key not in {
                    value["image_key"] for value in searched["candidates"]
                }:
                    raise WorkspaceServiceError("IMAGE_DECISION_MISMATCH", "选定图片不在当前安全候选快照中")
                selected_candidate = next(
                    value for value in searched["candidates"]
                    if value["image_key"] == item.image_key
                )
                cluster_id = str(
                    selected_candidate.get("perceptual_cluster_id") or item.image_key
                )
            else:
                cluster_id = ""
            normalized.append({
                "shot_no": item.shot_no,
                "decision_kind": item.decision_kind,
                "image_key": item.image_key,
                "temporary_reason": item.temporary_reason,
                "candidate_snapshot_digest": item.candidate_snapshot_digest,
                "query": item.query,
                "reuse_reason": item.reuse_reason.strip(),
                "perceptual_cluster_id": cluster_id,
                "confirmed": True,
            })
        plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        brief_version_id, brief_snapshot_id = self._brief_binding(context, plan.confirmed_brief_input)
        pool = self.services.images.resolve_for_delivery(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            brief_version_id=brief_version_id, brief_snapshot_id=brief_snapshot_id,
            expected_project_lock_version=context.project_lock_version,
        )
        clusters = self._require_reuse_reasons(pool, normalized, preserved_shot_nos=preserved_nos)
        for item in normalized:
            image_key = str(item.get("image_key") or "")
            item["perceptual_cluster_id"] = clusters.get(image_key, image_key)
        parameters = {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "base_version_id": base_version_id,
            "expected_project_version": expected_project_version,
            "expected_progress_revision": progress_revision,
            "selections": normalized,
            "idempotency_key": str(idempotency_key),
            "source_revision_task_id": str(source_revision_task_id or "") or None,
            "source_revision_shot_no": source_revision_shot_no or None,
        }
        if allowed_shot_ids is not None:
            parameters["allowed_shot_ids"] = list(allowed_shot_ids)
            parameters["submitted_shot_ids"] = [shot_ids[no] for no in submitted_nos]
        proposal_digest = sha256_json({"action": "confirm_formal_image_version", "parameters": parameters})
        tasks = TaskService(self.services.store)
        session = tasks.create_agent_session(auth_token, project_id=project_id)
        task = tasks.create_task(
            auth_token, agent_session_id=session["agent_session_id"],
            task_type="formal_image_version", payload_sha256=proposal_digest,
            expected_project_version=expected_project_version,
            reuse_payload=True,
        )
        if task.get("replayed"):
            replayed = next((
                event.get("details", {}).get("response")
                for event in reversed(tasks.list_task_events(auth_token, task["task_id"]))
                if isinstance(event.get("details", {}).get("response"), dict)
                and event.get("details", {}).get("response", {}).get("type") == "image_version_proposal"
            ), None)
            if replayed is None:
                raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "同一图片版本提案正在建立，请稍后重试")
            return replayed
        task = tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="understanding", details={"formal_context_version": expected_project_version},
        )
        response = {
            "type": "image_version_proposal",
            "proposal_id": task["task_id"],
            "proposal_digest": proposal_digest,
            "before": {"base_version_id": base_version_id, "project_version": expected_project_version},
            "after": {
                "image_confirmed_count": sum(item.decision_kind == "image_confirmed" for item in selections),
                "temporary_text_basis_count": sum(item.decision_kind == "temporary_text_basis" for item in selections),
            },
            "affected_shots": submitted_nos,
            "allowed_shot_ids": list(allowed_shot_ids) if allowed_shot_ids is not None else list(shot_ids.values()),
            "affected_fields": ["image_decision"],
            "rerun_nodes": ["visual_binding", "workbook_render", "machine_audit"],
            "review_eligible": all(item.decision_kind == "image_confirmed" for item in selections),
            "delivery_eligible": all(item.decision_kind == "image_confirmed" for item in selections),
            "confirmation_required": True,
        }
        tasks.transition_task(
            auth_token, task_id=task["task_id"], expected_task_version=task["task_version"],
            status="proposed", details={
                "response": response,
                "internal_formal_parameters": parameters,
            },
        )
        return response

    def confirm_version(
        self,
        auth_token: str,
        *,
        proposal_id: str,
        proposal_digest: str,
    ) -> dict:
        tasks = TaskService(self.services.store)
        task = tasks.get_task(auth_token, proposal_id)
        events = tasks.list_task_events(auth_token, proposal_id)
        details = next((
            event["details"] for event in reversed(events)
            if isinstance(event.get("details"), dict)
            and event["details"].get("internal_formal_parameters")
        ), None)
        if details is None:
            raise WorkspaceServiceError("NOT_FOUND", "图片版本提案不存在")
        parameters = details["internal_formal_parameters"]
        expected = sha256_json({"action": "confirm_formal_image_version", "parameters": parameters})
        if expected != proposal_digest:
            raise WorkspaceServiceError("PROPOSAL_DIGEST_MISMATCH", "图片版本提案摘要不匹配")
        current = tasks.get_task(auth_token, proposal_id)
        if current["status"] == "completed":
            return self._replay(
                auth_token,
                workspace_id=parameters["workspace_id"],
                project_id=parameters["project_id"],
                idempotency_key=parameters["idempotency_key"],
                request_sha256=sha256_json({
                    "project_id": parameters["project_id"],
                    "expected_project_version": parameters["expected_project_version"],
                    **({"expected_progress_revision": parameters["expected_progress_revision"]} if "expected_progress_revision" in parameters else {}),
                    "selections": [{key: item[key] for key in (
                        "shot_no", "decision_kind", "image_key", "temporary_reason",
                        "candidate_snapshot_digest", "query", "reuse_reason",
                    )} for item in parameters["selections"]],
                    **({"allowed_shot_ids": parameters["submitted_shot_ids"]}
                       if parameters.get("submitted_shot_ids") is not None else {}),
                }),
            )
        selections = tuple(ImageShotSelection(
            item["shot_no"], item["image_key"], True,
            decision_kind=item["decision_kind"],
            temporary_reason=item["temporary_reason"],
            candidate_snapshot_digest=item["candidate_snapshot_digest"],
            query=item["query"],
            reuse_reason=item.get("reuse_reason", ""),
        ) for item in parameters["selections"])
        # 再跑一次 preview 校验会创建新 task，因此在确认点逐镜重算快照并拒绝漂移。
        for item in selections:
            if item.decision_kind != "image_confirmed":
                continue
            if any(raw["shot_no"] == item.shot_no and raw.get("preserved") for raw in parameters["selections"]):
                continue
            searched = self.search_shot(
                auth_token,
                workspace_id=parameters["workspace_id"],
                project_id=parameters["project_id"],
                shot_no=item.shot_no, query=item.query,
                base_version_id=parameters["base_version_id"],
                expected_project_version=parameters["expected_project_version"],
                source_revision_task_id=(
                    parameters.get("source_revision_task_id")
                    if item.shot_no == parameters.get("source_revision_shot_no") else None
                ),
            )
            if searched["candidate_snapshot_digest"] != item.candidate_snapshot_digest:
                raise WorkspaceServiceError(
                    "IMAGE_CANDIDATE_SNAPSHOT_DRIFT", "候选、视觉分析或反馈在确认前发生变化",
                )
        current = tasks.transition_task(
            auth_token, task_id=proposal_id, expected_task_version=current["task_version"],
            status="waiting_confirmation", details={"proposal_digest": proposal_digest},
        )
        current = tasks.transition_task(
            auth_token, task_id=proposal_id, expected_task_version=current["task_version"],
            status="executing", details={"proposal_digest": proposal_digest},
        )
        try:
            result = self.save_version(
                auth_token,
                workspace_id=parameters["workspace_id"],
                project_id=parameters["project_id"],
                expected_project_version=parameters["expected_project_version"],
                selections=selections,
                idempotency_key=parameters["idempotency_key"],
                allowed_shot_ids=parameters.get("submitted_shot_ids"),
                expected_progress_revision=parameters.get("expected_progress_revision"),
            )
        except Exception as exc:
            error_code = (
                exc.code if isinstance(exc, WorkspaceServiceError)
                else "IMAGE_VERSION_SAVE_FAILED"
            )
            try:
                latest = tasks.get_task(auth_token, proposal_id)
                if latest["status"] == "executing":
                    tasks.transition_task(
                        auth_token,
                        task_id=proposal_id,
                        expected_task_version=latest["task_version"],
                        status="failed",
                        error_code=error_code,
                        details={
                            "action": "create_image_version",
                            "retryable": True,
                        },
                    )
            except WorkspaceServiceError:
                pass
            if isinstance(exc, WorkspaceServiceError):
                raise
            raise WorkspaceServiceError(
                "IMAGE_VERSION_SAVE_FAILED",
                "图片版本创建失败，本次未完成；请刷新后重新提交。",
            ) from exc
        source_revision_task_id = str(parameters.get("source_revision_task_id") or "")
        if source_revision_task_id:
            child_project = self.services.projects.get_project(
                auth_token, parameters["project_id"],
            )
            if child_project.get("current_version_id") == parameters["base_version_id"]:
                raise WorkspaceServiceError("INTEGRITY_ERROR", "历史图片反馈未创建不可变子版本")
            source_task = tasks.get_task(auth_token, source_revision_task_id)
            if source_task["status"] != "proposed":
                raise WorkspaceServiceError("IMAGE_FEEDBACK_TASK_INVALID", "历史图片反馈任务状态已变化")
            source_task = tasks.transition_task(
                auth_token,
                task_id=source_revision_task_id,
                expected_task_version=source_task["task_version"],
                status="waiting_confirmation",
                details={"image_version_proposal_id": proposal_id},
            )
            source_task = tasks.transition_task(
                auth_token,
                task_id=source_revision_task_id,
                expected_task_version=source_task["task_version"],
                status="executing",
                details={"image_version_proposal_id": proposal_id},
            )
            tasks.transition_task(
                auth_token,
                task_id=source_revision_task_id,
                expected_task_version=source_task["task_version"],
                status="completed",
                details={"response": {
                    "type": "formal_image_feedback_completed",
                    "version_label": result.get("version_label", ""),
                }},
            )
        tasks.transition_task(
            auth_token, task_id=proposal_id, expected_task_version=current["task_version"],
            status="completed", details={"response": {
                "type": "image_version_created",
                "state": result["state"],
                "version_label": result["version_label"],
            }},
        )
        return result

    def save_version(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        selections: tuple[ImageShotSelection, ...],
        idempotency_key: str,
        allowed_shot_ids: list[str] | tuple[str, ...] | None = None,
        working_evidence: dict | None = None,
        expected_progress_revision: int | None = None,
    ) -> dict:
        if not isinstance(selections, tuple) or not selections or not all(
            isinstance(item, ImageShotSelection) for item in selections
        ):
            raise TypeError("图片保存只接受 ImageShotSelection tuple")
        key = str(idempotency_key or "").strip()
        if not key or len(key) > 160:
            raise WorkspaceServiceError("INVALID_INPUT", "幂等键无效")
        normalized = [
            {
                "shot_no": item.shot_no.strip(),
                "decision_kind": item.decision_kind,
                "image_key": item.image_key.strip(),
                "temporary_reason": item.temporary_reason.strip(),
                "candidate_snapshot_digest": item.candidate_snapshot_digest.strip(),
                "query": item.query.strip(),
                "reuse_reason": item.reuse_reason.strip(),
            }
            for item in selections
        ]
        request_sha = sha256_json({
            "project_id": project_id,
            "expected_project_version": expected_project_version,
            "selections": normalized,
            **({"expected_progress_revision": expected_progress_revision} if expected_progress_revision is not None else {}),
            **({"allowed_shot_ids": list(allowed_shot_ids)} if allowed_shot_ids is not None else {}),
            **({"working_snapshot_digest": working_evidence["snapshot_digest"]} if working_evidence else {}),
        })
        if working_evidence and working_evidence.get("submission_sha256"):
            request_sha = working_evidence["submission_sha256"]
        replay = self._replay(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            idempotency_key=key,
            request_sha256=request_sha,
        )
        if replay:
            return replay
        if any(item.decision_kind != "image_confirmed" or not item.image_key for item in selections):
            raise WorkspaceServiceError("IMAGE_CONFIRMATION_REQUIRED", "配图尚未全部确认，请保存进度；补齐后再统一保存新版本")
        project, context = self._current_context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有脚本版本")
        with self.services.store.connection() as connection:
            progress_revision = self._progress_revision(connection, context)
            if expected_progress_revision is not None and progress_revision != expected_progress_revision:
                raise WorkspaceServiceError('IMAGE_PROGRESS_CONFLICT', '预览后选图进度已变化，请重新查看统一保存预览')
            require_no_unsaved_script_draft(
                connection,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                base_version_id=context.version_id,
            )
        plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        draft = read_script_draft(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        preserved_nos: set[str] = set()
        preserved_decisions = {}
        if allowed_shot_ids is not None:
            shot_ids = resolve_version_shot_ids(self.services.artifacts, context, draft.script_rows)
            allowed = set(allowed_shot_ids)
            if not allowed or len(allowed) != len(allowed_shot_ids) or not allowed <= set(shot_ids.values()):
                raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "图片修改范围已变化，请重新选择")
            preserved_nos = {no for no, identity in shot_ids.items() if identity not in allowed}
            prior, pending, _ = self._effective_snapshot(context, list(shot_ids))
            submitted = {item["shot_no"]: item for item in normalized}
            for no in preserved_nos:
                old, new = prior.get(no), submitted.get(no)
                if not old or no in pending or not new or any(
                    str(old.get(old_key) or "") != str(new.get(new_key) or "")
                    for old_key, new_key in (("image_id", "image_key"), ("decision_kind", "decision_kind"),
                                            ("temporary_reason", "temporary_reason"), ("reuse_reason", "reuse_reason"))
                ):
                    raise WorkspaceServiceError("IMAGE_SCOPE_MISMATCH", "未选择镜头的已有配图决定不能被改写")
                preserved_decisions[no] = old
        brief_version_id, brief_snapshot_id = self._brief_binding(context, plan.confirmed_brief_input)
        pool = self.services.images.resolve_for_delivery(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            brief_version_id=brief_version_id,
            brief_snapshot_id=brief_snapshot_id,
            expected_project_lock_version=context.project_lock_version,
        )
        self._require_reuse_reasons(pool, normalized, preserved_shot_nos=preserved_nos)
        decisions = self._decisions(
            context, draft.script_rows, pool, normalized, auth_token,
        )
        for decision in decisions:
            old = preserved_decisions.get(decision["shot_no"])
            if old:
                if decision.get("image_sha256", "") != old.get("image_sha256", ""):
                    raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "未修改镜头的图片身份已变化")
                decision.update({key: deepcopy(value) for key, value in old.items()
                                 if key != "confirmation_scope"})
        previous = self._read_snapshot(context)
        if previous and all(
            previous.get(item["shot_no"], {}).get("decision_kind") == item["decision_kind"]
            and previous.get(item["shot_no"], {}).get("image_id", "") == item.get("image_id", "")
            and previous.get(item["shot_no"], {}).get("temporary_reason", "") == item.get("temporary_reason", "")
            for item in decisions
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_NOT_CHANGED", "图片选择没有变化")
        actor_id = self._actor(auth_token, context, "review.record")
        created_at = utc_now()
        child_version_id = new_id("version")
        snapshot_id = new_id("image_decision_snapshot")
        for item in decisions:
            item["decision_id"] = new_id("image_decision")
            item.setdefault("decided_by_actor_id", actor_id)
            item.setdefault("decided_at", created_at)
        candidate_snapshot_sha = sha256_json([
            item.get("candidate_snapshot_digest") or "compatibility-snapshot"
            for item in normalized
        ])
        if working_evidence is not None:
            candidate_snapshot_sha = working_evidence["snapshot_digest"]
        materialized = self.materializer.materialize(
            context=context,
            child_version_id=child_version_id,
            snapshot_id=snapshot_id,
            decisions=decisions,
            request_sha256=request_sha,
            created_by_actor_id=actor_id,
            created_at=created_at,
        )
        self._commit(
            auth_token,
            context=context,
            child_version_id=child_version_id,
            snapshot_id=snapshot_id,
            decisions=decisions,
            materialized=materialized,
            idempotency_key=key,
            request_sha256=request_sha,
            candidate_snapshot_sha256=candidate_snapshot_sha,
            actor_id=actor_id,
            created_at=created_at,
            submitted_shot_ids=allowed_shot_ids,
            working_evidence=working_evidence,
            expected_progress_revision=progress_revision,
        )
        return {
            **self.read_current(
                auth_token, workspace_id=workspace_id, project_id=project_id,
            ),
            "save_status": "completed",
        }

    def _current_context(
        self,
        auth_token: str,
        workspace_id: str,
        project_id: str,
        expected_project_version: int | None,
        *,
        reuse_immutable: bool = False,
        version_id: str | None = None,
    ) -> tuple[dict, VersionDeliveryContext | None]:
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
        if expected_project_version is not None:
            check_version(
                int(project["project_version"]), expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
        anchor = str(project.get("current_version_id") or "")
        version_id = version_id if version_id is not None else anchor
        if not version_id:
            return project, None
        return project, self.services.delivery.resolve(
            auth_token,
            project_id,
            version_id,
            expected_project_version=expected_project_version,
            reuse_immutable=reuse_immutable, ancestor_of=anchor,
        )

    def _brief_binding(
        self, context: VersionDeliveryContext, evidence: dict, *, require_current: bool = True,
    ) -> tuple[str, str]:
        if not isinstance(evidence, dict):
            raise WorkspaceServiceError("FORMAL_BRIEF_DRIFT", "脚本缺少正式 Brief 绑定")
        brief_version_id = str(evidence.get("brief_version_id") or "")
        snapshot_id = str(evidence.get("brief_snapshot_id") or "")
        confirmation_id = str(evidence.get("confirmation_id") or "")
        payload = evidence.get("payload")
        if (
            evidence.get("workspace_id") != context.workspace_id
            or evidence.get("project_id") != context.project_id
            or evidence.get("confirmation_status") != "confirmed"
            or evidence.get("unresolved_conflict_count") != 0
            or not all((brief_version_id, snapshot_id, confirmation_id))
            or not isinstance(payload, dict)
        ):
            raise WorkspaceServiceError("FORMAL_BRIEF_DRIFT", "脚本正式 Brief 绑定无效")
        with self.services.store.connection() as connection:
            row = connection.execute(
                """SELECT p.current_brief_snapshot_id, v.payload_ref, v.payload_sha256
                FROM projects p
                JOIN brief_confirmations c ON c.project_id=p.project_id
                JOIN brief_versions v ON v.brief_version_id=c.brief_version_id
                WHERE p.project_id=? AND p.workspace_id=?
                  AND c.confirmation_id=? AND c.brief_version_id=?
                  AND c.brief_snapshot_id=? AND c.confirmation_status='confirmed'
                  AND c.unresolved_conflict_count=0""",
                (
                    context.project_id, context.workspace_id, confirmation_id,
                    brief_version_id, snapshot_id,
                ),
            ).fetchone()
        if row is None or (
            (require_current and row["current_brief_snapshot_id"] != snapshot_id)
            or payload.get("ref") != row["payload_ref"]
            or payload.get("sha256") != row["payload_sha256"]
        ):
            raise WorkspaceServiceError("FORMAL_BRIEF_DRIFT", "脚本 Brief 与项目当前确认版本不一致")
        return brief_version_id, snapshot_id

    def _read_snapshot(
        self,
        context: VersionDeliveryContext,
        *,
        version_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        resolved_version_id = str(version_id or context.version_id)
        resolved_run_id = str(run_id or context.run_id)
        with self.services.store.connection() as connection:
            snapshot = connection.execute(
                """SELECT *, 'v3' AS storage_version FROM version_image_decision_snapshots_v3
                WHERE workspace_id=? AND project_id=? AND materialized_version_id=?""",
                (context.workspace_id, context.project_id, resolved_version_id),
            ).fetchone()
            if snapshot is None:
                snapshot = connection.execute(
                """SELECT *, 'v2' AS storage_version FROM version_image_decision_snapshots_v2
                WHERE workspace_id=? AND project_id=? AND materialized_version_id=?""",
                (context.workspace_id, context.project_id, resolved_version_id),
            ).fetchone()
            if snapshot is None:
                snapshot = connection.execute(
                """SELECT *, 'v1' AS storage_version FROM version_image_decision_snapshots
                WHERE workspace_id=? AND project_id=? AND materialized_version_id=?""",
                    (context.workspace_id, context.project_id, resolved_version_id),
                ).fetchone()
            if snapshot is None:
                return {}
            table = (
                "version_image_decisions_v3" if snapshot["storage_version"] == "v3"
                else "version_image_decisions_v2" if snapshot["storage_version"] == "v2"
                else "version_image_decisions"
            )
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE snapshot_id=? ORDER BY shot_no",
                (snapshot["snapshot_id"],),
            ).fetchall()
        try:
            artifact = self.services.artifacts.resolve_artifact(resolved_run_id, "image_decisions")
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片决定快照无法读取") from exc
        if artifact.ref != snapshot["snapshot_ref"] or artifact.sha256 != snapshot["snapshot_sha256"]:
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片决定快照绑定漂移")
        payload = _read_json(artifact.path, "图片决定快照")
        if (
            payload.get("schema_version") not in {
                "mplus-version-image-decisions/v1", "mplus-version-image-decisions/v2",
                "mplus-version-image-decisions/v3",
            }
            or payload.get("snapshot_id") != snapshot["snapshot_id"]
            or payload.get("workspace_id") != context.workspace_id
            or payload.get("project_id") != context.project_id
            or payload.get("base_version_id") != snapshot["base_version_id"]
            or payload.get("materialized_version_id") != resolved_version_id
            or payload.get("run_id") != resolved_run_id
            or payload.get("shot_count") != len(rows)
            or (
                payload.get("confirmed_count") != len(rows)
                if snapshot["storage_version"] != "v3"
                else payload.get("image_confirmed_count") != snapshot["image_confirmed_count"]
                or payload.get("temporary_text_basis_count") != snapshot["temporary_text_basis_count"]
            )
            or payload.get("request_sha256") != snapshot["request_sha256"]
            or payload.get("created_by_actor_id") != snapshot["created_by_actor_id"]
            or payload.get("created_at") != snapshot["created_at"]
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片决定快照上下文漂移")
        raw = payload.get("decisions")
        if not isinstance(raw, list) or len(raw) != len(rows):
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片决定快照行数漂移")
        by_id = {str(item.get("decision_id") or ""): item for item in raw if isinstance(item, dict)}
        result = {}
        for row in rows:
            item = by_id.get(str(row["decision_id"]))
            if snapshot["storage_version"] == "v3":
                kind = str(row["decision_kind"])
                common = {
                    "shot_no": str(row["shot_no"]),
                    "decision_kind": kind,
                    "decided_by_actor_id": str(row["decided_by_actor_id"]),
                    "decided_at": str(row["decided_at"]),
                }
                if kind == "temporary_text_basis":
                    expected = {
                        **common,
                        "image_id": "",
                        "temporary_reason": str(row["temporary_reason"] or ""),
                    }
                else:
                    expected = {
                        **common,
                        "image_id": str(row["project_image_id"]),
                        "image_sha256": str(row["image_sha256"]),
                        "analysis_version_id": str(row["analysis_version_id"]),
                        "analysis_ref": str((item or {}).get("analysis_ref") or ""),
                        "analysis_sha256": str((item or {}).get("analysis_sha256") or ""),
                        "source_kind": str(row["source_kind"]),
                        "source_key": str(row["source_key"]),
                        "source_location": _json_object(
                            row["source_location_json"], "图片决定来源位置",
                        ),
                        "temporary_reason": "",
                        "reuse_reason": str((item or {}).get("reuse_reason") or ""),
                    }
                if item is None or any(item.get(key, "") != value for key, value in expected.items() if key != "image_id" or kind != "temporary_text_basis"):
                    raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片决定快照内容漂移")
                result[expected["shot_no"]] = expected
                continue
            if item is not None and payload.get("schema_version") == "mplus-version-image-decisions/v1":
                item = {
                    **item,
                    "source_kind": "brief",
                    "source_key": item.get("source_document_id"),
                }
            if item is not None:
                item = {**item, "decision_kind": "image_confirmed"}
            expected = {
                "shot_no": str(row["shot_no"]),
                "decision_kind": "image_confirmed",
                "image_id": str(row["project_image_id"]),
                "image_sha256": str(row["image_sha256"]),
                "analysis_version_id": str(row["analysis_version_id"]),
                "analysis_ref": str(row["analysis_ref"]),
                "analysis_sha256": str(row["analysis_sha256"]),
                "source_kind": (
                    str(row["source_kind"])
                    if snapshot["storage_version"] == "v2" else "brief"
                ),
                "source_key": (
                    str(row["source_key"])
                    if snapshot["storage_version"] == "v2"
                    else str(row["source_document_id"])
                ),
                "source_location": _json_object(
                    row["source_location_json"], "图片决定来源位置",
                ),
                "decided_by_actor_id": str(row["decided_by_actor_id"]),
                "decided_at": str(row["decided_at"]),
            }
            if item is None or any(item.get(key) != value for key, value in expected.items()):
                raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片决定快照内容漂移")
            result[expected["shot_no"]] = expected
        return result

    def _effective_snapshot(
        self,
        context: VersionDeliveryContext,
        shot_nos: list[str],
    ) -> tuple[dict[str, dict[str, Any]], set[str], bool]:
        """Project safe image decisions through verified script-version lineage."""
        exact = self._read_snapshot(context)
        if exact:
            return (
                {
                    shot_no: {**item, "confirmation_scope": "exact"}
                    for shot_no, item in exact.items()
                },
                set(shot_nos) - set(exact),
                True,
            )
        with self.services.store.connection() as connection:
            versions = {
                str(row["version_id"]): {
                    "version_id": str(row["version_id"]),
                    "parent_version_id": str(row["parent_version_id"] or ""),
                    "run_id": str(row["run_id"]),
                }
                for row in connection.execute(
                    """SELECT version_id, parent_version_id, run_id
                    FROM project_versions WHERE project_id=?""",
                    (context.project_id,),
                ).fetchall()
            }
            submissions = {
                str(row["materialized_version_id"]): dict(row)
                for row in connection.execute(
                    """SELECT base_version_id, materialized_version_id,
                    revision_report_ref, revision_report_sha256
                    FROM version_review_draft_submissions
                    WHERE workspace_id=? AND project_id=?""",
                    (context.workspace_id, context.project_id),
                ).fetchall()
            }
        lineage = {str(shot_no): str(shot_no) for shot_no in shot_nos}
        child_id = context.version_id
        image_fields = {"类型", "景别", "运镜", "画面描述", "source_refs"}
        visited: set[str] = set()
        while child_id and child_id not in visited:
            visited.add(child_id)
            child = versions.get(child_id)
            if child is None:
                break
            parent_id = str(child.get("parent_version_id") or "")
            if not parent_id:
                break
            parent = versions.get(parent_id)
            submission = submissions.get(child_id)
            if parent is None or submission is None or (
                str(submission.get("base_version_id") or "") != parent_id
            ):
                break
            try:
                resolved = self.services.artifacts.resolve_artifact(
                    str(child["run_id"]), "revision_report",
                )
            except ArtifactResolutionError:
                break
            if (
                resolved.ref != str(submission.get("revision_report_ref") or "")
                or resolved.sha256 != str(submission.get("revision_report_sha256") or "")
            ):
                break
            report = _read_json(resolved.path, "脚本版本差异报告")
            diff = report.get("version_diff")
            if (
                not isinstance(diff, dict)
                or diff.get("project_id") != context.project_id
                or diff.get("parent_version_id") != parent_id
                or diff.get("child_version_id") != child_id
                or diff.get("lineage_needs_review")
                or (report.get("actual_impact") or {}).get("lineage_needs_review")
            ):
                break
            edge: dict[str, str] = {}
            for item in diff.get("shot_diffs") or []:
                if not isinstance(item, dict) or item.get("relation") not in {"preserved", "moved"}:
                    continue
                child_no = str(item.get("child_shot_no") or "")
                parent_nos = [str(value) for value in item.get("parent_shot_nos") or []]
                parent_ids = [str(value) for value in item.get("parent_shot_ids") or []]
                child_shot_id = str(item.get("child_shot_id") or "")
                changed_fields = {
                    str(field.get("field") or "")
                    for field in item.get("field_diffs") or []
                    if isinstance(field, dict) and field.get("status") != "unchanged"
                }
                if (
                    child_no and len(parent_nos) == 1 and len(parent_ids) == 1
                    and child_shot_id and child_shot_id == parent_ids[0]
                    and not changed_fields.intersection(image_fields)
                ):
                    edge[child_no] = parent_nos[0]
            lineage = {
                current_no: edge[edge_child_no]
                for current_no, edge_child_no in lineage.items()
                if edge_child_no in edge
            }
            if not lineage:
                break
            parent_snapshot = self._read_snapshot(
                context,
                version_id=parent_id,
                run_id=str(parent["run_id"]),
            )
            if parent_snapshot:
                inherited = {
                    current_no: {
                        **parent_snapshot[parent_no],
                        "shot_no": current_no,
                        "confirmation_scope": "inherited",
                        "inherited_from_version_id": parent_id,
                    }
                    for current_no, parent_no in lineage.items()
                    if parent_no in parent_snapshot
                    and parent_snapshot[parent_no].get("decision_kind") == "image_confirmed"
                }
                return inherited, set(shot_nos) - set(inherited), False
            child_id = parent_id
        return {}, set(shot_nos), False

    @staticmethod
    def _ranked_selections(
        script_rows: list[dict], detail_rows: list[dict], brief: dict,
        assets: tuple[ProjectImageDeliveryAsset, ...],
        *, shot_plans: list[dict] | None = None,
        excluded_visuals=(), preferred_visuals=(), forced_visual: str = "",
        feedback_by_shot: dict[str, dict] | None = None,
    ) -> dict[str, dict]:
        details = formal_image_details_by_script_shot(script_rows, detail_rows)
        feedback_by_shot = feedback_by_shot or {}
        ranked = {}
        for index, row in enumerate(script_rows, start=1):
            shot_no = _shot_no(row)
            detail = details.get(shot_no, {})
            planned = (
                shot_plans[index - 1]
                if shot_plans and index <= len(shot_plans) else {}
            )
            slot = formal_image_search_slot(
                row=row,
                detail=detail,
                planned=planned,
                fallback_index=index,
            )
            ranked[shot_no] = search_project_candidates(
                slot=slot,
                brief=brief, assets=assets, top_k=None,
                excluded_visuals=(
                    *excluded_visuals,
                    *feedback_by_shot.get(shot_no, {}).get("excluded", ()),
                ),
                preferred_visuals=(
                    *preferred_visuals,
                    *feedback_by_shot.get(shot_no, {}).get("preferred", ()),
                ),
                forced_visual=str(
                    feedback_by_shot.get(shot_no, {}).get("forced") or forced_visual
                ),
            )
        return ranked if assets else {}

    def _image_feedback_policy(self, context: VersionDeliveryContext) -> dict:
        lineage = []
        current = context.version_id
        with self.services.store.connection() as connection:
            while current:
                lineage.append(current)
                row = connection.execute(
                    "SELECT parent_version_id FROM project_versions WHERE project_id=? AND version_id=?",
                    (context.project_id, current),
                ).fetchone()
                current = str(row["parent_version_id"] or "") if row else ""
            placeholders = ",".join("?" for _ in lineage)
            rows = connection.execute(
                f"""SELECT payload_ref, payload_sha256 FROM formal_version_feedback_records
                WHERE project_id=? AND feedback_kind='image' AND base_version_id IN ({placeholders})
                ORDER BY created_at, feedback_id""",
                (context.project_id, *lineage),
            ).fetchall()
        excluded: list[str] = []
        preferred: list[str] = []
        forced = ""
        by_shot: dict[str, dict[str, Any]] = {}
        for row in rows:
            payload_ref = str(row["payload_ref"] or "")
            payload_sha256 = str(row["payload_sha256"] or "")
            if payload_ref == f"artifact://project-image-feedback/{payload_sha256}":
                payload = self.services.images.artifacts.read_feedback(
                    ArtifactEvidence(payload_ref, payload_sha256),
                )
            else:
                parts = payload_ref.split("/")
                if len(parts) != 5 or parts[:3] != ["artifact:", "", "run"]:
                    raise WorkspaceServiceError("IMAGE_FEEDBACK_EVIDENCE_DRIFT", "图片反馈证据引用无效")
                try:
                    artifact = self.services.artifacts.resolve_artifact(parts[3], parts[4])
                except ArtifactResolutionError as exc:
                    raise WorkspaceServiceError("IMAGE_FEEDBACK_EVIDENCE_DRIFT", "图片反馈证据不可恢复") from exc
                if artifact.sha256 != payload_sha256:
                    raise WorkspaceServiceError("IMAGE_FEEDBACK_EVIDENCE_DRIFT", "图片反馈证据 SHA 已变化")
                try:
                    payload = json.loads(artifact.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise WorkspaceServiceError("IMAGE_FEEDBACK_EVIDENCE_DRIFT", "图片反馈证据内容无效") from exc
            policy = payload.get("image_feedback") or payload
            shot_policies = policy.get("shot_policies") or []
            if shot_policies:
                for shot_policy in shot_policies:
                    shot_no = str(shot_policy.get("shot_no") or "")
                    if not shot_no:
                        continue
                    current = by_shot.setdefault(
                        shot_no, {"excluded": [], "preferred": [], "forced": ""},
                    )
                    current["excluded"].extend(
                        str(item) for item in shot_policy.get("excluded_image_ids") or [] if str(item)
                    )
                    current["preferred"].extend(
                        str(item) for item in shot_policy.get("preferred_image_ids") or [] if str(item)
                    )
                    if str(shot_policy.get("forced_image_id") or ""):
                        current["forced"] = str(shot_policy["forced_image_id"])
            else:
                excluded.extend(str(item) for item in policy.get("excluded_image_ids") or [] if str(item))
                preferred.extend(str(item) for item in policy.get("preferred_image_ids") or [] if str(item))
                if str(policy.get("forced_image_id") or ""):
                    forced = str(policy["forced_image_id"])
        return {
            "excluded": tuple(dict.fromkeys(excluded)),
            "preferred": tuple(dict.fromkeys(preferred)),
            "forced": forced,
            "by_shot": {
                shot_no: {
                    "excluded": tuple(dict.fromkeys(value["excluded"])),
                    "preferred": tuple(dict.fromkeys(value["preferred"])),
                    "forced": value["forced"],
                }
                for shot_no, value in by_shot.items()
            },
        }

    @staticmethod
    def _employee_asset(
        asset: ProjectImageDeliveryAsset,
        match: dict | None = None,
        candidate_pool: list[dict] | None = None,
    ) -> dict:
        summary = asset.business_summary
        media_ids = []
        for source in asset.source_links:
            document_id = str(source.source_document_id or "")
            if document_id.startswith("package:") and ":" in document_id:
                media_ids.append(document_id.rsplit(":", 1)[-1])
        sources = [
            {
                "label": source.source_label,
                "file": source.source_label,
                **({"page": source.page} if source.page is not None else {}),
                **({"sheet": source.sheet} if source.sheet is not None else {}),
                **({"row": source.row} if source.row is not None else {}),
                **({"slide": source.slide} if source.slide is not None else {}),
                **({"image": source.image} if source.image is not None else {}),
            }
            for source in asset.source_links
        ]
        match = match or {}
        cluster_id = str(match.get("perceptual_cluster_id") or "")
        near_duplicate_count = len({
            str(item.get("image_id") or "")
            for item in (candidate_pool or [])
            if cluster_id
            and str(item.get("perceptual_cluster_id") or "") == cluster_id
            and str(item.get("image_id") or "") != asset.image_id
        })
        source_category = (
            "创意库" if any(str(item.get("sheet") or "") == "创意库" for item in sources)
            else "模板" if media_ids else "项目素材"
        )
        explanation = build_match_explanation(
            match, sources=sources, source_category=source_category,
            shot_functions=(summary.purpose,),
            near_duplicate=(
                f"候选池另有 {near_duplicate_count} 张近重复画面，采用前需核对"
                if near_duplicate_count else "当前候选池未发现近重复画面"
            ),
        )
        return {
            "image_key": asset.image_id,
            "content_sha256": asset.image_sha256,
            "media_id": media_ids[0] if len(set(media_ids)) == 1 else "",
            "caption": summary.caption,
            "actual_caption": "" if getattr(summary, "visual_analysis_available", None) is False else summary.caption,
            "template_usage": str(getattr(summary, "source_semantic_text", "") or ""),
            "tags": list(summary.tags),
            "subjects": list(summary.subjects),
            "scene": summary.scene,
            "purpose": summary.purpose,
            "quality": summary.quality,
            "brand": summary.brand,
            "sources": sources,
            "match_basis": str(match.get("match_reason") or ""),
            "score": int(match.get("score") or 0),
            "conflicts": list(match.get("conflicts") or []),
            "default_eligible": bool(match.get("default_eligible", False)),
            "suggestion_eligible": bool(match.get("suggestion_eligible", False)),
            "suggestion_kind": str(match.get("suggestion_kind") or "composition_reference"),
            "suggestion_reason": str(match.get("suggestion_reason") or ""),
            "visual_evidence_bound": bool(match.get("visual_evidence_bound", False)),
            "match_tier": str(match.get("match_tier") or ""),
            "brand_warning": str(match.get("brand_warning") or ""),
            "requires_human_review": bool(match.get("requires_human_review")),
            "rank_components": dict(match.get("rank_components") or {}),
            "quality_score": int(match.get("quality_score") or 0),
            "quality_evidence": dict(match.get("quality_evidence") or {}),
            "perceptual_cluster_id": cluster_id,
            "duplicate_group_id": str(match.get("duplicate_group_id") or ""),
            "duplicate_of": str(match.get("duplicate_of") or ""),
            "duplicate_count": int(match.get("duplicate_count") or 0),
            "match_explanation": explanation,
        }

    def _decisions(
        self,
        context: VersionDeliveryContext,
        script_rows: list[dict],
        pool: ProjectImageDeliveryPool,
        selections: list[dict[str, str]],
        auth_token: str,
    ) -> list[dict[str, Any]]:
        shots = [_shot_no(row) for row in script_rows]
        submitted_shots = [item["shot_no"] for item in selections]
        if (
            submitted_shots != shots
            or len(set(submitted_shots)) != len(shots)
        ):
            raise WorkspaceServiceError(
                "IMAGE_DECISION_MISMATCH", "必须按脚本顺序为每镜确认一张图片",
            )
        assets = {item.image_id: item for item in pool.assets}
        previous, _, _ = self._effective_snapshot(context, shots)
        result = []
        for selection in selections:
            if selection["decision_kind"] == "temporary_text_basis":
                result.append({
                    "shot_no": selection["shot_no"],
                    "decision_kind": "temporary_text_basis",
                    "image_id": "",
                    "image_sha256": "",
                    "analysis_version_id": "",
                    "analysis_ref": "",
                    "analysis_sha256": "",
                    "source_kind": "",
                    "source_key": "",
                    "source_location": {},
                    "summary": selection["temporary_reason"],
                    "tags": [],
                    "quality": "",
                    "temporary_reason": selection["temporary_reason"],
                    "reuse_reason": "",
                })
                continue
            asset = assets.get(selection["image_key"])
            if asset is None:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "图片不属于当前项目可用图片池")
            source = min(
                asset.source_links,
                key=lambda item: (
                    item.source_label, item.sheet or "", item.row or 0,
                    item.page or 0, item.slide or 0, item.image or 0,
                ),
            )
            generated = source.source_document_id.startswith("generated:")
            source_sheet = source.sheet or (
                f"PDF 第{source.page}页" if source.page else
                f"PPT 第{source.slide}页" if source.slide else
                "图片"
            )
            source_row = source.row or source.image or 1
            source_kind = (
                "generated" if generated else
                "asset_package"
                if source.source_document_id.startswith("package:")
                else "brief"
            )
            result.append({
                "shot_no": selection["shot_no"],
                "decision_kind": "image_confirmed",
                "image_id": asset.image_id,
                "image_sha256": asset.image_sha256,
                "controlled_path": str(asset.controlled_path),
                "analysis_version_id": asset.analysis.analysis_version_id,
                "analysis_ref": asset.analysis.evidence.ref,
                "analysis_sha256": asset.analysis.evidence.sha256,
                "source_kind": source_kind,
                "source_key": source.source_document_id,
                "source_location": {
                    "file": source.source_label,
                    **({"task_id": source.source_document_id.removeprefix("generated:"), "image_sha256": asset.image_sha256}
                       if generated else {"sheet": source_sheet, "row": source_row}),
                    **({"page": source.page} if source.page is not None else {}),
                    **({"slide": source.slide} if source.slide is not None else {}),
                    **({"image": source.image} if source.image is not None else {}),
                },
                "summary": asset.business_summary.caption,
                "tags": list(asset.business_summary.tags),
                "quality": asset.business_summary.quality,
                "analysis": self.services.images.artifacts.read_analysis(asset.analysis.evidence),
                "temporary_reason": "",
                "reuse_reason": selection.get("reuse_reason", ""),
                "excluded_image_id": (
                    str(previous.get(selection["shot_no"], {}).get("image_id") or "")
                    if previous.get(selection["shot_no"], {}).get("image_id")
                    and previous.get(selection["shot_no"], {}).get("image_id") != asset.image_id
                    else ""
                ),
            })
        return result

    @staticmethod
    def _require_reuse_reasons(
        pool: ProjectImageDeliveryPool,
        selections: list[dict[str, str]],
        *, preserved_shot_nos: set[str] | None = None,
    ) -> dict[str, str]:
        clusters = perceptual_cluster_ids([
            {
                "image_id": asset.image_id,
                "path": asset.controlled_path,
                "perceptual_hash": asset.business_summary.perceptual_hash,
            }
            for asset in pool.assets
        ])
        counts: dict[str, int] = {}
        for selection in selections:
            image_id = str(selection.get("image_key") or "")
            if image_id:
                cluster = clusters.get(image_id, image_id)
                counts[cluster] = counts.get(cluster, 0) + 1
        issues = []
        for selection in selections:
            image_id = str(selection.get("image_key") or "")
            cluster = clusters.get(image_id, image_id)
            if (
                image_id
                and selection["shot_no"] not in (preserved_shot_nos or set())
                and counts.get(cluster, 0) > 1
                and not str(selection.get("reuse_reason") or "").strip()
            ):
                issues.append({"shot_no": selection["shot_no"],
                    "shot_id": str(selection.get("shot_id") or ""),
                    "reason": "同图或近似图片用于多镜，请补充复用说明"})
        if issues:
            error = WorkspaceServiceError(
                "IMAGE_REUSE_REASON_REQUIRED",
                "感知近似图片跨镜复用时必须填写逐镜理由",
            )
            error.issues = issues
            raise error
        return clusters

    def _actor(self, auth_token: str, context: VersionDeliveryContext, permission: str) -> str:
        with self.services.store.connection() as connection:
            return str(require_permission(
                connection,
                auth_token,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                permission=permission,
            )["actor_id"])

    def _history_image_revision_parameters(
        self,
        auth_token: str,
        *,
        task_id: str,
        workspace_id: str,
        project_id: str,
        base_version_id: str,
        expected_project_version: int,
        shot_no: str | None,
        context: VersionDeliveryContext,
    ) -> dict:
        tasks = TaskService(self.services.store)
        task = tasks.get_task(auth_token, task_id)
        details = next((
            event.get("details", {}).get("internal_formal_parameters")
            for event in reversed(tasks.list_task_events(auth_token, task_id))
            if isinstance(event.get("details", {}).get("internal_formal_parameters"), dict)
        ), None)
        parameters = details or {}
        if (
            task["project_id"] != project_id
            or task["task_type"] != "formal_image_feedback"
            or task["status"] != "proposed"
            or parameters.get("entrypoint") != "history"
            or parameters.get("workspace_id") != workspace_id
            or parameters.get("project_id") != project_id
            or parameters.get("base_version_id") != base_version_id
            or parameters.get("expected_project_version") != expected_project_version
            or (shot_no is not None and str(parameters.get("shot_no") or "") != shot_no)
        ):
            raise WorkspaceServiceError("IMAGE_FEEDBACK_TASK_INVALID", "历史图片反馈任务与当前项目镜头不一致")
        snapshot = self._read_snapshot(context) or {}
        current = snapshot.get(str(parameters.get("shot_no") or "")) or {}
        if (
            current.get("decision_kind") != "image_confirmed"
            or str(current.get("image_id") or "") != str(parameters.get("image_key") or "")
        ):
            raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "历史图片反馈对应的当前图片已变化")
        return parameters

    def _replay(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> dict | None:
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
        actor_id = self._actor_for_project(auth_token, workspace_id, project_id)
        with self.services.store.connection() as connection:
            row = connection.execute(
                """SELECT request_sha256 FROM version_image_decision_snapshots_v3
                WHERE created_by_actor_id=? AND project_id=? AND idempotency_key=?""",
                (actor_id, project_id, idempotency_key),
            ).fetchone()
            if row is None:
                row = connection.execute(
                """SELECT request_sha256 FROM version_image_decision_snapshots_v2
                WHERE created_by_actor_id=? AND project_id=? AND idempotency_key=?""",
                (actor_id, project_id, idempotency_key),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """SELECT request_sha256 FROM version_image_decision_snapshots
                    WHERE created_by_actor_id=? AND project_id=? AND idempotency_key=?""",
                    (actor_id, project_id, idempotency_key),
                ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "幂等键已绑定不同图片决定")
        return {
            **self.read_current(
                auth_token, workspace_id=workspace_id, project_id=project_id,
            ),
            "save_status": "completed",
        }

    def _actor_for_project(self, auth_token: str, workspace_id: str, project_id: str) -> str:
        with self.services.store.connection() as connection:
            return str(require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="project.read",
            )["actor_id"])

    def validate_committing_decisions(self, connection, context, decisions) -> None:
        for decision in decisions:
            if decision["decision_kind"] == "temporary_text_basis":
                continue
            row = connection.execute(
                """SELECT i.image_sha256, a.project_image_analysis_version_id,
                a.artifact_ref, a.artifact_sha256, a.status
                FROM project_images i
                JOIN project_image_analysis_current_links l ON l.project_image_id=i.project_image_id
                JOIN project_image_analysis_versions a
                  ON a.project_image_analysis_version_id=l.project_image_analysis_version_id
                WHERE i.project_image_id=? AND i.workspace_id=? AND i.project_id=?""",
                (decision["image_id"], context.workspace_id, context.project_id),
            ).fetchone()
            expected = (
                decision["image_sha256"], decision["analysis_version_id"],
                decision["analysis_ref"], decision["analysis_sha256"], "pass",
            )
            if row is None or tuple(row) != expected:
                raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片或理解证据在保存时变化")
            if decision["source_kind"] == "generated":
                source_valid = connection.execute(
                    """SELECT 1 FROM project_image_generated_sources g JOIN tasks t ON t.task_id=g.task_id
                    WHERE g.project_image_id=? AND 'generated:' || g.task_id=?
                      AND t.project_id=? AND t.status='completed' AND t.result_ref=g.result_ref""",
                    (decision["image_id"], decision["source_key"], context.project_id),
                ).fetchone() is not None
            elif decision["source_kind"] == "brief":
                links = connection.execute(
                    """SELECT source_location_json FROM project_image_source_links
                    WHERE project_image_id=? AND source_document_id=?""",
                    (decision["image_id"], decision["source_key"]),
                ).fetchall()
                source_valid = any(
                    _source_location_matches(
                        link["source_location_json"], decision["source_location"],
                    )
                    for link in links
                )
            else:
                links = connection.execute(
                    """SELECT source_file, source_sheet, source_row
                    FROM project_image_package_source_links
                    WHERE project_image_id=?
                      AND 'package:' || package_id || '@' || package_version || ':' || media_id=?""",
                    (decision["image_id"], decision["source_key"]),
                ).fetchall()
                source_valid = any(
                    (
                        str(link["source_file"]), str(link["source_sheet"]), int(link["source_row"])
                    ) == (
                        decision["source_location"]["file"],
                        decision["source_location"]["sheet"],
                        int(decision["source_location"]["row"]),
                    )
                    for link in links
                )
            if not source_valid:
                    raise WorkspaceServiceError(
                        "IMAGE_DECISION_DRIFT", "图片来源证据在保存时变化",
                    )

    def validate_working_commit(self, connection, context, evidence) -> None:
        revision, _ = self._progress_script(connection, context)
        row = connection.execute("SELECT revision FROM formal_image_progress WHERE workspace_id=? AND project_id=? AND base_version_id=?",
            (context.workspace_id, context.project_id, context.version_id)).fetchone()
        if revision != evidence["script_revision"]:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "保存期间文字草稿已变化，原进度保留")
        if int(row["revision"] if row else 0) != evidence["progress_revision"]:
            raise WorkspaceServiceError("IMAGE_PROGRESS_CONFLICT", "保存期间选图进度已变化，原进度保留")
        self.validate_committing_decisions(connection, context, evidence["decisions"])
        for decision in evidence["decisions"]:
            if sha256_file(Path(decision["controlled_path"])) != decision["image_sha256"]:
                raise WorkspaceServiceError("IMAGE_ARTIFACT_DRIFT", "保存期间图片文件已变化，原进度保留")

    @staticmethod
    def _progress_revision(connection, context) -> int:
        row = connection.execute(
            'SELECT revision FROM formal_image_progress WHERE workspace_id=? AND project_id=? AND base_version_id=?',
            (context.workspace_id, context.project_id, context.version_id),
        ).fetchone()
        return int(row['revision']) if row else 0

    def _commit(
        self,
        auth_token: str,
        *,
        context: VersionDeliveryContext,
        child_version_id: str,
        snapshot_id: str,
        decisions: list[dict[str, Any]],
        materialized: dict[str, str],
        idempotency_key: str,
        request_sha256: str,
        candidate_snapshot_sha256: str,
        actor_id: str,
        created_at: str,
        submitted_shot_ids=None,
        working_evidence=None,
        expected_progress_revision=None,
    ) -> None:
        with self.services.store.transaction() as connection:
            if expected_progress_revision is not None and self._progress_revision(connection, context) != expected_progress_revision:
                raise WorkspaceServiceError('IMAGE_PROGRESS_CONFLICT', '保存期间选图进度已变化，当前草稿已保留')
            if working_evidence is not None:
                self.validate_working_commit(connection, context, working_evidence)
            current = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (context.project_id,),
            ).fetchone()
            if current is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            if current["workspace_id"] != context.workspace_id:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
            require_active_project(current)
            check_version(
                int(current["lock_version"]), context.project_lock_version,
                "PROJECT_VERSION_CONFLICT",
            )
            if current["current_version_id"] != context.version_id:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "当前脚本版本已变化")
            require_no_unsaved_script_draft(
                connection,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                base_version_id=context.version_id,
            )
            run_actor = require_permission(
                connection,
                auth_token,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                permission="run.bind",
            )
            version_actor = require_permission(
                connection,
                auth_token,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                permission="version.create",
            )
            review_actor = require_permission(
                connection,
                auth_token,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                permission="review.record",
            )
            if {str(run_actor["actor_id"]), str(version_actor["actor_id"]), str(review_actor["actor_id"])} != {actor_id}:
                raise WorkspaceServiceError("PERMISSION_DENIED", "图片确认、运行与版本操作人不一致")
            if connection.execute(
                "SELECT 1 FROM project_runs WHERE run_id=?", (materialized["run_id"],),
            ).fetchone() is not None:
                raise WorkspaceServiceError("RUN_ALREADY_BOUND", "运行已绑定")
            self.validate_committing_decisions(connection, context, decisions)
            connection.execute(
                """INSERT INTO project_runs(
                run_id, project_id, parent_run_id, source_kind,
                manifest_ref, manifest_sha256, bound_by_actor_id, bound_at
                ) VALUES (?, ?, ?, 'native', ?, ?, ?, ?)""",
                (
                    materialized["run_id"], context.project_id, context.run_id,
                    materialized["manifest_ref"], materialized["manifest_sha256"],
                    actor_id, created_at,
                ),
            )
            connection.execute(
                """INSERT INTO project_versions(
                version_id, project_id, run_id, parent_version_id,
                content_sha256, artifact_ref, created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    child_version_id, context.project_id, materialized["run_id"],
                    context.version_id, materialized["content_sha256"],
                    materialized["artifact_ref"], actor_id, created_at,
                ),
            )
            if materialized.get("feedback_ref"):
                replaced_shots = [
                    str(item["shot_no"]) for item in decisions if item.get("excluded_image_id")
                ]
                connection.execute(
                    """INSERT INTO formal_version_feedback_records(
                    feedback_id, workspace_id, project_id, base_version_id, child_version_id,
                    feedback_kind, shot_no, payload_ref, payload_sha256,
                    created_by_actor_id, created_at)
                    VALUES (?, ?, ?, ?, ?, 'image', ?, ?, ?, ?, ?)""",
                    (
                        new_id("formal_feedback"), context.workspace_id, context.project_id,
                        context.version_id, child_version_id,
                        ",".join(replaced_shots), materialized["feedback_ref"],
                        materialized["feedback_sha256"], actor_id, created_at,
                    ),
                )
            temporary_count = sum(
                item["decision_kind"] == "temporary_text_basis" for item in decisions
            )
            confirmed_count = len(decisions) - temporary_count
            connection.execute(
                """INSERT INTO version_image_decision_snapshots_v3 VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    snapshot_id, context.workspace_id, context.project_id,
                    context.version_id, child_version_id, materialized["run_id"],
                    len(decisions), confirmed_count, temporary_count,
                    materialized["snapshot_ref"],
                    materialized["snapshot_sha256"], request_sha256,
                    candidate_snapshot_sha256,
                    idempotency_key, actor_id, created_at,
                ),
            )
            for decision in decisions:
                connection.execute(
                    """INSERT INTO version_image_decisions_v3 VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )""",
                    (
                        decision["decision_id"], snapshot_id, decision["shot_no"],
                        decision["decision_kind"],
                        decision.get("image_id") or None,
                        decision.get("image_sha256") or None,
                        decision.get("analysis_version_id") or None,
                        decision.get("source_kind") or None,
                        decision.get("source_key") or None,
                        json.dumps(decision["source_location"], ensure_ascii=False, sort_keys=True)
                        if decision.get("source_location") else None,
                        decision.get("temporary_reason") or None,
                        decision["decided_by_actor_id"], decision["decided_at"],
                    ),
                )
            self.inherit_progress(
                connection, workspace_id=context.workspace_id, project_id=context.project_id,
                parent_version_id=context.version_id, child_version_id=child_version_id,
                materialized=materialized, image_commit=True, submitted_shot_ids=submitted_shot_ids,
            )
            parent_completion = connection.execute(
                "SELECT generation_mode FROM version_completion_states WHERE version_id=?",
                (context.version_id,),
            ).fetchone()
            connection.execute(
                """INSERT INTO version_completion_states(
                    completion_state_id, workspace_id, project_id, version_id,
                    generation_mode, image_state, review_eligible, delivery_eligible,
                    evidence_ref, evidence_sha256, created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("completion_state"), context.workspace_id, context.project_id,
                    child_version_id,
                    str(parent_completion["generation_mode"]) if parent_completion else "image_backed",
                    "temporary_text_basis" if temporary_count else "images_confirmed",
                    0 if temporary_count else 1,
                    0 if temporary_count else 1,
                    materialized["snapshot_ref"], materialized["snapshot_sha256"], actor_id, created_at,
                ),
            )
            updated = connection.execute(
                """UPDATE projects SET current_version_id=?, lock_version=lock_version+2,
                updated_at=? WHERE project_id=? AND current_version_id=? AND lock_version=?""",
                (
                    child_version_id, created_at, context.project_id,
                    context.version_id, context.project_lock_version,
                ),
            )
            if updated.rowcount != 1:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目在保存图片时已变化")
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=context.project_id,
                event_type="project.run_bound",
                payload={
                    "manifest_ref": materialized["manifest_ref"],
                    "manifest_sha256": materialized["manifest_sha256"],
                    "run_id": materialized["run_id"],
                    "source_kind": "native",
                },
            )
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=context.project_id,
                event_type="project.version_created",
                payload={
                    "artifact_ref": materialized["artifact_ref"],
                    "content_sha256": materialized["content_sha256"],
                    "run_id": materialized["run_id"],
                    "version_id": child_version_id,
                },
            )

    def _employee_history(self, auth_token: str, project_id: str, versions: list[dict]) -> list[dict]:
        with self.services.store.connection() as connection:
            snapshots = {
                str(row["materialized_version_id"]): dict(row)
                for row in connection.execute(
                    """SELECT materialized_version_id, shot_count, created_at
                    FROM version_image_decision_snapshots WHERE project_id=?
                    UNION ALL
                    SELECT materialized_version_id, shot_count, created_at
                    FROM version_image_decision_snapshots_v2 WHERE project_id=?
                    UNION ALL
                    SELECT materialized_version_id, shot_count, created_at
                    FROM version_image_decision_snapshots_v3 WHERE project_id=?""",
                    (project_id, project_id, project_id),
                ).fetchall()
            }
        return [
            {
                "version_no": index,
                "label": f"版本 {index}",
                "is_current": index == len(versions),
                "change_summary": (
                    f"已确认 {snapshots[str(version['version_id'])]['shot_count']} 镜图片"
                    if str(version["version_id"]) in snapshots
                    else "脚本内容版本"
                ),
                "created_at": str(version.get("created_at") or ""),
            }
            for index, version in enumerate(versions, start=1)
        ]


def _shot_no(row: dict) -> str:
    value = str(row.get("镜号") or row.get("shot_no") or "").strip()
    if not value:
        raise WorkspaceServiceError("ARTIFACT_INVALID", "脚本镜号为空")
    return value


def formal_image_details_by_script_shot(
    script_rows: list[dict], detail_rows: list[dict],
) -> dict[str, dict]:
    if len(script_rows) == len(detail_rows):
        return {
            _shot_no(script_row): detail_row
            for script_row, detail_row in zip(script_rows, detail_rows)
        }
    keyed: dict[str, dict] = {}
    duplicate = False
    for detail in detail_rows:
        key = str(detail.get("镜号") or detail.get("模板镜号") or "").strip()
        if not key or key in keyed:
            duplicate = True
            continue
        keyed[key] = detail
    expected = {_shot_no(row) for row in script_rows}
    return keyed if not duplicate and set(keyed) == expected else {}


_details_by_script_shot = formal_image_details_by_script_shot


def formal_image_search_slot(
    *, row: dict, detail: dict, planned: dict, fallback_index: int,
) -> dict:
    """Project every Formal image entry point onto one canonical visual slot."""
    logical_slot = int(
        detail.get("逻辑槽位") or detail.get("slot_index") or fallback_index
    )
    slot = {**detail, **row, "slot_index": logical_slot}
    # The saved description owns the requested picture. Old template metadata
    # remains provenance, not an instruction to keep the old brand/title scene.
    current_visual = str(row.get("画面描述") or row.get("visual") or "").strip()
    if current_visual:
        slot.update({
            "current_visual_authority": True,
            "template_visual_function": str(detail.get("槽位功能") or ""),
            "画面描述": current_visual,
            "槽位功能": "", "必须视觉证据": "", "镜头功能": "", "主视觉功能": "",
        })
        return slot
    primary_event = str(planned.get("primary_event") or "").strip()
    required_functions = desired_visual_functions(primary_event)
    effective_slot = _effective_visual_slot(logical_slot, required_functions)
    if required_functions and effective_slot != logical_slot:
        slot.update({
            "逻辑槽位": effective_slot,
            "slot_index": effective_slot,
            "槽位功能": primary_event,
            "必须视觉证据": primary_event,
            "required_visual_functions": required_functions,
        })
    return slot


def _effective_visual_slot(logical_slot: int, required_functions: tuple[str, ...]) -> int:
    if not required_functions:
        return logical_slot
    primary_function = required_functions[0]
    if primary_function in ALLOWED_FUNCTIONS_BY_SLOT.get(logical_slot, set()):
        return logical_slot
    return next(
        (
            slot_index
            for slot_index, function in EXPECTED_FUNCTION_BY_SLOT.items()
            if function == primary_function
        ),
        logical_slot,
    )


def _shot_sort_key(value: str) -> tuple[int, int | str]:
    normalized = str(value or "").strip()
    return (0, int(normalized)) if normalized.isdigit() else (1, normalized)


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceServiceError("ARTIFACT_INVALID", f"{label} 无法读取") from exc
    if not isinstance(value, dict):
        raise WorkspaceServiceError("ARTIFACT_INVALID", f"{label} 必须为 object")
    return value


def _json_object(value: str, label: str) -> dict:
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError as exc:
        raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", f"{label} 无法读取") from exc
    if not isinstance(parsed, dict):
        raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", f"{label} 必须为 object")
    return parsed


def _source_location_matches(stored_json: str, decided: dict[str, Any]) -> bool:
    stored = _json_object(stored_json, "项目图片来源位置")
    pairs = (
        ("page", "page"),
        ("sheet", "sheet"),
        ("row", "row"),
        ("slide", "slide"),
        ("image_index", "image"),
    )
    return all(
        key not in stored or stored[key] == decided.get(decision_key)
        for key, decision_key in pairs
    )


def _replace_main_images(
    path: Path, rows: list[dict], controlled_paths: dict[str, Path],
) -> None:
    from io import BytesIO

    from .excel_io import _image_cell, _preserve_workbook_package

    workbook = load_workbook(path)
    try:
        sheet = workbook["脚本"]
        headers = {
            str(sheet.cell(6, column).value or "").strip(): column
            for column in range(1, sheet.max_column + 1)
        }
        image_column = headers.get("画面示意")
        if image_column is None:
            raise WorkspaceServiceError("IMAGE_VERSION_AUDIT_FAILED", "脚本表缺少画面示意列")
        old_images = {}
        remaining = []
        for image in sheet._images:
            row_index, column = _image_cell(image)
            if column == image_column and 7 <= row_index < 7 + len(rows):
                old_images.setdefault(row_index, image)
            else:
                remaining.append(image)
        sheet._images = remaining
        for row_index, row in enumerate(rows, start=7):
            shot_no = _shot_no(row)
            image = ExcelImage(str(controlled_paths[shot_no]))
            cell = sheet.cell(row_index, image_column)
            cell.value = None
            if row_index in old_images:
                image.anchor = deepcopy(old_images[row_index].anchor)
            else:
                image.width, image.height = 320, 180
                image.anchor = cell.coordinate
                sheet.row_dimensions[row_index].height = max(
                    sheet.row_dimensions[row_index].height or 0, 210,
                )
            sheet.add_image(image)
        output = BytesIO()
        workbook.save(output)
        path.write_bytes(_preserve_workbook_package(path, output.getvalue(), {"脚本"}))
    finally:
        workbook.close()


def _verify_workbook(
    path: Path, decisions: list[dict[str, Any]], controlled_paths: dict[str, Path],
) -> None:
    workbook = load_workbook(path, read_only=False, data_only=False)
    try:
        sheet = workbook["脚本"]
        image_column = next((cell.column for cell in sheet[6]
                             if str(cell.value or "").strip() == "画面示意"), None)
        if image_column is None:
            raise WorkspaceServiceError("IMAGE_VERSION_AUDIT_FAILED", "脚本表缺少画面示意列")
        embedded = {}
        duplicate_rows = set()
        for image in sheet._images:
            marker = getattr(image.anchor, "_from", None)
            if marker is None or marker.col + 1 != image_column or not 7 <= marker.row + 1 < 7 + len(decisions):
                continue
            row_index = marker.row + 1
            if row_index in embedded:
                duplicate_rows.add(row_index)
            embedded[row_index] = sha256(image._data()).hexdigest()
        expected = {
            index: str(decision["image_sha256"])
            for index, decision in enumerate(decisions, start=7)
        }
        if duplicate_rows or embedded != expected:
            raise WorkspaceServiceError(
                "IMAGE_VERSION_AUDIT_FAILED", "Excel 每镜内嵌图片与人工决定 SHA-256 不一致",
            )
        if (
            ADJACENT_REVIEW_SHEET_NAME not in workbook.sheetnames
            or workbook[ADJACENT_REVIEW_SHEET_NAME].max_row - 1 != len(decisions) - 1
        ):
            raise WorkspaceServiceError("IMAGE_VERSION_AUDIT_FAILED", "Excel 相邻复核不是 N-1")
        sensitive = {str(item.resolve()) for item in controlled_paths.values()}
        for current in workbook.worksheets:
            for row in current.iter_rows():
                for cell in row:
                    value = str(cell.value or "")
                    if any(item in value for item in sensitive) or value.startswith("file://"):
                        raise WorkspaceServiceError(
                            "IMAGE_VERSION_AUDIT_FAILED", "Excel 仍包含服务端图片路径",
                        )
    finally:
        workbook.close()
