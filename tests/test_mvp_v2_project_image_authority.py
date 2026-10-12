from __future__ import annotations

from io import BytesIO
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
from openpyxl import Workbook
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from mvp_v2.formal_brief_input_adapter import FormalBriefInputAdapter, UploadedDocument
from mvp_v2.agent_tools import TOOL_REGISTRY, validate_tool_input
from mvp_v2.formal_brief_workspace_service import FormalBriefWorkspaceService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_image_authority import (
    ArtifactEvidence, ProjectImage, ProjectImageAnalysis,
    ProjectImageBusinessSummary, ProjectImagePlanningReadiness,
    ProjectImageSourceLocation,
    ManualImageAnalysisInput, ManualProjectImageAnalysisCommandService,
    ProjectImagePoolService, ProviderImageAnalysisInput,
    ProviderProjectImageAnalysisCommandService, _utc_timestamp,
)
from mvp_v2.project_service import ProjectService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore, _MIGRATIONS


QINGYU_STAR_ACTIVITY_FIXTURE = {
    "project_name": "青屿明星见面日",
    "brief": {
        "场景": "明星见面日签到、互动、合影与舞台环节",
        "时长": "60秒",
        "必须内容": "签到、明星互动、粉丝反应、舞台、品牌收尾",
        "禁忌内容": "未授权品牌和危险动作",
    },
    "shots": [
        {"shot_no": str(index), "function": function}
        for index, function in enumerate(
            ("主视觉", "签到", "入场", "明星互动", "粉丝反应", "舞台", "合影", "品牌收尾"),
            start=1,
        )
    ],
}


class ProjectImageAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-project-images-")
        self.root = Path(self.temporary.name)
        self.store = WorkspaceStore(self.root / "data" / "workspace.sqlite3")
        owner = LocalIdentityAdapter(self.store).bootstrap_owner(
            workspace_name="匿名工作区", display_name="匿名 owner", confirmed=True, token="anonymous-owner-token",
        )
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.first = self.projects.create_project(self.token, workspace_id=self.workspace_id, name="项目甲", category_id="发布会", expected_workspace_version=0)
        self.second = self.projects.create_project(self.token, workspace_id=self.workspace_id, name="项目乙", category_id="发布会", expected_workspace_version=1)
        self.briefs = FormalBriefWorkspaceService(self.store, artifact_root=self.root / "data")
        self.adapter = FormalBriefInputAdapter(self.briefs, data_root=self.root / "data")
        self.pool = ProjectImagePoolService(self.store, data_root=self.root / "data")
        self.commands = ManualProjectImageAnalysisCommandService(self.pool)

    def tearDown(self):
        self.temporary.cleanup()

    def test_five_formats_frozen_images_dedupe_sources_restart_and_manual_pass(self):
        confirmed, lock = self._confirmed_first()
        image_ids = self.pool.synchronize_confirmed_brief(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=self._snapshot_id(), expected_project_lock_version=lock,
        )
        self.assertEqual(len(image_ids), 1)
        image_root = self.root / "data" / "artifacts" / "formal-images"
        frozen = [path for path in image_root.rglob("*") if path.is_file()]
        self.assertEqual(len(frozen), 1)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_images").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_image_source_links").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM document_images").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT name FROM schema_migrations WHERE version=12").fetchone()[0], "project-image-analysis-authority")
        snapshot = self._snapshot_id()
        pending = self.pool.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=lock,
        )
        self.assertEqual((pending.total, pending.pending, pending.passed), (1, 1, 0))
        image = pending.images[0]
        self.assertIsNone(image.business_summary)
        self.assertEqual({source.source_label for source in image.source_links}, {"brief.docx", "hero.png"})
        self.assertEqual({source.image for source in image.source_links}, {1})
        analysis = self.commands.record_analysis(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_lock_version=lock,
            submission=_submission(image.image_id, image.image_sha256, confirmed["brief_key"], snapshot),
        )
        self.assertEqual(analysis.status, "pass")
        restarted = ProjectImagePoolService(WorkspaceStore(self.store.database_path), data_root=self.root / "data")
        ready = restarted.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=lock,
        )
        self.assertEqual((ready.total, ready.passed, ready.pending), (1, 1, 0))
        summary = ready.images[0].business_summary
        self.assertIsNotNone(summary)
        self.assertEqual(summary.caption, "产品发布现场的真实画面")
        self.assertEqual(summary.tags, ("发布",))
        self.assertEqual(summary.subjects, ("产品",))
        self.assertEqual(summary.evidence_status, "verified")
        self.assertNotEqual(summary.caption, "人工核验")
        self.assertEqual(ready.images[0].current_analysis.evidence, analysis.evidence)
        employee = restarted.employee_readiness(ready)
        text = json.dumps(employee, ensure_ascii=False)
        self.assertNotIn("artifact://", text)
        self.assertNotIn("sha256", text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("actor", text)
        self.assertNotIn("人工核验", text)
        self.assertNotIn("source_doc_", text)
        self.assertEqual({source["label"] for source in employee["images"][0]["sources"]}, {"brief.docx", "hero.png"})
        content, mime = restarted.read_preview_image(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            image_id=ready.images[0].image_id, brief_version_id=confirmed["brief_key"],
            brief_snapshot_id=snapshot, expected_project_lock_version=lock,
        )
        self.assertTrue(content.startswith(b"\x89PNG"))
        self.assertEqual(mime, "image/png")
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            restarted.read_preview_image(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                image_id=ready.images[0].image_id, brief_version_id=confirmed["brief_key"],
                brief_snapshot_id=snapshot, expected_project_lock_version=lock + 1,
            )
        again = self.commands.record_analysis(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_lock_version=lock,
            submission=_submission(image.image_id, image.image_sha256, confirmed["brief_key"], snapshot),
        )
        self.assertEqual(again.analysis_version_id, analysis.analysis_version_id)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_image_analysis_versions").fetchone()[0], 1)
        protected = restarted.artifacts.stage_analysis(restarted.artifacts.read_analysis(analysis.evidence))
        os.utime(protected.staged, (0, 0))
        os.utime(protected.final_path, (0, 0))
        ProjectImagePoolService(WorkspaceStore(self.store.database_path), data_root=self.root / "data")
        self.assertTrue(protected.staged.exists(), "已引用 SHA 的 staging 不得被恢复清理")
        self.assertTrue(protected.final_path.exists(), "已引用正式 artifact 不得被恢复清理")
        protected.rollback()

    def test_scope_lock_mapping_and_tamper_fail_closed(self):
        confirmed, lock = self._confirmed_first()
        snapshot = self._snapshot_id()
        self.pool.synchronize_confirmed_brief(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=lock,
        )
        pending = self.pool.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=lock,
        )
        image = pending.images[0]
        with self.assertRaises(TypeError):
            self.commands.record_analysis(self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_lock_version=lock, submission={})  # type: ignore[arg-type]
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            self.commands.record_analysis(
                self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"], expected_project_lock_version=0,
                submission=_submission(image.image_id, image.image_sha256, confirmed["brief_key"], snapshot),
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.pool.resolve_for_planning(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=lock + 1,
            )
        frozen = next(path for path in (self.root / "data" / "artifacts" / "formal-images").rglob("*") if path.is_file())
        frozen.write_bytes(b"tampered")
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_ARTIFACT_DRIFT"):
            self.pool.resolve_for_planning(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=lock,
            )

    def test_provider_low_confidence_is_not_human_review_and_can_be_manually_resolved(self):
        confirmed, lock = self._confirmed_first()
        snapshot, image = self._synced_image(confirmed, lock)
        providers = ProviderProjectImageAnalysisCommandService(self.pool)
        with self.assertRaises(TypeError):
            providers.record_analysis(
                self.token, workspace_id=self.workspace_id,
                project_id=self.first["project_id"], expected_project_lock_version=lock,
                submission={},  # type: ignore[arg-type]
            )
        analysis = providers.record_analysis(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_lock_version=lock,
            submission=ProviderImageAnalysisInput(
                image_id=image.image_id, image_sha256=image.image_sha256,
                brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot,
                status="low_confidence", caption="会议现场中的参与者",
                tags=("会议",), subjects=("参与者",), scene="室内会场",
                purpose="可用于活动氛围镜头", quality="画面清晰",
                brand="未见可辨识品牌", evidence=("画面可见多名参与者",),
                provider="minimax-cli", model="provider-managed",
                prompt_version="minimax-project-image-v1",
            ),
        )
        payload = self.pool.artifacts.read_analysis(analysis.evidence)
        self.assertEqual(payload["schema_version"], "mplus-project-image-analysis/v3")
        self.assertEqual(payload["analysis_origin"], "provider")
        self.assertEqual(payload["quality_flags"], [])
        self.assertEqual(payload["negative_flags"], [])
        self.assertGreater(payload["pixel_width"], 0)
        self.assertGreater(payload["pixel_height"], 0)
        self.assertRegex(payload["perceptual_hash"], r"^[0-9a-f]{16}$")
        self.assertNotIn("reviewed_by_actor_id", payload)
        readiness = self.pool.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot,
            expected_project_lock_version=lock,
        )
        self.assertEqual((readiness.low_confidence, readiness.passed), (1, 0))
        self.assertEqual(readiness.images[0].business_summary.evidence_status, "provider_generated")
        self.commands.record_analysis(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_lock_version=lock,
            submission=_submission(image.image_id, image.image_sha256, confirmed["brief_key"], snapshot),
        )
        resolved = self.pool.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot,
            expected_project_lock_version=lock,
        )
        self.assertEqual((resolved.low_confidence, resolved.passed), (0, 1))
        self.assertEqual(resolved.images[0].business_summary.evidence_status, "verified")

    def test_mixed_image_statuses_keep_pass_assets_for_partial_mainline(self):
        """青屿明星见面日：8 镜、6 张图片，注意项不得全局阻断通过图片。"""
        self.assertEqual(QINGYU_STAR_ACTIVITY_FIXTURE["project_name"], "青屿明星见面日")
        self.assertEqual(len(QINGYU_STAR_ACTIVITY_FIXTURE["shots"]), 8)
        statuses = ("pass", "pass", "pending", "low_confidence", "blocked", "failed")
        images = tuple(
            ProjectImage(
                image_id=f"qingyu-image-{index}",
                image_sha256=f"{index:064x}",
                source_links=(ProjectImageSourceLocation(source_label=f"qingyu-{index}.jpg"),),
                current_analysis=ProjectImageAnalysis(
                    analysis_version_id=f"analysis-{index}",
                    status=status,
                    evidence=ArtifactEvidence(
                        f"artifact://project-image-analysis/{index:064x}",
                        f"{index:064x}",
                    ),
                    created_by_actor_id="actor-qingyu",
                    created_at="2026-09-01T00:00:00Z",
                ),
                business_summary=ProjectImageBusinessSummary(
                    caption=f"青屿见面日图片 {index}", tags=("明星活动",),
                    subjects=("明星",), scene="见面日现场", purpose="活动视频",
                    quality="清晰", brand="未见冲突品牌", evidence_status="verified",
                ),
            )
            for index, status in enumerate(statuses, start=1)
        )
        readiness = ProjectImagePlanningReadiness(
            workspace_id=self.workspace_id,
            project_id=self.first["project_id"],
            brief_version_id="brief-qingyu",
            brief_snapshot_id="snapshot-qingyu",
            total=6, passed=2, pending=1, low_confidence=1, blocked=1, failed=1,
            images=images,
        )
        with patch.object(self.pool, "resolve_for_planning", return_value=readiness), \
                patch.object(self.pool.artifacts, "read_source_image", return_value=b"image"):
            delivery = self.pool.resolve_for_delivery(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.first["project_id"],
                brief_version_id="brief-qingyu",
                brief_snapshot_id="snapshot-qingyu",
                expected_project_lock_version=0,
            )
        self.assertEqual([asset.image_id for asset in delivery.assets], [
            "qingyu-image-1", "qingyu-image-2",
        ])
        employee = self.pool.employee_readiness(readiness)
        self.assertEqual(employee["usable_count"], 2)
        self.assertEqual(employee["attention_count"], 4)
        self.assertTrue(employee["can_plan_with_usable_images"])
        self.assertTrue(employee["can_continue_text_only"])

    def test_provider_pass_remains_history_when_human_confirmation_supersedes_it(self):
        confirmed, lock = self._confirmed_first()
        snapshot, image = self._synced_image(confirmed, lock)
        provider = ProviderProjectImageAnalysisCommandService(self.pool).record_analysis(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_lock_version=lock,
            submission=ProviderImageAnalysisInput(
                image_id=image.image_id, image_sha256=image.image_sha256,
                brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot,
                status="pass", caption="会议现场中的参与者",
                tags=("会议",), subjects=("参与者",), scene="室内会场",
                purpose="可用于活动氛围镜头", quality="画面清晰",
                brand="画面中未见品牌标识", evidence=("画面可见参与者",),
                provider="minimax-cli", model="provider-managed",
                prompt_version="minimax-project-image-v2-quality-flags",
            ),
        )
        manual = self.commands.record_analysis(
            self.token, workspace_id=self.workspace_id,
            project_id=self.first["project_id"], expected_project_lock_version=lock,
            submission=_submission(
                image.image_id, image.image_sha256, confirmed["brief_key"], snapshot,
            ),
        )
        self.assertNotEqual(manual.analysis_version_id, provider.analysis_version_id)
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM project_image_analysis_versions WHERE project_image_id=?",
                    (image.image_id,),
                ).fetchone()[0],
                2,
            )
        current = self.pool.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot,
            expected_project_lock_version=lock,
        )
        self.assertEqual(current.images[0].business_summary.evidence_status, "verified")

    def test_full_migration_sequence_and_agent_boundary(self):
        self.assertEqual([version for version, _name, _statements in _MIGRATIONS], list(range(1, 28)))
        source = Path(__file__).parents[1] / "mvp_v2" / "project_image_authority.py"
        agent_registry = Path(__file__).parents[1] / "mvp_v2" / "agent_tools.py"
        self.assertNotIn("agent_tools", source.read_text(encoding="utf-8"))
        self.assertNotIn("project_image_analysis", agent_registry.read_text(encoding="utf-8"))
        self.assertFalse({"project_image_analysis", "synchronize_project_images"} & set(TOOL_REGISTRY))
        with self.assertRaises(KeyError):
            validate_tool_input("project_image_analysis", {})
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_image_analysis_versions").fetchone()[0], 0)

    def test_sql_and_current_link_failures_compensate_staged_analysis_artifacts(self):
        confirmed, lock = self._confirmed_first()
        snapshot, image = self._synced_image(confirmed, lock)
        submission = _submission(image.image_id, image.image_sha256, confirmed["brief_key"], snapshot)
        for trigger, table in (
            ("force_analysis_insert_abort", "project_image_analysis_versions"),
            ("force_current_link_abort", "project_image_analysis_current_links"),
        ):
            with self.store.transaction() as connection:
                connection.execute(
                    f"CREATE TRIGGER {trigger} BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'forced'); END"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                self.commands.record_analysis(
                    self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                    expected_project_lock_version=lock, submission=submission,
                )
            self._assert_no_analysis_rows_or_files()
            with self.store.transaction() as connection:
                connection.execute(f"DROP TRIGGER {trigger}")
        with patch("mvp_v2.project_image_authority._AnalysisArtifactBatch.promote", side_effect=RuntimeError("forced")):
            with self.assertRaisesRegex(RuntimeError, "forced"):
                self.commands.record_analysis(
                    self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                    expected_project_lock_version=lock, submission=submission,
                )
        self._assert_no_analysis_rows_or_files()

    def test_stale_unreferenced_staging_is_not_readable_and_restart_cleans_only_it(self):
        batch = self.pool.artifacts.stage_analysis({"schema_version": "mplus-project-image-analysis/v1", "crash": "anonymous"})
        self.assertFalse((self.root / "data" / "artifacts" / "project-image-analysis" / batch.digest[:2] / batch.digest).exists())
        with self.assertRaisesRegex(WorkspaceServiceError, "图片 artifact 缺失"):
            self.pool.artifacts.read_analysis(batch.evidence)
        os.utime(batch.staged, (0, 0))
        ProjectImagePoolService(WorkspaceStore(self.store.database_path), data_root=self.root / "data")
        self.assertFalse(batch.staged.exists())

    def test_promoted_before_uncommitted_crash_is_cleaned_after_stale_threshold(self):
        batch = self.pool.artifacts.stage_analysis({"schema_version": "mplus-project-image-analysis/v1", "crash": "after-promote"})
        batch.promote()
        self.assertTrue(batch.final_path.is_file())
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_image_analysis_versions WHERE artifact_sha256=?", (batch.digest,)).fetchone()[0], 0)
        ProjectImagePoolService(WorkspaceStore(self.store.database_path), data_root=self.root / "data")
        self.assertTrue(batch.staged.exists(), "并发处理中、未超过阈值的 staging 不得清理")
        self.assertTrue(batch.final_path.exists(), "并发处理中、未超过阈值的同 SHA 正式文件不得清理")
        os.utime(batch.staged, (0, 0))
        os.utime(batch.final_path, (0, 0))
        ProjectImagePoolService(WorkspaceStore(self.store.database_path), data_root=self.root / "data")
        self.assertFalse(batch.staged.exists())
        self.assertFalse(batch.final_path.exists())

    def test_planning_summary_rejects_unknown_type_scope_actor_and_time_fields(self):
        confirmed, lock = self._confirmed_first()
        snapshot, image = self._synced_image(confirmed, lock)
        analysis = self.commands.record_analysis(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_lock_version=lock,
            submission=_submission(image.image_id, image.image_sha256, confirmed["brief_key"], snapshot),
        )
        payload = self.pool.artifacts.read_analysis(analysis.evidence)
        self.assertTrue(_utc_timestamp("2026-08-14T01:02:03Z"))
        for digits in range(1, 7):
            self.assertTrue(_utc_timestamp(f"2026-08-14T01:02:03.{('123456')[:digits]}Z"))
        mutations = []
        unknown = deepcopy(payload); unknown["unexpected"] = "forbidden"; mutations.append(unknown)
        wrong_type = deepcopy(payload); wrong_type["tags"] = "发布"; mutations.append(wrong_type)
        wrong_scope = deepcopy(payload); wrong_scope["project_id"] = self.second["project_id"]; mutations.append(wrong_scope)
        wrong_actor = deepcopy(payload); wrong_actor["reviewed_by_actor_id"] = "actor_forged"; mutations.append(wrong_actor)
        for invalid_time in ("not-a-time", "2026-08-14Z", "2026-08-14T00:00:00+00:00", " 2026-08-14T00:00:00Z"):
            wrong_time = deepcopy(payload); wrong_time["reviewed_at"] = invalid_time; mutations.append(wrong_time)
        for value in mutations:
            with self.subTest(keys=sorted(value)):
                with patch.object(self.pool.artifacts, "read_analysis", return_value=value):
                    with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_ANALYSIS_ARTIFACT_DRIFT"):
                        self.pool.resolve_for_planning(
                            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot,
                            expected_project_lock_version=lock,
                        )

    def test_source_filename_never_falls_back_to_internal_document_id(self):
        confirmed, lock = self._confirmed_first()
        snapshot, _image = self._synced_image(confirmed, lock)
        with self.store.transaction() as connection:
            connection.execute("DROP TRIGGER project_files_update_immutable")
            file_id = connection.execute(
                """SELECT f.file_id FROM project_files f JOIN document_images d ON d.file_id=f.file_id
                   WHERE f.project_id=? ORDER BY f.filename LIMIT 1""",
                (self.first["project_id"],),
            ).fetchone()[0]
        for filename in (
            "/private/secret.png", "folder/secret.png", "..\\secret.png", "bad\x00name.png",
            "bad\x7fname.png", "bad\x85name.png", "bad\u202ename.png",
        ):
            with self.store.transaction() as connection:
                connection.execute("UPDATE project_files SET filename=? WHERE file_id=?", (filename, file_id))
            with self.assertRaisesRegex(WorkspaceServiceError, "INTEGRITY_ERROR") as raised:
                self.pool.resolve_for_planning(
                    self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                    brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot,
                    expected_project_lock_version=lock,
                )
            self.assertNotIn("source_doc_", str(raised.exception))

    def _synced_image(self, confirmed, lock):
        snapshot = self._snapshot_id()
        self.pool.synchronize_confirmed_brief(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=lock,
        )
        pending = self.pool.resolve_for_planning(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=confirmed["brief_key"], brief_snapshot_id=snapshot, expected_project_lock_version=lock,
        )
        return snapshot, pending.images[0]

    def _assert_no_analysis_rows_or_files(self):
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_image_analysis_versions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_image_analysis_current_links").fetchone()[0], 0)
        root = self.root / "data" / "artifacts" / "project-image-analysis"
        self.assertEqual([path for path in root.rglob("*") if path.is_file()], [])

    def _confirmed_first(self):
        imported = self.adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], expected_project_version=0,
            uploads=_representative_uploads(),
        )
        confirmed = self.adapter.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"], brief_version_id=imported["brief_key"],
            confirmed_at="2026-08-14T00:00:00Z", expected_project_version=1,
        )
        return confirmed, 2

    def _snapshot_id(self):
        with self.store.connection() as connection:
            return str(connection.execute("SELECT current_brief_snapshot_id FROM projects WHERE project_id=?", (self.first["project_id"],)).fetchone()[0])


