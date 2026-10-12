from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pypdf import PdfReader

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_image_workspace import ImageShotSelection
import mvp_v2.formal_delivery_snapshot as delivery_snapshot_module
from mvp_v2.formal_review_workspace import (
    FormalReviewWorkspaceService,
    FormalReviewWorkspaceServices,
    WorkspaceBoundFinalConfirmation,
    WorkspaceBoundVersionQuality,
    WorkspaceReviewerActorResolver,
)
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.manual_review_commands import ManualReviewCommandService, ManualReviewCommandServices
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.review_evidence_artifacts import FileReviewEvidenceArtifactStore, ReviewEvidenceAuthority
from mvp_v2.task_service import TaskService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.version_publication_projection import VersionPublicationProjectionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from mvp_v2.v10_image_delivery import V10ImageDeliveryError
from tests import test_mvp_v2_formal_image_workspace as image_workspace_tests


class FormalReviewWorkspaceTests(unittest.TestCase):
    def setUp(self):
        base = image_workspace_tests.FormalImageWorkspaceTests("runTest")
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.base = base
        self.root = base.base.generated.root
        self.store = base.store
        self.project_id = base.project_id
        self.workspace_id = base.workspace_id
        self.token = base.token
        ready = base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.confirmed_images = base.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=ready["project_version"],
            selections=tuple(
                ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                for item in ready["shots"]
            ),
            idempotency_key="review-fixture-images",
        )
        self.review = self._review_service(self.store)

    def _review_service(self, store: WorkspaceStore) -> FormalReviewWorkspaceService:
        artifacts = RunArtifactResolver(self.root / "runs")
        delivery = VersionDeliveryService(store, artifacts)
        quality = WorkspaceBoundVersionQuality(
            store=store,
            delivery=delivery,
            artifacts=artifacts,
            data_root=self.root / "quality-evidence",
        )
        commands = ManualReviewCommandService(ManualReviewCommandServices(
            store=store,
            delivery=delivery,
            quality=quality,
            final_confirmation=WorkspaceBoundFinalConfirmation(
                quality=quality,
                actor_resolver=WorkspaceReviewerActorResolver(store),
            ),
            evidence_authority=ReviewEvidenceAuthority(
                FileReviewEvidenceArtifactStore(self.root / "review-evidence"),
            ),
        ))
        return FormalReviewWorkspaceService(FormalReviewWorkspaceServices(
            store=store,
            projects=ProjectService(store),
            delivery=delivery,
            artifacts=artifacts,
            commands=commands,
            image_workspace=self.base.workspace,
        ))

    def _start(self) -> dict:
        current = self.review.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(current["state"], "ready")
        return self.review.start_review(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=current["project_version"],
            idempotency_key="review-start",
        )

    def test_blocked_review_exposes_readonly_shot_repairs_without_approvable_keys(self):
        empty_preview = self.review._issue_view(
            issue_key="", kind="preview", shot_no="1", severity="attention",
            status="open", shot={"shot_id": "anonymous-shot-1"}, changed={}, preview=True,
        )
        self.assertNotEqual(empty_preview["image_caption"], "当前已确认图片")
        current = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        current = {**current, "state": "needs_image_reconfirmation"}
        before = self.store.database_path.read_bytes()
        with patch.object(self.base.workspace, "read_current", return_value=current):
            blocked = self.review.read_current(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            )
        self.assertEqual(blocked["state"], "blocked")
        self.assertTrue(blocked["issues"])
        self.assertGreater(len(blocked["issues"]), 1)
        self.assertEqual(len(blocked["issues"]), 2 * len(current["shots"]))
        self.assertEqual([item["kind"] for item in blocked["prerequisites"]], ["version_save"])
        self.assertEqual(blocked["decision"]["problem_count"], 1)
        self.assertEqual(blocked["decision"]["approved_count"], 0)
        self.assertEqual(
            blocked["decision"]["attention_count"],
            sum(item["kind"] != "machine" for item in blocked["prerequisites"]),
        )
        self.assertTrue(all(item["image_caption"] != "当前已确认图片" for item in blocked["issues"]))
        self.assertEqual(blocked["base_version_id"], current["base_version_id"])
        for issue in blocked["issues"]:
            self.assertTrue(issue["read_only"])
            self.assertEqual(issue["issue_key"], "")
            self.assertEqual(issue["repair_action"]["panel"], "images" if issue["kind"] == "image" else "script")
            self.assertEqual(issue["repair_action"]["shot_id"], issue["shot_id"])
        self.assertEqual(self.store.database_path.read_bytes(), before)
        with patch.object(self.review, "_machine_report", return_value={
            "machine_audit_status": "fail", "blocking_items": ["相邻复核数量必须为 N-1"],
        }):
            machine_blocked = self.review.read_current(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            )
        machine_issue = next(issue for issue in machine_blocked["issues"] if issue["kind"] == "machine")
        self.assertEqual(machine_issue["change_summary"], "相邻复核数量必须为 N-1")
        self.assertTrue(machine_issue["read_only"])
        self.assertFalse(machine_issue["issue_key"])
        self.assertEqual(machine_blocked["decision"]["blocking_count"], 1)

    def _confirm_current(self) -> dict:
        started = self._start()
        issue_keys = tuple(item["issue_key"] for item in started["issues"])
        approved = self.review.decide(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=started["project_version"],
            issue_keys=issue_keys,
            decision="approve",
            note="已逐镜核对内容、图片、来源和变化范围。",
            idempotency_key="review-approve-all",
        )
        return self.review.confirm_final(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=approved["project_version"],
            note="整份脚本、逐镜图片和变化镜头均已检查。",
            idempotency_key="review-final",
        )

    def test_optional_pdf_transient_failure_retries_same_snapshot_idempotently_after_restart(self):
        confirmed = self._confirm_current()
        excel = self.review.prepare_excel_delivery(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=confirmed["project_version"],
        )
        excel_sha = hashlib.sha256(excel.path.read_bytes()).hexdigest()
        before = self.review.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        with self.store.connection() as connection:
            excel_binding_before = tuple(connection.execute(
                """SELECT availability, artifact_ref, artifact_sha256, size_bytes
                FROM delivery_artifact_bindings WHERE artifact_kind='excel'"""
            ).fetchone())

        calls = 0

        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise V10ImageDeliveryError("缺少可嵌入的 CJK TrueType 字体，不能生成可读 PDF")
            return real_create(*args, **kwargs)

        real_create = delivery_snapshot_module.create_formal_snapshot_pdf
        with patch.object(delivery_snapshot_module, "create_formal_snapshot_pdf", side_effect=fail_once):
            first = self.review.prepare_pdf_delivery(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=confirmed["project_version"],
            )
            self.assertEqual(first.status, "optional_unavailable")
            self.assertIsNone(first.path)
            self.assertEqual(
                list((self.root / "runs" / "_formal_deliveries").glob(".*.pdf")), []
            )
            with self.store.connection() as connection:
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM delivery_artifact_bindings WHERE artifact_kind='pdf'"
                ).fetchone()[0], 0)

            second = self.review.prepare_pdf_delivery(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=confirmed["project_version"],
            )
            self.assertEqual(second.status, "available")
            self.assertIsNotNone(second.path)
            second_sha = hashlib.sha256(second.path.read_bytes()).hexdigest()

            third = self.review.prepare_pdf_delivery(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=confirmed["project_version"],
            )
            self.assertEqual(third.status, "available")
            self.assertEqual(third.path, second.path)
            self.assertEqual(hashlib.sha256(third.path.read_bytes()).hexdigest(), second_sha)
            self.assertEqual(calls, 2)

            reopened_store = WorkspaceStore(self.store.database_path)
            reopened_review = self._review_service(reopened_store)
            recovered = reopened_review.prepare_pdf_delivery(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=confirmed["project_version"],
            )
            self.assertEqual(recovered.status, "available")
            self.assertEqual(hashlib.sha256(recovered.path.read_bytes()).hexdigest(), second_sha)
            self.assertEqual(calls, 2)

        after = self.review.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual((after["state"], after["project_version"]), (before["state"], before["project_version"]))
        self.assertEqual(hashlib.sha256(excel.path.read_bytes()).hexdigest(), excel_sha)
        with self.store.connection() as connection:
            excel_binding_after = tuple(connection.execute(
                """SELECT availability, artifact_ref, artifact_sha256, size_bytes
                FROM delivery_artifact_bindings WHERE artifact_kind='excel'"""
            ).fetchone())
            pdf_binding = tuple(connection.execute(
                """SELECT availability, artifact_ref, artifact_sha256, size_bytes
                FROM delivery_artifact_bindings WHERE artifact_kind='pdf'"""
            ).fetchone())
        self.assertEqual(excel_binding_after, excel_binding_before)
        self.assertEqual(pdf_binding[0], "available")
        self.assertEqual(pdf_binding[1], f"artifact://delivery/{second.path.stem}/pdf")
        self.assertEqual(pdf_binding[2], second_sha)

    def test_pdf_contract_error_is_explicit_and_does_not_bind_optional_failure(self):
        confirmed = self._confirm_current()
        with patch.object(
            delivery_snapshot_module,
            "create_formal_snapshot_pdf",
            side_effect=V10ImageDeliveryError("Formal Excel 与同 snapshot 脚本 artifact 不一致"),
        ):
            with self.assertRaisesRegex(WorkspaceServiceError, "DELIVERY_PDF_INVALID"):
                self.review.prepare_pdf_delivery(
                    self.token,
                    workspace_id=self.workspace_id,
                    project_id=self.project_id,
                    expected_project_version=confirmed["project_version"],
                )
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM delivery_artifact_bindings WHERE artifact_kind='pdf'"
            ).fetchone()[0], 0)

    def test_project_manager_can_review_confirm_and_restart_without_publication(self):
        preview = self.review.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual((preview["state"], preview["review_started"]), ("ready", False))
        self.assertEqual(
            set(preview["decision_counts"]),
            {
                "requirements_uncovered", "hard_template_downgrades",
                "unresolved_conflicts", "unexplained_overrides",
            },
        )
        self.assertEqual(
            sum(item["kind"] == "image" for item in preview["issues"]),
            self.confirmed_images["shot_count"],
        )
        self.assertTrue(any(item["kind"] == "change" for item in preview["issues"]))
        started = self._start()
        self.assertEqual(started["state"], "reviewing")
        issue_keys = tuple(item["issue_key"] for item in started["issues"])
        approved = self.review.decide(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=started["project_version"],
            issue_keys=issue_keys,
            decision="approve",
            note="已逐镜核对内容、图片、来源和变化范围。",
            idempotency_key="review-approve-all",
        )
        self.assertEqual((approved["state"], approved["ready_for_final"]), ("ready_final", True))
        confirmed = self.review.confirm_final(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=approved["project_version"],
            note="整份脚本、逐镜图片和变化镜头均已检查。",
            idempotency_key="review-final",
        )
        self.assertEqual(confirmed["state"], "confirmed")
        self.assertEqual(confirmed["final_confirmation"]["decision"], "quality_pass")
        replayed = self.review.confirm_final(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=approved["project_version"],
            note="整份脚本、逐镜图片和变化镜头均已检查。",
            idempotency_key="review-final",
        )
        self.assertEqual(replayed["state"], "confirmed")
        self.assertEqual(replayed["project_version"], confirmed["project_version"])
        reviewed_version_id = ProjectService(self.store).get_project(
            self.token, self.project_id,
        )["current_version_id"]
        reviews = VersionService(self.store).list_reviews(self.token, reviewed_version_id)
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["decision"], "quality_pass")
        delivery = self.review.prepare_excel_delivery(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=confirmed["project_version"],
        )
        self.assertTrue(delivery.path.is_file())
        self.assertTrue(delivery.filename.endswith("-脚本交付.xlsx"))
        current_version_id = self.review.services.projects.get_project(
            self.token, self.project_id,
        )["current_version_id"]
        delivery_context = self.review.services.delivery.resolve(
            self.token, self.project_id, current_version_id,
            expected_project_version=confirmed["project_version"],
        )
        with self.store.connection() as connection:
            excel_ref = connection.execute(
                """SELECT artifact_ref FROM delivery_artifact_bindings
                WHERE artifact_kind='excel' AND availability='available'"""
            ).fetchone()[0]
        trace = self.review.delivery_snapshots.reverse_trace(
            self.token,
            project_id=self.project_id,
            artifact_ref=excel_ref,
        )
        self.assertEqual(trace["status"], "verified")
        self.assertEqual(trace["image_decision_count"], self.confirmed_images["shot_count"])
        self.assertEqual(trace["version_lineage"][0], delivery_context.version_id)
        self.assertTrue(trace["brief_snapshot_id"])
        self.assertTrue(trace["plan_task_id"])
        for item in trace["source_trace"]:
            self.assertTrue(item["source_file"])
            self.assertTrue(item["source_sheet"])
            self.assertGreater(int(item["source_row"]), 0)
        pdf_delivery = self.review.prepare_pdf_delivery(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=confirmed["project_version"],
        )
        self.assertEqual(pdf_delivery.status, "available", pdf_delivery.unavailable_reason)
        self.assertIsNotNone(pdf_delivery.path)
        pdf_reader = PdfReader(str(pdf_delivery.path))
        self.assertEqual(len(pdf_reader.pages), self.confirmed_images["shot_count"])
        self.assertIn(trace["delivery_snapshot_id"], str(pdf_delivery.path))
        with self.store.connection() as connection:
            pdf_ref = connection.execute(
                """SELECT artifact_ref FROM delivery_artifact_bindings
                WHERE artifact_kind='pdf' AND availability='available'"""
            ).fetchone()[0]
        pdf_trace = self.review.delivery_snapshots.reverse_trace(
            self.token, project_id=self.project_id, artifact_ref=pdf_ref,
        )
        self.assertEqual(pdf_trace["delivery_snapshot_id"], trace["delivery_snapshot_id"])
        self.assertEqual(pdf_trace["artifact_kind"], "pdf")
        workbook_digest = hashlib.sha256(delivery.path.read_bytes()).hexdigest()
        workbook = load_workbook(delivery.path, read_only=False, data_only=False)
        try:
            self.assertIn("脚本", workbook.sheetnames)
            self.assertIn("审核记录", workbook.sheetnames)
            self.assertEqual(
                len(workbook["脚本"]._images),
                self.confirmed_images["shot_count"],
            )
            review_log = workbook["审核记录"]
            self.assertEqual(review_log.max_row, len(issue_keys) + 2)
            self.assertEqual(
                [cell.value for cell in review_log[1]],
                ["审核范围", "镜号", "结论", "处理人", "处理时间"],
            )
            self.assertEqual(review_log.cell(review_log.max_row, 1).value, "整片确认")
            self.assertEqual(review_log.cell(review_log.max_row, 3).value, "通过")
            shot_review = workbook["剪辑复核"]
            shot_headers = {
                str(cell.value or ""): cell.column for cell in shot_review[1]
            }
            self.assertEqual(
                {
                    shot_review.cell(row, shot_headers["总体审核"]).value
                    for row in range(2, shot_review.max_row + 1)
                },
                {"通过"},
            )
            full_review = workbook["整片复核"]
            full_headers = {
                str(cell.value or ""): cell.column for cell in full_review[1]
            }
            self.assertEqual(full_review.cell(2, full_headers["总体审核"]).value, "通过")
            workbook_text = "\n".join(
                str(cell.value or "")
                for sheet in workbook.worksheets
                for row in sheet.iter_rows()
                for cell in row
            ).casefold()
            for forbidden in (
                "artifact://", "image_sha256", "analysis_sha256", str(self.root),
            ):
                self.assertNotIn(forbidden.casefold(), workbook_text)
        finally:
            workbook.close()

        # The workspace uses this opaque binding to restore a shot after a
        # version change; all other internal evidence remains private.
        self.assertEqual(confirmed["base_version_id"], delivery_context.version_id)
        public = json.dumps(
            {key: value for key, value in confirmed.items() if key != "base_version_id"},
            ensure_ascii=False,
        )
        for forbidden in (
            "artifact://", "sha256", "actor_id", "version_id", "run_id",
            "CURRENT", "publication", "review-item", "human:", str(self.root),
        ):
            self.assertNotIn(forbidden, public)

        reopened_store = WorkspaceStore(self.store.database_path)
        reopened_review = self._review_service(reopened_store)
        recovered = reopened_review.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(recovered["state"], "confirmed")
        self.assertEqual(recovered["decision"]["approved_count"], len(issue_keys))
        recovered_delivery = reopened_review.prepare_excel_delivery(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=recovered["project_version"],
        )
        self.assertEqual(
            hashlib.sha256(recovered_delivery.path.read_bytes()).hexdigest(),
            workbook_digest,
        )
        agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=reopened_store,
            identity=LocalIdentityAdapter(reopened_store),
            projects=ProjectService(reopened_store),
            tasks=TaskService(reopened_store),
            versions=VersionService(reopened_store),
            artifacts=RunArtifactResolver(self.root / "runs"),
        ))
        client = TestClient(create_app(
            agent_api=agent, formal_review_service=reopened_review,
        ))
        self.addCleanup(client.close)
        excel_path = (
            f"/api/formal/projects/{self.project_id}/review/excel"
            f"?workspace_id={self.workspace_id}"
            f"&expected_project_version={recovered['project_version']}"
        )
        self.assertEqual(client.get(excel_path).status_code, 401)
        self.assertEqual(client.get(
            excel_path.replace(self.workspace_id, "workspace_other"),
            headers={"X-MPlus-Agent-Auth": self.token},
        ).status_code, 403)
        response = client.get(
            excel_path, headers={"X-MPlus-Agent-Auth": self.token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["content-type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("filename*=utf-8''", response.headers["content-disposition"].casefold())
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), workbook_digest)
        pdf_response = client.get(
            excel_path.replace("/excel", "/pdf"),
            headers={"X-MPlus-Agent-Auth": self.token},
        )
        self.assertEqual(pdf_response.status_code, 200)
        self.assertEqual(pdf_response.headers["content-type"], "application/pdf")
        self.assertEqual(len(PdfReader(BytesIO(pdf_response.content)).pages), self.confirmed_images["shot_count"])
        with reopened_store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_quality_final_confirmations",
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_review_items",
            ).fetchone()[0], len(issue_keys))
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_review_human_decisions",
            ).fetchone()[0], len(issue_keys))
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM delivery_snapshots",
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM delivery_artifact_bindings WHERE artifact_kind='excel'",
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM delivery_artifact_bindings WHERE artifact_kind='pdf'",
            ).fetchone()[0], 1)
            with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                connection.execute(
                    "UPDATE delivery_snapshots SET shot_count=shot_count+1"
                )
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_publications",
            ).fetchone()[0], 0)
        self.assertFalse((self.root / "CURRENT").exists())
        self.assertFalse((self.root / "runs" / "CURRENT").exists())

    def test_copy_only_child_can_review_and_export_with_inherited_image_evidence(self):
        initial = self.base.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        changed = self.base.base.workspace.apply_table_change(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=initial["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=initial["shots"][0]["shot_id"],
            field="subtitle",
            value="青屿明星见面日，今夜见。",
            idempotency_key="review-inherited-copy-change-1",
        )
        self.base.base.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=initial["project_version"],
            expected_revision=changed["revision"],
            reason="仅修改字幕，复验继承图片的审核与交付",
            idempotency_key="review-inherited-copy-save-1",
        )

        confirmed = self._confirm_current()
        delivery = self.review.prepare_excel_delivery(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=confirmed["project_version"],
        )

        self.assertTrue(delivery.path.is_file())
        workbook = load_workbook(delivery.path, read_only=False, data_only=False)
        try:
            header_row = next(
                row
                for row in range(1, min(workbook["脚本"].max_row, 8) + 1)
                if "字幕" in {
                    str(cell.value or "") for cell in workbook["脚本"][row]
                }
            )
            script_headers = {
                str(cell.value or ""): cell.column for cell in workbook["脚本"][header_row]
            }
            self.assertEqual(
                workbook["脚本"].cell(header_row + 1, script_headers["字幕"]).value,
                "青屿明星见面日，今夜见。",
            )
            self.assertEqual(
                len(workbook["脚本"]._images), self.confirmed_images["shot_count"],
            )
            self.assertIn("审核记录", workbook.sheetnames)
        finally:
            workbook.close()

    def test_return_requires_a_new_version_and_strict_http_hides_internal_evidence(self):
        started = self._start()
        returned_key = started["issues"][0]["issue_key"]
        returned = self.review.decide(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=started["project_version"],
            issue_keys=(returned_key,),
            decision="return",
            note="本镜图片与画面重点不一致，请修改后重新提交。",
            idempotency_key="review-return-one",
        )
        self.assertEqual(returned["state"], "needs_change")
        with self.assertRaisesRegex(WorkspaceServiceError, "REVIEW_NEW_VERSION_REQUIRED"):
            self.review.decide(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=returned["project_version"],
                issue_keys=(returned_key,),
                decision="approve",
                note="不得覆盖退回结论",
                idempotency_key="review-invalid-override",
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "FINAL_REVIEW_NOT_READY"):
            self.review.confirm_final(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=returned["project_version"],
                note="不得绕过退回项",
                idempotency_key="review-invalid-final",
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "DELIVERY_NOT_READY"):
            self.review.prepare_excel_delivery(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=returned["project_version"],
            )

        agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=LocalIdentityAdapter(self.store),
            projects=ProjectService(self.store),
            tasks=TaskService(self.store),
            versions=VersionService(self.store),
            artifacts=RunArtifactResolver(self.root / "runs"),
        ))
        client = TestClient(create_app(agent_api=agent, formal_review_service=self.review))
        self.addCleanup(client.close)
        path = f"/api/formal/projects/{self.project_id}/review?workspace_id={self.workspace_id}"
        self.assertEqual(client.get(path).status_code, 401)
        self.assertEqual(client.get(
            f"/api/formal/projects/{self.project_id}/review?workspace_id=workspace_other",
            headers={"X-MPlus-Agent-Auth": self.token},
        ).status_code, 403)
        response = client.get(path, headers={"X-MPlus-Agent-Auth": self.token})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["base_version_id"],
            ProjectService(self.store).get_project(self.token, self.project_id)["current_version_id"],
        )
        rendered = json.dumps(
            {key: value for key, value in payload.items() if key != "base_version_id"},
            ensure_ascii=False,
        )
        for forbidden in (
            "artifact://", "sha256", "actor_id", "version_id", "run_id",
            "CURRENT", "publication", str(self.root),
        ):
            self.assertNotIn(forbidden, rendered)
        rejected = client.post(
            f"/api/formal/projects/{self.project_id}/review/start?workspace_id={self.workspace_id}",
            headers={"X-MPlus-Agent-Auth": self.token},
            json={
                "expected_project_version": returned["project_version"],
                "idempotency_key": "review-extra-field",
                "actor_id": "forged",
            },
        )
        self.assertEqual(rejected.status_code, 422)
        with sqlite3.connect(self.store.database_path) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_quality_final_confirmations",
            ).fetchone()[0], 0)

    def test_publication_projection_requires_excel_and_can_retry_same_event(self):
        started = self._start()
        approved = self.review.decide(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=started["project_version"],
            issue_keys=tuple(item["issue_key"] for item in started["issues"]),
            decision="approve",
            note="已完成全部匿名镜头复核。",
            idempotency_key="projection-review-all",
        )
        confirmed = self.review.confirm_final(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=approved["project_version"],
            note="整片确认通过。",
            idempotency_key="projection-final",
        )
        version_id = ProjectService(self.store).get_project(
            self.token, self.project_id,
        )["current_version_id"]
        review_record = VersionService(self.store).list_reviews(self.token, version_id)[-1]
        candidate = VersionService(self.store).select_candidate(
            self.token,
            version_id=version_id,
            expected_project_version=confirmed["project_version"],
            reason="匿名交付候选",
        )
        publication = VersionService(self.store).publish_version(
            self.token,
            version_id=version_id,
            review_id=review_record["review_id"],
            expected_project_version=candidate["project_version"],
        )
        with self.store.connection() as connection:
            event_id = connection.execute(
                """SELECT event_id FROM outbox
                WHERE event_type='project.version_published' AND aggregate_id=?""",
                (self.project_id,),
            ).fetchone()["event_id"]
        projector = VersionPublicationProjectionService(
            self.store, self.review.services.artifacts,
        )
        failed = projector.process_event(event_id)
        self.assertEqual(failed["projection_status"], "failed")
        self.assertFalse((self.root / "CURRENT").exists())
        replayed = VersionService(self.store).publish_version(
            self.token,
            version_id=version_id,
            review_id=review_record["review_id"],
            expected_project_version=publication["project_version"],
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["publication_id"], publication["publication_id"])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_publications WHERE version_id=?",
                (version_id,),
            ).fetchone()[0], 1)
        self.review.prepare_excel_delivery(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=publication["project_version"],
        )
        projected = projector.process_event(event_id)
        self.assertEqual(projected["projection_status"], "published")
        self.assertEqual((self.root / "CURRENT").resolve().name, projected["run_id"])
        stored = VersionService(self.store).list_publications(self.token, version_id)[-1]
        self.assertEqual(stored["projection_status"], "published")

    def test_manual_publication_receipt_recovers_projection_and_lost_response(self):
        started = self._start()
        approved = self.review.decide(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=started["project_version"],
            issue_keys=tuple(item["issue_key"] for item in started["issues"]),
            decision="approve",
            note="已完成全部匿名镜头复核。",
            idempotency_key="projection-review-all",
        )
        confirmed = self.review.confirm_final(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=approved["project_version"],
            note="整片确认通过。",
            idempotency_key="projection-final",
        )
        version_id = ProjectService(self.store).get_project(
            self.token, self.project_id,
        )["current_version_id"]
        review_record = VersionService(self.store).list_reviews(self.token, version_id)[-1]
        candidate = VersionService(self.store).select_candidate(
            self.token,
            version_id=version_id,
            expected_project_version=confirmed["project_version"],
            reason="匿名交付候选",
        )
        from mvp_v2.manual_version_commands import ManualVersionCommandService, ManualVersionCommandServices
        projector = VersionPublicationProjectionService(self.store, self.review.services.artifacts)
        commands = ManualVersionCommandService(ManualVersionCommandServices(
            store=self.store, projects=ProjectService(self.store), versions=VersionService(self.store),
            artifacts=self.review.services.artifacts, publication_projection=projector))
        parameters = dict(project_id=self.project_id, version_id=version_id,
            review_id=review_record['review_id'], expected_project_version=candidate['project_version'],
            idempotency_key='manual-projection-original')
        prepared = commands.prepare(self.token, tool_name='publish_version', parameters=parameters)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_publications').fetchone()[0], 0)
            self.assertEqual(connection.execute('SELECT status FROM manual_command_confirmations WHERE command_id=?',
                (prepared['command_id'],)).fetchone()[0], 'pending')
        commands = ManualVersionCommandService(commands.services)
        with self.assertRaisesRegex(WorkspaceServiceError, 'PERMISSION_DENIED'):
            commands.execute(self.token, tool_name='publish_version',
                parameters={**parameters, 'version_id': 'version_different_target'},
                command_token=prepared['command_token'])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_publications').fetchone()[0], 0)
        def execute():
            return commands.execute(self.token, tool_name='publish_version', parameters=parameters,
                command_token=prepared['command_token'])
        first = execute()
        self.assertEqual(first['result']['projection_status'], 'failed')
        publication_id = first['result']['publication_id']
        self.review.prepare_excel_delivery(self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=first['result']['project_version'])
        original = projector.process_publication
        def lost_response(value):
            original(value)
            raise OSError('response lost after CURRENT projection')
        with patch.object(projector, 'process_publication', side_effect=lost_response):
            with self.assertRaisesRegex(OSError, 'response lost'):
                execute()
        restored = execute()
        self.assertEqual(restored['result']['publication_id'], publication_id)
        self.assertEqual(restored['result']['projection_status'], 'published')
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_publications').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM manual_command_results').fetchone()[0], 1)
        with self.store.transaction() as connection:
            connection.execute("UPDATE auth_sessions SET revoked_at='2026-09-20T00:00:00Z'")
        with self.assertRaisesRegex(WorkspaceServiceError, 'AUTH_SESSION_INVALID'):
            execute()
        with self.store.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM version_publications').fetchone()[0], 1)

    def test_review_return_records_decision_and_hands_off_without_creating_version(self):
        started = self._start()
        agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=LocalIdentityAdapter(self.store),
            projects=ProjectService(self.store),
            tasks=TaskService(self.store),
            versions=VersionService(self.store),
            artifacts=RunArtifactResolver(self.root / "runs"),
        ))
        client = TestClient(create_app(agent_api=agent, formal_review_service=self.review))
        self.addCleanup(client.close)
        path = f"/api/formal/projects/{self.project_id}/review?workspace_id={self.workspace_id}"
        headers = {"X-MPlus-Agent-Auth": self.token}
        before = ProjectService(self.store).list_project_versions(self.token, self.project_id)
        response = client.post(
            f"/api/formal/projects/{self.project_id}/review/decisions?workspace_id={self.workspace_id}",
            headers=headers,
            json={
                "expected_project_version": started["project_version"],
                "issue_keys": [started["issues"][0]["issue_key"]],
                "decision": "return",
                "note": "把第4镜字幕修改为匿名审核字幕；把第7镜声音修改为克制环境声",
                "idempotency_key": "review-http-batch-return",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        returned = response.json()
        self.assertNotIn("revision_proposal", returned)
        self.assertEqual(returned["state"], "needs_change")
        self.assertEqual(
            returned["edit_actions"],
            [
                {"label": "前往脚本与版本", "href": f"/projects/{self.project_id}/script"},
                {"label": "前往逐镜配图", "href": f"/projects/{self.project_id}/shot-images"},
            ],
        )
        refreshed = client.get(path, headers=headers)
        after = ProjectService(self.store).list_project_versions(self.token, self.project_id)
        self.assertEqual(refreshed.json()["edit_actions"], returned["edit_actions"])
        self.assertEqual(after, before)
        with sqlite3.connect(self.store.database_path) as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_type='formal_review_revision'",
            ).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
