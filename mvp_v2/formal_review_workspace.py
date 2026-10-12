"""Formal project-manager review workspace.

The employee boundary is deliberately business-only. It resolves the current
version or an explicitly selected ancestor, derives review items from bound artifacts,
and records decisions through the existing human-only command channel.  It has
no publication, CURRENT, Agent-tool, model, or caller-supplied evidence path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re
from typing import Any
from zipfile import is_zipfile

from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .formal_delivery_snapshot import FormalDeliverySnapshotService, FormalPdfDelivery
from .generation_stages import read_visual_binding
from .identity_service import require_permission
from .manual_review_commands import ManualReviewCommandService
from .project_service import ProjectService
from .revision_service import version_shots_from_artifacts
from .version_delivery import VersionDeliveryContext, VersionDeliveryService, resolve_version_shot_ids
from .version_quality import (
    AuthenticatedFinalConfirmationService,
    VersionQualityResolver,
)
from .version_quality_artifacts import FileVersionQualityArtifactStore
from .version_quality_factory import build_workspace_version_quality_resolver
from .version_review_repository import WorkspaceVersionReviewRepository
from .version_review_service import VersionReviewError, VersionReviewService
from .version_service import VersionService
from .workspace_store import WorkspaceServiceError, WorkspaceStore


_SEVERITY_ORDER = {"blocking": 0, "serious": 1, "attention": 2}
_STATUS_LABELS = {
    "open": "待处理",
    "approve": "已通过",
    "return": "已退回修改",
    "recheck": "重点复审",
}
_SEVERITY_LABELS = {
    "blocking": "阻断",
    "serious": "重点",
    "attention": "提醒",
}


@dataclass(frozen=True)
class FormalReviewWorkspaceServices:
    store: WorkspaceStore
    projects: ProjectService
    delivery: VersionDeliveryService
    artifacts: RunArtifactResolver
    commands: ManualReviewCommandService
    image_workspace: Any
    delivery_snapshots: FormalDeliverySnapshotService | None = None


@dataclass(frozen=True)
class FormalExcelDelivery:
    """Internal download handle; technical identity never enters employee DTOs."""

    path: Path
    filename: str


class WorkspaceReviewerActorResolver:
    """Derive the project-manager identity from the authenticated session."""

    def __init__(self, store: WorkspaceStore):
        self.store = store

    def resolve_reviewer(
        self, auth_token: str, *, workspace_id: str, project_id: str,
    ) -> str:
        with self.store.connection() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="review.record",
            )
        return str(actor["actor_id"])


class WorkspaceBoundVersionQuality:
    """Build the authoritative quality resolver for each current version read."""

    def __init__(
        self,
        *,
        store: WorkspaceStore,
        delivery: VersionDeliveryService,
        artifacts: RunArtifactResolver,
        data_root: str | Path,
    ) -> None:
        self.store = store
        self.delivery = delivery
        self.artifacts = artifacts
        self.evidence = FileVersionQualityArtifactStore(data_root)

    def resolver(
        self,
        auth_token: str,
        *,
        project_id: str,
        version_id: str,
        expected_project_lock_version: int,
    ) -> VersionQualityResolver:
        context = self.delivery.resolve(
            auth_token,
            project_id,
            version_id,
            expected_project_version=expected_project_lock_version,
        )
        repository = WorkspaceVersionReviewRepository(
            self.store,
            auth_token=auth_token,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            version_id=context.version_id,
            run_id=context.run_id,
            expected_project_lock_version=context.project_lock_version,
        )
        return build_workspace_version_quality_resolver(
            store=self.store,
            delivery_service=self.delivery,
            run_artifacts=self.artifacts,
            reviews=VersionReviewService(repository),
            evidence_artifacts=self.evidence,
        )

    def materialize_shot_snapshot(self, auth_token: str, **values):
        return self.resolver(auth_token, **values).materialize_shot_snapshot(
            auth_token, **values,
        )

    def resolve_authoritative_evidence(self, auth_token: str, **values):
        return self.resolver(auth_token, **values).resolve_authoritative_evidence(
            auth_token, **values,
        )


class WorkspaceBoundFinalConfirmation:
    """Bind the final command to the same current-version quality authority."""

    def __init__(
        self,
        *,
        quality: WorkspaceBoundVersionQuality,
        actor_resolver: WorkspaceReviewerActorResolver,
    ) -> None:
        self.quality = quality
        self.actor_resolver = actor_resolver

    def confirm(self, auth_token: str, **values):
        resolver = self.quality.resolver(
            auth_token,
            project_id=values["project_id"],
            version_id=values["version_id"],
            expected_project_lock_version=values["expected_project_lock_version"],
        )
        return AuthenticatedFinalConfirmationService(
            resolver=resolver,
            actor_resolver=self.actor_resolver,
        ).confirm(auth_token, **values)


class FormalReviewWorkspaceService:
    """Business review facade used by the unified Formal UI."""

    def __init__(self, services: FormalReviewWorkspaceServices):
        self.services = services
        self.services.store.initialize()
        self.delivery_snapshots = services.delivery_snapshots or FormalDeliverySnapshotService(
            services.store, services.artifacts,
        )

    def read_current(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int | None = None,
        version_id: str | None = None,
    ) -> dict:
        project, context = self._context(
            auth_token, workspace_id, project_id, expected_project_version, version_id=version_id,
        )
        if context is None:
            return self._empty("当前项目还没有可审核的脚本版本")
        image_model = self.services.image_workspace.read_current(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=context.project_lock_version,
            include_working_draft=False, version_id=version_id,
        )
        if image_model.get("state") != "confirmed":
            progress = None if version_id is not None else self.services.image_workspace.read_progress(
                auth_token, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=context.project_lock_version,
            )
            blocked = self._blocked(
                project,
                context,
                "请先完成每镜一张图片的检查、选择和确认，再进入项目经理审核。",
                image_model=image_model, changed=self._changed_shots(context), image_progress=progress,
            )
            return {**blocked, "read_only": version_id is not None,
                    "view_scope": "frozen_version" if version_id is not None else "current_version"}
        machine = self._machine_report(context)
        if machine.get("machine_audit_status") != "pass":
            blocked = self._blocked(
                project,
                context,
                "当前版本机器检查未通过。请先修复脚本或图片问题，再提交项目经理审核。",
                image_model=image_model, changed=self._changed_shots(context),
                machine=machine,
            )
            return {**blocked, "read_only": version_id is not None,
                    "view_scope": "frozen_version" if version_id is not None else "current_version"}
        try:
            models = self.services.commands.read_model(
                auth_token,
                project_id=project_id,
                version_id=context.version_id,
                expected_project_lock_version=context.project_lock_version,
                ancestor_of=str(project["current_version_id"]) if version_id is not None else None,
            )
        except WorkspaceServiceError as exc:
            if exc.code != "NOT_FOUND":
                raise
            models = {
                "issues": [], "decisions": [], "evidence": [],
                "final_confirmation": None,
            }
        changed = self._changed_shots(context)
        persisted = list(models.get("issues") or [])
        if persisted:
            issues = self._employee_issues(persisted, image_model, changed)
        else:
            issues = self._preview_issues(image_model, changed)
        counts = self._counts(issues)
        final = models.get("final_confirmation")
        if final and final.get("decision") == "quality_pass":
            state, label, copy = (
                "confirmed",
                "整片确认完成",
                "当前版本已由项目经理完成整片质量确认，可进入交付准备。",
            )
        elif counts["returned"]:
            state, label, copy = (
                "needs_change",
                "需修改",
                "存在已退回镜头。请在脚本或图片区生成新版本后重新审核。",
            )
        elif persisted and not counts["open"]:
            state, label, copy = (
                "ready_final",
                "可通过",
                "全部问题镜头和变化镜头已处理，请执行一次整片最终确认。",
            )
        else:
            state, label, copy = (
                "reviewing" if persisted else "ready",
                "审核中" if persisted else "需审核",
                "先核对问题和本轮修改，其余镜头可一起检查。",
            )
        decision_counts = self._decision_counts(context)
        model = {
            "state": state,
            "read_only": version_id is not None,
            "view_scope": "frozen_version" if version_id is not None else "current_version",
            "decision": {
                "label": label,
                "copy": copy,
                "problem_count": counts["open"] + counts["returned"],
                "blocking_count": counts["blocking"],
                "serious_count": counts["serious"],
                "attention_count": counts["attention"],
                "approved_count": counts["approved"],
            },
            "project_name": str(project.get("name") or "当前项目"),
            "version_label": str(image_model.get("version_label") or "当前版本"),
            "project_version": context.project_lock_version,
            "base_version_id": context.version_id,
            "review_started": bool(persisted),
            "ready_for_final": state == "ready_final",
            "issues": issues,
            "evidence": self._employee_evidence(list(models.get("evidence") or [])),
            "traceability": self._traceability(context),
            "decision_counts": decision_counts,
            "final_confirmation": final,
            "next_action": (
                "进入交付准备"
                if state == "confirmed"
                else "前往脚本或图片区修改并保存新版本"
                if state == "needs_change"
                else "完成整片确认"
                if state == "ready_final"
                else "登记当前版本审核项"
                if not persisted
                else "处理所有待审核项"
            ),
            "production_certification": "BLOCK",
        }
        if state == "needs_change":
            model["edit_actions"] = [
                {
                    "label": "前往脚本与版本",
                    "href": f"/projects/{project_id}/script",
                },
                {
                    "label": "前往逐镜配图",
                    "href": f"/projects/{project_id}/shot-images",
                },
            ]
        return model

    def start_review(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        idempotency_key: str,
    ) -> dict:
        _, context = self._context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可审核版本")
        current = self.read_current(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=expected_project_version,
        )
        if current["state"] == "blocked":
            raise WorkspaceServiceError("REVIEW_PREREQUISITE_BLOCKED", current["message"])
        if current.get("review_started"):
            if not current.get("final_confirmation"):
                self._inherit_unchanged_image_reviews(auth_token, context)
            return self.read_current(auth_token, workspace_id=workspace_id, project_id=project_id)
        shots = self._subject_shots(context)
        changed = self._changed_shots(context)
        items = [
            {
                "kind": "image",
                "shot_no": shot["shot_no"],
                "severity": "attention",
                "note": f"镜头 {shot['shot_no']} 的清晰图片、来源和匹配关系等待项目经理复核。",
            }
            for shot in shots
        ]
        items.extend(
            {
                "kind": "change",
                "shot_no": shot_no,
                "severity": "serious",
                "note": f"镜头 {shot_no} 已发生变化，等待项目经理核对修改范围和意外变化。",
            }
            for shot_no in sorted(changed, key=_shot_sort_key)
        )
        self._command(
            auth_token,
            action="create_review_items",
            context=context,
            idempotency_key=idempotency_key,
            shots=shots,
            items=items,
        )
        self._inherit_unchanged_image_reviews(auth_token, context)
        return self.read_current(
            auth_token, workspace_id=workspace_id, project_id=project_id,
        )

    def decide(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        issue_keys: tuple[str, ...],
        decision: str,
        note: str,
        idempotency_key: str,
    ) -> dict:
        _, context = self._context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可审核版本")
        if decision not in {"approve", "return"}:
            raise WorkspaceServiceError("INVALID_INPUT", "审核决定只能是通过或退回修改")
        if not isinstance(note, str) or len(note) > 4000:
            raise WorkspaceServiceError("INVALID_INPUT", "审核意见格式不正确")
        note = note.strip()
        if decision == "return" and not note:
            raise WorkspaceServiceError("INVALID_INPUT", "请填写需要修改的内容")
        keys = tuple(str(value or "").strip() for value in issue_keys)
        if not keys or any(not value.startswith("review_") for value in keys):
            raise WorkspaceServiceError("INVALID_INPUT", "请选择当前版本的审核项")
        if len(set(keys)) != len(keys):
            raise WorkspaceServiceError("INVALID_INPUT", "审核项不能重复选择")
        models = self.services.commands.read_model(
            auth_token,
            project_id=project_id,
            version_id=context.version_id,
            expected_project_lock_version=context.project_lock_version,
        )
        indexed = {item["issue_key"]: item for item in models.get("issues") or []}
        if any(value not in indexed for value in keys):
            raise WorkspaceServiceError("NOT_FOUND", "审核项不属于当前版本")
        if decision == "approve" and any(indexed[value]["status"] == "return" for value in keys):
            raise WorkspaceServiceError(
                "REVIEW_NEW_VERSION_REQUIRED",
                "已退回的问题必须在脚本或图片区生成新版本后重新审核。",
            )
        self._command(
            auth_token,
            action="decide_review_items",
            context=context,
            idempotency_key=idempotency_key,
            item_keys=list(keys),
            decision=decision,
            note=note or f"已明确通过所选 {len(keys)} 项审核。",
        )
        return self.read_current(
            auth_token, workspace_id=workspace_id, project_id=project_id,
        )

    def confirm_final(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        note: str,
        idempotency_key: str,
    ) -> dict:
        if not isinstance(note, str) or len(note) > 4000:
            raise WorkspaceServiceError("INVALID_INPUT", "审核意见格式不正确")
        _, context = self._context(auth_token, workspace_id, project_id, None)
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可审核版本")
        current = self.read_current(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
        )
        if current.get("state") == "confirmed":
            self._ensure_version_review_projection(auth_token, context)
            return self.read_current(
                auth_token, workspace_id=workspace_id, project_id=project_id,
            )
        _, context = self._context(
            auth_token, workspace_id, project_id, expected_project_version,
        )
        if context is None:
            raise WorkspaceServiceError("NOT_FOUND", "当前项目还没有可审核版本")
        if not current.get("ready_for_final"):
            raise WorkspaceServiceError(
                "FINAL_REVIEW_NOT_READY",
                "仍有未处理或已退回的镜头，不能完成整片确认。",
            )
        self._command(
            auth_token,
            action="confirm_final",
            context=context,
            idempotency_key=idempotency_key,
            decision="quality_pass",
            note=note.strip() or "已明确完成当前版本的整片确认。",
        )
        self._ensure_version_review_projection(auth_token, context)
        return self.read_current(
            auth_token, workspace_id=workspace_id, project_id=project_id,
        )

    def _ensure_version_review_projection(
        self, auth_token: str, context: VersionDeliveryContext,
    ) -> dict:
        with self.services.store.connection() as connection:
            final = connection.execute(
                """
                SELECT decision, confirmation_ref FROM version_quality_final_confirmations
                WHERE workspace_id = ? AND project_id = ? AND version_id = ? AND run_id = ?
                """,
                (
                    context.workspace_id,
                    context.project_id,
                    context.version_id,
                    context.run_id,
                ),
            ).fetchone()
        if final is None:
            raise WorkspaceServiceError(
                "FINAL_CONFIRMATION_MISSING",
                "整片确认完成后未找到权威审核记录",
            )
        return VersionService(self.services.store).record_review(
            auth_token,
            version_id=context.version_id,
            decision=str(final["decision"]),
            evidence_ref=str(final["confirmation_ref"]),
            expected_project_version=context.project_lock_version,
        )

    def prepare_excel_delivery(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        version_id: str | None = None,
    ) -> FormalExcelDelivery:
        project, context = self._context(
            auth_token, workspace_id, project_id, expected_project_version, version_id=version_id,
        )
        if context is None:
            raise WorkspaceServiceError("DELIVERY_NOT_READY", "当前项目还没有可交付版本")
        review = self.read_current(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=expected_project_version,
            version_id=version_id,
        )
        if review.get("state") != "confirmed":
            raise WorkspaceServiceError(
                "DELIVERY_NOT_READY", "完成整片人工确认后才能导出 Excel",
            )
        try:
            workbook = self.services.artifacts.resolve_artifact(context.run_id, "workbook")
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError(exc.code, exc.message) from exc
        if (
            workbook.ref != context.version_artifact.ref
            or workbook.sha256 != context.version_artifact.sha256
            or workbook.path.suffix.casefold() != ".xlsx"
            or not is_zipfile(workbook.path)
        ):
            raise WorkspaceServiceError(
                "DELIVERY_ARTIFACT_DRIFT", "当前版本 Excel 与已确认版本不一致",
            )
        verified = self.services.delivery.resolve(
            auth_token,
            project_id,
            context.version_id,
            expected_project_version=expected_project_version,
            ancestor_of=str(project["current_version_id"]) if version_id is not None else None,
        )
        if verified.run_id != context.run_id or verified.version_artifact != context.version_artifact:
            raise WorkspaceServiceError(
                "DELIVERY_ARTIFACT_DRIFT", "项目在导出校验期间已变化",
            )
        review_model = self.services.commands.read_model(
            auth_token,
            project_id=project_id,
            version_id=context.version_id,
            expected_project_lock_version=context.project_lock_version,
            ancestor_of=str(project["current_version_id"]) if version_id is not None else None,
        )
        snapshot = self.delivery_snapshots.ensure_excel_snapshot(
            auth_token,
            context=verified,
            workbook=workbook,
            review_model=review_model,
            ancestor_of=str(project["current_version_id"]) if version_id is not None else None,
        )
        version_suffix = f"-{_safe_delivery_name(review['version_label'])}" if version_id is not None else ""
        return FormalExcelDelivery(
            path=snapshot.excel_path,
            filename=f"{_safe_delivery_name(str(project.get('name') or 'Loomtake项目'))}{version_suffix}-脚本交付.xlsx",
        )

    def prepare_pdf_delivery(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        expected_project_version: int,
        version_id: str | None = None,
    ) -> FormalPdfDelivery:
        project, context = self._context(
            auth_token, workspace_id, project_id, expected_project_version, version_id=version_id,
        )
        if context is None:
            raise WorkspaceServiceError("DELIVERY_NOT_READY", "当前项目还没有可交付版本")
        review = self.read_current(
            auth_token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=expected_project_version,
            version_id=version_id,
        )
        if review.get("state") != "confirmed":
            raise WorkspaceServiceError("DELIVERY_NOT_READY", "完成整片人工确认后才能导出 PDF")
        try:
            workbook = self.services.artifacts.resolve_artifact(context.run_id, "workbook")
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError(exc.code, exc.message) from exc
        if workbook.ref != context.version_artifact.ref or workbook.sha256 != context.version_artifact.sha256:
            raise WorkspaceServiceError("DELIVERY_ARTIFACT_DRIFT", "PDF 来源 Excel 与确认版本不一致")
        review_model = self.services.commands.read_model(
            auth_token,
            project_id=project_id,
            version_id=context.version_id,
            expected_project_lock_version=context.project_lock_version,
            ancestor_of=str(project["current_version_id"]) if version_id is not None else None,
        )
        snapshot = self.delivery_snapshots.ensure_excel_snapshot(
            auth_token,
            context=context,
            workbook=workbook,
            review_model=review_model,
            ancestor_of=str(project["current_version_id"]) if version_id is not None else None,
        )
        version_suffix = f"-{_safe_delivery_name(review['version_label'])}" if version_id is not None else ""
        return self.delivery_snapshots.prepare_optional_pdf(
            auth_token,
            context=context,
            snapshot=snapshot,
            filename=f"{_safe_delivery_name(str(project.get('name') or 'Loomtake项目'))}{version_suffix}-脚本交付.pdf",
        )

    def _context(
        self,
        auth_token: str,
        workspace_id: str,
        project_id: str,
        expected_project_version: int | None,
        *, version_id: str | None = None,
    ) -> tuple[dict, VersionDeliveryContext | None]:
        project = self.services.projects.get_project(auth_token, project_id)
        if project["workspace_id"] != workspace_id:
            raise WorkspaceServiceError("CROSS_PROJECT_DENIED", "project 不属于当前 workspace")
        if expected_project_version is not None and int(project["project_version"]) != expected_project_version:
            raise WorkspaceServiceError("PROJECT_VERSION_CONFLICT", "project lock version 不匹配")
        anchor = str(project.get("current_version_id") or "")
        version_id = version_id if version_id is not None else anchor
        if not version_id:
            return project, None
        context = self.services.delivery.resolve(
            auth_token,
            project_id,
            version_id,
            expected_project_version=project["project_version"], ancestor_of=anchor,
        )
        return project, context

    def _repository(
        self, auth_token: str, context: VersionDeliveryContext,
    ) -> WorkspaceVersionReviewRepository:
        return WorkspaceVersionReviewRepository(
            self.services.store,
            auth_token=auth_token,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            version_id=context.version_id,
            run_id=context.run_id,
            expected_project_lock_version=context.project_lock_version,
        )

    def _command(
        self,
        auth_token: str,
        *,
        action: str,
        context: VersionDeliveryContext,
        idempotency_key: str,
        **values,
    ) -> dict:
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
            )["result"]
        except VersionReviewError as exc:
            raise WorkspaceServiceError("REVIEW_INVALID", str(exc)) from exc

    def _subject_shots(self, context: VersionDeliveryContext) -> list[dict]:
        resolved = self.services.artifacts.resolve_stage_artifact(
            context.run_id,
            "visual_binding",
            expected_manifest_sha256=context.manifest.sha256,
        )
        binding = read_visual_binding(resolved.path)
        try:
            shots = version_shots_from_artifacts(
                binding.script_rows,
                binding.detail_rows,
                bindings=binding.bindings,
            )
        except ValueError as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", str(exc)) from exc
        if not shots or any(not item.get("source_refs") for item in shots):
            raise WorkspaceServiceError("ARTIFACT_INVALID", "当前版本镜头或来源链不完整")
        return shots

    def _inherit_unchanged_image_reviews(self, auth_token: str, context: VersionDeliveryContext) -> None:
        """Associate lawful image approvals; change items and final confirmation stay new."""
        if not context.parent_version_id:
            return
        repository = self._repository(auth_token, context)
        reviewed = {item.review_item_id for item in repository.decisions(context.version_id)}
        pending = {str(item.shot_no): item for item in repository.review_items(context.version_id)
                   if item.kind == "image" and item.review_item_id not in reviewed}
        if not pending:
            return
        identities = resolve_version_shot_ids(self.services.artifacts, context)
        targets = {identities[no]: item for no, item in pending.items()}
        eligible = set(targets)
        labels = {item["version_id"]: f"版本 {number}" for number, item in enumerate(
            self.services.projects.list_project_versions(auth_token, context.project_id), start=1)}

        def subjects(bound):
            ids = resolve_version_shot_ids(self.services.artifacts, bound)
            shots = self._subject_shots(bound)
            images, _, _ = self.services.image_workspace._effective_snapshot(bound, [str(item["shot_no"]) for item in shots])
            return {ids[str(item["shot_no"])]: (item, images.get(str(item["shot_no"]), {})) for item in shots}, ids

        child_subjects, _ = subjects(context)
        parent_id = context.parent_version_id
        candidates = []
        while parent_id and eligible:
            parent = self.services.delivery.resolve(auth_token, context.project_id, parent_id,
                expected_project_version=context.project_lock_version, ancestor_of=context.version_id)
            parent_subjects, parent_ids = subjects(parent)
            eligible = {key for key in eligible if key in parent_subjects and key in child_subjects
                and VersionReviewService.same_image_review_subject(child_subjects[key][0], parent_subjects[key][0],
                    child_subjects[key][1], parent_subjects[key][1])}
            reviews_by_id = {}
            for raw in repository.ancestor_image_reviews(parent_id):
                key = parent_ids.get(str(raw["shot_no"]))
                reviews_by_id.setdefault(key, []).append(raw)
            for key in tuple(eligible):
                prior = reviews_by_id.get(key)
                if not prior:
                    continue
                # An unresolved/returned item in the nearest reviewed ancestor
                # must never be skipped in favour of an older approval.
                eligible.remove(key)
                if len(prior) != 1 or prior[0].get("decision") != "approve":
                    continue
                source = {**prior[0], "workspace_id": context.workspace_id, "project_id": context.project_id,
                    "version_label": labels[parent_id]}
                candidates.append({"review_item_id": targets[key].review_item_id, "source": source})
            child_subjects = parent_subjects
            parent_id = parent.parent_version_id
        VersionReviewService(repository).inherit_image_reviews(candidates,
            evidence_authority=self.services.commands.services.evidence_authority)

    def _traceability(self, context: VersionDeliveryContext) -> list[dict]:
        resolved = self.services.artifacts.resolve_stage_artifact(
            context.run_id,
            "visual_binding",
            expected_manifest_sha256=context.manifest.sha256,
        )
        shot_ids = resolve_version_shot_ids(self.services.artifacts, context)
        rows = []
        for raw in read_visual_binding(resolved.path).traceability:
            item = dict(raw)
            item["template_source"] = [
                {
                    "file": Path(str(source.get("source_file") or source.get("file") or "")).name,
                    "sheet": source.get("source_sheet") or source.get("sheet"),
                    "row": source.get("source_row") or source.get("row"),
                    "structure_source": {
                        "file": Path(str(
                            source.get("structure_source_file")
                            or source.get("source_file") or source.get("file") or ""
                        )).name,
                        "sheet": source.get("structure_source_sheet")
                        or source.get("source_sheet") or source.get("sheet"),
                        "row": source.get("structure_source_row")
                        or source.get("source_row") or source.get("row"),
                    },
                }
                for source in item.get("template_source") or []
            ]
            item["output_evidence"] = [
                {**evidence, "content_evidence": {
                    **{key: value for key, value in evidence["content_evidence"].items()
                       if key != "script_sha256"},
                    "shot_id": shot_ids.get(str(evidence.get("shot_no") or ""), ""),
                    "requirement_id": str(item.get("requirement_id") or ""),
                }} if isinstance(evidence.get("content_evidence"), dict) else dict(evidence)
                for evidence in item.get("output_evidence") or []
            ]
            rows.append(item)
        return rows

    def _decision_counts(self, context: VersionDeliveryContext) -> dict:
        resolved = self.services.artifacts.resolve_stage_artifact(
            context.run_id,
            "visual_binding",
            expected_manifest_sha256=context.manifest.sha256,
        )
        stored = read_visual_binding(resolved.path).decision_counts
        return {
            key: int(stored.get(key) or 0)
            for key in (
                "requirements_uncovered",
                "hard_template_downgrades",
                "unresolved_conflicts",
                "unexplained_overrides",
            )
        }

    def _machine_report(self, context: VersionDeliveryContext) -> dict:
        try:
            resolved = self.services.artifacts.resolve_artifact(context.run_id, "run_report")
            value = json.loads(resolved.path.read_text(encoding="utf-8"))
        except (ArtifactResolutionError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "当前版本机器检查报告不可读取") from exc
        if not isinstance(value, dict) or value.get("run_id") != context.run_id:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "机器检查报告不属于当前版本")
        return value

    def _changed_shots(self, context: VersionDeliveryContext) -> dict[str, tuple[str, ...]]:
        try:
            resolved = self.services.artifacts.resolve_artifact(context.run_id, "revision_report")
            report = json.loads(resolved.path.read_text(encoding="utf-8"))
        except ArtifactResolutionError:
            return {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "当前版本差异报告不可读取") from exc
        if not isinstance(report, dict) or report.get("child_version_id") != context.version_id:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "版本差异报告不属于当前版本")
        result: dict[str, tuple[str, ...]] = {}
        shot_diffs = (report.get("version_diff") or {}).get("shot_diffs") or []
        for shot in shot_diffs:
            shot_no = str(shot.get("child_shot_no") or "").strip()
            changed = tuple(
                _business_field(str(item.get("field") or ""))
                for item in shot.get("field_diffs") or []
                if item.get("status") != "unchanged"
            )
            if shot_no and changed:
                result[shot_no] = tuple(dict.fromkeys(changed))
        # Structural edits change opening/closing duties and neighboring shots
        # even when the surviving shot's own fields remain identical.
        parent_positions = {
            str(number): str(identity)
            for shot in shot_diffs
            for number, identity in zip(shot.get("parent_shot_nos") or [], shot.get("parent_shot_ids") or [])
        }
        child_positions = {
            str(shot["child_shot_no"]): str(shot["child_shot_id"])
            for shot in shot_diffs
            if shot.get("child_shot_no") and shot.get("child_shot_id")
        }
        parent_order = [parent_positions[number] for number in sorted(parent_positions, key=_shot_sort_key)]
        child_numbers = sorted(child_positions, key=_shot_sort_key)
        child_order = [child_positions[number] for number in child_numbers]
        if parent_order != child_order:
            for index, number in enumerate(child_numbers):
                identity = child_positions[number]
                if identity not in parent_order:
                    continue
                parent_index = parent_order.index(identity)
                fields = list(result.get(number, ()))
                if index == 0 and parent_index != 0:
                    fields.append("首镜开场")
                if index == len(child_order) - 1 and parent_index != len(parent_order) - 1:
                    fields.append("末镜收束")
                before = child_order[index - 1] if index else None
                after = child_order[index + 1] if index + 1 < len(child_order) else None
                old_before = parent_order[parent_index - 1] if parent_index else None
                old_after = parent_order[parent_index + 1] if parent_index + 1 < len(parent_order) else None
                if (before, after) != (old_before, old_after):
                    fields.append("镜头承接")
                if fields:
                    result[number] = tuple(dict.fromkeys(fields))
        return result

    def _preview_issues(self, image_model: dict, changed: dict[str, tuple[str, ...]]) -> list[dict]:
        issues = []
        for shot in image_model.get("shots") or []:
            issues.append(self._issue_view(
                issue_key="",
                kind="image",
                shot_no=str(shot["shot_no"]),
                severity="attention",
                status="open",
                shot=shot,
                changed=changed,
                preview=True,
            ))
        for shot_no in sorted(changed, key=_shot_sort_key):
            shot = next(
                (item for item in image_model.get("shots") or [] if str(item["shot_no"]) == shot_no),
                {"shot_no": shot_no, "visual": "当前镜头", "candidates": []},
            )
            issues.append(self._issue_view(
                issue_key="",
                kind="change",
                shot_no=shot_no,
                severity="serious",
                status="open",
                shot=shot,
                changed=changed,
                preview=True,
                adjacent_edges=self._adjacent_edges(image_model, shot_no),
            ))
        return sorted(issues, key=_issue_sort_key)

    def _employee_issues(
        self,
        items: list[dict],
        image_model: dict,
        changed: dict[str, tuple[str, ...]],
    ) -> list[dict]:
        shots = {str(item["shot_no"]): item for item in image_model.get("shots") or []}
        result = []
        for item in items:
            shot_no = str(item.get("shot_no") or "")
            shot = shots.get(shot_no, {"shot_no": shot_no, "visual": "整片", "candidates": []})
            result.append(self._issue_view(
                issue_key=str(item["issue_key"]),
                kind=str(item["kind"]),
                shot_no=shot_no,
                severity=str(item["severity"]),
                status=str(item["status"]),
                shot=shot,
                changed=changed,
                preview=False,
                adjacent_edges=self._adjacent_edges(image_model, shot_no),
            ))
            if item.get("inherited_review"):
                result[-1]["inherited_review"] = dict(item["inherited_review"])
        return sorted(result, key=_issue_sort_key)

    @staticmethod
    def _adjacent_edges(image_model: dict, shot_no: str) -> tuple[str, ...]:
        numbers = [str(shot["shot_no"]) for shot in image_model.get("shots") or []]
        return tuple(f"{before}->{after}" for before, after in zip(numbers, numbers[1:])
                     if shot_no in {before, after})

    @staticmethod
    def _issue_view(
        *,
        issue_key: str,
        kind: str,
        shot_no: str,
        severity: str,
        status: str,
        shot: dict,
        changed: dict[str, tuple[str, ...]],
        preview: bool,
        adjacent_edges: tuple[str, ...] = (),
    ) -> dict:
        selected = str(shot.get("selected_image_key") or "")
        candidate = next(
            (item for item in shot.get("candidates") or [] if item.get("image_key") == selected),
            {},
        )
        sources = list(candidate.get("sources") or [])
        source = sources[0] if sources else {}
        fields = changed.get(shot_no, ())
        affected_edges = adjacent_edges if kind == "change" and fields else ()
        change_summary = f"已变化：{'、'.join(fields)}" if fields else "本镜图片、脚本内容和来源待人工确认"
        if affected_edges:
            dimension = "声音承接" if "声音" in fields else "镜头承接"
            change_summary += f"；请复核相邻{dimension}：" + "、".join(edge.replace("->", "→") for edge in affected_edges)
        title = (
            "复核本镜已发生的变化"
            if kind == "change"
            else "检查本镜图片、内容与来源"
        )
        return {
            "issue_key": issue_key,
            "kind": kind,
            "title": title,
            "shot_no": shot_no,
            "shot_id": str(shot.get("shot_id") or ""),
            "severity": severity,
            "severity_label": _SEVERITY_LABELS.get(severity, "提醒"),
            "status": status,
            "status_label": _STATUS_LABELS.get(status, "待处理"),
            "focus_required": (
                status != "approve"
                and (kind == "change" or status in {"return", "recheck"}
                     or severity in {"blocking", "serious"})
            ),
            "visual": str(shot.get("visual") or "当前镜头"),
            "image_key": selected,
            "image_caption": str(candidate.get("caption") or ("当前参考图片" if preview else "当前已确认图片")),
            "source": {
                key: source[key]
                for key in ("label", "page", "sheet", "row", "slide", "image")
                if key in source
            },
            "change_summary": change_summary,
            "affected_adjacent_edges": list(affected_edges),
            "evidence_status": "available",
            "preview": preview,
            "read_only": preview,
        }

    @staticmethod
    def _counts(issues: list[dict]) -> dict[str, int]:
        return {
            "open": sum(item["status"] in {"open", "recheck"} for item in issues),
            "returned": sum(item["status"] == "return" for item in issues),
            "approved": sum(item["status"] == "approve" for item in issues),
            "blocking": sum(item["severity"] == "blocking" and item["status"] != "approve" for item in issues),
            "serious": sum(item["severity"] == "serious" and item["status"] != "approve" for item in issues),
            "attention": sum(item["severity"] == "attention" and item["status"] != "approve" for item in issues),
        }

    @staticmethod
    def _employee_evidence(items: list[dict]) -> list[dict]:
        labels = {
            "review-item": "当前版本审核项已由服务端登记",
            "human:approve": "人工通过意见已记录",
            "human:return": "人工退回意见已记录",
            "human:recheck": "重点复审意见已记录",
        }
        result = []
        for item in items:
            raw = str(item.get("summary") or "")
            prefix = "review-item" if raw.startswith("review-item:") else raw
            result.append({
                "summary": labels.get(prefix, "人工审核业务记录已保存"),
                "created_at": str(item.get("created_at") or ""),
                "evidence_status": "available" if item.get("evidence_status") == "available" else "missing",
            })
        return result

    @staticmethod
    def _empty(message: str) -> dict:
        return {
            "state": "empty",
            "message": message,
            "issues": [],
            "production_certification": "BLOCK",
        }

    def _blocked(
        self, project: dict, context: VersionDeliveryContext, message: str,
        *, image_model: dict, changed: dict[str, tuple[str, ...]], machine: dict | None = None,
        image_progress: dict | None = None,
    ) -> dict:
        issues = self._preview_issues(image_model, changed)
        for issue in issues:
            panel = "images" if issue["kind"] == "image" else "script"
            issue["repair_action"] = {
                "label": "处理本镜配图" if panel == "images" else "修改本镜",
                "panel": panel, "shot_id": issue["shot_id"], "shot_no": issue["shot_no"],
                "href": f"/projects/{context.project_id}/script?panel={panel}&shot_id={issue['shot_id']}",
            }
        if machine is not None:
            # run_report publishes blocking item names; audit paths and raw
            # report objects never cross the employee boundary.
            labels = [str(item) for item in machine.get("blocking_items") or [] if isinstance(item, str)]
            for label in labels or ["当前脚本或配图机器检查尚未通过"]:
                label = re.sub(r"(?:artifact://|file://|/Users/|/Applications/|/tmp/|[A-Za-z]:\\)[^\s，。；]+", "相关文件", label)[:300]
                issues.append({
                    "issue_key": "", "kind": "machine", "title": "处理机器检查问题",
                    "shot_id": "", "shot_no": "", "severity": "blocking", "status": "open",
                    "preview": True, "read_only": True, "focus_required": True,
                    "change_summary": label,
                    "repair_action": {"label": "查看脚本检查", "panel": "script", "shot_id": "",
                                      "href": f"/projects/{context.project_id}/script?panel=script"},
                })
        progress_by_shot = {item["shot_id"]: item for item in (image_progress or {}).get("selections") or [] if item.get("selection_active", True)}
        projection = {item['shot_id']: item for item in (image_progress or image_model.get('progress') or {}).get('shots', [])}
        prerequisites = []
        for shot in image_model.get("shots") or []:
            progress = progress_by_shot.get(shot.get("shot_id")) or {}
            projected = projection.get(shot.get('shot_id')) or {}
            needs_image = (not shot.get("confirmed") or not shot.get("selected_image_key")
                           or shot.get("decision_kind") == "temporary_text_basis")
            if projected.get("status") == "saved" or not projected and not needs_image and not shot.get("image_reconfirmation_required") and not progress:
                continue
            if progress.get("image_key") or progress.get("library_candidate_key"):
                kind = "unsaved_images" if projected.get("status") == "confirmed" else "image_recheck"
                reason = "本镜选图进度已保存，尚未保存到新版本。" if kind == "unsaved_images" else "请核对本镜已保存的暂选图片。"
            elif shot.get("image_reconfirmation_required"):
                kind, reason = "image_recheck", "镜头内容已变化，请重新核对配图。"
            else:
                kind, reason = "missing_images", "本镜尚未完成配图。"
            prerequisites.append({
                "kind": kind, "shot_id": str(shot.get("shot_id") or ""), "shot_no": str(shot["shot_no"]),
                "label": f"处理第 {shot['shot_no']} 镜配图", "reason": reason,
                "href": f"/projects/{context.project_id}/script?panel=images&shot_id={shot.get('shot_id', '')}",
            })
        prerequisites.extend({"kind": "machine", "label": issue["repair_action"]["label"], "reason": issue["change_summary"],
                              "href": issue["repair_action"]["href"]}
                             for issue in issues if issue["kind"] == "machine")
        if not prerequisites:
            prerequisites.append({"kind": "version_save", "label": "保存新版本", "reason": "已选图片需要保存为当前配图版本后进入审核。",
                                  "href": f"/projects/{context.project_id}/script?panel=images"})
        groups = []
        for kind, title, action in (
            ("machine", "项机器检查问题待处理", "查看检查问题"),
            ("missing_images", "镜待完成配图", "继续选图"),
            ("image_recheck", "镜配图待核对", "继续核对配图"),
            ("unsaved_images", "镜选图进度已保存，待保存新版本", "保存新版本"),
            ("version_save", "项版本保存待完成", "保存新版本"),
        ):
            items = [item for item in prerequisites if item["kind"] == kind]
            if items:
                groups.append({"kind": kind, "count": len(items), "label": f"{len(items)} {title}",
                               "action_label": action, "href": items[0]["href"], "items": items})
        actual_shots = {item.get("shot_id") for item in prerequisites if item.get("shot_id")}
        for issue in issues:
            issue["is_prerequisite"] = issue["kind"] == "machine" or (
                issue["kind"] == "image" and issue.get("shot_id") in actual_shots)
        blocking_count = sum(item["kind"] == "machine" for item in prerequisites)
        return {
            "state": "blocked",
            "prerequisites": prerequisites,
            "prerequisite_groups": groups,
            "image_progress_saved_at": (image_progress or {}).get("saved_at", ""),
            "decision": {
                "label": "前置未完成",
                "copy": message,
                "problem_count": len(prerequisites),
                "blocking_count": blocking_count,
                "serious_count": 0,
                "attention_count": len(prerequisites) - blocking_count,
                "approved_count": 0,
            },
            "project_name": str(project.get("name") or "当前项目"),
            "version_label": str(image_model.get("version_label") or "当前版本"),
            "project_version": context.project_lock_version,
            "base_version_id": context.version_id,
            "message": message,
            "issues": issues,
            "review_started": False,
            "ready_for_final": False,
            "read_only": True,
            "production_certification": "BLOCK",
        }


def _business_field(value: str) -> str:
    return {
        "image_id": "图片选择",
        "decision": "图片决定",
        "resolution_status": "处理状态",
        "basis": "判断依据",
        "visual": "画面",
        "画面描述": "画面",
        "sound": "声音",
        "声音": "声音",
        "voiceover": "旁白",
        "旁白": "旁白",
        "subtitle": "字幕",
        "字幕": "字幕",
        "notes": "备注",
        "备注": "备注",
        "structure": "结构", "结构": "结构",
        "camera_type": "机位", "类型": "机位",
        "shot_size": "景别", "景别": "景别",
        "camera_move": "运镜", "运镜": "运镜",
        "duration_seconds": "时长", "时长(秒)": "时长",
    }.get(value, "镜头内容")


def _safe_delivery_name(value: str) -> str:
    name = re.sub(r"[\\/:*?\"<>|\x00-\x1f\x7f]+", "-", value).strip(" .-")
    return (name or "Loomtake项目")[:80]


def _shot_sort_key(value: str) -> tuple[int, str]:
    text = str(value)
    digits = "".join(character for character in text if character.isdigit())
    return (int(digits) if digits else 10**9, text)


def _issue_sort_key(item: dict) -> tuple[int, tuple[int, str], int]:
    return (
        _SEVERITY_ORDER.get(str(item.get("severity")), 3),
        _shot_sort_key(str(item.get("shot_no") or "")),
        0 if item.get("kind") == "change" else 1,
    )