def _submission(image_id, image_sha256, brief_version_id, brief_snapshot_id):
    return ManualImageAnalysisInput(
        image_id=image_id, image_sha256=image_sha256, brief_version_id=brief_version_id, brief_snapshot_id=brief_snapshot_id,
        status="pass", caption="产品发布现场的真实画面", tags=("发布",), subjects=("产品",), scene="发布会现场",
        purpose="用于脚本镜头", quality="清晰", brand="匿名品牌", evidence=("人工核验",),
    )


def _representative_uploads():
    png = _image_bytes()
    return [
        _upload("brief.pdf", _pdf("项目名称: 匿名项目\n风格调性: 克制真实\n视频目标: 说明发布重点\n必须出现的内容: 产品演示\n关键事件与流程: 嘉宾发布\n禁止或不能出现的内容: 虚构")),
        _upload("brief.docx", _office("word/document.xml", "<w:document xmlns:w=\"w\"><w:body><w:p><w:r><w:t>品牌: 匿名品牌</w:t></w:r></w:p></w:body></w:document>", {"word/media/image1.png": png})),
        _upload("brief.xlsx", _xlsx()),
        _upload("brief.pptx", _office("ppt/slides/slide1.xml", "<p:sld xmlns:p=\"p\"><p:spTree><p:txBody><a:p xmlns:a=\"a\"><a:r><a:t>参考方案: 主视觉</a:t></a:r></a:p></p:txBody></p:spTree></p:sld>")),
        _upload("hero.png", png),
    ]


