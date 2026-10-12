"""Immutable Formal delivery snapshot and reverse HERMES verification."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any

from .artifact_service import ArtifactResolutionError, ResolvedArtifact, RunArtifactResolver
from .excel_io import write_reviewed_delivery_workbook
from .identity_service import require_permission
from .run_contract import sha256_file, sha256_json
from .version_delivery import VersionDeliveryContext, require_version_ancestor
from .v10_image_delivery import V10ImageDeliveryError, create_formal_snapshot_pdf
from .workspace_store import (
    WorkspaceServiceError,
    WorkspaceStore,
    new_id,
    row_dict,
    utc_now,
)


@dataclass(frozen=True)
class FormalDeliverySnapshot:
    delivery_snapshot_id: str
    project_id: str
    version_id: str
    run_id: str
    shot_count: int
    snapshot_digest: str
    excel_ref: str
    excel_sha256: str
    excel_path: Path


@dataclass(frozen=True)
class FormalPdfDelivery:
    status: str
    path: Path | None
    filename: str
    unavailable_reason: str


class FormalDeliverySnapshotService:
    def __init__(self, store: WorkspaceStore, artifacts: RunArtifactResolver):
        self.store = store
        self.artifacts = artifacts
        self.store.initialize()

    def ensure_excel_snapshot(
        self,
        auth_token: str,
        *,
        context: VersionDeliveryContext,
        workbook: ResolvedArtifact,
        review_model: dict,
        ancestor_of: str | None = None,
    ) -> FormalDeliverySnapshot:
        if (
            workbook.ref != context.version_artifact.ref
            or workbook.sha256 != context.version_artifact.sha256
            or sha256_file(workbook.path) != workbook.sha256
        ):
            raise WorkspaceServiceError(
                "DELIVERY_ARTIFACT_DRIFT", "交付 Excel 与当前确认版本不一致",
            )
        evidence = self._evidence(context)
        parameters = {
            "workspace_id": context.workspace_id,
            "project_id": context.project_id,
            "version_id": context.version_id,
            "run_id": context.run_id,
            "brief_snapshot_id": evidence["brief_snapshot_id"],
            "plan_task_id": evidence["plan_task_id"],
            "image_snapshot_id": evidence["image_snapshot_id"],
            "quality_snapshot_id": evidence["quality_snapshot_id"],
            "final_confirmation_id": evidence["final_confirmation_id"],
            "shot_count": evidence["shot_count"],
            "script_plan_ref": context.artifacts.script_plan.ref,
            "script_plan_sha256": context.artifacts.script_plan.sha256,
            "script_draft_ref": context.artifacts.script_draft.ref,
            "script_draft_sha256": context.artifacts.script_draft.sha256,
            "visual_binding_ref": context.artifacts.visual_binding.ref,
            "visual_binding_sha256": context.artifacts.visual_binding.sha256,
            "image_decision_ref": evidence["image_decision_ref"],
            "image_decision_sha256": evidence["image_decision_sha256"],
            "workbook_ref": workbook.ref,
            "workbook_sha256": workbook.sha256,
            "source_trace_sha256": evidence["source_trace_sha256"],
        }
        with self.store.connection() as connection:
            require_permission(
                connection,
                auth_token,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                permission="project.read",
            )
            project = connection.execute(
                "SELECT current_version_id, lock_version FROM projects WHERE project_id=? AND workspace_id=?",
                (context.project_id, context.workspace_id),
            ).fetchone()
            if (
                project is None
                or project["current_version_id"] != (ancestor_of or context.version_id)
                or int(project["lock_version"]) != int(context.project_lock_version)
            ):
                raise WorkspaceServiceError(
                    "PROJECT_VERSION_CONFLICT", "项目在交付 snapshot 确认前已变化",
                )
            if ancestor_of:
                require_version_ancestor(connection, context.project_id, context.version_id, ancestor_of)
            existing = connection.execute(
                "SELECT * FROM delivery_snapshots WHERE version_id=?", (context.version_id,),
            ).fetchone()
            if existing is not None:
                snapshot_id = str(existing["delivery_snapshot_id"])
                binding = connection.execute(
                    """SELECT * FROM delivery_artifact_bindings
                    WHERE delivery_snapshot_id=? AND artifact_kind='excel'""",
                    (snapshot_id,),
                ).fetchone()
                output_path = self._delivery_file_path(snapshot_id, "excel")
                if (
                    binding is None
                    or binding["availability"] != "available"
                    or binding["artifact_ref"] != f"artifact://delivery/{snapshot_id}/excel"
                    or not output_path.is_file()
                    or sha256_file(output_path) != binding["artifact_sha256"]
                    or output_path.stat().st_size != int(binding["size_bytes"])
                ):
                    raise WorkspaceServiceError(
                        "DELIVERY_ARTIFACT_DRIFT", "已绑定 Excel 不存在或 SHA 已漂移",
                    )
                digest = sha256_json({
                    "schema_version": "mplus-formal-delivery-snapshot/v1",
                    **parameters,
                    "delivery_excel_ref": binding["artifact_ref"],
                    "delivery_excel_sha256": binding["artifact_sha256"],
                })
                if existing["snapshot_digest"] != digest:
                    raise WorkspaceServiceError(
                        "DELIVERY_SNAPSHOT_DRIFT", "既有交付 snapshot 与当前权威证据不一致",
                    )
                return FormalDeliverySnapshot(
                    snapshot_id, context.project_id, context.version_id, context.run_id,
                    int(parameters["shot_count"]), digest, binding["artifact_ref"],
                    binding["artifact_sha256"], output_path,
                )

        snapshot_id = new_id("delivery_snapshot")
        output_path = self._delivery_file_path(snapshot_id, "excel")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{snapshot_id}.", suffix=".xlsx", dir=str(output_path.parent),
        )
        os.close(descriptor)
        temporary_path = Path(temporary_name)
        try:
            write_reviewed_delivery_workbook(
                workbook.path, temporary_path, review_model=review_model,
            )
            excel_sha256 = sha256_file(temporary_path)
            excel_size = temporary_path.stat().st_size
            excel_ref = f"artifact://delivery/{snapshot_id}/excel"
            digest = sha256_json({
                "schema_version": "mplus-formal-delivery-snapshot/v1",
                **parameters,
                "delivery_excel_ref": excel_ref,
                "delivery_excel_sha256": excel_sha256,
            })
            os.replace(temporary_path, output_path)
            now = utc_now()
            with self.store.transaction() as connection:
                actor = require_permission(
                    connection,
                    auth_token,
                    workspace_id=context.workspace_id,
                    project_id=context.project_id,
                    permission="project.read",
                )
                project = connection.execute(
                    "SELECT current_version_id, lock_version FROM projects WHERE project_id=? AND workspace_id=?",
                    (context.project_id, context.workspace_id),
                ).fetchone()
                if (
                    project is None
                    or project["current_version_id"] != (ancestor_of or context.version_id)
                    or int(project["lock_version"]) != int(context.project_lock_version)
                ):
                    raise WorkspaceServiceError(
                        "PROJECT_VERSION_CONFLICT", "项目在交付 snapshot 确认前已变化",
                    )
                if ancestor_of:
                    require_version_ancestor(connection, context.project_id, context.version_id, ancestor_of)
                connection.execute(
                    """INSERT INTO delivery_snapshots(
                    delivery_snapshot_id, workspace_id, project_id, version_id, run_id,
                    brief_snapshot_id, plan_task_id, image_snapshot_id, quality_snapshot_id,
                    final_confirmation_id, project_lock_version, shot_count,
                    script_plan_ref, script_plan_sha256, script_draft_ref, script_draft_sha256,
                    visual_binding_ref, visual_binding_sha256, image_decision_ref,
                    image_decision_sha256, workbook_ref, workbook_sha256,
                    source_trace_sha256, snapshot_digest, created_by_actor_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id, context.workspace_id, context.project_id,
                        context.version_id, context.run_id, parameters["brief_snapshot_id"],
                        parameters["plan_task_id"], parameters["image_snapshot_id"],
                        parameters["quality_snapshot_id"], parameters["final_confirmation_id"],
                        context.project_lock_version, parameters["shot_count"],
                        parameters["script_plan_ref"], parameters["script_plan_sha256"],
                        parameters["script_draft_ref"], parameters["script_draft_sha256"],
                        parameters["visual_binding_ref"], parameters["visual_binding_sha256"],
                        parameters["image_decision_ref"], parameters["image_decision_sha256"],
                        parameters["workbook_ref"], parameters["workbook_sha256"],
                        parameters["source_trace_sha256"], digest, actor["actor_id"], now,
                    ),
                )
                connection.execute(
                    """INSERT INTO delivery_artifact_bindings(
                    delivery_artifact_binding_id, delivery_snapshot_id, artifact_kind,
                    availability, artifact_ref, artifact_sha256, size_bytes,
                    unavailable_reason, created_by_actor_id, created_at)
                    VALUES (?, ?, 'excel', 'available', ?, ?, ?, NULL, ?, ?)""",
                    (
                        new_id("delivery_artifact"), snapshot_id, excel_ref,
                        excel_sha256, excel_size, actor["actor_id"], now,
                    ),
                )
        except (OSError, ValueError) as exc:
            temporary_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
            raise WorkspaceServiceError(
                "DELIVERY_EXCEL_INVALID", f"正式 Excel 审核结果写入失败：{exc}",
            ) from exc
        except (sqlite3.IntegrityError, WorkspaceServiceError):
            temporary_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)
            raise
        return FormalDeliverySnapshot(
            snapshot_id, context.project_id, context.version_id, context.run_id,
            int(parameters["shot_count"]), digest, excel_ref, excel_sha256, output_path,
        )

    def prepare_optional_pdf(
        self,
        auth_token: str,
        *,
        context: VersionDeliveryContext,
        snapshot: FormalDeliverySnapshot,
        filename: str,
    ) -> FormalPdfDelivery:
        output_path = self._delivery_file_path(snapshot.delivery_snapshot_id, "pdf")
        with self.store.connection() as connection:
            binding = connection.execute(
                """SELECT * FROM delivery_artifact_bindings
                WHERE delivery_snapshot_id=? AND artifact_kind='pdf'""",
                (snapshot.delivery_snapshot_id,),
            ).fetchone()
            source_trace = self._source_trace(
                connection,
                connection.execute(
                    "SELECT image_snapshot_id FROM delivery_snapshots WHERE delivery_snapshot_id=?",
                    (snapshot.delivery_snapshot_id,),
                ).fetchone()[0],
            )["items"]
        if binding is not None:
            if binding["availability"] == "optional_unavailable":
                # 失败状态不再由本服务写入；历史失败 binding 仍受不可变合同保护。
                return FormalPdfDelivery(
                    "optional_unavailable", None, filename, binding["unavailable_reason"],
                )
            if not output_path.is_file() or sha256_file(output_path) != binding["artifact_sha256"]:
                raise WorkspaceServiceError("DELIVERY_ARTIFACT_DRIFT", "已绑定 PDF 不存在或 SHA 已漂移")
            return FormalPdfDelivery("available", output_path, filename, "")
        temp_path: Path | None = None
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{snapshot.delivery_snapshot_id}.",
                suffix=".pdf",
                dir=str(output_path.parent),
            )
            temp_path = Path(temporary_name)
            os.close(descriptor)
            draft_artifact = self.artifacts.resolve_stage_artifact(context.run_id, "script_draft")
            draft_payload = json.loads(draft_artifact.path.read_text(encoding="utf-8"))
            from .formal_image_workspace import formal_image_details_by_script_shot
            details = formal_image_details_by_script_shot(
                list(draft_payload.get("script_rows") or []), list(draft_payload.get("detail_rows") or []))
            create_formal_snapshot_pdf(
                snapshot.excel_path,
                workbook_sha256=snapshot.excel_sha256,
                snapshot_digest=snapshot.snapshot_digest,
                project_id=context.project_id,
                version_id=context.version_id,
                run_id=context.run_id,
                source_trace=source_trace,
                script_rows=list(draft_payload.get("script_rows") or []),
                output_path=temp_path,
                adaptation_by_shot={shot_no: str(detail.get("改编说明") or "") for shot_no, detail in details.items()},
            )
            os.replace(temp_path, output_path)
            temp_path = None
            artifact_ref = f"artifact://delivery/{snapshot.delivery_snapshot_id}/pdf"
            artifact_sha256 = sha256_file(output_path)
            size_bytes = output_path.stat().st_size
        except OSError as exc:
            self._cleanup_pdf_paths(temp_path, output_path)
            return FormalPdfDelivery("optional_unavailable", None, filename, str(exc))
        except ArtifactResolutionError as exc:
            self._cleanup_pdf_paths(temp_path, output_path)
            raise WorkspaceServiceError(exc.code, f"PDF 交付依赖 artifact 无法读取：{exc.message}") from exc
        except json.JSONDecodeError as exc:
            self._cleanup_pdf_paths(temp_path, output_path)
            raise WorkspaceServiceError("DELIVERY_PDF_INVALID", "PDF 交付输入不是可解析的脚本 artifact") from exc
        except V10ImageDeliveryError as exc:
            self._cleanup_pdf_paths(temp_path, output_path)
            if self._is_retryable_pdf_error(exc):
                return FormalPdfDelivery("optional_unavailable", None, filename, str(exc))
            raise WorkspaceServiceError("DELIVERY_PDF_INVALID", f"PDF 交付合同无效：{exc}") from exc
        now = utc_now()
        with self.store.transaction() as connection:
            actor = require_permission(
                connection,
                auth_token,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                permission="project.read",
            )
            connection.execute(
                """INSERT INTO delivery_artifact_bindings(
                delivery_artifact_binding_id, delivery_snapshot_id, artifact_kind,
                availability, artifact_ref, artifact_sha256, size_bytes,
                unavailable_reason, created_by_actor_id, created_at)
                VALUES (?, ?, 'pdf', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("delivery_artifact"), snapshot.delivery_snapshot_id,
                    "available", artifact_ref, artifact_sha256, size_bytes,
                    None, actor["actor_id"], now,
                ),
            )
        return FormalPdfDelivery("available", output_path, filename, "")

    def reverse_trace(
        self,
        auth_token: str,
        *,
        project_id: str,
        artifact_ref: str,
    ) -> dict[str, Any]:
        with self.store.connection() as connection:
            project = connection.execute(
                "SELECT workspace_id FROM projects WHERE project_id=?", (project_id,),
            ).fetchone()
            if project is None:
                raise WorkspaceServiceError("NOT_FOUND", "project 不存在")
            require_permission(
                connection,
                auth_token,
                workspace_id=project["workspace_id"],
                project_id=project_id,
                permission="project.read",
            )
            row = connection.execute(
                """SELECT s.*, b.artifact_kind, b.artifact_ref, b.artifact_sha256,
                          b.availability
                FROM delivery_artifact_bindings b
                JOIN delivery_snapshots s ON s.delivery_snapshot_id=b.delivery_snapshot_id
                WHERE s.project_id=? AND b.artifact_ref=?""",
                (project_id, artifact_ref),
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("NOT_FOUND", "交付 artifact 未绑定权威 snapshot")
            source_trace = self._source_trace(connection, row["image_snapshot_id"])
            if source_trace["digest"] != row["source_trace_sha256"]:
                raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "图片来源反查摘要不一致")
            lineage = []
            current = row["version_id"]
            while current:
                version = connection.execute(
                    "SELECT version_id, parent_version_id FROM project_versions WHERE project_id=? AND version_id=?",
                    (project_id, current),
                ).fetchone()
                if version is None:
                    raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "版本血缘不完整")
                lineage.append(version["version_id"])
                current = version["parent_version_id"]
            feedback_count = int(connection.execute(
                """SELECT COUNT(*) FROM formal_version_feedback_records
                WHERE project_id=? AND (base_version_id IN ({}) OR child_version_id IN ({}))""".format(
                    ",".join("?" for _ in lineage), ",".join("?" for _ in lineage),
                ),
                (project_id, *lineage, *lineage),
            ).fetchone()[0])
            result = row_dict(row)
        for ref_key, sha_key in (
            ("script_plan_ref", "script_plan_sha256"),
            ("script_draft_ref", "script_draft_sha256"),
            ("visual_binding_ref", "visual_binding_sha256"),
            ("image_decision_ref", "image_decision_sha256"),
            ("workbook_ref", "workbook_sha256"),
        ):
            self._verify_artifact(result[ref_key], result[sha_key])
        if result["artifact_kind"] == "pdf":
            pdf_path = self._delivery_file_path(result["delivery_snapshot_id"], "pdf")
            if not pdf_path.is_file() or sha256_file(pdf_path) != result["artifact_sha256"]:
                raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "PDF artifact SHA 已漂移")
        elif result["artifact_kind"] == "excel":
            excel_path = self._delivery_file_path(result["delivery_snapshot_id"], "excel")
            if (
                result["artifact_ref"] != f"artifact://delivery/{result['delivery_snapshot_id']}/excel"
                or not excel_path.is_file()
                or sha256_file(excel_path) != result["artifact_sha256"]
            ):
                raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "Excel artifact SHA 已漂移")
        return {
            "status": "verified",
            "delivery_snapshot_id": result["delivery_snapshot_id"],
            "project_id": result["project_id"],
            "brief_snapshot_id": result["brief_snapshot_id"],
            "plan_task_id": result["plan_task_id"],
            "run_id": result["run_id"],
            "version_lineage": lineage,
            "feedback_count": feedback_count,
            "image_snapshot_id": result["image_snapshot_id"],
            "image_decision_count": len(source_trace["items"]),
            "source_trace": source_trace["items"],
            "quality_snapshot_id": result["quality_snapshot_id"],
            "final_confirmation_id": result["final_confirmation_id"],
            "artifact_kind": result["artifact_kind"],
            "artifact_sha256": result["artifact_sha256"],
        }

    def history_artifacts(
        self,
        auth_token: str,
        *,
        workspace_id: str,
        project_id: str,
        run_id: str,
    ) -> tuple[dict[str, Any], ...]:
        """Resolve only immutable delivery bindings allowed on Formal history pages."""
        with self.store.connection() as connection:
            require_permission(
                connection,
                auth_token,
                workspace_id=workspace_id,
                project_id=project_id,
                permission="project.read",
            )
            rows = connection.execute(
                """SELECT s.delivery_snapshot_id, b.artifact_kind, b.availability,
                          b.artifact_ref, b.artifact_sha256, b.size_bytes
                FROM delivery_snapshots s
                JOIN delivery_artifact_bindings b
                  ON b.delivery_snapshot_id=s.delivery_snapshot_id
                WHERE s.workspace_id=? AND s.project_id=? AND s.run_id=?
                ORDER BY b.artifact_kind""",
                (workspace_id, project_id, run_id),
            ).fetchall()
        artifacts = []
        for row in rows:
            if row["availability"] != "available":
                continue
            if row["artifact_kind"] == "excel":
                path = self._delivery_file_path(row["delivery_snapshot_id"], "excel")
                ref = str(row["artifact_ref"] or "")
                digest = sha256_file(path) if path.is_file() else ""
                label = "下载脚本 Excel"
                kind = "workbook"
            elif row["artifact_kind"] == "pdf":
                path = self._delivery_file_path(row["delivery_snapshot_id"], "pdf")
                ref = str(row["artifact_ref"] or "")
                digest = sha256_file(path) if path.is_file() else ""
                label = "下载脚本 PDF"
                kind = "pdf"
            else:
                continue
            if (
                ref != row["artifact_ref"]
                or digest != row["artifact_sha256"]
                or not path.is_file()
                or path.stat().st_size != int(row["size_bytes"])
            ):
                raise WorkspaceServiceError(
                    "DELIVERY_ARTIFACT_DRIFT", "历史交付 artifact 与不可变 binding 不一致",
                )
            artifacts.append({"path": path, "kind": kind, "label": label})
        return tuple(artifacts)

    def _evidence(self, context: VersionDeliveryContext) -> dict[str, Any]:
        try:
            plan_artifact = self.artifacts.resolve_stage_artifact(
                context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
            )
            image_artifact = self.artifacts.resolve_artifact(context.run_id, "image_decisions")
            plan = json.loads(plan_artifact.path.read_text(encoding="utf-8"))
        except (ArtifactResolutionError, OSError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("ARTIFACT_INVALID", "交付 snapshot 无法读取规划或图片决定") from exc
        brief_snapshot_id = str((plan.get("confirmed_brief_input") or {}).get("brief_snapshot_id") or "")
        if not brief_snapshot_id:
            raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "规划缺少已确认 Brief 绑定")
        with self.store.connection() as connection:
            completion = connection.execute(
                """SELECT * FROM version_completion_states WHERE workspace_id=?
                AND project_id=? AND version_id=?""",
                (context.workspace_id, context.project_id, context.version_id),
            ).fetchone()
            image = connection.execute(
                """SELECT * FROM version_image_decision_snapshots_v3
                WHERE workspace_id=? AND project_id=? AND materialized_version_id=? AND run_id=?""",
                (context.workspace_id, context.project_id, context.version_id, context.run_id),
            ).fetchone()
            final = connection.execute(
                """SELECT f.*, q.snapshot_id AS quality_snapshot_id
                FROM version_quality_final_confirmations f
                JOIN version_quality_snapshots q ON q.snapshot_id=f.snapshot_id
                WHERE f.workspace_id=? AND f.project_id=? AND f.version_id=? AND f.run_id=?
                  AND f.decision='quality_pass'""",
                (context.workspace_id, context.project_id, context.version_id, context.run_id),
            ).fetchone()
            brief = connection.execute(
                "SELECT 1 FROM project_brief_snapshots WHERE brief_snapshot_id=? AND project_id=?",
                (brief_snapshot_id, context.project_id),
            ).fetchone()
            plan_task_id = self._plan_task_id(
                connection, context.project_id, context.run_id,
                context.artifacts.script_plan.sha256,
            )
            if completion is None or completion["image_state"] != "images_confirmed" or not completion["delivery_eligible"]:
                raise WorkspaceServiceError("DELIVERY_NOT_READY", "逐镜图片决定尚未达到交付状态")
            missing = [
                label for label, absent in (
                    ("图片决定 snapshot", image is None),
                    ("人工最终确认", final is None),
                    ("确认 Brief snapshot", brief is None),
                    ("规划 task", not plan_task_id),
                ) if absent
            ]
            if missing:
                raise WorkspaceServiceError(
                    "HERMES_TRACE_MISMATCH",
                    f"交付 HERMES 权威链不完整：{'、'.join(missing)}",
                )
            source_trace = self._source_trace(connection, image["snapshot_id"])
        if int(image["shot_count"]) != len(source_trace["items"]):
            raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "图片决定数量与来源追溯数量不一致")
        if image_artifact.ref != image["snapshot_ref"] or image_artifact.sha256 != image["snapshot_sha256"]:
            raise WorkspaceServiceError("DELIVERY_ARTIFACT_DRIFT", "图片决定 artifact 与 v3 snapshot 不一致")
        return {
            "brief_snapshot_id": brief_snapshot_id,
            "plan_task_id": plan_task_id,
            "image_snapshot_id": image["snapshot_id"],
            "quality_snapshot_id": final["quality_snapshot_id"],
            "final_confirmation_id": final["final_confirmation_id"],
            "shot_count": int(image["shot_count"]),
            "image_decision_ref": image["snapshot_ref"],
            "image_decision_sha256": image["snapshot_sha256"],
            "source_trace_sha256": source_trace["digest"],
        }

    @staticmethod
    def _plan_task_id(
        connection, project_id: str, run_id: str, plan_sha256: str,
    ) -> str:
        run_lineage = set()
        current_run = run_id
        while current_run:
            if current_run in run_lineage:
                raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "run 血缘形成循环")
            run_lineage.add(current_run)
            row = connection.execute(
                "SELECT parent_run_id FROM project_runs WHERE project_id=? AND run_id=?",
                (project_id, current_run),
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "run 血缘不完整")
            current_run = str(row["parent_run_id"] or "")
        rows = connection.execute(
            """SELECT t.task_id, e.details_json FROM tasks t
            JOIN task_events e ON e.task_id=t.task_id
            WHERE t.project_id=? AND t.task_type='formal_generation_plan'
            ORDER BY t.created_at DESC, e.sequence_no DESC""",
            (project_id,),
        ).fetchall()
        for row in rows:
            try:
                details = json.loads(row["details_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            plan_ref = str(details.get("plan_ref") or "")
            ref_parts = plan_ref.split("/")
            referenced_run = (
                ref_parts[3]
                if len(ref_parts) == 5 and ref_parts[:3] == ["artifact:", "", "run"]
                else ""
            )
            if (
                str(details.get("plan_sha256") or "") == plan_sha256
                or referenced_run in run_lineage
            ):
                return str(row["task_id"])
        return ""

    @staticmethod
    def _source_trace(connection, snapshot_id: str) -> dict[str, Any]:
        rows = connection.execute(
            """SELECT shot_no, decision_kind, project_image_id, image_sha256,
                      source_key, source_kind, source_location_json
            FROM version_image_decisions_v3 WHERE snapshot_id=? ORDER BY shot_no""",
            (snapshot_id,),
        ).fetchall()
        items = []
        for row in rows:
            if row["decision_kind"] != "image_confirmed":
                raise WorkspaceServiceError("DELIVERY_NOT_READY", "交付 snapshot 不允许暂准文字依据")
            try:
                location = json.loads(row["source_location_json"] or "{}")
            except json.JSONDecodeError as exc:
                raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "图片来源三元组不可解析") from exc
            if not isinstance(location, dict):
                raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "图片来源位置格式不正确")
            if row["source_kind"] == "generated":
                task_id = str(location.get("task_id") or "")
                source = connection.execute(
                    """SELECT s.origin, s.result_sha256 FROM project_image_generated_sources s
                    JOIN project_images p ON p.project_image_id=s.project_image_id
                    JOIN version_image_decision_snapshots_v3 v
                      ON v.project_id=p.project_id AND v.workspace_id=p.workspace_id
                    WHERE v.snapshot_id=? AND s.task_id=? AND s.project_image_id=?
                      AND p.image_sha256=?""",
                    (snapshot_id, task_id, row["project_image_id"], row["image_sha256"]),
                ).fetchone()
                if (
                    not location.get("file")
                    or row["source_key"] != f"generated:{task_id}"
                    or location.get("image_sha256") != row["image_sha256"]
                    or source is None
                    or source["origin"] not in {"generated", "edited"}
                ):
                    raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "生成图片的任务与内容来源不一致")
                items.append({
                    "shot_no": row["shot_no"],
                    "project_image_id": row["project_image_id"],
                    "image_sha256": row["image_sha256"],
                    "source_key": row["source_key"],
                    "source_kind": "generated",
                    "source_file": location["file"],
                    "source_sheet": None,
                    "source_row": None,
                    "source_task_id": task_id,
                    "source_origin": source["origin"],
                    "source_result_sha256": source["result_sha256"],
                })
                continue
            if not all((location.get("file"), location.get("sheet"), location.get("row"))):
                raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "图片来源文件、Sheet 或行缺失")
            items.append({
                "shot_no": row["shot_no"],
                "project_image_id": row["project_image_id"],
                "image_sha256": row["image_sha256"],
                "source_key": row["source_key"],
                "source_file": location["file"],
                "source_sheet": location["sheet"],
                "source_row": location["row"],
            })
        return {"items": items, "digest": sha256_json(items)}

    def _verify_artifact(self, artifact_ref: str, expected_sha256: str) -> Path:
        parts = str(artifact_ref or "").split("/")
        if len(parts) != 5 or parts[:3] != ["artifact:", "", "run"]:
            raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "交付引用不是受控 run artifact")
        try:
            artifact = (
                self.artifacts.resolve_manifest(parts[3], expected_sha256=expected_sha256)
                if parts[4] == "manifest"
                else self.artifacts.resolve_stage_artifact(parts[3], parts[4])
                if parts[4] in {"script_plan", "script_draft", "visual_binding"}
                else self.artifacts.resolve_artifact(parts[3], parts[4])
            )
        except ArtifactResolutionError as exc:
            raise WorkspaceServiceError(exc.code, f"{artifact_ref}: {exc.message}") from exc
        if artifact.sha256 != expected_sha256:
            raise WorkspaceServiceError("HERMES_TRACE_MISMATCH", "交付 artifact SHA 已漂移")
        return artifact.path

    def _delivery_file_path(self, snapshot_id: str, kind: str) -> Path:
        if kind not in {"excel", "pdf"} or not str(snapshot_id).startswith("delivery_snapshot_"):
            raise WorkspaceServiceError("INVALID_INPUT", "delivery artifact 身份无效")
        root = (self.artifacts.runs_root / "_formal_deliveries").resolve()
        suffix = ".xlsx" if kind == "excel" else ".pdf"
        path = (root / f"{snapshot_id}{suffix}").resolve()
        if path.parent != root:
            raise WorkspaceServiceError("INVALID_INPUT", "delivery artifact 路径越界")
        return path

    @staticmethod
    def _cleanup_pdf_paths(*paths: Path | None) -> None:
        for path in paths:
            if path is None:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _is_retryable_pdf_error(error: V10ImageDeliveryError) -> bool:
        return "缺少可嵌入的 CJK TrueType 字体" in str(error)
