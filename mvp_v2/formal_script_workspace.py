"""Formal script editing and immutable draft-version materialization.

The browser boundary only sees project-scoped business rows.  Internal draft,
version, run and artifact identifiers stay server-side.  Reversible edits share
the migration-10 draft state; an explicit save creates a child run/version and
records the immutable migration-13 submission mapping.  Nothing in this module
publishes a version or writes CURRENT.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import json
from pathlib import Path
import re
from typing import Any, Callable

from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .audit import add_audit_checks, audit_output_workbook_format
from .excel_io import build_script_review_workbook, scrub_workbook_paths, write_template_output_workbook
from .generation_stages import (
    _traceability_with_output,
    read_script_draft,
    read_script_plan,
    read_visual_binding,
    run_machine_audit_stage,
    run_workbook_render_stage,
)
from .identity_service import require_permission
from .loop_runner import _expected_embedded_image_count, audit_generation_content
from .mutation_policy import require_active_project
from .manual_review_commands import ManualReviewCommandService
from .manual_review_commands import _digest as _manual_command_digest
from .models import SourceShot, parse_positive_seconds_strict, validate_script_brief
from .generator import generate_template_rows
from .scoring import match_template_slots
from .pipeline_runtime import finalize_pipeline_run, fork_pipeline_run
from .project_service import ProjectService
from .revision_service import build_revision_report, compare_versions, version_shots_from_artifacts
from .run_contract import sha256_file, sha256_json
from .run_state import write_json_atomic
from .script_type_adapter import normalize_script_type, resolve_script_type_workbook
from .stage_contract import StageStatus
from .stage_runtime import finish_pipeline_stage, start_pipeline_stage
from .version_delivery import VersionDeliveryContext, VersionDeliveryService, resolve_version_shot_ids
from .version_review_repository import (
    WorkspaceVersionReviewRepository,
    open_script_draft_id,
    _canonical as _review_canonical,
    _sha as _review_sha,
)
from .version_review_service import Draft, VersionReviewError
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
from .visual_assets import apply_visual_analysis
from .visual_function import annotate_visual_function_fields


_EDITABLE_FIELDS = {
    "structure": "结构",
    "camera_type": "类型",
    "shot_size": "景别",
    "camera_move": "运镜",
    "duration_seconds": "时长(秒)",
    "visual": "画面描述",
    "sound": "声音",
    "voiceover": "旁白",
    "subtitle": "字幕",
    "notes": "备注",
}
_ORIGINAL_TECHNICAL_VALUES = {
    "camera_type": ("实拍", "地面", "地面拍摄", "航拍", "无人机", "FPV", "手持", "稳定器", "延时", "动画", "CG"),
    "shot_size": ("远景", "大全景", "全景", "中全景", "中景", "中近景", "近景", "特写", "大特写", "广角"),
    "camera_move": ("固定", "推", "拉", "摇", "移", "跟拍", "跟随", "环绕", "升降", "缓推", "缓拉", "前推", "后拉", "上摇", "下摇", "横移", "推拉", "推近", "拉远", "甩镜"),
}
_INSTRUCTION_FIELDS = {
    "结构": "structure",
    "类型": "camera_type",
    "景别": "shot_size",
    "运镜": "camera_move",
    "时长": "duration_seconds",
    "画面": "visual",
    "画面描述": "visual",
    "声音": "sound",
    "旁白": "voiceover",
    "字幕": "subtitle",
    "文案": "visual",
    "备注": "notes",
}
_INSTRUCTION = re.compile(
    r"^(?:把|将)?第?\s*(\d+)\s*镜(?:的)?\s*"
    r"(画面描述|画面|声音|旁白|字幕|文案|时长|备注|结构|类型|景别|运镜)\s*"
    r"(?:修改|改|调整|替换)(?:为|成)?\s*[：:]?\s*(.+?)\s*$"
)
_SAFE_FILENAME = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")


@dataclass(frozen=True)
class FormalScriptWorkspaceServices:
    store: WorkspaceStore
    projects: ProjectService
    delivery: VersionDeliveryService
    commands: ManualReviewCommandService
    artifacts: RunArtifactResolver
    image_workspace: Any | None = None


class DraftVersionMaterializer:
    """Create a provider-free child run from one authenticated persisted draft."""

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
        draft: Draft,
        child_version_id: str,
        reason: str,
        request_sha256: str,
        change_history: list[dict],
        inherited_image_evidence: dict[str, Any] | None = None,
        image_authority: Any | None = None,
        created_by_actor_id: str = "",
    ) -> dict:
        if draft.base_version_id != context.version_id or draft.status != "open":
            raise WorkspaceServiceError("DRAFT_VERSION_CONFLICT", "草稿不属于当前脚本版本")
        parent_plan_ref = self.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        )
        parent_draft_ref = self.artifacts.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256,
        )
        parent_binding_ref = self.artifacts.resolve_stage_artifact(
            context.run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256,
        )
        plan = read_script_plan(parent_plan_ref.path)
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
        parent_draft = read_script_draft(parent_draft_ref.path)
        parent_binding = read_visual_binding(parent_binding_ref.path)
        parent_workbook = self.artifacts.resolve_artifact(context.run_id, "workbook").path
        origin_to_child = self._revision_shot_projection(context, draft, parent_draft.script_rows)
        script_rows = self._materialized_rows(
            draft, parent_draft.script_rows, parent_draft.detail_rows, origin_to_child,
        )
        detail_rows = self._materialized_details(draft, parent_draft.detail_rows, origin_to_child)
        binding_detail_rows = self._materialized_details(draft, parent_binding.detail_rows, origin_to_child)
        bindings = self._materialized_bindings(draft, parent_binding.bindings, origin_to_child)
        visual_asset_rows = self._materialized_visual_rows(
            draft, parent_binding.visual_asset_rows, origin_to_child,
        )
        working_images = bool((inherited_image_evidence or {}).get("working_draft"))
        if working_images:
            bindings = [image_authority.materializer._binding(item, index)
                for index, item in enumerate(inherited_image_evidence["decisions"], start=1)]
        revision_report = self._revision_report(
            context=context,
            child_version_id=child_version_id,
            parent_draft=parent_draft,
            parent_binding=parent_binding,
            draft=draft,
            script_rows=script_rows,
            detail_rows=detail_rows,
            bindings=bindings,
            reason=reason,
            draft_revision=draft.revision,
            request_sha256=request_sha256,
            change_history=change_history,
            confirmed_image_shots=({str(item["shot_no"]) for item in inherited_image_evidence["decisions"]}
                                   if working_images else set()),
        )
        cache_root = self.runs_root / context.project_id
        inherited_image_payload = None
        with fork_pipeline_run(
            context.run_id,
            cache_root,
            brief=plan.brief,
            source_root=source_root,
            cache_fingerprint=sha256_json({
                "route": "formal-manual-draft",
                "parent_manifest_sha256": context.manifest.sha256,
                "draft_revision": draft.revision,
                "draft_content_sha256": sha256_json(script_rows),
                "request_sha256": request_sha256,
            }),
            from_stage="script_plan",
        ) as state:
            child_plan_path = state.run_dir / "script_plan.json"
            self._write_plan_stage(
                state,
                parent=parent_plan_ref.path,
                output=child_plan_path,
                script_rows=script_rows,
                detail_rows=detail_rows,
                draft=draft,
                request_sha256=request_sha256,
                origin_to_child=origin_to_child,
                frozen_draft=parent_draft_ref.path,
                frozen_binding=parent_binding_ref.path,
                shot_ids_by_no={str(item["child_shot_no"]): str(item["child_shot_id"])
                    for item in revision_report["version_diff"]["shot_diffs"]
                    if item.get("child_shot_no") and item.get("child_shot_id")},
            )
            draft_path = self._write_draft_stage(
                state,
                parent=parent_draft_ref.path,
                child_plan_path=child_plan_path,
                script_rows=script_rows,
                detail_rows=detail_rows,
                request_sha256=request_sha256,
                revision=draft.revision,
                reason=reason,
            )
            binding_path = image_authority.materializer._write_binding_stage(
                state, parent=parent_binding_ref.path, script_plan_path=child_plan_path,
                script_draft_path=draft_path, child_bindings=bindings,
                decisions=inherited_image_evidence["decisions"], request_sha256=request_sha256,
                working_script_rows=script_rows, working_detail_rows=binding_detail_rows,
            ) if working_images else self._write_binding_stage(
                state,
                parent=parent_binding_ref.path,
                child_plan_path=child_plan_path,
                child_draft_path=draft_path,
                script_rows=script_rows,
                detail_rows=binding_detail_rows,
                bindings=bindings,
                visual_asset_rows=visual_asset_rows,
                plan_constraints=plan.constraints,
                request_sha256=request_sha256,
            )
            binding = read_visual_binding(binding_path)
            inherited_source_paths = (inherited_image_evidence or {}).get("paths_by_shot") or {}
            inherited_paths = {no: Path(path).expanduser().resolve() for no, path in inherited_source_paths.items()} if working_images else {
                child_no: Path(inherited_source_paths[origin_no]).expanduser().resolve()
                for origin_no, child_no in origin_to_child.items()
                if origin_no in inherited_source_paths
            }
            renderer, auditor = self._workbook_functions(
                binding, source_root, inherited_paths=inherited_paths,
                template_workbook_path=source_root,
                parent_workbook_path=parent_workbook,
                parent_shot_nos=list(origin_to_child),
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
                    "SCRIPT_VERSION_AUDIT_FAILED", "草稿未通过机器校验，未创建正式版本",
                )
            report_path = state.run_dir / "run_report.json"
            write_json_atomic(report_path, {
                "schema_version": "mplus-run-report/v1",
                "run_id": state.run_id,
                "parent_run_id": context.run_id,
                "machine_audit_status": audited.audit["status"],
                "machine_audit": audited.audit,
                "human_review_status": "pending",
                "release_decision": "blocked",
                "status": "waiting_human_review",
                "draft_revision": draft.revision,
                "request_sha256": request_sha256,
            })
            revision_report_path = state.run_dir / "revision_report.json"
            write_json_atomic(revision_report_path, revision_report)
            final_artifacts = {
                "workbook": audited.output_path,
                "audit": audited.audit_path,
                "run_report": report_path,
                "revision_report": revision_report_path,
            }
            if inherited_image_evidence is not None:
                if image_authority is None:
                    raise WorkspaceServiceError("IMAGE_DECISION_DRIFT", "图片继承权威服务不可用")
                inherited_image_payload = image_authority.build_script_child_snapshot(
                    inherited_image_evidence,
                    context=context,
                    child_version_id=child_version_id,
                    child_run_id=state.run_id,
                    request_sha256=request_sha256,
                    created_by_actor_id=created_by_actor_id,
                    origin_to_child=None if working_images else origin_to_child,
                )
                inherited_image_path = state.run_dir / "image_decisions.json"
                write_json_atomic(inherited_image_path, inherited_image_payload)
                final_artifacts["image_decisions"] = inherited_image_path
            finalize_pipeline_run(
                state,
                "completed",
                final_artifacts,
                metrics={
                    "provider_calls": 0,
                    "manual_draft_revision": draft.revision,
                    "manual_draft_request_sha256": request_sha256,
                },
            )
            run_id = state.run_id
        manifest = self.artifacts.resolve_manifest(run_id)
        workbook = self.artifacts.resolve_artifact(run_id, "workbook")
        report = self.artifacts.resolve_artifact(run_id, "revision_report")
        result = {
            "run_id": run_id,
            "manifest_ref": manifest.ref,
            "manifest_sha256": manifest.sha256,
            "content_sha256": workbook.sha256,
            "artifact_ref": workbook.ref,
            "revision_report_ref": report.ref,
            "revision_report_sha256": report.sha256,
        }
        if inherited_image_payload is not None:
            image_decisions = self.artifacts.resolve_artifact(run_id, "image_decisions")
            result.update({
                "inherited_image_payload": inherited_image_payload,
                "image_decision_ref": image_decisions.ref,
                "image_decision_sha256": image_decisions.sha256,
                "candidate_snapshot_sha256": str(
                    inherited_image_evidence["candidate_snapshot_sha256"]
                ),
                "image_source_version_id": str(
                    inherited_image_evidence["source_version_id"]
                ),
                "working_image_evidence": inherited_image_evidence if working_images else None,
            })
        return result

    def _revision_report(
        self,
        *,
        context: VersionDeliveryContext,
        child_version_id: str,
        parent_draft,
        parent_binding,
        draft: Draft,
        script_rows: list[dict],
        detail_rows: list[dict],
        bindings: list[dict],
        reason: str,
        draft_revision: int,
        request_sha256: str,
        change_history: list[dict],
        confirmed_image_shots: set[str] | None = None,
    ) -> dict:
        shot_ids = self._parent_shot_ids(context, parent_draft.script_rows)
        parent_shots = version_shots_from_artifacts(
            parent_draft.script_rows,
            parent_draft.detail_rows,
            bindings=parent_binding.bindings,
            shot_ids_by_no=shot_ids,
        )
        child_ids = {
            str(shot.get("shot_no") or ""): shot_ids[str(shot.get("origin_shot_no") or shot.get("shot_no") or "")]
            for shot in draft.shots
            if str(shot.get("origin_shot_no") or shot.get("shot_no") or "") in shot_ids
        }
        child_shots = version_shots_from_artifacts(
            script_rows,
            detail_rows,
            bindings=bindings,
            shot_ids_by_no=child_ids,
        )
        diff = compare_versions(
            project_id=context.project_id,
            parent_version_id=context.version_id,
            child_version_id=child_version_id,
            parent_shots=parent_shots,
            child_shots=child_shots,
        )
        changed = [
            (str(shot.get("child_shot_no") or ""), str(field.get("field") or ""))
            for shot in diff.get("shot_diffs") or []
            for field in shot.get("field_diffs") or []
            if field.get("status") != "unchanged"
        ]
        deleted = [
            str(shot.get("parent_shot_nos", [""])[0])
            for shot in diff.get("shot_diffs") or []
            if shot.get("relation") == "deleted"
        ]
        if not changed and not deleted:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "草稿没有形成可保存的内容变化")
        patch_paths = {
            (str(item.get("shot_no") or ""), str(item.get("field") or ""))
            for item in change_history
        }
        image_sensitive_shots = {
            shot_no
            for shot_no, field in patch_paths
            if field in {"类型", "景别", "运镜", "画面描述", "source_refs"}
        }
        allowed_derived = {
            (shot_no, field)
            for shot_no in image_sensitive_shots | (confirmed_image_shots or set())
            for field in {"image_id", "original_path", "decision", "resolution_status", "basis"}
        }
        if any(item not in patch_paths | allowed_derived for item in changed if item[0]):
            raise WorkspaceServiceError("INTEGRITY_ERROR", "草稿变化缺少持久修改记录")
        if deleted and not any(item[1] == "__delete_shot__" for item in patch_paths):
            raise WorkspaceServiceError("INTEGRITY_ERROR", "镜头删除缺少持久修改记录")
        changed_id_fields = {
            (child_ids[shot_no], field)
            for shot_no, field in changed if shot_no
        }
        impacted_shot_nos = {
            str(shot.get("child_shot_no") or (shot.get("parent_shot_nos") or [""])[0])
            for shot in diff.get("shot_diffs") or []
            if (
                shot.get("relation") not in {"preserved", "moved"}
                or shot.get("position_changed")
                or any(
                    field.get("status") != "unchanged"
                    for field in shot.get("field_diffs") or []
                )
            )
        }
        actual_changed_fields = {
            str(field.get("field") or "")
            for shot in diff.get("shot_diffs") or []
            for field in shot.get("field_diffs") or []
            if field.get("status") != "unchanged"
        }
        deleted_ids = {
            str(shot.get("parent_shot_ids", [""])[0])
            for shot in diff.get("shot_diffs") or []
            if shot.get("relation") == "deleted"
        }
        locked_fields = [
            {"shot_id": str(shot.get("shot_id") or ""), "field": str(field)}
            for shot in parent_shots
            for field in (shot.get("fields") or {})
            if shot.get("shot_id") not in deleted_ids
            and (str(shot.get("shot_id") or ""), str(field)) not in changed_id_fields
        ]
        report = build_revision_report(
            diff=diff,
            original_feedback=reason,
            structured_intent={
                "scope": "persisted_draft",
                "draft_revision": draft_revision,
                "changes": change_history,
            },
            expected_impact={
                "shot_nos": sorted(impacted_shot_nos, key=_shot_sort_key),
                "fields": sorted(actual_changed_fields),
                "earliest_restart_stage": "script_draft",
            },
            locked_fields=locked_fields,
            rerun_nodes=["script_draft", "visual_binding", "workbook_render", "machine_audit"],
            unresolved_items=[],
            machine_review={"status": "pass"},
            human_review={"status": "waiting_human_review"},
            publication={"status": "unpublished"},
        )
        if report.get("completion", {}).get("status") != "waiting_human_review":
            raise WorkspaceServiceError("SCRIPT_VERSION_AUDIT_FAILED", "版本差异与草稿修改记录不一致")
        return {
            **report,
            "version_diff": diff,
            "draft_revision": draft_revision,
            "request_sha256": request_sha256,
        }

    def _parent_shot_ids(
        self, context: VersionDeliveryContext, script_rows: list[dict],
    ) -> dict[str, str]:
        return resolve_version_shot_ids(self.artifacts, context, script_rows)

    def _revision_shot_projection(
        self, context: VersionDeliveryContext, draft: Draft, parent_rows: list[dict],
    ) -> dict[str, str]:
        identities = self._parent_shot_ids(context, parent_rows)
        projection: dict[str, str] = {}
        selected_ids = set()
        for number, shot in enumerate(draft.shots, 1):
            origin = str(shot.get("origin_shot_no") or shot.get("shot_no") or "")
            child = str(shot.get("shot_no") or "")
            identity = identities.get(origin)
            if child != str(number) or not identity or identity in selected_ids or origin in projection:
                raise WorkspaceServiceError("DRAFT_VERSION_CONFLICT", "草稿镜头身份与父版本不一致")
            selected_ids.add(identity)
            projection[origin] = child
        return projection

    def _materialized_rows(
        self, draft: Draft, parent_rows: list[dict], detail_rows: list[dict],
        origin_to_child: dict[str, str],
    ) -> list[dict]:
        parent_by_shot = {
            str(row.get("镜号") or row.get("shot_no") or "").strip(): dict(row)
            for row in parent_rows
        }
        if len(parent_by_shot) != len(parent_rows) or len(detail_rows) != len(parent_rows):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "父版本镜头链不完整")
        detail_by_shot = {_detail_shot_no(detail): detail for detail in detail_rows}
        result: list[dict] = []
        for origin_shot_no, shot in zip(origin_to_child, draft.shots):
            shot_no = str(shot.get("shot_no") or "").strip()
            sources = list(shot.get("source_refs") or [])
            detail = detail_by_shot.get(origin_shot_no)
            parent = parent_by_shot.get(origin_shot_no)
            if parent is None or detail is None:
                raise WorkspaceServiceError("DRAFT_VERSION_CONFLICT", "草稿镜头链与父版本不一致")
            fields = shot.get("fields")
            if not isinstance(fields, dict):
                raise WorkspaceServiceError("DRAFT_INVALID", "草稿镜头字段无效")
            row = dict(parent)
            row.update({
                key: deepcopy(value)
                for key, value in fields.items()
                if key in parent or value not in (None, "")
            })
            row["镜号"] = shot_no
            _validate_business_row(row)
            override = shot.get("source_detail_override")
            expected_source = _source_ref(
                override if isinstance(override, dict) else detail
            )
            if sources != [expected_source]:
                raise WorkspaceServiceError("DRAFT_SOURCE_DRIFT", "草稿来源与父版本不一致")
            result.append(row)
        if [str(row["镜号"]) for row in result] != [str(index) for index in range(1, len(result) + 1)]:
            raise WorkspaceServiceError("DRAFT_VERSION_CONFLICT", "草稿镜号必须连续")
        return result

    @staticmethod
    def _write_plan_stage(
        state,
        *,
        parent: Path,
        output: Path,
        script_rows: list[dict],
        detail_rows: list[dict],
        draft: Draft,
        request_sha256: str,
        origin_to_child: dict[str, str] | None = None,
        frozen_draft: Path | None = None,
        frozen_binding: Path | None = None,
        shot_ids_by_no: dict[str, str] | None = None,
    ) -> None:
        payload = _read_json(parent, "父 ScriptPlan")
        origin_to_child = origin_to_child or {
            str(shot.get("origin_shot_no") or shot.get("shot_no") or ""): str(shot.get("shot_no") or "")
            for shot in draft.shots
        }

        recovery = None
        if not payload.get("decisions") and frozen_draft is not None and frozen_binding is not None:
            recovered = _recover_frozen_decisions(parent, payload, frozen_draft, frozen_binding)
            payload = {**payload, **recovered}
            recovery = {
                "reason": "empty_parent_plan_decisions",
                "script_draft_sha256": sha256_file(frozen_draft),
                "visual_binding_sha256": sha256_file(frozen_binding),
            }
        projected = _project_frozen_shot_fields(payload, origin_to_child)
        projected["traceability"] = _traceability_with_output(
            projected.get("traceability") or [], script_rows, projected.get("shot_plans") or [],
            shot_ids_by_no=shot_ids_by_no,
        )

        capacity = deepcopy(payload.get("capacity_report") or {})
        for key in ("shot_count", "planned_shot_count", "actual_shot_count"):
            if key in capacity:
                capacity[key] = len(script_rows)
        start_pipeline_stage(
            state,
            "script_plan",
            input_sha256=sha256_json({
                "parent_script_plan_sha256": sha256_file(parent),
                "script_rows": script_rows,
                "request_sha256": request_sha256,
            }),
            input_refs=(str(parent.resolve()),),
        )
        decisions = projected["decisions"]
        by_child = {
            str(shot.get("shot_no") or ""): shot
            for shot in draft.shots
        }
        for decision in decisions:
            slot = decision.get("slot") if isinstance(decision, dict) else None
            shot = by_child.get(str(slot.get("shot_no") or "")) if isinstance(slot, dict) else None
            evidence = shot.get("source_match_evidence") if isinstance(shot, dict) else None
            if not isinstance(evidence, dict) or evidence.get("decision_mode") == "ORIGINAL":
                continue
            decision.update({
                "mode": str(evidence.get("decision_mode") or "REPLACE"),
                "score": int(evidence.get("score") or 0),
                "match_reason": str(evidence.get("match_reason") or ""),
                "selected_reason": str(evidence.get("selected_reason") or ""),
                "variant": deepcopy(evidence.get("variant")),
            })
        write_json_atomic(output, {
            **payload,
            **projected,
            "shot_count": len(script_rows),
            "structures": [str(row.get("结构") or "") for row in script_rows],
            "script_rows": script_rows,
            "detail_rows": detail_rows,
            "decisions": decisions,
            "source_refs": [_source_ref(detail) for detail in detail_rows],
            "capacity_report": capacity,
            "manual_revision": {
                "request_sha256": request_sha256,
                "shot_count": len(script_rows),
                "origin_to_child": origin_to_child,
                **({"recovered_frozen_decisions": recovery} if recovery else {}),
            },
        })
        finish_pipeline_stage(
            state, "script_plan", StageStatus.SUCCEEDED,
            output_sha256=sha256_file(output), output_refs=(str(output.resolve()),),
        )

    @staticmethod
    def _materialized_details(
        draft: Draft, detail_rows: list[dict], origin_to_child: dict[str, str],
    ) -> list[dict]:
        by_shot = {_detail_shot_no(detail): detail for detail in detail_rows}
        result = []
        for (origin, child), shot in zip(origin_to_child.items(), draft.shots):
            detail = by_shot.get(origin)
            if detail is None:
                raise WorkspaceServiceError("DRAFT_SOURCE_DRIFT", "草稿来源与父版本不一致")
            override = shot.get("source_detail_override")
            result.append(_renumber_shot_record(
                override if isinstance(override, dict) else detail,
                int(child),
            ))
        return result

    @staticmethod
    def _materialized_bindings(
        draft: Draft, bindings: list[dict], origin_to_child: dict[str, str],
    ) -> list[dict]:
        by_shot = {str(item.get("shot_no") or ""): item for item in bindings}
        result = []
        for (origin, child), shot in zip(origin_to_child.items(), draft.shots):
            item = by_shot.get(origin)
            if item is None:
                raise WorkspaceServiceError("DRAFT_VERSION_CONFLICT", "草稿图片绑定链与父版本不一致")
            projected = _renumber_shot_record(item, int(child))
            if shot.get("image_reconfirmation_required"):
                projected.update({
                    "image_id": "",
                    "original_path": "",
                    "decision": "",
                    "resolution_status": "visual_resolution_required",
                    "recommended_action": "脚本来源已变化，请重新检索并确认图片",
                    "basis": "",
                    "image_reconfirmation_required": True,
                })
            result.append(projected)
        return result

    @staticmethod
    def _materialized_visual_rows(
        draft: Draft, visual_rows: list[dict], origin_to_child: dict[str, str],
    ) -> list[dict]:
        if not visual_rows:
            return []
        by_shot = {
            str(item.get("镜号") or item.get("shot_no") or item.get("slot_index") or ""): item
            for item in visual_rows
        }
        result = []
        for (origin, child), shot in zip(origin_to_child.items(), draft.shots):
            if shot.get("image_reconfirmation_required"):
                continue
            item = by_shot.get(origin)
            if item is not None:
                result.append(_renumber_shot_record(item, int(child)))
        return result

    @staticmethod
    def _write_draft_stage(
        state,
        *,
        parent: Path,
        child_plan_path: Path,
        script_rows: list[dict],
        detail_rows: list[dict],
        request_sha256: str,
        revision: int,
        reason: str,
    ) -> Path:
        parent_payload = _read_json(parent, "父 ScriptDraft")
        child_plan = _read_json(child_plan_path, "子 ScriptPlan")
        projected = _project_frozen_shot_fields(
            parent_payload, child_plan["manual_revision"]["origin_to_child"],
        )
        record = start_pipeline_stage(
            state,
            "script_draft",
            input_sha256=sha256_json({
                "parent_script_draft_sha256": sha256_file(parent),
                "script_rows": script_rows,
                "request_sha256": request_sha256,
            }),
            input_refs=(str(child_plan_path.resolve()), str(parent.resolve())),
        )
        if int(record.get("attempt") or 0) != 1:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "草稿物化阶段重复执行")
        output = state.run_dir / "script_draft.json"
        payload = {
            **parent_payload,
            **projected,
            "decisions": deepcopy(child_plan["decisions"]),
            "template_slots": deepcopy(child_plan["template_slots"]),
            "traceability": deepcopy(child_plan.get("traceability") or []),
            "script_plan_ref": str(child_plan_path.resolve()),
            "script_rows": script_rows,
            "detail_rows": detail_rows,
            "shot_count": len(script_rows),
            "manual_revision": {
                "revision": revision,
                "reason": reason,
                "request_sha256": request_sha256,
                "provider_calls": 0,
            },
        }
        write_json_atomic(output, payload)
        finish_pipeline_stage(
            state,
            "script_draft",
            StageStatus.SUCCEEDED,
            output_sha256=sha256_file(output),
            output_refs=(str(output.resolve()),),
        )
        return output

    @staticmethod
    def _write_binding_stage(
        state,
        *,
        parent: Path,
        child_plan_path: Path,
        child_draft_path: Path,
        script_rows: list[dict],
        detail_rows: list[dict],
        bindings: list[dict],
        visual_asset_rows: list[dict],
        plan_constraints: dict,
        request_sha256: str,
    ) -> Path:
        parent_payload = _read_json(parent, "父 VisualBinding")
        child_plan = _read_json(child_plan_path, "子 ScriptPlan")
        projected = _project_frozen_shot_fields(
            parent_payload, child_plan["manual_revision"]["origin_to_child"],
        )
        parent_shots = [
            str(row.get("镜号") or row.get("shot_no") or "").strip()
            for row in parent_payload.get("script_rows") or []
        ]
        child_shots = [str(row.get("镜号") or "").strip() for row in script_rows]
        binding_shots = [str(item.get("shot_no") or "").strip() for item in bindings]
        detail_shots = [_detail_shot_no(item) for item in detail_rows]
        if not child_shots or child_shots != binding_shots or child_shots != detail_shots:
            raise WorkspaceServiceError("DRAFT_VERSION_CONFLICT", "草稿镜头、来源或图片绑定链不一致")
        # Re-evaluate the frozen image against the edited intent; keep its original provenance.
        analysis_by_id = {}
        for asset in visual_asset_rows:
            key = str(asset.get("visual_asset_id") or "")
            if key and asset.get("视觉内容摘要"):
                analysis_by_id[key] = {
                    "caption": asset["视觉内容摘要"],
                    "tags": str(asset.get("视觉标签") or "").replace("、", "；").split("；"),
                    "brand_names": str(asset.get("brand_names") or "").split("；") if asset.get("brand_names") else [],
                    "negative_flags": str(asset.get("negative_flags") or "").split("；") if asset.get("negative_flags") else [],
                    "source": str(asset.get("视觉读取状态") or "未记录分析来源"),
                    "ocr_text": str(asset.get("ocr_text") or ""),
                }
        apply_visual_analysis(script_rows, detail_rows, visual_asset_rows, analysis_by_id)
        annotate_visual_function_fields(detail_rows, visual_asset_rows, str(parent_payload.get("brief", {}).get("脚本类型") or "发布会"))
        identity_fields = ("image_id", "media_id", "visual_asset_id", "视觉资产ID")
        current_image_ids = {
            str(item[field]) for item in [*bindings, *visual_asset_rows]
            for field in identity_fields if item.get(field)
        }
        image_analysis_rows = [
            item for item in parent_payload.get("image_analysis_rows") or []
            if any(str(item.get(field) or "") in current_image_ids for field in identity_fields)
        ]
        start_pipeline_stage(
            state,
            "visual_binding",
            input_sha256=sha256_json({
                "script_draft_sha256": sha256_file(child_draft_path),
                "parent_visual_binding_sha256": sha256_file(parent),
                "request_sha256": request_sha256,
            }),
            input_refs=(str(child_draft_path.resolve()), str(parent.resolve())),
        )
        output = state.run_dir / "visual_binding.json"
        write_json_atomic(output, {
            **parent_payload,
            **projected,
            "decisions": deepcopy(child_plan["decisions"]),
            "template_slots": deepcopy(child_plan["template_slots"]),
            "traceability": deepcopy(child_plan.get("traceability") or []),
            "script_plan_ref": str(child_plan_path.resolve()),
            "script_draft_ref": str(child_draft_path.resolve()),
            "constraints": deepcopy(plan_constraints),
            "script_rows": script_rows,
            "detail_rows": detail_rows,
            "bindings": bindings,
            "visual_asset_rows": visual_asset_rows,
            "image_analysis_rows": image_analysis_rows,
            "shot_count": len(script_rows),
        })
        finish_pipeline_stage(
            state,
            "visual_binding",
            StageStatus.SUCCEEDED,
            output_sha256=sha256_file(output),
            output_refs=(str(output.resolve()),),
        )
        return output

    def _workbook_functions(
        self,
        initial_binding,
        source_root: Path,
        *,
        inherited_paths: dict[str, Path] | None = None,
        template_workbook_path: Path | None = None,
        parent_workbook_path: Path | None = None,
        parent_shot_nos: list[str] | None = None,
    ):
        script_type = normalize_script_type(initial_binding.brief.get("脚本类型") or "发布会")
        template = template_workbook_path or resolve_script_type_workbook(source_root, script_type)
        inherited_paths = inherited_paths or {}
        validate_script_brief(initial_binding.brief)
        duration_seconds = sum(
            parse_positive_seconds_strict(row.get("时长(秒)", "")) or 0
            for row in initial_binding.script_rows
        )
        if duration_seconds <= 0:
            raise WorkspaceServiceError("DRAFT_INVALID", "脚本总时长必须为正整数秒")

        def renderer(binding, target, checks):
            script_rows = [
                {
                    **dict(row),
                    "画面示意": str(inherited_paths[_detail_shot_no(row)]),
                    "效果示意": "",
                }
                if _detail_shot_no(row) in inherited_paths else dict(row)
                for row in binding.script_rows
            ]
            visual_asset_rows = [
                {
                    **dict(row),
                    "原图路径": str(inherited_paths[str(row.get("镜号") or row.get("slot_index") or "")]),
                }
                if str(row.get("镜号") or row.get("slot_index") or "") in inherited_paths
                else dict(row)
                for row in binding.visual_asset_rows
            ]
            write_template_output_workbook(
                target,
                script_rows,
                binding.detail_rows,
                checks,
                template_workbook_path=template,
                visual_asset_rows=visual_asset_rows,
                overall_summary=binding.overall_summary,
                script_type=script_type,
                duration_seconds=duration_seconds,
                parent_workbook_path=parent_workbook_path,
                parent_shot_nos=parent_shot_nos,
            )
            if inherited_paths:
                scrub_workbook_paths(target, set(inherited_paths.values()))

        def auditor(binding, workbook_path):
            checks = audit_output_workbook_format(
                workbook_path,
                expected_image_count=(
                    len(inherited_paths)
                    if inherited_paths else _expected_embedded_image_count(binding.script_rows)
                ),
                template_workbook_path=parent_workbook_path or template,
                script_type=script_type,
                duration_seconds=duration_seconds,
            )
            return add_audit_checks(audit_generation_content(binding, revision=True), checks)

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


class FormalScriptWorkspaceService:
    """Project-scoped business API for one current script and its shared draft."""

    def __init__(
        self,
        services: FormalScriptWorkspaceServices,
        *,
        runs_root: str | Path,
        source_root: str | Path,
        source_root_resolver: Callable[[str], Path] | None = None,
        package_registry: LocalPackageRegistry | None = None,
    ) -> None:
        self.services = services
        self.materializer = DraftVersionMaterializer(
            artifacts=services.artifacts,
            runs_root=runs_root,
            source_root=source_root,
            source_root_resolver=source_root_resolver,
            package_registry=package_registry,
        )
        self.services.store.initialize()

    def read_current(self, auth_token: str, *, workspace_id: str, project_id: str) -> dict:
        project, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            return {"state": "empty", "message": "当前项目还没有脚本版本"}
        repository = self._repository(auth_token, context)
        draft_id = self._open_draft_id(context)
        if draft_id:
            draft, history, cursor = repository.draft_state(draft_id)
            shots = list(draft.shots)
            has_changes = cursor > 0
            status = "editing" if has_changes else "ready"
            revision = draft.revision
            can_undo, can_redo = cursor > 0, cursor < len(history) - 1
        else:
            shots, has_changes = self._subject_shots(context), False
            status, revision, can_undo, can_redo = "ready", 0, False, False
        return self._employee_model(
            auth_token,
            project,
            context,
            shots,
            pending_confirmations=self._pending_confirmations(context),
            status=status,
            revision=revision,
            can_undo=can_undo,
            can_redo=can_redo,
            can_save_version=has_changes,
        )

    def _draft_save_changes(
        self, auth_token: str, context: VersionDeliveryContext, *, expected_revision: int,
    ) -> list[dict]:
        """Project the canonical version diff for the current, unsaved draft."""
        draft_id = self._open_draft_id(context)
        if not draft_id:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "当前没有可保存的草稿修改")
        draft, _, _ = self._repository(auth_token, context).draft_state(draft_id)
        if draft.revision != expected_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿已变化，请刷新后重新预览")
        identities = self._current_shot_ids(context)

        def diff_shots(shots):
            return [{
                "shot_no": str(shot["shot_no"]),
                "shot_id": identities[str(shot.get("origin_shot_no") or shot["shot_no"])],
                "fields": shot["fields"], "source_refs": shot.get("source_refs") or [],
            } for shot in shots]

        diff = compare_versions(
            project_id=context.project_id, parent_version_id=context.version_id,
            child_version_id=draft_id,
            parent_shots=diff_shots(self._subject_shots(context)),
            child_shots=diff_shots(draft.shots),
        )
        public_fields = {label: field for field, label in _EDITABLE_FIELDS.items()}
        changes = []
        for shot in diff["shot_diffs"]:
            shot_no = str(shot["child_shot_no"] or shot["parent_shot_nos"][0])
            shot_id = str(shot["child_shot_id"] or shot["parent_shot_ids"][0])

            def append_change(field, label, before, after):
                changes.append({
                    "shot_id": shot_id, "shot_no": shot_no, "field": field,
                    "label": f"第 {shot_no} 镜 · {label}", "before": before, "after": after,
                })

            if shot["relation"] == "deleted":
                append_change("delete_shot", "整镜", "镜头内容与来源", None)
                continue
            if shot["position_changed"]:
                append_change("shot_no", "镜号", str(shot["parent_shot_nos"][0]), shot_no)
            for field in shot["field_diffs"]:
                if field["status"] == "unchanged":
                    continue
                storage_field = field["field"]
                if storage_field in public_fields:
                    append_change(
                        public_fields[storage_field], storage_field,
                        _employee_diff_value(field["before"]), _employee_diff_value(field["after"]),
                    )
                elif storage_field == "source_refs":
                    def source_label(refs):
                        return "；".join(
                            f"{Path(str(ref.get('source_file') or '')).name} / "
                            f"{ref.get('source_sheet') or ''} / {ref.get('source_row') or ''}"
                            for ref in refs or []
                        )
                    append_change("source", "来源", source_label(field["before"]), source_label(field["after"]))
        if not changes:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "草稿没有形成可保存的内容变化")
        return changes

    def assistant_context(
        self, auth_token: str, *, workspace_id: str, project_id: str,
    ) -> dict:
        """Read the current version's frozen creative context for the existing editor."""
        from .script_draft import frozen_creative_context
        _, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可修改脚本")
        plan = read_script_plan(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        ).path)
        catalog = _read_json(self.services.artifacts.resolve_stage_artifact(
            context.run_id, "source_catalog", expected_manifest_sha256=context.manifest.sha256,
        ).path, "冻结来源目录")
        current = self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)
        # No browser-supplied context or model-selected source paths enter this view.
        source_fields = ("camera_type", "shot_size", "camera_move")
        records = [record for record in catalog.get("records") or [] if isinstance(record, dict)]
        used_sources = {(item.get('source_file'), item.get('source_sheet'), item.get('source_row')) for item in plan.source_refs}
        relevant_records = [record for record in records if record.get('record_type') == 'template'
            or (record.get('source_file'), record.get('source_sheet'), record.get('source_row')) in used_sources]
        technical = {
            field: sorted({
                str(record.get(field) or "").strip()
                for record in records if str(record.get(field) or "").strip()
            } | {
                str(shot.get(field) or "").strip()
                for shot in current.get("shots") or [] if str(shot.get(field) or "").strip()
            } | set(_ORIGINAL_TECHNICAL_VALUES[field])) for field in source_fields
        }
        return {
            "brief": deepcopy(plan.brief),
            "creative_standard": deepcopy(plan.constraints.get("creative_standard") or {}),
            **frozen_creative_context(plan.constraints, plan.template_rule_contract),
            "current_script_summary": {
                "shot_count": len(current.get("shots") or []),
                "events": deepcopy(plan.events),
                "requirements": deepcopy(plan.requirements),
            },
            "neighbor_shots": [{
                key: shot[key] for key in (
                    "shot_id", "shot_no", "structure", *source_fields,
                    "duration_seconds", "visual", "sound", "voiceover", "subtitle", "notes",
                ) if key in shot
            } for shot in current.get("shots") or []],
            "source_summary": {
                "role": "创意参考；画面描述可提出拍摄建议，项目事实以 Brief 为准",
                "references": [{
                    key: record.get(key) for key in (
                        "structure", "shot_function", "generic_desc", "visual_desc", "brand_judgement_original",
                        "source_file", "source_sheet", "source_row", *source_fields,
                    ) if record.get(key)
                } for record in relevant_records],
            },
            "technical_candidates": technical,
            "allow_original_adaptation": True,
        }

    def _validate_technical_edit(self, auth_token: str, *, workspace_id: str, project_id: str, field: str, value: Any) -> None:
        if field not in _ORIGINAL_TECHNICAL_VALUES:
            return
        _, normalized = _normalize_edit(field, value)
        allowed = self.assistant_context(auth_token, workspace_id=workspace_id, project_id=project_id)["technical_candidates"][field]
        if normalized not in allowed:
            raise WorkspaceServiceError("INVALID_INPUT", f"{_EDITABLE_FIELDS[field]}不是可用技术值，请选择明确的拍摄参数。")

    def set_assistant_locks(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        locked_shot_ids: list[str],
        idempotency_key: str,
    ) -> dict:
        project, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有脚本版本")
        check_version(
            context.project_lock_version, expected_project_version,
            "PROJECT_VERSION_CONFLICT",
        )
        requested = {str(value or "").strip() for value in locked_shot_ids}
        if "" in requested or len(requested) != len(locked_shot_ids):
            raise WorkspaceServiceError("INVALID_INPUT", "保护镜头身份为空或重复")
        if not str(idempotency_key or "").strip():
            raise WorkspaceServiceError("INVALID_INPUT", "idempotency_key 不能为空")
        draft_id = self._open_draft_id(context)
        current_shots = (list(self._repository(auth_token, context).draft_state(draft_id)[0].shots)
                         if draft_id else self._subject_shots(context))
        current_ids = set(self._current_shot_ids(context, current_shots).values())
        active = sorted(requested.intersection(current_ids))
        ignored = sorted(requested - current_ids)
        request_sha256 = sha256_json({
            "expected_project_version": expected_project_version,
            "locked_shot_ids": sorted(requested),
        })
        response = {
            "status": "completed",
            "locked_shot_ids": active,
            "ignored_shot_ids": ignored,
        }
        now = utc_now()
        with self.services.store.transaction() as connection:
            actor = require_permission(
                connection, auth_token, workspace_id=workspace_id,
                project_id=project_id, permission="project.update",
            )
            row = connection.execute(
                """SELECT request_sha256, response_json
                FROM formal_script_assistant_lock_requests
                WHERE actor_id=? AND project_id=? AND idempotency_key=?""",
                (actor["actor_id"], project_id, idempotency_key),
            ).fetchone()
            if row is not None:
                if str(row["request_sha256"]) != request_sha256:
                    raise WorkspaceServiceError(
                        "IDEMPOTENCY_CONFLICT", "保护请求的幂等键已用于其他内容",
                    )
                return json.loads(str(row["response_json"]))
            stored = connection.execute(
                "SELECT workspace_id, lock_version, current_version_id FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if stored is None or str(stored["workspace_id"]) != workspace_id:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
            check_version(
                int(stored["lock_version"]), expected_project_version,
                "PROJECT_VERSION_CONFLICT",
            )
            if str(stored["current_version_id"] or "") != context.version_id:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "当前脚本版本已变化")
            connection.execute(
                "DELETE FROM formal_script_assistant_locks WHERE workspace_id=? AND project_id=?",
                (workspace_id, project_id),
            )
            connection.executemany(
                """INSERT INTO formal_script_assistant_locks(
                workspace_id, project_id, shot_id, created_by_actor_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                [
                    (workspace_id, project_id, shot_id, actor["actor_id"], now, now)
                    for shot_id in active
                ],
            )
            connection.execute(
                """INSERT INTO formal_script_assistant_lock_requests(
                actor_id, project_id, idempotency_key, request_sha256, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    actor["actor_id"], project_id, idempotency_key, request_sha256,
                    json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    now,
                ),
            )
        return response

    def build_review_workbook(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        version_no: int,
    ) -> dict:
        """Export a saved version from its immutable workbook and original layout."""
        project, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有脚本版本")
        if type(version_no) is not int or version_no < 1:
            raise WorkspaceServiceError("INVALID_INPUT", "版本序号无效")
        versions = self.services.projects.list_project_versions(auth_token, project_id)
        if version_no > len(versions):
            raise WorkspaceServiceError("NOT_FOUND", "脚本版本不存在")
        target = versions[version_no - 1]
        if target["version_id"] == context.version_id:
            current = self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)
            if current.get("state") != "ready" or current.get("can_save_version"):
                raise WorkspaceServiceError("DRAFT_NOT_SAVED", "当前有未保存草稿，请先保存新版本再下载复核稿")
        else:
            context = self.services.delivery.resolve(auth_token, project_id, target["version_id"],
                expected_project_version=context.project_lock_version, ancestor_of=context.version_id)

        saved_workbook = self.services.artifacts.resolve_artifact(context.run_id, "workbook")
        content = build_script_review_workbook(saved_workbook.path, version_no=version_no)
        safe_name = re.sub(r'[\\/:*?"<>|]+', "-", project["name"]).strip(" .-")
        return {
            "filename": f"{safe_name or '项目'}-脚本复核稿-V{version_no}.xlsx",
            "content": content,
        }

    def read_version_diff(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        version_no: int,
    ) -> dict:
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
        if type(version_no) is not int or version_no < 1:
            raise WorkspaceServiceError("INVALID_INPUT", "版本序号无效")
        versions = self.services.projects.list_project_versions(auth_token, project_id)
        if version_no > len(versions):
            raise WorkspaceServiceError("NOT_FOUND", "脚本版本不存在")
        version = versions[version_no - 1]
        with self.services.store.connection() as connection:
            row = connection.execute(
                """SELECT materialized_version_id, revision_report_ref,
                revision_report_sha256
                FROM version_review_draft_submissions
                WHERE workspace_id=? AND project_id=? AND materialized_version_id=?""",
                (workspace_id, project_id, version["version_id"]),
            ).fetchone()
        if row is None:
            raise WorkspaceServiceError("NOT_FOUND", "该版本没有可核对的修改记录")
        report = self._verified_revision_report(version, dict(row))
        diff = report.get("version_diff")
        if not isinstance(diff, dict) or (
            diff.get("schema_version") != "mplus-version-diff/v1"
            or diff.get("project_id") != project_id
            or diff.get("parent_version_id") != version.get("parent_version_id")
            or diff.get("child_version_id") != version.get("version_id")
            or diff.get("lineage_needs_review")
        ):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异上下文不一致")
        completion = report.get("completion")
        locks = report.get("locked_field_verification")
        actual = report.get("actual_impact")
        statuses = report.get("statuses")
        if not all(isinstance(item, dict) for item in (completion, locks, actual, statuses)):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异校验结果不完整")
        machine_review = statuses.get("machine_review")
        publication = statuses.get("publication")
        if (
            not isinstance(machine_review, dict)
            or not isinstance(publication, dict)
            or completion.get("status") not in {"waiting_human_review", "completed"}
            or str(machine_review.get("status") or "") != "pass"
            or str(publication.get("status") or "") != "unpublished"
        ):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异尚未通过保存校验")
        if (
            not locks.get("all_preserved")
            or locks.get("violations")
            or locks.get("lineage_needs_review")
            or actual.get("unexpected_fields")
            or actual.get("missing_expected_fields")
            or actual.get("unexpected_shot_nos")
            or actual.get("missing_expected_shot_nos")
        ):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异存在未解释影响")
        changes = []
        unchanged_business_fields = 0
        hidden_changes = []
        business_fields = set(_EDITABLE_FIELDS.values())
        image_sensitive_fields = {"类型", "景别", "运镜", "画面描述", "source_refs"}
        derived_image_fields = {
            "image_id", "original_path", "decision", "resolution_status", "basis",
        }
        image_snapshot, saved_bindings = {}, {}
        if self.services.image_workspace is not None and any(
            field.get("status") == "changed" and field.get("field") in derived_image_fields
            for shot in diff.get("shot_diffs") or [] if isinstance(shot, dict)
            for field in shot.get("field_diffs") or []
            if isinstance(field, dict)
        ):
            context = self.services.delivery.resolve(auth_token, project_id, version["version_id"],
                expected_project_version=project["project_version"], ancestor_of=project["current_version_id"])
            image_snapshot = self.services.image_workspace._read_snapshot(context)
            binding = read_visual_binding(self.services.artifacts.resolve_stage_artifact(
                context.run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256).path)
            saved_bindings = {str(item["shot_no"]): item for item in binding.bindings}
        image_changed_shots = set()
        actual_changed_fields_from_diff = set()
        has_delete = any(
            item.get("relation") == "deleted" for item in diff.get("shot_diffs") or []
            if isinstance(item, dict)
        )
        for shot in diff.get("shot_diffs") or []:
            if not isinstance(shot, dict):
                raise WorkspaceServiceError("ARTIFACT_INVALID", "镜头差异记录无效")
            relation = str(shot.get("relation") or "")
            shot_no = str(
                shot.get("child_shot_no")
                or (shot.get("parent_shot_nos") or [""])[0]
            ).strip()
            if not shot_no or relation not in {"preserved", "moved", "deleted"}:
                raise WorkspaceServiceError("ARTIFACT_INVALID", "镜头差异包含未授权关系")
            if relation == "deleted":
                changes.append({
                    "shot_no": shot_no, "field": "整镜",
                    "before": "镜头内容与来源", "after": "已删除",
                })
                continue
            if shot.get("position_changed"):
                changes.append({
                    "shot_no": shot_no, "field": "镜号",
                    "before": str((shot.get("parent_shot_nos") or [""])[0]),
                    "after": shot_no,
                })
            field_diffs = shot.get("field_diffs") or []
            if not all(isinstance(field, dict) for field in field_diffs):
                raise WorkspaceServiceError("ARTIFACT_INVALID", "字段差异记录无效")
            changed_in_shot = {
                str(field.get("field") or "").strip()
                for field in field_diffs
                if str(field.get("status") or "").strip() == "changed"
            }
            actual_changed_fields_from_diff.update(changed_in_shot)
            image_reset_explained = bool(changed_in_shot.intersection(image_sensitive_fields))
            image_fields = [field for field in field_diffs
                            if field.get("status") == "changed" and field.get("field") in derived_image_fields]
            decision = image_snapshot.get(shot_no) or {}
            saved_binding = saved_bindings.get(shot_no) or {}
            confirmed_image_change = bool(image_fields and decision.get("decision_kind") == "image_confirmed"
                and decision.get("image_id") == saved_binding.get("image_id")
                and all(field.get("after") == saved_binding.get(field["field"]) for field in image_fields))
            if confirmed_image_change:
                image_changed_shots.add(shot_no)
                image_id_diff = next((field for field in field_diffs if field.get("field") == "image_id"), {})
                changes.append({"shot_no": shot_no, "field": "配图",
                    "before": "原版本配图" if image_id_diff.get("before") else "未配图",
                    "after": "已确认配图",
                    "before_image_key": str(image_id_diff.get("before") or ""),
                    "after_image_key": str(decision["image_id"])})
            for field in field_diffs:
                if not isinstance(field, dict):
                    raise WorkspaceServiceError("ARTIFACT_INVALID", "字段差异记录无效")
                field_name = str(field.get("field") or "").strip()
                status = str(field.get("status") or "").strip()
                if status == "unchanged":
                    if field_name in business_fields:
                        unchanged_business_fields += 1
                    continue
                if status == "changed" and field_name == "source_refs" and image_reset_explained:
                    changes.append({
                        "shot_no": shot_no,
                        "field": "来源",
                        "before": _employee_diff_value(field.get("before")),
                        "after": _employee_diff_value(field.get("after")),
                    })
                    continue
                if status == "changed" and field_name in derived_image_fields and (image_reset_explained or confirmed_image_change):
                    continue
                if status != "changed" or field_name not in business_fields:
                    hidden_changes.append((shot_no, field_name))
                    continue
                changes.append({
                    "shot_no": shot_no,
                    "field": field_name,
                    "before": _employee_diff_value(field.get("before")),
                    "after": _employee_diff_value(field.get("after")),
                })
        if hidden_changes or not changes:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异包含未授权字段或没有有效变化")
        changed_shots = sorted({item["shot_no"] for item in changes}, key=_shot_sort_key)
        if not has_delete and (
            sorted({str(item) for item in actual.get("changed_shot_nos") or []}, key=_shot_sort_key)
            != changed_shots
            or sorted({str(item) for item in actual.get("changed_fields") or []})
            != sorted(actual_changed_fields_from_diff)
        ):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本变化明细与影响摘要不一致")
        parent_no = next(
            (
                index
                for index, item in enumerate(versions, start=1)
                if item["version_id"] == version.get("parent_version_id")
            ),
            None,
        )
        if parent_no is None:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "父版本不属于当前项目版本链")
        review_status = self._review_status(project_id, str(version["version_id"]))
        return {
            "state": "verified",
            "comparison_label": f"版本 {parent_no} → 版本 {version_no}",
            "version_no": version_no,
            "changes": changes,
            "changed_shot_count": len(changed_shots),
            "changed_field_count": len(changes),
            "unchanged_field_count": unchanged_business_fields,
            "protected_check_count": len(locks.get("checks") or []),
            "locked_field_status": "pass",
            "all_other_content_unchanged": True,
            "verification_items": [
                "实际变化与本次修改范围一致",
                f"其余 {unchanged_business_fields} 个逐镜字段保持不变",
                (
                    ("删除目标镜头后，剩余镜头连续重排；配图按已确认快照更新"
                     if has_delete else "镜头数量、顺序及来源保持不变；配图按已确认快照更新")
                    if image_changed_shots else "删除目标镜头后，剩余镜头连续重排且保留原始来源和图片绑定"
                    if has_delete else "镜头数量、顺序、来源和图片绑定保持不变"
                ),
            ],
            "review_status": review_status,
            "production_certification": "BLOCK",
        }

    def apply_table_change(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        shot_no: str,
        shot_id: str | None = None,
        field: str,
        value: Any,
        idempotency_key: str,
    ) -> dict:
        return self._apply(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_revision=expected_revision,
            expected_project_version=expected_project_version,
            shot_no=shot_no,
            shot_id=shot_id,
            field=field,
            value=value,
            origin="table",
            idempotency_key=idempotency_key,
        )

    def apply_instruction(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        instruction: str,
        idempotency_key: str,
    ) -> dict:
        shot_no, field, value = _parse_instruction(instruction)
        current = self.read_current(
            auth_token, workspace_id=workspace_id, project_id=project_id,
        )
        target = next(
            (item for item in current.get("shots") or []
             if str(item.get("shot_no") or "") == shot_no),
            None,
        )
        if target is None:
            raise WorkspaceServiceError("DRAFT_INVALID", "补丁目标镜头或字段不存在")
        return self._apply(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_revision=expected_revision,
            expected_project_version=expected_project_version,
            shot_no=shot_no,
            shot_id=str(target["shot_id"]),
            field=field,
            value=value,
            origin="natural_language",
            idempotency_key=idempotency_key,
        )

    def apply_revision_operation(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        operation: dict[str, Any],
        idempotency_key: str,
    ) -> dict:
        kind = str(operation.get("operation") or "")
        if kind == "replace_field":
            return self._apply(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_revision=expected_revision,
                expected_project_version=expected_project_version,
                shot_no=str(operation.get("shot_no") or ""),
                shot_id=str(operation.get("shot_id") or "") or None,
                field=str(operation.get("field") or ""),
                value=operation.get("value"),
                origin="natural_language",
                idempotency_key=idempotency_key,
            )
        if kind == "clear_optional_field":
            field = str(operation.get("field") or "")
            if field not in {"voiceover", "subtitle", "notes"}:
                raise WorkspaceServiceError(
                    "SCRIPT_FIELD_CLEAR_FORBIDDEN", "只能清空旁白、字幕或备注",
                )
            return self._apply(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_revision=expected_revision,
                expected_project_version=expected_project_version,
                shot_no=str(operation.get("shot_no") or ""),
                shot_id=str(operation.get("shot_id") or "") or None,
                field=field,
                value="",
                origin="natural_language",
                idempotency_key=idempotency_key,
            )
        if kind == "delete_shot":
            return self._delete_shot(
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_revision=expected_revision,
                expected_project_version=expected_project_version,
                shot_no=str(operation.get("shot_no") or ""),
                shot_id=str(operation.get("shot_id") or "") or None,
                idempotency_key=idempotency_key,
            )
        raise WorkspaceServiceError("INVALID_INPUT", "未知脚本修改操作")

    def apply_revision_operations(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        operations: list[dict[str, Any]],
        idempotency_key: str,
    ) -> dict:
        """Apply a preflighted assistant batch as one reversible draft revision."""
        if not operations:
            raise WorkspaceServiceError("INVALID_INPUT", "批量修改不能为空")
        current = self.read_current(
            auth_token, workspace_id=workspace_id, project_id=project_id,
        )
        if current.get("state") == "empty":
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可编辑的脚本版本")
        if int(current.get("revision") or 0) != expected_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿状态已变化，请刷新")
        by_no = {str(item["shot_no"]): item for item in current["shots"]}
        changes: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        project, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可编辑的脚本版本")
        self._check_expected_project_version(
            current_project=project, expected_project_version=expected_project_version,
        )
        catalog_ref = None
        for operation in operations:
            kind = str(operation.get("operation") or "")
            if kind not in {"replace_field", "clear_optional_field", "source_rebind"}:
                raise WorkspaceServiceError("INVALID_INPUT", "批量修改包含未知操作")
            shot_no = str(operation.get("shot_no") or "").strip()
            shot = by_no.get(shot_no)
            if shot is None:
                raise WorkspaceServiceError("DRAFT_INVALID", "批量修改包含未知镜头")
            supplied_shot_id = str(operation.get("shot_id") or "").strip()
            if not supplied_shot_id:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "镜头身份缺失，请重新读取")
            if supplied_shot_id != str(shot.get("shot_id") or ""):
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "镜头身份已变化，请重新分析")
            if shot.get("assistant_locked"):
                raise WorkspaceServiceError("SCRIPT_TARGET_LOCKED", "助手不能修改受保护镜头")
            if kind == "source_rebind":
                value = operation.get("value")
                if not isinstance(value, dict):
                    raise WorkspaceServiceError("INVALID_INPUT", "来源重匹配证据无效")
                refs = value.get("source_refs")
                detail = value.get("detail_override")
                evidence = value.get("evidence")
                if not isinstance(refs, list) or len(refs) != 1 or not isinstance(detail, dict):
                    raise WorkspaceServiceError("INVALID_INPUT", "来源重匹配证据不完整")
                if _source_ref(detail) != refs[0]:
                    raise WorkspaceServiceError("INTEGRITY_ERROR", "来源明细与三元组不一致")
                if catalog_ref is None:
                    catalog_ref = self.services.artifacts.resolve_stage_artifact(
                        context.run_id, "source_catalog",
                        expected_manifest_sha256=context.manifest.sha256,
                    )
                if (
                    not isinstance(evidence, dict)
                    or str(evidence.get("source_catalog_sha256") or "")
                    != sha256_file(catalog_ref.path)
                ):
                    raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "来源目录已变化，请重新匹配")
                catalog = _read_json(catalog_ref.path, "冻结来源目录")
                if refs[0] not in list(catalog.get("source_refs") or []):
                    raise WorkspaceServiceError("ARTIFACT_INVALID", "来源三元组不在冻结目录中")
                if evidence.get("decision_mode") == "ORIGINAL":
                    adaptation = evidence.get("adaptation")
                    plan_ref = self.services.artifacts.resolve_stage_artifact(
                        context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
                    )
                    expected_values = {
                        str(item["field"]): item.get("value") for item in operations
                        if str(item.get("shot_no") or "") == shot_no
                        and item.get("field") in {"camera_type", "shot_size", "camera_move", "visual"}
                    }
                    reference = shot["source"]
                    expected_refs = [{"source_file": reference["file"], "source_sheet": reference["sheet"], "source_row": reference["row"]}]
                    if (not isinstance(adaptation, dict) or adaptation.get("origin") != "original_suggestion"
                            or refs != expected_refs or adaptation.get("reference_source_refs") != refs
                            or adaptation.get("fields") != expected_values
                            or (adaptation.get("basis") or {}).get("plan_sha256") != sha256_file(plan_ref.path)
                            or detail.get("原创改编依据") != json.dumps(adaptation, ensure_ascii=False, sort_keys=True)):
                        raise WorkspaceServiceError("INTEGRITY_ERROR", "原创改编依据与本次修改不一致")
                target = (shot_no, "__source_rebind__")
                if target in seen:
                    raise WorkspaceServiceError("INVALID_INPUT", "来源重匹配目标重复")
                seen.add(target)
                changes.append({
                    "shot_no": shot_no,
                    "field": "__source_rebind__",
                    "value": value,
                })
                continue
            field = str(operation.get("field") or "")
            self._validate_technical_edit(auth_token, workspace_id=workspace_id, project_id=project_id, field=field, value=operation.get("value"))
            allow_empty = kind == "clear_optional_field"
            if allow_empty and field not in {"voiceover", "subtitle", "notes"}:
                raise WorkspaceServiceError("SCRIPT_FIELD_CLEAR_FORBIDDEN", "只能清空旁白、字幕或备注")
            storage_field, normalized = _normalize_edit(
                field, "" if allow_empty else operation.get("value"),
            )
            target = (shot_no, storage_field)
            if target in seen:
                raise WorkspaceServiceError("INVALID_INPUT", "批量修改目标重复")
            if shot.get(field) == normalized:
                raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "修改内容与当前草稿相同")
            seen.add(target)
            changes.append({"shot_no": shot_no, "field": storage_field, "value": normalized})
        draft_id = self._open_draft_id(context)
        if draft_id is None:
            created = self._command(
                auth_token,
                action="create_draft",
                context=context,
                idempotency_key=f"{idempotency_key}:create",
                shots=self._subject_shots(context),
            )
            draft_id = str(created["draft_id"])
        result = self._command(
            auth_token,
            action="apply_agent_change_set",
            context=context,
            idempotency_key=idempotency_key,
            draft_id=draft_id,
            expected_revision=expected_revision,
            operations=changes,
        )
        if int(result["revision"]) != expected_revision + 1:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "批量修改未形成单一草稿 revision")
        return self.read_current(
            auth_token, workspace_id=workspace_id, project_id=project_id,
        )

    def preflight_source_rematches(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        operations: list[dict[str, Any]],
        allow_original_adaptation: bool = False,
    ) -> dict:
        """Rematch technical changes; expression edits retain their reference provenance.

        Visual text is a proposed shooting description, not a new media identity.
        It still participates in the existing draft audit and image reconfirmation.
        """
        for operation in operations:
            self._validate_technical_edit(auth_token, workspace_id=workspace_id, project_id=project_id,
                                          field=str(operation.get("field") or ""), value=operation.get("value"))
        source_fields = {"camera_type", "shot_size", "camera_move"}
        targets = {
            str(item.get("shot_no") or "")
            for item in operations
            if item.get("field") in source_fields
        }
        if not targets:
            return {"ready": True, "operations": operations, "matches": []}
        current = self.read_current(
            auth_token, workspace_id=workspace_id, project_id=project_id,
        )
        if int(current.get("revision") or 0) != expected_revision:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿状态已变化，请刷新")
        by_no = {str(item["shot_no"]): item for item in current.get("shots") or []}
        if any(shot_no not in by_no for shot_no in targets):
            raise WorkspaceServiceError("DRAFT_INVALID", "来源重匹配包含未知镜头")
        _, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可编辑的脚本版本")
        self._check_expected_project_version(
            current_project=self.services.projects.get_project(auth_token, project_id),
            expected_project_version=expected_project_version,
        )
        plan_ref = self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        )
        catalog_ref = self.services.artifacts.resolve_stage_artifact(
            context.run_id, "source_catalog", expected_manifest_sha256=context.manifest.sha256,
        )
        plan = read_script_plan(plan_ref.path)
        catalog = _read_json(catalog_ref.path, "冻结来源目录")
        if catalog.get("schema_version") != "mplus-source-catalog/v1":
            raise WorkspaceServiceError("ARTIFACT_INVALID", "冻结来源目录版本无效")
        if str(catalog.get("script_type") or "") != str(plan.brief.get("脚本类型") or ""):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "冻结来源目录类别与脚本不一致")
        open_draft_id = self._open_draft_id(context)
        draft_details = {}
        if open_draft_id:
            current_draft, _, _ = self._repository(auth_token, context).draft_state(open_draft_id)
            draft_details = {str(shot["shot_no"]): shot.get("source_detail_override") for shot in current_draft.shots}
        records = list(catalog.get("records") or [])
        allowed = set(SourceShot.__dataclass_fields__)
        variants = [
            SourceShot(**{
                key: (tuple(value) if key == "missing_metadata" else value)
                for key, value in item.items()
                if key in allowed
            })
            for item in records
            if isinstance(item, dict) and item.get("record_type") == "creative"
        ]
        slots = list(plan.template_slots)
        if (not variants and not allow_original_adaptation) or len(slots) != len(current.get("shots") or []):
            return {
                "ready": False,
                "issues": [{
                    "code": "source_catalog_incomplete",
                    "message": "当前冻结创意库不足以完成逐镜来源重匹配。",
                    "count": len(targets),
                }],
            }
        constraints = deepcopy(plan.constraints)
        required = dict(constraints.get("required_by_slot") or {})
        target_indexes: dict[str, int] = {}
        for index, slot in enumerate(slots):
            shot_no = str(index + 1)
            shot = by_no[shot_no]
            slots[index] = replace(
                slot,
                structure=str(shot.get("structure") or slot.structure),
                camera_type=str(shot.get("camera_type") or slot.camera_type),
                shot_size=str(shot.get("shot_size") or slot.shot_size),
                camera_move=str(shot.get("camera_move") or slot.camera_move),
                visual_desc=str(shot.get("visual") or slot.visual_desc),
            )
            if shot_no in targets:
                target_indexes[shot_no] = index
        for operation in operations:
            shot_no = str(operation.get("shot_no") or "")
            if shot_no not in targets:
                continue
            index = target_indexes[shot_no]
            slot = slots[index]
            field = str(operation.get("field") or "")
            value = str(operation.get("value") or "").strip()
            if field == "camera_type":
                slot = replace(slot, camera_type=value)
            elif field == "shot_size":
                slot = replace(slot, shot_size=value)
            elif field == "camera_move":
                slot = replace(slot, camera_move=value)
            elif field == "visual":
                slot = replace(slot, visual_desc=value)
            slots[index] = slot
            if field in {"camera_type", "shot_size", "camera_move"}:
                required.setdefault(str(slot.slot_index), {})[field] = value
        constraints["required_by_slot"] = required
        constraints["force_keep_slots"] = [
            slot.slot_index
            for index, slot in enumerate(slots, start=1)
            if str(index) not in targets
        ]
        decisions = match_template_slots(plan.brief, slots, variants, constraints=constraints)
        projected_rows, projected_details = generate_template_rows(plan.brief, decisions)
        enhanced = [dict(item) for item in operations]
        matches = []
        issues = []
        for shot_no, index in sorted(target_indexes.items(), key=lambda item: int(item[0])):
            decision = decisions[index]
            variant = decision.variant
            requested = {
                str(item["field"]): str(item.get("value") or "").strip()
                for item in operations
                if str(item.get("shot_no") or "") == shot_no
                and item.get("field") in {"camera_type", "shot_size", "camera_move"}
            }
            projected = projected_rows[index]
            storage = {
                "camera_type": "类型", "shot_size": "景别", "camera_move": "运镜",
            }
            valid_projection = all(
                str(projected.get(storage[field]) or "").strip() == value
                for field, value in requested.items()
            )
            if variant is None or decision.mode != "REPLACE" or not valid_projection:
                if allow_original_adaptation:
                    shot = by_no[shot_no]
                    # Keep the frozen reference. Original parameters are explained
                    # separately, never presented as a newly matched source record.
                    prior_detail = deepcopy(draft_details.get(shot_no) or plan.detail_rows[index])
                    source_ref = _source_ref(prior_detail)
                    if source_ref not in list(catalog.get("source_refs") or []):
                        raise WorkspaceServiceError("ARTIFACT_INVALID", "原创改编缺少可追溯的参考来源")
                    adapted_values = {
                        str(item["field"]): item.get("value") for item in operations
                        if str(item.get("shot_no") or "") == shot_no
                        and item.get("field") in source_fields | {"visual"}
                    }
                    prior_adaptation = prior_detail.get("原创改编依据")
                    if isinstance(prior_adaptation, str) and prior_adaptation:
                        prior_adaptation = json.loads(prior_adaptation)
                    adaptation = {
                        "origin": "original_suggestion",
                        "label": "原创拍摄建议",
                        "fields": adapted_values,
                        "reference_role": "创意参考，不证明原创技术参数",
                        "reference_source_refs": [source_ref],
                        "basis": {
                            "brief": deepcopy(plan.brief),
                            "creative_standard": deepcopy(plan.constraints.get("creative_standard") or {}),
                            "plan_sha256": sha256_file(plan_ref.path),
                            "source_catalog_sha256": sha256_file(catalog_ref.path),
                        },
                        "previous": prior_adaptation or None,
                    }
                    prior_detail["原创改编依据"] = json.dumps(adaptation, ensure_ascii=False, sort_keys=True)
                    prior_detail["改编说明"] = "原创拍摄建议：" + "；".join(_EDITABLE_FIELDS[field] + "=" + str(value) for field, value in adapted_values.items()) + "。依据：已确认 Brief、创作标准与本次修改；来源仅供创意参考。"
                    enhanced.append({
                        "operation": "source_rebind", "shot_no": shot_no,
                        "shot_id": shot["shot_id"], "field": "source_refs",
                        "value": {"source_refs": [source_ref], "detail_override": prior_detail,
                                  "evidence": {"source_catalog_sha256": sha256_file(catalog_ref.path),
                                               "decision_mode": "ORIGINAL", "adaptation": adaptation}},
                    })
                    matches.append({"shot_no": shot_no, "shot_id": shot["shot_id"],
                                    "origin": "original_suggestion", "reason": prior_detail["改编说明"],
                                    "source": {"file": source_ref["source_file"], "sheet": source_ref["source_sheet"], "row": source_ref["source_row"]}})
                    continue
                issues.append({
                    "code": "source_match_required",
                    "shot_no": shot_no,
                    "shot_id": by_no[shot_no]["shot_id"],
                    "message": decision.gap_note or "没有满足目标类型且可安全采用的创意库来源。",
                    "count": 1,
                })
                continue
            source_ref = {
                "source_file": variant.source_file,
                "source_sheet": variant.source_sheet,
                "source_row": variant.source_row,
            }
            detail = dict(projected_details[index])
            evidence = {
                "source_catalog_sha256": sha256_file(catalog_ref.path),
                "source_view": str(catalog.get("source_view") or ""),
                "asset_package": catalog.get("asset_package"),
                "formal_asset_package": catalog.get("formal_asset_package"),
                "decision_mode": decision.mode,
                "score": decision.score,
                "match_reason": decision.match_reason,
                "selected_reason": decision.selected_reason,
                "variant": asdict(variant),
            }
            rebind = {
                "source_refs": [source_ref],
                "detail_override": detail,
                "evidence": evidence,
            }
            enhanced.append({
                "operation": "source_rebind",
                "shot_no": shot_no,
                "shot_id": by_no[shot_no]["shot_id"],
                "field": "source_refs",
                "value": rebind,
            })
            matches.append({
                "shot_no": shot_no,
                "shot_id": by_no[shot_no]["shot_id"],
                "source": {
                    "file": variant.source_file,
                    "sheet": variant.source_sheet,
                    "row": variant.source_row,
                    "category": "创意库",
                },
                "score": decision.score,
                "reason": decision.selected_reason or decision.match_reason,
            })
        if issues:
            return {"ready": False, "issues": issues}
        return {"ready": True, "operations": enhanced, "matches": matches}

    def undo(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        idempotency_key: str,
    ) -> dict:
        return self._history_action(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_revision=expected_revision,
            expected_project_version=expected_project_version,
            idempotency_key=idempotency_key,
            action="undo",
        )

    def redo(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        idempotency_key: str,
    ) -> dict:
        return self._history_action(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_revision=expected_revision,
            expected_project_version=expected_project_version,
            idempotency_key=idempotency_key,
            action="redo",
        )

    def discard_changes(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        idempotency_key: str,
    ) -> dict:
        return self._history_action(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            expected_revision=expected_revision,
            expected_project_version=expected_project_version,
            idempotency_key=idempotency_key,
            action="discard_changes",
        )

    def save_version(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        reason: str,
        idempotency_key: str,
        transaction_hook: Callable | None = None,
        expected_progress_revision: int | None = None,
        image_snapshot_digest: str | None = None,
    ) -> dict:
        reason = str(reason or "").strip()
        key = str(idempotency_key or "").strip()
        if not reason or len(reason) > 500 or not key or len(key) > 160:
            raise WorkspaceServiceError("INVALID_INPUT", "保存原因或幂等键无效")
        request_sha = sha256_json({
            "project_id": project_id,
            "expected_revision": expected_revision,
            "reason": reason,
            **({"image_snapshot_digest": image_snapshot_digest} if image_snapshot_digest else {}),
        })
        replay = self._submission_replay(
            auth_token, workspace_id=workspace_id, project_id=project_id,
            idempotency_key=key, request_sha256=request_sha,
        )
        if replay is not None:
            return replay
        project, context = self._current_context(auth_token, workspace_id, project_id)
        self._check_expected_project_version(
            current_project=project, expected_project_version=expected_project_version,
        )
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可保存的脚本版本")
        working_images = None
        if self.services.image_workspace is not None:
            working_images = self.services.image_workspace.prepare_working_version(auth_token,
                workspace_id=workspace_id, project_id=project_id,
                expected_project_version=context.project_lock_version, expected_script_revision=expected_revision,
                expected_progress_revision=expected_progress_revision)
            if image_snapshot_digest and working_images["snapshot_digest"] != image_snapshot_digest:
                raise WorkspaceServiceError("IMAGE_CANDIDATE_SNAPSHOT_DRIFT", "配图或分析依据已变化，请重新查看修改")
        draft_id = self._open_draft_id(context)
        if working_images is not None and (not draft_id or not self.read_current(auth_token,
                workspace_id=workspace_id, project_id=project_id)["can_save_version"]):
            from .formal_image_workspace import ImageShotSelection
            if not working_images["changed_shot_ids"]:
                raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "当前没有待保存的文字或配图修改")
            working_images["submission_sha256"] = request_sha
            self.services.image_workspace.save_version(auth_token, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=context.project_lock_version,
                selections=tuple(ImageShotSelection(item["shot_no"], item["image_key"], True,
                    candidate_snapshot_digest=item["candidate_snapshot_digest"], query=item["query"],
                    reuse_reason=item["reuse_reason"]) for item in working_images["selections"]),
                allowed_shot_ids=working_images["changed_shot_ids"], idempotency_key=key,
                working_evidence=working_images)
            return {**self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id), "save_status": "completed"}
        if not draft_id:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "当前脚本没有待保存的草稿修改")
        repository = self._repository(auth_token, context)
        draft, _, cursor = repository.draft_state(draft_id)
        if draft.revision != expected_revision or draft.revision <= 0:
            raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿已变化，请刷新后重试")
        if cursor == 0:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "当前脚本没有待保存的草稿修改")
        change_history = self._draft_change_history(draft.draft_id)
        with self.services.store.connection() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="version.create",
            )
        inherited_image_evidence = working_images
        if (
            working_images is None
            and
            self.services.image_workspace is not None
            and change_history
            and all(
                str(item.get("field") or "")
                not in {"类型", "景别", "运镜", "画面描述", "source_refs"}
                for item in change_history
            )
        ):
            try:
                inherited_image_evidence = self.services.image_workspace.resolve_script_inheritance(
                    auth_token,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    expected_project_version=context.project_lock_version,
                )
            except WorkspaceServiceError as exc:
                if exc.code != "DELIVERY_NOT_READY":
                    raise
        child_version_id = new_id("version")
        materialized = self.materializer.materialize(
            context=context,
            draft=draft,
            child_version_id=child_version_id,
            reason=reason,
            request_sha256=request_sha,
            change_history=change_history,
            inherited_image_evidence=inherited_image_evidence,
            image_authority=self.services.image_workspace,
            created_by_actor_id=str(actor["actor_id"]),
        )
        self._commit_materialized_version(
            auth_token,
            context=context,
            workspace_id=workspace_id,
            project_id=project_id,
            draft=draft,
            child_version_id=child_version_id,
            materialized=materialized,
            reason=reason,
            idempotency_key=key,
            request_sha256=request_sha,
            transaction_hook=transaction_hook,
        )
        current = self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)
        return {**current, "save_status": "completed"}

    def _apply(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None,
        shot_no: str,
        shot_id: str | None = None,
        field: str,
        value: Any,
        origin: str,
        idempotency_key: str,
    ) -> dict:
        project, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可编辑的脚本版本")
        self._check_expected_project_version(
            current_project=project, expected_project_version=expected_project_version,
        )
        self._validate_technical_edit(auth_token, workspace_id=workspace_id, project_id=project_id, field=field, value=value)
        storage_field, normalized = _normalize_edit(field, value)
        draft_id = self._open_draft_id(context)
        if draft_id is None:
            current_shots = self._subject_shots(context)
        else:
            current_draft, _, _ = self._repository(auth_token, context).draft_state(draft_id)
            current_shots = list(current_draft.shots)
        shot_key = str(shot_no or "").strip()
        target = next(
            (item for item in current_shots if str(item.get("shot_no") or "").strip() == shot_key),
            None,
        )
        if target is None or storage_field not in dict(target.get("fields") or {}):
            raise WorkspaceServiceError("DRAFT_INVALID", "补丁目标镜头或字段不存在")
        supplied_shot_id = str(shot_id or "").strip()
        current_shot_id = self._current_shot_ids(context, current_shots).get(shot_key, "")
        if not supplied_shot_id:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "缺少镜头身份，请重新读取")
        if supplied_shot_id != current_shot_id:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "镜头身份已变化，请重新读取")
        replay = self._replay_table_command(
            auth_token,
            context=context,
            expected_revision=expected_revision,
            shot_no=shot_key,
            shot_id=supplied_shot_id,
            field=storage_field,
            value=normalized,
            origin=origin,
            idempotency_key=idempotency_key,
            draft_id=draft_id,
        )
        if replay is not None:
            return replay
        if origin == "natural_language":
            self._require_assistant_editable(workspace_id, project_id, current_shot_id)
        if target["fields"][storage_field] == normalized:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "修改内容与当前草稿相同")
        if draft_id is None:
            if expected_revision != 0:
                raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿状态已变化，请刷新")
            try:
                created = self._command(
                    auth_token,
                    action="create_draft",
                    context=context,
                    idempotency_key=f"{idempotency_key}:create",
                    shots=self._subject_shots(context),
                )
                draft_id = str(created["draft_id"])
            except WorkspaceServiceError as exc:
                if exc.code != "SCRIPT_DRAFT_ALREADY_EXISTS":
                    raise
                draft_id = self._open_draft_id(context)
                if draft_id is None:
                    raise
        result = self._command(
            auth_token,
            action="apply_change_set",
            context=context,
            idempotency_key=idempotency_key,
            draft_id=draft_id,
            expected_revision=expected_revision,
            origin=origin,
            shot_no=shot_key,
            shot_id=supplied_shot_id,
            field=storage_field,
            value=normalized,
        )
        if int(result["revision"]) != expected_revision + 1:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "草稿 revision 未按预期推进")
        return {
            **self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id),
            "applied_revision": int(result["revision"]),
            "replayed": False,
        }

    def _check_expected_project_version(
        self, *, current_project: dict, expected_project_version: int | None,
    ) -> None:
        if expected_project_version is None:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "缺少项目版本，请重新读取")
        check_version(
            int(current_project["project_version"]), expected_project_version,
            "PROJECT_VERSION_CONFLICT",
        )

    def _replay_table_command(
        self,
        auth_token: str,
        *,
        context: VersionDeliveryContext,
        expected_revision: int,
        shot_no: str,
        shot_id: str,
        field: str,
        value: Any,
        origin: str,
        idempotency_key: str,
        draft_id: str | None,
    ) -> dict | None:
        if not draft_id:
            return None
        parameters = {
            "project_id": context.project_id,
            "version_id": context.version_id,
            "expected_project_lock_version": context.project_lock_version,
            "idempotency_key": str(idempotency_key or "").strip(),
            "draft_id": draft_id,
            "expected_revision": expected_revision,
            "origin": origin,
            "shot_no": shot_no,
            "shot_id": shot_id,
            "field": field,
            "value": value,
        }
        request_sha = _manual_command_digest("apply_change_set", parameters)
        with self.services.store.connection() as connection:
            actor = require_permission(
                connection, auth_token, workspace_id=context.workspace_id,
                project_id=context.project_id, permission="project.update",
            )
            row = connection.execute(
                """SELECT request_sha256, command_id FROM manual_command_idempotency
                WHERE actor_id=? AND project_id=? AND tool_name=? AND idempotency_key=?""",
                (actor["actor_id"], context.project_id, "apply_change_set", idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if str(row["request_sha256"]) != request_sha:
            raise WorkspaceServiceError(
                "IDEMPOTENCY_CONFLICT", "修改请求的幂等键已用于不同内容",
            )
        recovered = self.services.commands.recover_idempotent(
            auth_token, action="apply_change_set", parameters=parameters,
            trusted_scope="formal_script",
        )
        if recovered is None or recovered.get("applied_revision") is None:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "脚本命令恢复后缺少精确域证据")
        applied_revision = int(recovered["applied_revision"])
        current = self.read_current(
            auth_token, workspace_id=context.workspace_id, project_id=context.project_id,
        )
        return {**current, "applied_revision": applied_revision, "replayed": True}

    def _delete_shot(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        shot_no: str,
        shot_id: str | None,
        idempotency_key: str,
    ) -> dict:
        project, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可编辑的脚本版本")
        self._check_expected_project_version(
            current_project=project, expected_project_version=expected_project_version,
        )
        draft_id = self._open_draft_id(context)
        current_shots = (list(self._repository(auth_token, context).draft_state(draft_id)[0].shots)
                         if draft_id else self._subject_shots(context))
        current_shot_id = self._current_shot_ids(context, current_shots).get(str(shot_no or "").strip(), "")
        if not str(shot_id or "").strip():
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "缺少镜头身份，请重新读取")
        if str(shot_id).strip() != current_shot_id:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "镜头身份已变化，请重新读取")
        self._require_assistant_editable(workspace_id, project_id, current_shot_id)
        draft_id = self._open_draft_id(context)
        if draft_id is None:
            if expected_revision != 0:
                raise WorkspaceServiceError("DRAFT_REVISION_CONFLICT", "草稿状态已变化，请刷新")
            try:
                created = self._command(
                    auth_token,
                    action="create_draft",
                    context=context,
                    idempotency_key=f"{idempotency_key}:create",
                    shots=self._subject_shots(context),
                )
                draft_id = str(created["draft_id"])
            except WorkspaceServiceError as exc:
                if exc.code != "SCRIPT_DRAFT_ALREADY_EXISTS":
                    raise
                draft_id = self._open_draft_id(context)
                if draft_id is None:
                    raise
        result = self._command(
            auth_token,
            action="delete_shot",
            context=context,
            idempotency_key=idempotency_key,
            draft_id=draft_id,
            expected_revision=expected_revision,
            origin="natural_language",
            shot_no=str(shot_no or "").strip(),
            shot_id=str(shot_id).strip(),
        )
        if int(result["revision"]) != expected_revision + 1:
            raise WorkspaceServiceError("INTEGRITY_ERROR", "草稿 revision 未按预期推进")
        return self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)

    def _history_action(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_revision: int,
        expected_project_version: int | None = None,
        idempotency_key: str,
        action: str,
    ) -> dict:
        project, context = self._current_context(auth_token, workspace_id, project_id)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有脚本版本")
        self._check_expected_project_version(
            current_project=project, expected_project_version=expected_project_version,
        )
        draft_id = self._open_draft_id(context)
        if not draft_id:
            raise WorkspaceServiceError("DRAFT_NOT_CHANGED", "当前没有可撤销或恢复的修改")
        self._command(
            auth_token,
            action=action,
            context=context,
            idempotency_key=idempotency_key,
            draft_id=draft_id,
            expected_revision=expected_revision,
        )
        return self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)

    def _command(self, auth_token: str, *, action: str, context: VersionDeliveryContext, idempotency_key: str, **values) -> dict:
        parameters = {
            "project_id": context.project_id,
            "version_id": context.version_id,
            "expected_project_lock_version": context.project_lock_version,
            "idempotency_key": str(idempotency_key or "").strip(),
            **values,
        }
        try:
            prepared = self.services.commands.prepare(
                auth_token, action=action, parameters=parameters,
            )
            return self.services.commands.execute(
                auth_token,
                action=action,
                parameters=parameters,
                command_token=prepared["command_token"],
                trusted_scope="formal_script",
            )["result"]
        except WorkspaceServiceError as exc:
            if exc.code == "IDEMPOTENCY_REPLAY_REQUIRED" and action in {"create_draft", "apply_change_set"}:
                recovered = self.services.commands.recover_idempotent(
                    auth_token, action=action, parameters=parameters,
                    trusted_scope="formal_script",
                )
                if recovered is not None:
                    return recovered
            raise
        except VersionReviewError as exc:
            message = str(exc)
            code = (
                "DRAFT_REVISION_CONFLICT"
                if "DRAFT_REVISION_CONFLICT" in message
                else "DRAFT_INVALID"
            )
            raise WorkspaceServiceError(code, message) from exc

    def _current_context(self, auth_token: str, workspace_id: str, project_id: str):
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
        version_id = str(project.get("current_version_id") or "")
        if not version_id:
            return project, None
        return project, self.services.delivery.resolve(
            auth_token,
            project_id,
            version_id,
            expected_project_version=project["project_version"],
        )

    def _repository(self, auth_token: str, context: VersionDeliveryContext):
        return WorkspaceVersionReviewRepository(
            self.services.store,
            auth_token=auth_token,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            version_id=context.version_id,
            run_id=context.run_id,
            expected_project_lock_version=context.project_lock_version,
        )

    def _open_draft_id(self, context: VersionDeliveryContext) -> str | None:
        with self.services.store.connection() as connection:
            return open_script_draft_id(
                connection,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                base_version_id=context.version_id,
            )

    def _subject_shots(self, context: VersionDeliveryContext) -> list[dict]:
        resolved = self.services.artifacts.resolve_stage_artifact(
            context.run_id,
            "visual_binding",
            expected_manifest_sha256=context.manifest.sha256,
        )
        binding = read_visual_binding(resolved.path)
        if len(binding.script_rows) != len(binding.detail_rows) or not binding.script_rows:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "当前脚本镜头与来源链不完整")
        result = []
        for row, detail in zip(binding.script_rows, binding.detail_rows):
            shot_no = str(row.get("镜号") or row.get("shot_no") or "").strip()
            if not shot_no or _detail_shot_no(detail) != shot_no:
                raise WorkspaceServiceError("ARTIFACT_INVALID", "当前脚本镜号链不一致")
            fields = {
                key: deepcopy(value)
                for key, value in row.items()
                if key not in {"镜号", "shot_no"}
            }
            for storage_field in _EDITABLE_FIELDS.values():
                fields.setdefault(storage_field, "")
            result.append({
                "shot_no": shot_no,
                "origin_shot_no": shot_no,
                "fields": fields,
                "source_refs": [_source_ref(detail)],
                **({"source_detail_override": deepcopy(detail)} if detail.get("原创改编依据") else {}),
            })
        return result

    def _pending_confirmations(self, context: VersionDeliveryContext) -> list[dict]:
        resolved = self.services.artifacts.resolve_stage_artifact(
            context.run_id,
            "script_draft",
            expected_manifest_sha256=context.manifest.sha256,
        )
        pending = read_script_draft(resolved.path).pending_confirmations
        allowed = ("item", "shot_no", "status", "question", "message", "fact")
        return [
            {
                key: str(item[key])
                for key in allowed
                if key in item and str(item[key]).strip()
            }
            for item in pending
            if isinstance(item, dict)
        ]

    def _current_shot_ids(self, context: VersionDeliveryContext, shots: list[dict] | None = None) -> dict[str, str]:
        identities = resolve_version_shot_ids(self.services.artifacts, context)
        if shots is None:
            return identities
        projected = {
            str(shot.get("shot_no") or ""): identities.get(
                str(shot.get("origin_shot_no") or shot.get("shot_no") or ""), "")
            for shot in shots
        }
        if (len(projected) != len(shots) or not all(projected.values())
                or len(set(projected.values())) != len(projected)):
            raise WorkspaceServiceError("INTEGRITY_ERROR", "当前脚本缺少稳定镜头身份")
        return projected

    def _require_assistant_editable(self, workspace_id: str, project_id: str, shot_id: str) -> None:
        if shot_id in self._assistant_locked_shot_ids(workspace_id, project_id):
            raise WorkspaceServiceError("SCRIPT_TARGET_LOCKED", "助手不能修改受保护镜头")

    def _assistant_locked_shot_ids(self, workspace_id: str, project_id: str) -> set[str]:
        with self.services.store.connection() as connection:
            rows = connection.execute(
                """SELECT shot_id FROM formal_script_assistant_locks
                WHERE workspace_id=? AND project_id=? ORDER BY shot_id""",
                (workspace_id, project_id),
            ).fetchall()
        return {str(row["shot_id"]) for row in rows}

    def _image_reconfirmation_shot_ids(
        self,
        context: VersionDeliveryContext,
        *,
        shots: list[dict],
        shot_ids: dict[str, str],
    ) -> set[str]:
        relevant = {"类型", "景别", "运镜", "画面描述", "source_refs"}
        result = set()
        if self._open_draft_id(context):
            base = {str(shot["shot_no"]): shot for shot in self._subject_shots(context)}
            for shot in shots:
                shot_no = str(shot["shot_no"])
                prior = base.get(str(shot.get("origin_shot_no") or shot_no), {})
                if (shot.get("source_refs") != prior.get("source_refs")
                        or any((shot.get("fields") or {}).get(field) != (prior.get("fields") or {}).get(field)
                               for field in relevant - {"source_refs"})):
                    result.add(shot_ids[shot_no])
        try:
            resolved = self.services.artifacts.resolve_artifact(context.run_id, "revision_report")
            report = _read_json(resolved.path, "版本差异报告")
        except ArtifactResolutionError:
            return result
        for item in (report.get("version_diff") or {}).get("shot_diffs") or []:
            if any(
                str(field.get("field") or "") in relevant
                and str(field.get("status") or "") != "unchanged"
                for field in item.get("field_diffs") or []
            ):
                shot_id = str(item.get("child_shot_id") or "")
                if shot_id:
                    result.add(shot_id)
        return result

    def _employee_model(
        self,
        auth_token: str,
        project: dict,
        context: VersionDeliveryContext,
        shots: list[dict],
        *,
        pending_confirmations: list[dict],
        status: str,
        revision: int,
        can_undo: bool,
        can_redo: bool,
        can_save_version: bool,
    ) -> dict:
        plan_artifact = self.services.artifacts.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        )
        frozen_brief = _read_json(plan_artifact.path, "冻结 ScriptPlan").get("confirmed_brief_input") or {}
        frozen_snapshot = str(frozen_brief.get("brief_snapshot_id") or "")
        latest_snapshot = str(project.get("current_brief_snapshot_id") or "")
        has_new_confirmed_brief = bool(frozen_snapshot and latest_snapshot and frozen_snapshot != latest_snapshot)
        versions = self.services.projects.list_project_versions(auth_token, context.project_id)
        shot_ids = self._current_shot_ids(context, shots)
        assistant_locked = self._assistant_locked_shot_ids(
            project["workspace_id"], context.project_id,
        ).intersection(shot_ids.values())
        image_reconfirmation = self._image_reconfirmation_shot_ids(
            context, shots=shots, shot_ids=shot_ids,
        )
        with self.services.store.connection() as connection:
            submissions = {
                str(row["materialized_version_id"]): dict(row)
                for row in connection.execute(
                    """SELECT materialized_version_id, revision_report_ref,
                    revision_report_sha256
                    FROM version_review_draft_submissions WHERE project_id=?""",
                    (context.project_id,),
                ).fetchall()
            }
            confirmations = {
                str(row["version_id"]): str(row["decision"])
                for row in connection.execute(
                    """SELECT version_id, decision
                    FROM version_quality_final_confirmations WHERE project_id=?""",
                    (context.project_id,),
                ).fetchall()
            }
        current_index = next(
            (index for index, item in enumerate(versions, start=1) if item["version_id"] == context.version_id),
            len(versions),
        )
        return {
            "state": status,
            "project_name": str(project.get("name") or "当前项目"),
            "version_label": f"版本 {current_index}",
            "base_version_id": context.version_id,
            "project_version": context.project_lock_version,
            "has_new_confirmed_brief": has_new_confirmed_brief,
            "revision": revision,
            "can_undo": can_undo,
            "can_redo": can_redo,
            "can_save_version": can_save_version,
            "assistant_locked_shot_ids": sorted(assistant_locked),
            "shots": [
                _employee_shot(
                    item,
                    shot_id=shot_ids[str(item.get("shot_no") or "")],
                    assistant_locked=shot_ids[str(item.get("shot_no") or "")] in assistant_locked,
                    image_reconfirmation_required=(
                        shot_ids[str(item.get("shot_no") or "")] in image_reconfirmation
                    ),
                )
                for item in shots
            ],
            "pending_confirmations": pending_confirmations,
            "versions": [self._employee_version(
                item,
                index=index,
                is_current=item["version_id"] == context.version_id,
                submission=submissions.get(str(item["version_id"])),
                final_decision=confirmations.get(str(item["version_id"])),
            ) for index, item in enumerate(versions, start=1)],
            "production_certification": "BLOCK",
        }

    def _employee_version(
        self,
        version: dict,
        *,
        index: int,
        is_current: bool,
        submission: dict | None,
        final_decision: str | None,
    ) -> dict:
        changed_shots: list[str] = []
        changed_fields: list[str] = []
        if submission is not None:
            report = self._verified_revision_report(version, submission)
            impact = report.get("actual_impact")
            if not isinstance(impact, dict):
                raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异报告缺少影响范围")
            changed_shots = [str(value) for value in impact.get("changed_shot_nos") or []]
            changed_fields = [str(value) for value in impact.get("changed_fields") or []]
            if not changed_shots or not changed_fields:
                raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异报告没有有效变化")
        review_status = (
            "已完成整片确认"
            if final_decision == "quality_pass"
            else "整片确认未通过"
            if final_decision == "quality_fail"
            else "等待项目经理审核"
        )
        return {
            "version_no": index,
            "label": f"版本 {index}",
            "created_at": str(version.get("created_at") or ""),
            "is_current": is_current,
            "change_summary": (
                f"镜头 {'、'.join(changed_shots)} · {'、'.join(changed_fields)}"
                if changed_shots else "首个脚本版本"
            ),
            "review_status": review_status,
            "has_diff": submission is not None,
        }

    def _verified_revision_report(self, version: dict, submission: dict) -> dict:
        try:
            resolved = self.services.artifacts.resolve_artifact(
                str(version["run_id"]), "revision_report",
            )
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异报告无法读取") from exc
        if (
            resolved.ref != submission["revision_report_ref"]
            or resolved.sha256 != submission["revision_report_sha256"]
        ):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异报告绑定已变化")
        report = _read_json(resolved.path, "版本差异报告")
        if (
            report.get("schema_version") != "mplus-revision-report/v1"
            or report.get("project_id") != version["project_id"]
            or report.get("parent_version_id") != version.get("parent_version_id")
            or report.get("child_version_id") != version["version_id"]
        ):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异报告上下文不一致")
        return report

    def _review_status(self, project_id: str, version_id: str) -> str:
        with self.services.store.connection() as connection:
            row = connection.execute(
                """SELECT decision FROM version_quality_final_confirmations
                WHERE project_id=? AND version_id=?""",
                (project_id, version_id),
            ).fetchone()
        return (
            "已完成整片确认"
            if row is not None and row["decision"] == "quality_pass"
            else "整片确认未通过"
            if row is not None and row["decision"] == "quality_fail"
            else "等待项目经理审核"
        )

    def _submission_replay(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> dict | None:
        with self.services.store.connection() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="version.create",
            )
            row = connection.execute(
                """SELECT request_sha256 FROM version_review_draft_submissions
                WHERE submitted_by_actor_id=? AND project_id=? AND idempotency_key=?""",
                (actor["actor_id"], project_id, idempotency_key),
            ).fetchone()
            if row is None:
                row = connection.execute("""SELECT request_sha256 FROM version_image_decision_snapshots_v3
                    WHERE created_by_actor_id=? AND project_id=? AND idempotency_key=?""",
                    (actor["actor_id"], project_id, idempotency_key)).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_sha256:
            raise WorkspaceServiceError("IDEMPOTENCY_CONFLICT", "保存请求的幂等键已用于不同内容")
        current = self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)
        return {**current, "save_status": "completed"}

    def _draft_change_history(self, draft_id: str) -> list[dict]:
        with self.services.store.connection() as connection:
            rows = connection.execute(
                """SELECT revision, origin, shot_no, field_name
                FROM version_review_cell_patches
                WHERE draft_id=? ORDER BY revision, patch_id""",
                (draft_id,),
            ).fetchall()
        return [
            {
                "revision": int(row["revision"]),
                "origin": str(row["origin"]),
                "shot_no": str(row["shot_no"]),
                "field": str(row["field_name"]),
            }
            for row in rows
        ]

    def _commit_materialized_version(
        self,
        auth_token: str,
        *,
        context: VersionDeliveryContext,
        workspace_id: str,
        project_id: str,
        draft: Draft,
        child_version_id: str,
        materialized: dict,
        reason: str,
        idempotency_key: str,
        request_sha256: str,
        transaction_hook: Callable | None = None,
    ) -> dict:
        version_id = child_version_id
        timestamp = utc_now()
        with self.services.store.transaction() as connection:
            project = connection.execute(
                "SELECT * FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if project is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            if project["workspace_id"] != workspace_id:
                raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
            require_active_project(project)
            check_version(
                int(project["lock_version"]), context.project_lock_version,
                "PROJECT_VERSION_CONFLICT",
            )
            if project["current_version_id"] != context.version_id:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "当前脚本版本已变化")
            run_actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="run.bind",
            )
            version_actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="version.create",
            )
            if run_actor["actor_id"] != version_actor["actor_id"]:
                raise WorkspaceServiceError("PERMISSION_DENIED", "运行与版本操作人不一致")
            stored_draft = connection.execute(
                "SELECT * FROM version_review_drafts WHERE draft_id=?", (draft.draft_id,)
            ).fetchone()
            if stored_draft is None or (
                stored_draft["workspace_id"], stored_draft["project_id"],
                stored_draft["base_version_id"], int(stored_draft["current_revision"]),
            ) != (workspace_id, project_id, context.version_id, draft.revision):
                raise WorkspaceServiceError("DRAFT_VERSION_CONFLICT", "草稿在保存前已变化")
            if materialized.get("working_image_evidence") is not None:
                self.services.image_workspace.validate_working_commit(connection, context,
                    materialized["working_image_evidence"])
            parent_run = connection.execute(
                "SELECT project_id FROM project_runs WHERE run_id=?", (context.run_id,)
            ).fetchone()
            if parent_run is None or parent_run["project_id"] != project_id:
                raise WorkspaceServiceError("INVALID_PARENT_RUN", "父运行不属于当前 project")
            if connection.execute(
                "SELECT 1 FROM project_runs WHERE run_id=?", (materialized["run_id"],)
            ).fetchone() is not None:
                raise WorkspaceServiceError("RUN_ALREADY_BOUND", "运行已绑定")
            connection.execute(
                """INSERT INTO project_runs(
                    run_id, project_id, parent_run_id, source_kind,
                    manifest_ref, manifest_sha256, bound_by_actor_id, bound_at
                ) VALUES (?, ?, ?, 'native', ?, ?, ?, ?)""",
                (
                    materialized["run_id"], project_id, context.run_id,
                    materialized["manifest_ref"], materialized["manifest_sha256"],
                    run_actor["actor_id"], timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO project_versions(
                    version_id, project_id, run_id, parent_version_id,
                    content_sha256, artifact_ref, created_by_actor_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    version_id, project_id, materialized["run_id"], context.version_id,
                    materialized["content_sha256"], materialized["artifact_ref"],
                    version_actor["actor_id"], timestamp,
                ),
            )
            connection.execute(
                """INSERT INTO version_review_draft_submissions(
                    submission_id, workspace_id, project_id, draft_id,
                    base_version_id, materialized_version_id, revision, reason,
                    idempotency_key, request_sha256, revision_report_ref,
                    revision_report_sha256, submitted_by_actor_id, submitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("draft_submission"), workspace_id, project_id, draft.draft_id,
                    draft.base_version_id, version_id, draft.revision, reason,
                    idempotency_key, request_sha256,
                    materialized["revision_report_ref"], materialized["revision_report_sha256"],
                    version_actor["actor_id"], timestamp,
                ),
            )
            if self.services.image_workspace is not None:
                self.services.image_workspace.bind_script_child_snapshot(
                    connection,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    parent_version_id=context.version_id,
                    child_version_id=version_id,
                    materialized=materialized,
                    actor_id=str(version_actor["actor_id"]),
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                )
                self.services.image_workspace.inherit_progress(
                    connection, workspace_id=workspace_id, project_id=project_id,
                    parent_version_id=context.version_id, child_version_id=version_id,
                    materialized=materialized,
                    image_commit=bool(materialized.get("working_image_evidence")),
                )
            if transaction_hook is not None:
                transaction_hook(connection, version_id, materialized, version_actor)
            updated = connection.execute(
                """UPDATE projects
                SET current_version_id=?, lock_version=lock_version+2, updated_at=?
                WHERE project_id=? AND current_version_id=? AND lock_version=?""",
                (
                    version_id, timestamp, project_id, context.version_id,
                    context.project_lock_version,
                ),
            )
            if updated.rowcount != 1:
                raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "项目在保存时已变化")
            enqueue_outbox(
                connection,
                aggregate_type="project",
                aggregate_id=project_id,
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
                aggregate_id=project_id,
                event_type="project.version_created",
                payload={
                    "artifact_ref": materialized["artifact_ref"],
                    "content_sha256": materialized["content_sha256"],
                    "run_id": materialized["run_id"],
                    "version_id": version_id,
                },
            )
        return {
            "version_id": version_id,
            "project_id": project_id,
            "run_id": materialized["run_id"],
            "parent_version_id": context.version_id,
            "content_sha256": materialized["content_sha256"],
            "artifact_ref": materialized["artifact_ref"],
            "current_version_id": version_id,
            "project_version": context.project_lock_version + 2,
        }


def _read_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceServiceError("ARTIFACT_INVALID", f"{label} 无法读取") from exc
    if not isinstance(value, dict):
        raise WorkspaceServiceError("ARTIFACT_INVALID", f"{label} 必须为 object")
    return value


def _detail_shot_no(detail: dict) -> str:
    return str(
        detail.get("镜号")
        or detail.get("shot_no")
        or detail.get("模板镜号")
        or detail.get("slot_index")
        or ""
    ).strip()


def _source_ref(detail: dict) -> dict:
    filename = str(detail.get("采用来源文件") or "").strip()
    sheet = str(detail.get("采用来源 sheet") or "").strip()
    try:
        row = int(detail.get("采用来源行") or 0)
    except (TypeError, ValueError):
        row = 0
    if not filename or not _SAFE_FILENAME.fullmatch(filename) or Path(filename).name != filename:
        raise WorkspaceServiceError("ARTIFACT_INVALID", "脚本来源文件名无效")
    if not sheet or row <= 0:
        raise WorkspaceServiceError("ARTIFACT_INVALID", "脚本来源 Sheet 或行无效")
    return {"source_file": filename, "source_sheet": sheet, "source_row": row}


def _validate_business_row(row: dict) -> None:
    shot_no = str(row.get("镜号") or "").strip()
    if not shot_no:
        raise WorkspaceServiceError("DRAFT_INVALID", "镜号不能为空")
    for field in ("画面描述", "声音"):
        value = row.get(field)
        if not isinstance(value, str) or not value.strip():
            raise WorkspaceServiceError("DRAFT_INVALID", f"镜 {shot_no} 的{field}不能为空")
    if parse_positive_seconds_strict(row.get("时长(秒)", "")) is None:
        raise WorkspaceServiceError("DRAFT_INVALID", f"镜 {shot_no} 的时长必须为正整数秒")


def _normalize_edit(field: str, value: Any) -> tuple[str, Any]:
    storage = _EDITABLE_FIELDS.get(str(field or "").strip())
    if storage is None:
        raise WorkspaceServiceError("INVALID_INPUT", "未知脚本编辑字段")
    if storage == "时长(秒)":
        if type(value) is not int or value <= 0 or value > 3600:
            raise WorkspaceServiceError("INVALID_INPUT", "镜头时长必须为 1 至 3600 秒的整数")
        return storage, value
    if storage in {"旁白", "字幕", "备注"} and isinstance(value, str) and len(value) <= 4000 and not value.strip():
        return storage, ""
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise WorkspaceServiceError("INVALID_INPUT", "脚本字段不能为空或超过长度限制")
    return storage, value.strip()


def _parse_instruction(value: str) -> tuple[str, str, Any]:
    text = str(value or "").strip()
    if not text or len(text) > 4000:
        raise WorkspaceServiceError("INVALID_INPUT", "自然语言修改不能为空或过长")
    match = _INSTRUCTION.fullmatch(text)
    if match is None:
        raise WorkspaceServiceError(
            "INSTRUCTION_AMBIGUOUS",
            "请使用“把第 N 镜画面/声音/字幕/旁白/时长/备注/结构修改为 …”的明确句式",
        )
    shot_no, label, raw = match.groups()
    field = _INSTRUCTION_FIELDS[label]
    if field == "duration_seconds":
        duration = re.fullmatch(r"(\d+)\s*(?:秒)?", raw.strip())
        if duration is None:
            raise WorkspaceServiceError("INVALID_INPUT", "自然语言时长必须是正整数秒")
        return shot_no, field, int(duration.group(1))
    return shot_no, field, raw.strip()


def _employee_shot(
    shot: dict,
    *,
    shot_id: str,
    assistant_locked: bool,
    image_reconfirmation_required: bool,
) -> dict:
    fields = dict(shot.get("fields") or {})
    sources = list(shot.get("source_refs") or [])
    if len(sources) != 1:
        raise WorkspaceServiceError("INTEGRITY_ERROR", "员工脚本 DTO 来源数量无效")
    source = sources[0]
    return {
        "shot_id": shot_id,
        "shot_no": str(shot.get("shot_no") or ""),
        "structure": str(fields.get("结构") or ""),
        "camera_type": str(fields.get("类型") or ""),
        "shot_size": str(fields.get("景别") or ""),
        "camera_move": str(fields.get("运镜") or ""),
        "duration_seconds": fields.get("时长(秒)"),
        "visual": str(fields.get("画面描述") or ""),
        "sound": str(fields.get("声音") or ""),
        "voiceover": str(fields.get("旁白") or ""),
        "subtitle": str(fields.get("字幕") or ""),
        "notes": str(fields.get("备注") or ""),
        "assistant_locked": assistant_locked,
        "image_reconfirmation_required": image_reconfirmation_required,
        "adaptation": ({"origin": "original_suggestion", "label": "原创拍摄建议",
                        "summary": str((shot.get("source_detail_override") or {}).get("改编说明") or "")}
                       if (shot.get("source_detail_override") or {}).get("原创改编依据") else None),
        "source": {
            "file": str(source.get("source_file") or ""),
            "sheet": str(source.get("source_sheet") or ""),
            "row": int(source.get("source_row") or 0),
        },
    }


def _employee_diff_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异包含不可展示的字段值")


def _renumber_shot_record(value: dict, number: int) -> dict:
    result = deepcopy(value)
    text = str(number)
    for key in ("镜号", "shot_no", "模板镜号"):
        if key in result:
            result[key] = text
    if "slot_index" in result:
        result["slot_index"] = number
    return result


def _project_frozen_shot_fields(payload: dict, origin_to_child: dict[str, str]) -> dict:
    """Project known output identities; source rows and event IDs are immutable."""
    def shot_records(values):
        numbered = []
        for raw in values or []:
            if not isinstance(raw, dict):
                raise WorkspaceServiceError("ARTIFACT_INVALID", "冻结镜头记录无效")
            slot = raw.get("slot")
            origin = _detail_shot_no(slot if isinstance(slot, dict) else raw)
            child = origin_to_child.get(origin)
            if child is None:
                continue
            item = _renumber_shot_record(raw, int(child))
            if isinstance(slot, dict):
                item["slot"] = _renumber_shot_record(slot, int(child))
            for key in ("field_decisions", "decisions"):
                if isinstance(item.get(key), list):
                    item[key] = [_renumber_shot_record(value, int(child)) for value in item[key]]
            numbered.append((int(child), item))
        return [item for _, item in sorted(numbered, key=lambda row: row[0])]

    result = {
        key: shot_records(payload.get(key))
        for key in ("decisions", "template_slots", "shot_plans", "field_decisions")
        if key in payload or key in {"decisions", "template_slots"}
    }
    for key, shot_field in (("coverage", "shot_nos"), ("traceability", "plan_shot_nos")):
        if key not in payload:
            continue
        result[key] = []
        for raw in payload.get(key) or []:
            item = deepcopy(raw)
            origins = [str(value) for value in item.get(shot_field) or []]
            item[shot_field] = sorted(
                [origin_to_child[value] for value in origins if value in origin_to_child],
                key=_shot_sort_key,
            )
            if "output_evidence" in item:
                item["output_evidence"] = shot_records(item["output_evidence"])
            if isinstance(item.get("template_source"), list) and len(item["template_source"]) == len(origins):
                source_pairs = [(int(origin_to_child[origin]), source)
                                for origin, source in zip(origins, item["template_source"])
                                if origin in origin_to_child]
                item["template_source"] = [source for _, source in sorted(source_pairs)]
            if origins and not item[shot_field]:
                status_key = "status" if key == "coverage" else "final_status"
                item[status_key] = "uncovered"
            result[key].append(item)
    return result


def _recover_frozen_decisions(
    plan_path: Path, plan: dict, draft_path: Path, binding_path: Path,
) -> dict:
    """Recover only from two agreeing artifacts already verified by the manifest."""
    drafted = _read_json(draft_path, "父 ScriptDraft")
    bound = _read_json(binding_path, "父 VisualBinding")
    rows = drafted.get("script_rows") or []
    decisions = drafted.get("decisions") or []
    slots = drafted.get("template_slots") or []
    valid = bool(rows) and len(decisions) == len(slots) == len(rows)
    for key in ("decisions", "template_slots", "script_rows"):
        valid = valid and drafted.get(key) == bound.get(key)
    for artifact in (drafted, bound):
        valid = valid and artifact.get("script_plan_ref") == str(plan_path.resolve())
    valid = valid and bound.get("script_draft_ref") == str(draft_path.resolve())
    if plan.get("template_slots"):
        valid = valid and plan["template_slots"] == slots
    details = drafted.get("detail_rows") or []
    valid = valid and len(details) == len(rows)
    if valid:
        for index, (row, detail, decision, slot) in enumerate(zip(rows, details, decisions, slots), 1):
            origin = str(index)
            valid = valid and _detail_shot_no(row) == _detail_shot_no(detail) == _detail_shot_no(slot) == origin
            valid = valid and decision.get("slot") == slot
            source = decision.get("variant") if decision.get("mode") in {"ADAPT", "REPLACE"} else slot
            if not isinstance(source, dict):
                valid = False
                break
            expected = {key: source.get(key) for key in ("source_file", "source_sheet", "source_row")}
            valid = valid and _source_ref(detail) == expected
    if not valid:
        raise WorkspaceServiceError("DRAFT_SOURCE_DRIFT", "冻结计划缺少匹配决定，且同版来源证据无法一致恢复")
    return {"decisions": deepcopy(decisions), "template_slots": deepcopy(slots)}


def _shot_sort_key(value: str) -> tuple[int, int | str]:
    normalized = str(value or "").strip()
    return (0, int(normalized)) if normalized.isdigit() else (1, normalized)