def _upload(filename, value):
    return UploadedDocument(filename, BytesIO(value))


def _image_bytes():
    output = BytesIO(); Image.new("RGB", (24, 16), "blue").save(output, format="PNG"); return output.getvalue()


def _xlsx():
    workbook = Workbook(); sheet = workbook.active; sheet.title = "资料"
    for row, value in enumerate((
        "脚本类型: 发布会", "风格调性: 克制真实", "视频目标: 说明发布重点",
        "必须出现: 产品演示", "关键事件与流程: 嘉宾发布", "禁止或不能出现的内容: 虚构",
    ), start=1):
        sheet.cell(row, 1, value)
    output = BytesIO(); workbook.save(output); return output.getvalue()


def _office(member, content, extra=None):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        archive.writestr(member, content)
        for name, value in (extra or {}).items(): archive.writestr(name, value)
    return output.getvalue()


def _pdf(text):
    writer = PdfWriter(); page = writer.add_blank_page(width=300, height=300)
    stream = DecodedStreamObject(); stream.set_data(f"BT /F1 12 Tf 10 280 Td ({text}) Tj ET".encode("latin-1", "replace"))
    resources = DictionaryObject(); resources[NameObject("/Font")] = DictionaryObject(); page[NameObject("/Resources")] = resources
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO(); writer.write(output); return output.getvalue()
