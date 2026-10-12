from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from openpyxl.drawing.image import Image as SpreadsheetImage
from PIL import Image

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.asset_package import bundle_asset_package_v2, create_asset_package, load_asset_package
from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.executable_agent import ExecutableAgentServices
from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.formal_image_candidates import search_project_candidates
from mvp_v2.formal_image_workspace import (
    FormalImageWorkspaceService,
    FormalImageWorkspaceServices,
    ImageShotSelection,
    ImageVersionMaterializer,
)
from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_image_authority import (
    ManualImageAnalysisInput,
    ManualProjectImageAnalysisCommandService,
    ProjectImagePlanningReadiness,
    ProjectImagePoolService,
)
from mvp_v2.project_service import ProjectService
from mvp_v2.package_registry import build_package_record, package_record_sha256
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.run_contract import sha256_file
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_service import VersionService
from mvp_v2.visual_function import ALLOWED_FUNCTIONS_BY_SLOT, desired_visual_functions
from mvp_v2.workspace_store import WorkspaceServiceError, new_id
from tests import test_mvp_v2_formal_script_workspace as script_workspace_tests
from tests.test_mvp_v2_formal_agent import FakeFormalGeneration
from tests.test_mvp_v2_asset_package import _write_source_with_reference_image


class FormalImageWorkspaceTests(unittest.TestCase):
    def setUp(self):
        base = script_workspace_tests.FormalScriptWorkspaceTests("runTest")
        base.setUp()
        self.addCleanup(base.doCleanups)
        self.base = base
        self.store = base.store
        self.projects = ProjectService(self.store)
        self.resolver = RunArtifactResolver(base.generated.root / "runs")
        self.delivery = VersionDeliveryService(self.store, self.resolver)
        self.pool = ProjectImagePoolService(
            self.store, data_root=base.generated.root / "runtime",
        )
        self.project_id = base.generated.project["project_id"]
        self.workspace_id = base.generated.workspace_id
        self.token = base.generated.token
        # 每个镜头功能准备两张通过视觉核验的候选，验证真实多候选逐镜检索。
        self._install_pass_images(36)
        self.workspace = FormalImageWorkspaceService(
            FormalImageWorkspaceServices(
                store=self.store,
                projects=self.projects,
                delivery=self.delivery,
                artifacts=self.resolver,
                images=self.pool,
            ),
            runs_root=base.generated.root / "runs",
            source_root=base.generated.source_root,
        )
        base.workspace.services = replace(
            base.workspace.services, image_workspace=self.workspace,
        )

    def test_multishot_search_reuses_verified_version_and_rejects_artifact_drift(self):
        current = self.workspace.read_current(self.token, workspace_id=self.workspace_id, project_id=self.project_id)
        scope = dict(workspace_id=self.workspace_id, project_id=self.project_id,
                     expected_project_version=current["project_version"], base_version_id=current["base_version_id"], query="")
        with patch.object(self.delivery, "_validate_stage_chain", wraps=self.delivery._validate_stage_chain) as validate:
            results = [self.workspace.search_shot(self.token, **scope, shot_no=shot["shot_no"])
                       for shot in current["shots"][:3]]
        self.assertEqual(len(results), 3)
        self.assertEqual(validate.call_count, 1)
        context = self.delivery.resolve(self.token, self.project_id, current["base_version_id"],
            expected_project_version=current["project_version"], reuse_immutable=True)
        stage = self.resolver.resolve_stage_artifact(context.run_id, "script_draft").path
        stage.write_text(stage.read_text() + " ")
        with self.assertRaisesRegex(WorkspaceServiceError, "HASH_MISMATCH"):
            self.workspace.search_shot(self.token, **scope, shot_no=current["shots"][0]["shot_no"])

    def test_adopted_package_image_remains_a_source_reference_candidate(self):
        path = self.base.generated.root / "adopted-source-reference.png"
        Image.new("RGB", (1280, 720), "#334155").save(path)
        asset = SimpleNamespace(
            image_id="project_image_adopted01",
            image_sha256=sha256(path.read_bytes()).hexdigest(),
            controlled_path=path,
            source_links=(SimpleNamespace(
                source_label="M+明星活动类通用脚本库.xlsx",
                sheet="创意库",
                row=17,
                image=None,
                source_document_id="package:star-activity-core@4:media-adopted01",
            ),),
            business_summary=SimpleNamespace(
                caption="明星在品牌活动舞台向观众挥手",
                scene="明星活动舞台",
                purpose="明星亮相",
                subjects=("明星", "舞台", "观众"),
                tags=("明星活动", "舞台", "挥手"),
                brand="兰蔻",
                quality="资产包校验通过",
                quality_flags=(),
                negative_flags=(),
                pixel_width=1280,
                pixel_height=720,
                perceptual_hash="",
            ),
        )

        result = search_project_candidates(
            slot={"slot_index": 1, "画面描述": "明星在活动舞台向观众挥手"},
            brief={
                "脚本类型": "明星活动",
                "品牌或客户": "青屿香氛",
                "禁忌或不能出现的内容": "不得出现竞品品牌",
            },
            assets=(asset,),
        )

        self.assertEqual(len(result["candidates"]), 1, result)
        self.assertTrue(result["candidates"][0]["brand_warning"])

    def test_save_uses_frozen_parent_source_for_fork_and_workbook_without_alias_resolution(self):
        resolved_types = []

        def resolve_source(script_type):
            resolved_types.append(script_type)
            if len(resolved_types) > 1:
                raise AssertionError("同一图片版本保存不得二次解析素材包")
            return self.base.generated.source_root

        workspace = FormalImageWorkspaceService(
            self.workspace.services,
            runs_root=self.base.generated.root / "runs",
            source_root=self.base.generated.root / "missing-fallback-source",
            source_root_resolver=resolve_source,
        )
        current = workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        saved = workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=current["project_version"],
            selections=tuple(
                ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                for item in current["shots"]
            ),
            idempotency_key="image-routed-source-save",
        )

        self.assertEqual(saved["save_status"], "completed")
        self.assertEqual(resolved_types, [])

    def test_unsaved_script_draft_blocks_image_version_without_hidden_draft_or_artifacts(self):
        current = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        script_current = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        changed = self.base.workspace.apply_table_change(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=current["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=script_current["shots"][0]["shot_id"],
            field="subtitle",
            value="未保存脚本不得被图片版本切换隐藏",
            idempotency_key="image-save-open-script-draft-1",
        )
        before_project = self.projects.get_project(self.token, self.project_id)
        before_versions = self.projects.list_project_versions(self.token, self.project_id)
        before_runs = {path.name for path in (self.base.generated.root / "runs").iterdir()}

        with self.assertRaises(WorkspaceServiceError) as raised:
            self.workspace.save_version(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=current["project_version"],
                selections=tuple(
                    ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                    for item in current["shots"]
                ),
                idempotency_key="image-save-open-script-draft-2",
            )

        self.assertEqual(raised.exception.code, "SCRIPT_DRAFT_NOT_SAVED")
        self.assertEqual(
            self.projects.get_project(self.token, self.project_id)["current_version_id"],
            before_project["current_version_id"],
        )
        self.assertEqual(
            len(self.projects.list_project_versions(self.token, self.project_id)),
            len(before_versions),
        )
        self.assertEqual(
            {path.name for path in (self.base.generated.root / "runs").iterdir()},
            before_runs,
        )
        recovered = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(recovered["revision"], changed["revision"])
        self.assertEqual(recovered["shots"][0]["subtitle"], "未保存脚本不得被图片版本切换隐藏")

    def test_discarded_script_draft_does_not_block_image_version(self):
        current = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        script_current = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        changed = self.base.workspace.apply_table_change(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=current["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=script_current["shots"][0]["shot_id"],
            field="subtitle",
            value="明确放弃后不应阻断图片版本",
            idempotency_key="image-save-discarded-script-1",
        )
        discarded = self.base.workspace.discard_changes(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=current["project_version"],
            expected_revision=changed["revision"],
            idempotency_key="image-save-discarded-script-2",
        )
        self.assertFalse(discarded["can_save_version"])

        saved = self.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=current["project_version"],
            selections=tuple(
                ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                for item in current["shots"]
            ),
            idempotency_key="image-save-discarded-script-3",
        )
        self.assertEqual(saved["save_status"], "completed")

    def test_script_child_inherits_only_unaffected_image_decisions(self):
        initial = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        initial_script = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual([s.get("shot_id") for s in initial["shots"]],
                         [s["shot_id"] for s in initial_script["shots"]])
        image_version = self.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=initial["project_version"],
            selections=tuple(
                ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                for item in initial["shots"]
            ),
            idempotency_key="inherit-images-parent-1",
        )
        script_current = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        changed = self.base.workspace.apply_table_change(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=image_version["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=script_current["shots"][0]["shot_id"],
            field="visual",
            value="第一镜改为地面人物特写，突出入场动作。",
            idempotency_key="inherit-images-script-change-1",
        )
        versions_before = self.projects.list_project_versions(self.token, self.project_id)
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_CONFIRMATION_REQUIRED"):
            self.base.workspace.save_version(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                expected_project_version=image_version["project_version"],
                expected_revision=changed["revision"], reason="等待第一镜重新核对",
                idempotency_key="inherit-images-incomplete",
            )
        self.assertEqual(self.projects.list_project_versions(self.token, self.project_id), versions_before)
        progress = self.workspace.read_progress(self.token, workspace_id=self.workspace_id, project_id=self.project_id)
        self.assertEqual(progress["base_version_id"], image_version["base_version_id"])
        target = script_current["shots"][0]
        searched = self.workspace.search_shot(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            base_version_id=image_version["base_version_id"],
            expected_project_version=image_version["project_version"], shot_no="1", query="",
        )
        image_key = image_version["shots"][0]["selected_image_key"]
        self.assertIn(image_key, {item["image_key"] for item in searched["candidates"]})
        self.workspace.save_progress(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            base_version_id=image_version["base_version_id"],
            expected_project_version=image_version["project_version"], expected_revision=progress["revision"],
            expected_script_revision=changed["revision"],
            selections=[dict(shot_id=target["shot_id"], image_key=image_key, checked=True,
                             reuse_reason="已按新画面核对第一镜")],
            idempotency_key="inherit-images-reconfirm-first",
        )
        self.base.workspace.save_version(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=image_version["project_version"], expected_revision=changed["revision"],
            reason="文字与已重核配图统一保存", idempotency_key="inherit-images-script-save-1",
        )
        projected = self.workspace.read_current(self.token, workspace_id=self.workspace_id, project_id=self.project_id)
        self.assertEqual(len(self.projects.list_project_versions(self.token, self.project_id)), len(versions_before) + 1)
        self.assertEqual([s["shot_id"] for s in projected["shots"]],
                         [s["shot_id"] for s in initial_script["shots"]])
        self.assertEqual(projected["state"], "confirmed")
        self.assertTrue(projected["review_eligible"])
        self.assertEqual(projected["pending_reconfirmation_shot_nos"], [])
        self.assertTrue(projected["shots"][0]["confirmed"])
        self.assertEqual([s["selected_image_key"] for s in projected["shots"][1:]],
                         [s["selected_image_key"] for s in image_version["shots"][1:]])
        parent_context = self.delivery.resolve(self.token, self.project_id, image_version["base_version_id"],
                                               ancestor_of=projected["base_version_id"])
        child_context = self.delivery.resolve(self.token, self.project_id, projected["base_version_id"])
        before = self.workspace._read_snapshot(parent_context)
        after = self.workspace._read_snapshot(child_context)
        for no in before:
            if no != "1":
                for key, value in before[no].items():
                    if key not in {"decision_id", "confirmation_scope"}:
                        self.assertEqual(after[no].get(key), value, (no, key))

    def test_explicit_single_image_scope_preserves_other_decisions_and_rejects_extra_targets(self):
        initial = self.workspace.read_current(self.token, workspace_id=self.workspace_id, project_id=self.project_id)
        saved = self.workspace.save_version(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=initial["project_version"],
            selections=tuple(ImageShotSelection(s["shot_no"], s["selected_image_key"], True) for s in initial["shots"]),
            idempotency_key="scope-image-baseline",
        )
        target = saved["shots"][2]
        other_images = {shot["selected_image_key"] for shot in saved["shots"] if shot["shot_no"] != "3"}
        selected = next(c["image_key"] for c in target["candidates"]
                        if c["image_key"] != target["selected_image_key"] and c["image_key"] in other_images)
        selection = ImageShotSelection("3", selected, True, candidate_snapshot_digest=target["candidate_snapshot_digest"], reuse_reason="本镜为动作细节，允许与同场景参考复用")
        arguments = dict(workspace_id=self.workspace_id, project_id=self.project_id,
                         base_version_id=saved["base_version_id"], expected_project_version=saved["project_version"],
                         allowed_shot_ids=[target["shot_id"]])
        context = self.delivery.resolve(self.token, self.project_id, saved["base_version_id"])
        before = self.workspace._read_snapshot(context)
        with self.assertRaises(WorkspaceServiceError) as failure:
            self.workspace.preview_version(self.token, **arguments, selections=(selection, ImageShotSelection(
                "1", saved["shots"][0]["selected_image_key"], True,
                candidate_snapshot_digest=saved["shots"][0]["candidate_snapshot_digest"],
            )), idempotency_key="scope-extra-target")
        self.assertEqual(failure.exception.code, "IMAGE_SCOPE_MISMATCH")
        self.assertEqual(self.projects.get_project(self.token, self.project_id)["project_version"], saved["project_version"])
        with self.assertRaises(WorkspaceServiceError) as reused:
            self.workspace.preview_version(self.token, **arguments, selections=(
                replace(selection, reuse_reason=""),), idempotency_key="scope-reuse-without-reason")
        self.assertEqual(reused.exception.code, "IMAGE_REUSE_REASON_REQUIRED")
        proposal = self.workspace.preview_version(self.token, **arguments, selections=(selection,), idempotency_key="scope-only-third")
        self.assertEqual(proposal["affected_shots"], ["3"])
        self.assertEqual(proposal["allowed_shot_ids"], [target["shot_id"]])
        changed = self.workspace.confirm_version(self.token, proposal_id=proposal["proposal_id"], proposal_digest=proposal["proposal_digest"])
        context = self.delivery.resolve(self.token, self.project_id, changed["base_version_id"])
        after = self.workspace._read_snapshot(context)
        self.assertNotEqual(after["3"]["image_id"], before["3"]["image_id"])
        for shot_no, decision in before.items():
            if shot_no != "3":
                self.assertEqual(after[shot_no], decision, shot_no)
        self.assertEqual([s["shot_id"] for s in changed["shots"]], [s["shot_id"] for s in saved["shots"]])

    def test_copy_only_script_child_remains_review_eligible_with_inherited_images(self):
        initial = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        script_initial = self.base.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=initial["project_version"],
            selections=tuple(
                ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                for item in initial["shots"]
            ),
            idempotency_key="inherit-copy-parent-images-1",
        )
        changed = self.base.workspace.apply_table_change(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=self.projects.get_project(
                self.token, self.project_id,
            )["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=script_initial["shots"][0]["shot_id"],
            field="subtitle",
            value="青屿明星见面日，今夜见。",
            idempotency_key="inherit-copy-subtitle-change-1",
        )
        self.base.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=self.projects.get_project(
                self.token, self.project_id,
            )["project_version"],
            expected_revision=changed["revision"],
            reason="仅修改字幕，图片决定应完整继承",
            idempotency_key="inherit-copy-script-save-1",
        )

        restarted = FormalImageWorkspaceService(
            self.workspace.services,
            runs_root=self.base.generated.root / "runs",
            source_root=self.base.generated.source_root,
        )
        projected = restarted.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )

        self.assertEqual(projected["state"], "confirmed")
        self.assertTrue(projected["review_eligible"])
        self.assertTrue(projected["delivery_eligible"])
        self.assertFalse(projected["requires_exact_snapshot_save"])
        self.assertEqual(projected["pending_reconfirmation_shot_nos"], [])
        self.assertEqual(
            sum(item["confirmation_scope"] == "exact" for item in projected["shots"]),
            projected["shot_count"],
        )

    def test_mixed_image_statuses_keep_formal_workspace_on_pass_pool(self):
        original = self.pool.resolve_for_planning

        def mixed_readiness(*args, **kwargs):
            readiness = original(*args, **kwargs)
            images = tuple(replace(
                image,
                current_analysis=(
                    replace(image.current_analysis, status=status)
                    if index >= 2 and image.current_analysis is not None else image.current_analysis
                ),
            ) for index, (image, status) in enumerate(
                zip(readiness.images[:6], ("pass", "pass", "pending", "low_confidence", "blocked", "failed")),
                start=1,
            ))
            return ProjectImagePlanningReadiness(
                readiness.workspace_id, readiness.project_id,
                readiness.brief_version_id, readiness.brief_snapshot_id,
                total=6, passed=2, pending=1, low_confidence=1, blocked=1, failed=1,
                images=images,
            )

        with patch.object(self.pool, "resolve_for_planning", side_effect=mixed_readiness):
            current = self.workspace.read_current(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            )
        self.assertEqual(current["available_image_count"], 2)
        self.assertEqual(current["pending_image_count"], 1)
        self.assertEqual(current["low_confidence_image_count"], 1)
        self.assertEqual(current["blocked_image_count"], 1)
        self.assertEqual(current["failed_image_count"], 1)
        self.assertIn("已通过的 2 张仍可继续匹配", current["unavailable_asset_warning"])

    def test_confirm_failure_records_terminal_task_and_allows_new_retry_proposal(self):
        current = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        selections = tuple(
            ImageShotSelection(
                item["shot_no"], item["selected_image_key"], True,
                candidate_snapshot_digest=item["candidate_snapshot_digest"],
            )
            for item in current["shots"]
        )
        proposal = self.workspace.preview_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            base_version_id=current["base_version_id"],
            expected_project_version=current["project_version"],
            selections=selections,
            idempotency_key="image-materialize-failure-1",
        )
        versions_before = self.projects.list_project_versions(
            self.token, self.project_id,
        )
        with patch.object(
            self.workspace.materializer,
            "materialize",
            side_effect=ValueError("anonymous materializer failure"),
        ):
            with self.assertRaises(WorkspaceServiceError) as failed:
                self.workspace.confirm_version(
                    self.token,
                    proposal_id=proposal["proposal_id"],
                    proposal_digest=proposal["proposal_digest"],
                )
        self.assertEqual(failed.exception.code, "IMAGE_VERSION_SAVE_FAILED")
        task = TaskService(self.store).get_task(self.token, proposal["proposal_id"])
        self.assertEqual((task["status"], task["error_code"]), (
            "failed", "IMAGE_VERSION_SAVE_FAILED",
        ))
        self.assertEqual(
            self.projects.list_project_versions(self.token, self.project_id),
            versions_before,
        )
        retry = self.workspace.preview_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            base_version_id=current["base_version_id"],
            expected_project_version=current["project_version"],
            selections=selections,
            idempotency_key="image-materialize-failure-retry-2",
        )
        self.assertNotEqual(retry["proposal_id"], proposal["proposal_id"])

    def test_history_image_feedback_excludes_exact_current_image_and_completes_with_child(self):
        initial = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        first = self.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=initial["project_version"],
            selections=tuple(
                ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                for item in initial["shots"]
            ),
            idempotency_key="history-image-initial-version",
        )
        target = first["shots"][0]
        old_image_key = target["selected_image_key"]
        revision = FormalScriptRevisionService(
            projects=self.projects,
            tasks=TaskService(self.store),
            workspace=self.base.workspace,
            writes_enabled=True,
            image_workspace=self.workspace,
        )
        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=LocalIdentityAdapter(self.store),
            projects=self.projects,
            tasks=TaskService(self.store),
            versions=VersionService(self.store),
            artifacts=self.resolver,
        ))
        agent = FormalExecutableAgentService(
            ExecutableAgentServices(
                readonly=readonly,
                projects=self.projects,
                tasks=TaskService(self.store),
                confirmations=ActionConfirmationService(self.store, b"h" * 32),
                idempotency=ToolIdempotencyService(self.store),
                brief_store=BriefArtifactStore(self.base.generated.root / "data"),
                backend=FormalExecutionBackend(),
            ),
            generation=FakeFormalGeneration(),
            advisor=None,
            writes_enabled=True,
            script_revision=revision,
        )
        client = TestClient(create_app(
            agent_api=agent,
            formal_image_service=self.workspace,
        ))
        self.addCleanup(client.close)
        headers = {
            "X-MPlus-Agent-Auth": self.token,
            "X-MPlus-UI-Token": client.app.state.session_token,
        }
        project = self.projects.get_project(self.token, self.project_id)
        proposal = client.post(
            f"/api/formal/projects/{self.project_id}/image-workspace/feedback-proposals"
            f"?workspace_id={self.workspace_id}",
            headers=headers,
            json={
                "expected_project_version": project["project_version"],
                "shot_no": target["shot_no"],
                "instruction": f"第 {target['shot_no']} 镜原图不合适，改用安全候选",
                "idempotency_key": "history-image-feedback-1",
            },
        )
        self.assertEqual(proposal.status_code, 200, proposal.text)
        proposed = proposal.json()
        source_task_id = proposed["task_id"]
        self.assertIn(f"revision_task_id={source_task_id}", proposed["response"]["workspace_url"])
        repeated_proposal = client.post(
            f"/api/formal/projects/{self.project_id}/image-workspace/feedback-proposals"
            f"?workspace_id={self.workspace_id}",
            headers=headers,
            json={
                "expected_project_version": project["project_version"],
                "shot_no": target["shot_no"],
                "instruction": f"第 {target['shot_no']} 镜原图不合适，改用安全候选",
                "idempotency_key": "history-image-feedback-1",
            },
        )
        self.assertEqual(repeated_proposal.status_code, 200, repeated_proposal.text)
        self.assertEqual(repeated_proposal.json()["task_id"], source_task_id)
        search = client.get(
            f"/api/formal/projects/{self.project_id}/image-workspace/shots/{target['shot_no']}/candidates",
            headers=headers,
            params={
                "workspace_id": self.workspace_id,
                "base_version_id": first["base_version_id"],
                "expected_project_version": first["project_version"],
                "query": "排除原图并重新匹配",
                "source_revision_task_id": source_task_id,
            },
        )
        self.assertEqual(search.status_code, 200, search.text)
        searched = search.json()
        self.assertNotIn(old_image_key, {item["image_key"] for item in searched["candidates"]})
        self.assertTrue(searched["candidates"])
        selected_candidate = searched["candidates"][0]
        explanation = selected_candidate["match_explanation"]
        self.assertEqual(explanation["total_score"], selected_candidate["score"])
        self.assertEqual(explanation["components"], selected_candidate["rank_components"])
        self.assertEqual(explanation["sources"], selected_candidate["sources"])
        for field in (
            "semantic", "safety", "reuse", "near_duplicate", "recommendation",
        ):
            self.assertTrue(explanation[field])
        selected_replacement = selected_candidate["image_key"]
        replacement_cluster = str(
            selected_candidate.get("perceptual_cluster_id") or selected_replacement
        )
        replacement_reuse_reason = (
            "历史反馈后仅该感知近似候选满足安全门，已人工核对必要复用"
            if int(selected_candidate.get("reuse_count") or 0) > 0 else ""
        )
        selections = []
        for item in first["shots"]:
            if item["shot_no"] == target["shot_no"]:
                selections.append({
                    "shot_no": item["shot_no"],
                    "image_key": selected_replacement,
                    "confirmed": True,
                    "candidate_snapshot_digest": searched["candidate_snapshot_digest"],
                    "query": "排除原图并重新匹配",
                    "reuse_reason": replacement_reuse_reason,
                })
            else:
                selected_cluster = next((
                    str(candidate.get("perceptual_cluster_id") or "")
                    for candidate in item.get("candidates") or []
                    if candidate.get("image_key") == item["selected_image_key"]
                ), "")
                existing_reuse_reason = item.get("reuse_reason") or ""
                if selected_cluster == replacement_cluster and not existing_reuse_reason:
                    existing_reuse_reason = (
                        "历史反馈后该感知簇与目标镜复用，已逐镜核对本镜继续保留"
                    )
                selections.append({
                    "shot_no": item["shot_no"],
                    "image_key": item["selected_image_key"],
                    "confirmed": True,
                    "candidate_snapshot_digest": item["candidate_snapshot_digest"],
                    "query": "",
                    "reuse_reason": existing_reuse_reason,
                })
        preview = client.post(
            f"/api/formal/projects/{self.project_id}/image-workspace/proposals"
            f"?workspace_id={self.workspace_id}",
            headers=headers,
            json={
                "base_version_id": first["base_version_id"],
                "expected_project_version": first["project_version"],
                "selections": selections,
                "idempotency_key": "history-image-child-1",
                "source_revision_task_id": source_task_id,
            },
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        previewed = preview.json()
        if int(selected_candidate.get("reuse_count") or 0) > 0:
            self.assertTrue(replacement_reuse_reason)
        confirmed = client.post(
            f"/api/formal/projects/{self.project_id}/image-workspace/proposals/"
            f"{previewed['proposal_id']}/confirm?workspace_id={self.workspace_id}",
            headers=headers,
            json={"proposal_digest": previewed["proposal_digest"]},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        version_count = len(self.projects.list_project_versions(self.token, self.project_id))
        replayed_confirmation = client.post(
            f"/api/formal/projects/{self.project_id}/image-workspace/proposals/"
            f"{previewed['proposal_id']}/confirm?workspace_id={self.workspace_id}",
            headers=headers,
            json={"proposal_digest": previewed["proposal_digest"]},
        )
        self.assertEqual(replayed_confirmation.status_code, 200, replayed_confirmation.text)
        self.assertEqual(
            len(self.projects.list_project_versions(self.token, self.project_id)),
            version_count,
        )
        final_project = self.projects.get_project(self.token, self.project_id)
        self.assertNotEqual(final_project["current_version_id"], first["base_version_id"])
        source_task = TaskService(self.store).get_task(self.token, source_task_id)
        self.assertEqual(source_task["status"], "completed")
        with self.store.connection() as connection:
            feedback = connection.execute(
                """SELECT child_version_id, payload_ref, payload_sha256
                FROM formal_version_feedback_records
                WHERE project_id=? AND feedback_kind='image' AND child_version_id=?""",
                (self.project_id, final_project["current_version_id"]),
            ).fetchone()
        self.assertIsNotNone(feedback)
        after = self.workspace.search_shot(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            shot_no=target["shot_no"],
            query="",
            base_version_id=final_project["current_version_id"],
            expected_project_version=final_project["project_version"],
        )
        self.assertNotIn(old_image_key, {item["image_key"] for item in after["candidates"]})

    def test_image_revision_scope_accepts_original_path_as_an_image_field(self):
        script_rows = [{"镜号": "1", "画面描述": "匿名现场入口"}]
        detail_rows = [{"镜号": "1", "source_refs": []}]
        parent_bindings = [{
            "shot_no": "1",
            "image_id": "",
            "original_path": "/isolated/source/template-reference.png",
            "decision": "",
            "resolution_status": "visual_resolution_required",
            "basis": "使用当前输出行对应图片",
        }]
        child_bindings = [{
            "shot_no": "1",
            "image_id": "project_image_anonymous",
            "original_path": "",
            "decision": "人工确认",
            "resolution_status": "resolved",
            "basis": "项目经理逐镜人工确认",
        }]

        report = ImageVersionMaterializer._revision_report(
            context=SimpleNamespace(
                project_id="project_anonymous",
                version_id="version_parent",
            ),
            child_version_id="version_child",
            script_rows=script_rows,
            detail_rows=detail_rows,
            parent_bindings=parent_bindings,
            child_bindings=child_bindings,
            request_sha256="a" * 64,
        )

        self.assertEqual(report["completion"]["status"], "waiting_human_review")
        self.assertEqual(
            set(report["actual_impact"]["changed_fields"]),
            {"basis", "decision", "image_id", "original_path", "resolution_status"},
        )

    def test_local_supplement_preserves_confirmed_brief_and_requires_analysis(self):
        project = self.projects.get_project(self.token, self.project_id)
        context = self.delivery.resolve(
            self.token, self.project_id, project["current_version_id"],
            expected_project_version=project["project_version"],
        )
        before = self.workspace._brief_binding(
            context,
            json.loads(self.resolver.resolve_stage_artifact(
                context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
            ).path.read_text(encoding="utf-8"))["confirmed_brief_input"],
        )
        content = (Path(__file__).parent / "fixtures" / "v10_image_delivery" / "anonymous-conference-arrival.png").read_bytes()
        result = self.workspace.supplement_local_image(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=project["project_version"],
            filename="本地补图.png",
            content=content,
        )
        self.assertFalse(result["brief_reupload_required"])
        self.assertTrue(result["requires_visual_analysis"])
        readiness = self.pool.resolve_for_planning(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            brief_version_id=before[0],
            brief_snapshot_id=before[1],
            expected_project_lock_version=project["project_version"],
        )
        uploaded = next(item for item in readiness.images if item.image_id == result["image_key"])
        self.assertIsNone(uploaded.current_analysis)
        self.assertEqual(
            (uploaded.source_links[0].source_label, uploaded.source_links[0].sheet, uploaded.source_links[0].row),
            ("本地补图.png", "本地补图", 1),
        )

    def test_usable_image_pool_without_defaults_keeps_manual_candidates_and_accurate_message(self):
        with patch(
            "mvp_v2.formal_image_workspace.allocate_diverse_defaults",
            return_value={"selected_by_shot": {}},
        ):
            current = self.workspace.read_current(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            )

        self.assertEqual(current["available_image_count"], 36)
        self.assertEqual(current["usable_image_count"], 36)
        self.assertGreater(current["shot_count"], 1)
        self.assertEqual(len(current["shots"]), current["shot_count"])
        self.assertFalse(current["ready_to_save"])
        for shot in current["shots"]:
            self.assertEqual(shot["selected_image_key"], "")
            self.assertFalse(shot["confirmed"])
            self.assertGreaterEqual(len(shot["candidates"]), 2)
            self.assertTrue(all(candidate["image_key"] for candidate in shot["candidates"]))
        self.assertNotIn("没有可用项目图片", current["message"])
        self.assertIn("逐镜", current["message"])

    def test_confirm_n_images_create_child_replace_one_and_restart(self):
        initial = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(initial["state"], "ready")
        self.assertEqual(initial["available_image_count"], 36)
        self.assertTrue(all(len(item["candidates"]) >= 2 for item in initial["shots"]))
        self.assertEqual(initial["shot_count"], len(initial["shots"]))
        self.assertTrue(initial["ready_to_save"])
        selected = [item["selected_image_key"] for item in initial["shots"]]
        self.assertEqual(len(set(selected)), initial["shot_count"])
        employee_json = json.dumps(initial, ensure_ascii=False)
        for secret in (
            "artifact://", "plan_sha256", "source_catalog_sha256", "actor_id", "analysis_ref",
            "run_id", str(self.base.generated.root),
        ):
            self.assertNotIn(secret, employee_json)
        # The content digest is the explicit image identity used to restore a
        # library choice; provenance objects and internal paths stay private.
        self.assertTrue(all(
            re.fullmatch(r"[0-9a-f]{64}", candidate["content_sha256"])
            for shot in initial["shots"] for candidate in shot["candidates"]
        ))
        before_versions = self.projects.list_project_versions(self.token, self.project_id)
        saved = self.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=initial["project_version"],
            selections=tuple(
                ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
                for item in initial["shots"]
            ),
            idempotency_key="image-save-initial",
        )
        self.assertEqual((saved["save_status"], saved["state"]), ("completed", "confirmed"))
        self.assertTrue(all(item["confirmed"] for item in saved["shots"]))
        self.assertEqual(saved["history"][-1]["change_summary"], f"已确认 {initial['shot_count']} 镜图片")
        after_versions = self.projects.list_project_versions(self.token, self.project_id)
        self.assertEqual(len(after_versions), len(before_versions) + 1)
        self.assertEqual(after_versions[-1]["parent_version_id"], before_versions[-1]["version_id"])
        project = self.projects.get_project(self.token, self.project_id)
        context = self.delivery.resolve(
            self.token, self.project_id, project["current_version_id"],
            expected_project_version=project["project_version"],
        )
        manifest = self.resolver.read_manifest(
            context.run_id, expected_sha256=context.manifest.sha256,
        )
        self.assertEqual(manifest["metrics"]["provider_calls"], 0)
        self.assertEqual(manifest["metrics"]["image_decision_count"], initial["shot_count"])
        binding = json.loads(self.resolver.resolve_stage_artifact(
            context.run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256,
        ).path.read_text(encoding="utf-8"))
        self.assertEqual(len(binding["bindings"]), initial["shot_count"])
        self.assertEqual({item["resolution_status"] for item in binding["bindings"]}, {"resolved"})
        self.assertEqual(len({item["image_sha256"] for item in binding["bindings"]}), initial["shot_count"])
        parent_binding = json.loads(self.resolver.resolve_stage_artifact(
            before_versions[-1]["run_id"], "visual_binding",
        ).path.read_text(encoding="utf-8"))
        self.assertEqual(binding["script_rows"], parent_binding["script_rows"])
        workbook_path = self.resolver.resolve_artifact(context.run_id, "workbook").path
        workbook = load_workbook(workbook_path, read_only=False, data_only=True)
        try:
            self.assertEqual(len(workbook["脚本"]._images), initial["shot_count"])
            self.assertEqual(workbook["相邻镜头复核"].max_row - 1, initial["shot_count"] - 1)
            text = "\n".join(
                str(cell.value or "")
                for sheet in workbook.worksheets
                for row in sheet.iter_rows()
                for cell in row
            )
            self.assertNotIn(str(self.base.generated.root), text)
        finally:
            workbook.close()
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT name FROM schema_migrations WHERE version=17",
            ).fetchone()[0], "formal-capability-regression-v3")
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_image_decision_snapshots_v3",
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_image_decisions_v3",
            ).fetchone()[0], initial["shot_count"])
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_image_decision_snapshots_v2",
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_publications",
            ).fetchone()[0], 0)
        self.assertFalse((self.base.generated.root / "CURRENT").exists())
        self.assertEqual(len(self.base.calls), 2)

        restarted = FormalImageWorkspaceService(
            FormalImageWorkspaceServices(
                store=self.store,
                projects=self.projects,
                delivery=self.delivery,
                artifacts=self.resolver,
                images=ProjectImagePoolService(
                    self.store, data_root=self.base.generated.root / "runtime",
                ),
            ),
            runs_root=self.base.generated.root / "runs",
            source_root=self.base.generated.source_root,
        )
        recovered = restarted.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(recovered["state"], "confirmed")
        used = {item["selected_image_key"] for item in recovered["shots"]}
        spare = next(
            item["image_key"]
            for item in recovered["shots"][0]["candidates"]
            if item["image_key"] not in used
        )
        replacement = tuple(
            ImageShotSelection(
                item["shot_no"], spare if index == 0 else item["selected_image_key"], True,
                reuse_reason="测试 fixture 已核对感知近似复用",
            )
            for index, item in enumerate(recovered["shots"])
        )
        replaced = restarted.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=recovered["project_version"],
            selections=replacement,
            idempotency_key="image-save-replacement",
        )
        self.assertEqual(replaced["state"], "confirmed")
        versions = self.projects.list_project_versions(self.token, self.project_id)
        self.assertEqual(len(versions), len(before_versions) + 2)
        self.assertEqual(versions[-1]["parent_version_id"], versions[-2]["version_id"])
        self.assertEqual(replaced["shots"][0]["selected_image_key"], spare)
        self.assertEqual(
            [item["selected_image_key"] for item in replaced["shots"][1:]],
            [item["selected_image_key"] for item in recovered["shots"][1:]],
        )
        with self.store.connection() as connection:
            feedback = connection.execute(
                """SELECT base_version_id, child_version_id, feedback_kind,
                payload_ref, payload_sha256 FROM formal_version_feedback_records
                WHERE project_id=? ORDER BY created_at DESC LIMIT 1""",
                (self.project_id,),
            ).fetchone()
        self.assertEqual(feedback["feedback_kind"], "image")
        searched = restarted.search_shot(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            shot_no=replaced["shots"][0]["shot_no"],
            query="",
            base_version_id=versions[-1]["version_id"],
            expected_project_version=replaced["project_version"],
        )
        self.assertNotIn(
            recovered["shots"][0]["selected_image_key"],
            {item["image_key"] for item in searched["candidates"]},
        )
        self.assertEqual(len(self.base.calls), 2)

    def test_temporary_text_basis_is_persisted_blocked_and_can_later_adopt_images(self):
        current = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        versions_before = self.projects.list_project_versions(self.token, self.project_id)
        parent = versions_before[-1]
        initial_selected = {
            item["shot_no"]: item["selected_image_key"] for item in current["shots"]
        }
        mixed = []
        for index, item in enumerate(current["shots"]):
            if index == 0:
                mixed.append(ImageShotSelection(
                    item["shot_no"], "", True,
                    decision_kind="temporary_text_basis",
                    temporary_reason="现场图片待客户确认，先保留本镜文字依据",
                ))
            else:
                mixed.append(ImageShotSelection(
                    item["shot_no"], item["selected_image_key"], True,
                    candidate_snapshot_digest=item["candidate_snapshot_digest"],
                ))
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_CONFIRMATION_REQUIRED"):
            self.workspace.preview_version(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                base_version_id=current["base_version_id"], expected_project_version=current["project_version"],
                selections=tuple(mixed), idempotency_key="temporary-blocked-mixed",
            )
        progress = self.workspace.save_progress(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            base_version_id=current["base_version_id"], expected_project_version=current["project_version"],
            expected_revision=0, expected_script_revision=0,
            selections=[dict(shot_id=shot["shot_id"], image_key=item.image_key,
                             checked=item.confirmed, decision_kind=item.decision_kind,
                             temporary_reason=item.temporary_reason)
                        for shot, item in zip(current["shots"], mixed)],
            idempotency_key="temporary-progress-only",
        )
        self.assertFalse(progress["review_eligible"])
        self.assertEqual(progress["selections"][0]["temporary_reason"], "现场图片待客户确认，先保留本镜文字依据")
        self.assertEqual(self.projects.list_project_versions(self.token, self.project_id), versions_before)
        restarted = FormalImageWorkspaceService(self.workspace.services,
            runs_root=self.base.generated.root / "runs", source_root=self.base.generated.source_root)
        self.assertEqual(restarted.read_progress(self.token, workspace_id=self.workspace_id,
                         project_id=self.project_id), progress)
        recovered = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        final_selections = []
        for item in recovered["shots"]:
            searched = self.workspace.search_shot(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                shot_no=item["shot_no"],
                query="",
                base_version_id=recovered["base_version_id"],
                expected_project_version=recovered["project_version"],
            )
            final_selections.append(ImageShotSelection(
                item["shot_no"], initial_selected[item["shot_no"]], True,
                candidate_snapshot_digest=searched["candidate_snapshot_digest"],
            ))
        stale = list(final_selections)
        stale[1] = replace(stale[1], candidate_snapshot_digest="0" * 64)
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_CANDIDATE_SNAPSHOT_DRIFT"):
            self.workspace.preview_version(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                base_version_id=recovered["base_version_id"], expected_project_version=recovered["project_version"],
                selections=tuple(stale), idempotency_key="temporary-completed-stale",
            )
        final_proposal = self.workspace.preview_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            base_version_id=recovered["base_version_id"],
            expected_project_version=recovered["project_version"],
            selections=tuple(final_selections),
            idempotency_key="temporary-text-basis-complete-v3",
        )
        completed = self.workspace.confirm_version(
            self.token,
            proposal_id=final_proposal["proposal_id"],
            proposal_digest=final_proposal["proposal_digest"],
        )
        self.assertEqual(completed["state"], "confirmed")
        self.assertTrue(completed["review_eligible"])
        self.assertTrue(completed["delivery_eligible"])
        versions_after = self.projects.list_project_versions(self.token, self.project_id)
        self.assertEqual(len(versions_after), len(versions_before) + 1)
        self.assertEqual(versions_after[-1]["parent_version_id"], parent["version_id"])
        self.assertEqual(versions_after[:-1], versions_before)


    def test_reuse_requires_reason_while_stale_and_transaction_failure_write_nothing(self):
        current = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        selections = [
            ImageShotSelection(item["shot_no"], item["selected_image_key"], True)
            for item in current["shots"]
        ]
        with self.assertRaises(TypeError):
            self.workspace.save_version(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=current["project_version"],
                selections=tuple({"shot_no": item.shot_no} for item in selections),  # type: ignore[arg-type]
                idempotency_key="forged-mapping",
            )
        duplicate = list(selections)
        duplicate[1] = replace(duplicate[1], image_key=duplicate[0].image_key)
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_REUSE_REASON_REQUIRED"):
            self.workspace.save_version(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=current["project_version"],
                selections=tuple(duplicate),
                idempotency_key="duplicate-image-without-reason",
            )
        duplicate[0] = replace(
            duplicate[0], reuse_reason="该镜作为会场建立镜头保留同一视觉依据",
        )
        duplicate[1] = ImageShotSelection(
            duplicate[1].shot_no, duplicate[0].image_key, True,
            reuse_reason="无其他安全候选，明确复用同一会场视觉",
        )
        reused = self.workspace.save_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=current["project_version"],
            selections=tuple(duplicate),
            idempotency_key="duplicate-image",
        )
        self.assertEqual(reused["state"], "confirmed")
        self.assertLess(
            len({item["selected_image_key"] for item in reused["shots"]}),
            len(reused["shots"]),
        )
        current = reused
        selections = [
            ImageShotSelection(
                item["shot_no"], item["selected_image_key"], True,
                reuse_reason=item.get("reuse_reason", ""),
            )
            for item in current["shots"]
        ]
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.workspace.save_version(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=current["project_version"] - 1,
                selections=tuple(selections),
                idempotency_key="stale-image-save",
            )
        spare = next(
            item["image_key"] for item in current["shots"][0]["candidates"]
            if item["image_key"] != current["shots"][0]["selected_image_key"]
        )
        selections = [
            replace(
                item,
                reuse_reason=item.reuse_reason or "事务回滚测试已显式核对感知近似复用",
            )
            for item in selections
        ]
        selections[0] = replace(selections[0], image_key=spare)
        before = self._counts()
        with self.store.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER test_image_snapshot_abort
                BEFORE INSERT ON version_image_decision_snapshots_v3
                BEGIN SELECT RAISE(ABORT, 'TEST_IMAGE_SNAPSHOT_ABORT'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "TEST_IMAGE_SNAPSHOT_ABORT"):
            self.workspace.save_version(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=current["project_version"],
                selections=tuple(selections),
                idempotency_key="snapshot-rollback",
            )
        self.assertEqual(self._counts(), before)
        self.assertEqual(len(self.base.calls), 2)

    def test_reuse_is_allowed_while_stale_and_transaction_failure_write_nothing(self):
        """保留 UAT 合同名，并以候选分支更严格的有理由复用合同执行。"""
        self.test_reuse_requires_reason_while_stale_and_transaction_failure_write_nothing()

    def test_strict_http_routes_hide_evidence_and_save_complete_snapshot(self):
        agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=LocalIdentityAdapter(self.store),
            projects=self.projects,
            tasks=TaskService(self.store),
            versions=VersionService(self.store),
            artifacts=self.resolver,
        ))
        client = TestClient(create_app(
            agent_api=agent,
            formal_image_service=self.workspace,
        ))
        self.addCleanup(client.close)
        path = f"/api/formal/projects/{self.project_id}/image-workspace"
        query = f"?workspace_id={self.workspace_id}"
        headers = {
            "X-MPlus-Agent-Auth": self.token,
            "X-MPlus-UI-Token": client.app.state.session_token,
        }
        self.assertEqual(client.get(path + query).status_code, 401)
        self.assertEqual(client.get(
            path + "?workspace_id=workspace_other", headers=headers,
        ).status_code, 403)
        current = client.get(path + query, headers=headers)
        self.assertEqual(current.status_code, 200)
        body = current.json()
        public = json.dumps(body, ensure_ascii=False)
        for secret in (
            "artifact://", "plan_sha256", "source_catalog_sha256", "actor_id", "analysis_ref",
            "run_id", "controlled_path", str(self.base.generated.root),
        ):
            self.assertNotIn(secret, public)
        searched = client.get(
            f"{path}/shots/{body['shots'][0]['shot_no']}/candidates",
            headers=headers,
            params={
                "workspace_id": self.workspace_id,
                "base_version_id": body["base_version_id"],
                "expected_project_version": body["project_version"],
                "query": "发布会舞台全景",
            },
        )
        self.assertEqual(searched.status_code, 200)
        search_body = searched.json()
        self.assertEqual(len(search_body["candidate_snapshot_digest"]), 64)
        self.assertGreaterEqual(len(search_body["candidates"]), 2)
        self.assertTrue(all(len(item["content_sha256"]) == 64 for item in search_body["candidates"]))
        drifted = client.get(
            f"{path}/shots/{body['shots'][0]['shot_no']}/candidates",
            headers=headers,
            params={
                "workspace_id": self.workspace_id,
                "base_version_id": "version_stale0000",
                "expected_project_version": body["project_version"],
            },
        )
        self.assertEqual(drifted.status_code, 409)
        self.assertEqual(drifted.json()["detail"]["code"], "PROJECT_VERSION_CONFLICT")
        selections = [
            {
                "shot_no": item["shot_no"],
                "image_key": item["selected_image_key"],
                "confirmed": True,
                "decision_kind": "image_confirmed",
                "candidate_snapshot_digest": item["candidate_snapshot_digest"],
            }
            for item in body["shots"]
        ]
        before = self._counts()
        rejected = client.post(
            path + "/save" + query,
            headers=headers,
            json={
                "expected_project_version": body["project_version"],
                "selections": selections,
                "idempotency_key": "http-extra",
                "artifact_ref": "forged",
            },
        )
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(self._counts(), before)
        forged = [dict(item) for item in selections]
        forged[0]["image_key"] = "image_from_another_project"
        denied = client.post(
            path + "/save" + query,
            headers=headers,
            json={
                "expected_project_version": body["project_version"],
                "selections": forged,
                "idempotency_key": "http-cross-project",
            },
        )
        self.assertEqual(denied.status_code, 409)
        self.assertEqual(denied.json()["detail"]["code"], "TWO_STEP_CONFIRMATION_REQUIRED")
        self.assertEqual(self._counts(), before)
        proposal = client.post(
            path + "/proposals" + query,
            headers=headers,
            json={
                "expected_project_version": body["project_version"],
                "base_version_id": body["base_version_id"],
                "selections": selections,
                "idempotency_key": "http-image-save",
            },
        )
        self.assertEqual(proposal.status_code, 200)
        self.assertTrue(proposal.json()["confirmation_required"])
        saved = client.post(
            f"{path}/proposals/{proposal.json()['proposal_id']}/confirm{query}",
            headers=headers,
            json={"proposal_digest": proposal.json()["proposal_digest"]},
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["save_status"], "completed")
        self.assertEqual(saved.json()["state"], "confirmed")
        saved_public = json.dumps(saved.json(), ensure_ascii=False)
        for secret in (
            "artifact://", "plan_sha256", "source_catalog_sha256", "actor_id", "analysis_ref",
            "run_id", "controlled_path", str(self.base.generated.root),
        ):
            self.assertNotIn(secret, saved_public)

    def test_category_default_candidate_can_be_reused_and_adopted_with_package_provenance(self):
        source = self.base.generated.root / "default-package-source"
        source.mkdir()
        workbook = source / "M+发布会类通用脚本库.xlsx"
        _write_default_reference_workbook(
            workbook, self.base.generated.root / "default-package-reference.png",
        )
        v1 = create_asset_package(
            self.base.generated.root / "default-package-v1",
            package_id="launch-default-fixture", source_root=source,
        )
        package = bundle_asset_package_v2(
            v1, self.base.generated.root / "default-package-v2", package_version=4,
        )
        package_record = build_package_record(package)
        service = FormalDefaultImageService(
            projects=self.projects, tasks=TaskService(self.store),
            delivery=self.delivery, artifacts=self.resolver,
            image_pool=self.workspace.services.images,
            image_workspace=self.workspace,
            asset_package_resolver=lambda _script_type: {
                **package_record,
                "alias": "formal",
                "resolved_ref": "launch-default-fixture@4",
            },
            writes_enabled=True,
        )
        with self.store.transaction() as connection:
            connection.execute("UPDATE projects SET current_version_id=NULL WHERE project_id=?", (self.project_id,))
        project = self.projects.get_project(self.token, self.project_id)
        invalid_mappings = []
        for key, value in (
            ("local_path", ""), ("alias", "latest"),
            ("resolved_ref", "launch-default-fixture@latest"),
            ("package_sha256", "0" * 64),
        ):
            invalid_mappings.append((key, {
                **package_record, "alias": "formal",
                "resolved_ref": "launch-default-fixture@4", key: value,
            }))
        for key, value in (
            ("package_id", "other-package"), ("package_version", 5),
            ("manifest_sha256", "0" * 64),
        ):
            invalid = {
                **package_record, "alias": "formal",
                "resolved_ref": "launch-default-fixture@4", key: value,
            }
            invalid["resolved_ref"] = f"{invalid['package_id']}@{invalid['package_version']}"
            invalid["package_sha256"] = package_record_sha256(invalid)
            invalid_mappings.append((key, invalid))
        for label, invalid in invalid_mappings:
            invalid_service = FormalDefaultImageService(
                projects=self.projects, tasks=TaskService(self.store),
                delivery=self.delivery, artifacts=self.resolver,
                image_pool=self.workspace.services.images,
                image_workspace=self.workspace,
                asset_package_resolver=lambda _script_type, value=invalid: value,
                writes_enabled=True,
            )
            with self.subTest(mapping_field=label), self.assertRaises(WorkspaceServiceError):
                invalid_service.candidates(
                    self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                    expected_project_version=project["project_version"],
                )
        preview = service.candidates(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=project["project_version"],
        )
        self.assertEqual(preview["package"]["package_version"], 4)
        self.assertEqual(preview["candidate_count"], 2)
        self.assertEqual(
            {item["source_category"] for item in preview["library_candidates"]},
            {"创意库", "模板"},
        )
        self.assertTrue(preview["warnings"])
        selected_candidate = next(
            item for item in preview["library_candidates"]
            if any(
                recommendation["source"]["sheet"] == "创意库"
                for recommendation in item["template_recommendations"]
            )
        )
        media_id = selected_candidate["candidate_key"]
        selections = [{"shot_no": "library:1", "candidate_key": media_id}]
        proposed = service.propose(
            self.token,
            agent_session_id="",
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=project["project_version"],
            selections=selections,
            idempotency_key="default-adoption-proposal-fixture",
        )
        self.assertEqual(proposed["status"], "proposed")
        result = service.execute(
            self.token, project=project, parameters={
                "project_id": self.project_id,
                "expected_project_version": project["project_version"],
                "package_id": "launch-default-fixture", "package_version": 4,
                "package_manifest_sha256": preview["package"]["manifest_sha256"],
                "selections": [{"shot_no": "library:1", "media_id": media_id}],
                "idempotency_key": "default-adoption-fixture",
            },
        )
        self.assertEqual(result["adopted_image_count"], 1)
        with self.store.connection() as connection:
            link = connection.execute(
                """SELECT package_id, package_version, media_id, source_file,
                          source_sheet, source_row
                   FROM project_image_package_source_links"""
            ).fetchone()
        self.assertEqual((link["package_id"], link["package_version"], link["media_id"]), (
            "launch-default-fixture", 4, media_id,
        ))
        self.assertTrue(link["source_file"])
        self.assertTrue(link["source_sheet"])
        self.assertGreater(link["source_row"], 0)
        refreshed_project = self.projects.get_project(self.token, self.project_id)
        refreshed = service.candidates(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=refreshed_project["project_version"],
        )
        refreshed_candidate = next(
            item for item in refreshed["library_candidates"]
            if item["media_id"] == media_id
        )
        self.assertEqual(
            refreshed_candidate["content_sha256"],
            selected_candidate["content_sha256"],
        )
        self.assertEqual(refreshed_candidate["source"]["sheet"], link["source_sheet"])

    def test_partial_default_proposal_accepts_explicit_subset_and_keeps_unselected_pending(self):
        service = FormalDefaultImageService(
            projects=self.projects,
            tasks=TaskService(self.store),
            delivery=self.delivery,
            artifacts=self.resolver,
            image_pool=self.workspace.services.images,
            image_workspace=self.workspace,
            asset_package_resolver=lambda _script_type: None,
            writes_enabled=True,
        )
        project = self.projects.get_project(self.token, self.project_id)

        def candidate(shot_no):
            return {
                "candidate_key": f"media-{shot_no}",
                "media_id": f"media-{shot_no}",
            }

        preview = {
            "state": "partial",
            "project_version": project["project_version"],
            "package": {
                "package_id": "anonymous-defaults",
                "package_version": 4,
                "category_id": project["category_id"],
                "manifest_sha256": "a" * 64,
            },
            "shot_count": 3,
            "ready_shot_count": 2,
            "gap_shot_count": 1,
            "shots": [
                {"state": "ready", "shot_no": "1", "candidates": [candidate("1")]},
                {"state": "ready", "shot_no": "2", "candidates": [candidate("2")]},
                {
                    "state": "gap",
                    "shot_no": "3",
                    "candidates": [],
                    "gap_reason": "缺少安全候选",
                },
            ],
        }
        selections = [
            {"shot_no": "1", "candidate_key": "media-1"},
            {"shot_no": "2", "candidate_key": "media-2"},
        ]
        with patch.object(service, "candidates", return_value=preview):
            proposed = service.propose(
                self.token,
                agent_session_id="",
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_version=project["project_version"],
                selections=selections,
                idempotency_key="partial-default-proposal",
            )
            subset = service.propose(self.token, agent_session_id="", workspace_id=self.workspace_id,
                project_id=self.project_id, expected_project_version=project["project_version"],
                selections=selections[:1], idempotency_key="partial-default-subset")
            self.assertEqual(subset["response"]["parameters"]["pending_shot_count"], 2)
            with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_DECISION_MISMATCH"):
                service.propose(
                    self.token,
                    agent_session_id="",
                    workspace_id=self.workspace_id,
                    project_id=self.project_id,
                    expected_project_version=project["project_version"],
                    selections=[*selections, {"shot_no": "99", "candidate_key": "media-gap"}],
                    idempotency_key="partial-default-gap-forbidden",
                )
        self.assertEqual(proposed["status"], "proposed")
        self.assertEqual(proposed["response"]["parameters"]["pending_shot_count"], 1)
        self.assertIn("还有 1 镜未选图", proposed["response"]["warnings"][-1])

    def test_confirmed_brief_can_preview_default_image_pool_before_first_script_version(self):
        source = self.base.generated.root / "preplan-default-source"
        source.mkdir()
        workbook = source / "M+发布会类通用脚本库.xlsx"
        _write_default_reference_workbook(
            workbook, self.base.generated.root / "preplan-default-reference.png",
        )
        v1 = create_asset_package(
            self.base.generated.root / "preplan-default-v1",
            package_id="launch-preplan-fixture", source_root=source,
        )
        package = bundle_asset_package_v2(
            v1, self.base.generated.root / "preplan-default-v2", package_version=2,
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE projects SET current_version_id=NULL WHERE project_id=?",
                (self.project_id,),
            )
        service = FormalDefaultImageService(
            projects=self.projects, tasks=TaskService(self.store),
            delivery=self.delivery, artifacts=self.resolver,
            image_pool=self.workspace.services.images,
            image_workspace=self.workspace,
            asset_package_resolver=lambda _script_type: package,
            writes_enabled=True,
        )
        project = self.projects.get_project(self.token, self.project_id)
        with (
            patch(
                "mvp_v2.formal_default_images.sha256_file", wraps=sha256_file,
            ) as hash_file,
            patch.object(
                service, "_verified_blob_records",
                wraps=service._verified_blob_records,
            ) as parse_blob_manifest,
        ):
            preview = service.candidates(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                expected_project_version=project["project_version"],
            )
            full_verification_hash_count = hash_file.call_count
            refreshed = service.candidates(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                expected_project_version=project["project_version"],
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "默认图片候选不存在"):
                service.preview(
                    self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                    expected_project_version=project["project_version"],
                    media_id="missing-library-candidate",
                )
            for item in preview["library_candidates"]:
                service.preview(
                    self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                    expected_project_version=project["project_version"],
                    media_id=item["media_id"],
                )
            self.assertGreater(full_verification_hash_count, 0)
            self.assertEqual(hash_file.call_count, full_verification_hash_count)
            self.assertEqual(parse_blob_manifest.call_count, 1)
        self.assertEqual(preview["shot_count"], 0)
        self.assertEqual(preview["selection_mode"], "category_library_multi")
        self.assertTrue(preview["library_candidates"])
        self.assertEqual(
            refreshed["library_candidates"][0]["media_id"],
            preview["library_candidates"][0]["media_id"],
        )
        self.assertEqual(
            preview["safe_candidate_count"], len(preview["library_candidates"]),
        )
        candidate = next(
            item for item in preview["library_candidates"]
            if any(
                recommendation["source"]["sheet"] == "创意库"
                for recommendation in item["template_recommendations"]
            )
        )
        self.assertEqual(len(candidate["content_sha256"]), 64)
        self.assertEqual(candidate["source_category"], "创意库")
        explanation = candidate["match_explanation"]
        self.assertEqual(explanation["total_score"], candidate["score"])
        self.assertEqual(explanation["components"], candidate["rank_components"])
        self.assertEqual(explanation["conflicts"], candidate["conflicts"])
        self.assertEqual(explanation["source_category"], "创意库")
        self.assertTrue(explanation["sources"])
        for field in (
            "semantic", "safety", "reuse", "near_duplicate", "recommendation",
        ):
            self.assertTrue(explanation[field])
        public_candidate = json.dumps(candidate, ensure_ascii=False)
        self.assertNotIn(str(self.base.generated.root), public_candidate)
        for internal_field in (
            "local_path", "package_sha256", "manifest_sha256", "resolved_ref",
        ):
            self.assertNotIn(internal_field, public_candidate)
        self.assertTrue(any(
            item["source"]["sheet"] == "创意库"
            and item["source"]["file"] == workbook.name
            and item["source"]["row"] > 0
            for item in candidate["template_recommendations"]
        ))
        self.assertIn("可多选类别图片", preview["warnings"][0])
        loaded = load_asset_package(package)
        connection = sqlite3.connect(str(loaded["index_path"]))
        try:
            blob_path = Path(connection.execute(
                "SELECT current_path FROM media_assets ORDER BY media_id LIMIT 1",
            ).fetchone()[0])
        finally:
            connection.close()
        if not blob_path.is_absolute():
            blob_path = Path(loaded["package_root"]) / blob_path
        blob_path.write_bytes(blob_path.read_bytes() + b"drift-after-cache")
        with self.assertRaisesRegex(WorkspaceServiceError, "ASSET_PACKAGE_BLOB_DRIFT"):
            service.candidates(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                expected_project_version=project["project_version"],
            )

    def test_default_image_package_rejects_category_manifest_and_blob_drift(self):
        source = self.base.generated.root / "default-package-drift-source"
        source.mkdir()
        workbook = source / "M+发布会类通用脚本库.xlsx"
        _write_default_reference_workbook(
            workbook, self.base.generated.root / "default-package-drift-reference.png",
        )
        v1 = create_asset_package(
            self.base.generated.root / "default-package-drift-v1",
            package_id="launch-default-drift", source_root=source,
        )
        package = bundle_asset_package_v2(
            v1, self.base.generated.root / "default-package-drift-v2", package_version=3,
        )

        def service_for(path: Path) -> FormalDefaultImageService:
            return FormalDefaultImageService(
                projects=self.projects, tasks=TaskService(self.store),
                delivery=self.delivery, artifacts=self.resolver,
                image_pool=self.workspace.services.images,
                image_workspace=self.workspace,
                asset_package_resolver=lambda _script_type: path,
                writes_enabled=True,
            )

        project = self.projects.get_project(self.token, self.project_id)
        with self.store.transaction() as connection:
            connection.execute("UPDATE projects SET current_version_id=NULL WHERE project_id=?", (self.project_id,))
        preview = service_for(package).candidates(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            expected_project_version=project["project_version"],
        )
        manifest = json.loads(package.read_text(encoding="utf-8"))
        manifest["package_version"] = 4
        package.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceServiceError, "ASSET_PACKAGE_DRIFT"):
            service_for(package).execute(
                self.token, project=project, parameters={
                    "project_id": self.project_id,
                    "expected_project_version": project["project_version"],
                    "package_id": "launch-default-drift", "package_version": 3,
                    "package_manifest_sha256": preview["package"]["manifest_sha256"],
                    "selections": [{"shot_no": "library:1", "media_id": preview["library_candidates"][0]["candidate_key"]}],
                    "idempotency_key": "default-drift-fixture",
                },
            )

        manifest["category_id"] = "star_activity"
        package.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceServiceError, "ASSET_PACKAGE_CATEGORY_MISMATCH"):
            service_for(package).candidates(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                expected_project_version=project["project_version"],
            )

        manifest["category_id"] = project["category_id"]
        package.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        loaded = load_asset_package(package)
        index = sqlite3.connect(str(Path(loaded["index_path"])))
        try:
            blob_path = Path(index.execute(
                "SELECT current_path FROM media_assets ORDER BY media_id LIMIT 1",
            ).fetchone()[0])
        finally:
            index.close()
        if not blob_path.is_absolute():
            blob_path = Path(loaded["package_root"]) / blob_path
        original = blob_path.read_bytes()
        blob_path.write_bytes(original + b"drift")
        with self.assertRaisesRegex(WorkspaceServiceError, "ASSET_PACKAGE_BLOB_DRIFT"):
            service_for(package).candidates(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                expected_project_version=project["project_version"],
            )

    def test_agent_replaces_only_target_shot_with_recent_safe_image(self):
        initial = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        proposal = self.workspace.preview_version(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            base_version_id=initial["base_version_id"],
            expected_project_version=initial["project_version"],
            selections=tuple(ImageShotSelection(
                item["shot_no"], item["selected_image_key"], True,
                candidate_snapshot_digest=item["candidate_snapshot_digest"],
            ) for item in initial["shots"]),
            idempotency_key="agent-image-initial",
        )
        self.workspace.confirm_version(
            self.token,
            proposal_id=proposal["proposal_id"],
            proposal_digest=proposal["proposal_digest"],
        )
        before = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        before_selected = {
            item["shot_no"]: item["selected_image_key"] for item in before["shots"]
        }
        parent_version_id = before["base_version_id"]
        parent_context = self.delivery.resolve(
            self.token, self.project_id, parent_version_id,
            expected_project_version=before["project_version"],
        )
        parent_script_sha = self.resolver.resolve_stage_artifact(
            parent_context.run_id, "script_draft",
            expected_manifest_sha256=parent_context.manifest.sha256,
        ).sha256

        fixture = Path(__file__).parent / "fixtures" / "v10_image_delivery" / "anonymous-conference-arrival.png"
        output = BytesIO()
        with Image.open(fixture) as source:
            unique = source.convert("RGB").resize((1280, 720))
            unique.putpixel((0, 0), (17, 83, 149))
            unique.save(output, format="PNG")
        project = self.projects.get_project(self.token, self.project_id)
        uploaded = self.workspace.supplement_local_image(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_version=project["project_version"],
            filename="刚上传的产品图.png",
            content=output.getvalue(),
        )
        project = self.projects.get_project(self.token, self.project_id)
        context = self.delivery.resolve(
            self.token, self.project_id, project["current_version_id"],
            expected_project_version=project["project_version"],
        )
        plan = json.loads(self.resolver.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        ).path.read_text(encoding="utf-8"))
        brief_version_id, brief_snapshot_id = self.workspace._brief_binding(
            context, plan["confirmed_brief_input"],
        )
        shot_three = next(item for item in before["shots"] if item["shot_no"] == "3")
        with self.store.connection() as connection:
            image_sha = connection.execute(
                "SELECT image_sha256 FROM project_images WHERE project_image_id=?",
                (uploaded["image_key"],),
            ).fetchone()[0]
        ManualProjectImageAnalysisCommandService(self.pool).record_analysis(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            expected_project_lock_version=project["project_version"],
            submission=ManualImageAnalysisInput(
                image_id=uploaded["image_key"],
                image_sha256=image_sha,
                brief_version_id=brief_version_id,
                brief_snapshot_id=brief_snapshot_id,
                status="pass",
                caption=f"嘉宾在会场内合影交流；{shot_three['visual']}",
                tags=("发布会", "嘉宾合影", "嘉宾交流", "逻辑槽位3"),
                subjects=("嘉宾", "产品"),
                scene=f"嘉宾在会场内合影交流；{shot_three['visual']}",
                purpose="开场造势；嘉宾人物入画",
                quality="清晰可用",
                brand="匿名品牌",
                evidence=("匿名 fixture 人工核验",),
            ),
        )
        searched_recent = self.workspace.search_shot(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            shot_no="3",
            query="",
            base_version_id=parent_version_id,
            expected_project_version=project["project_version"],
        )
        self.assertIn(
            uploaded["image_key"],
            {item["image_key"] for item in searched_recent["candidates"]},
            json.dumps(searched_recent, ensure_ascii=False),
        )

        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=LocalIdentityAdapter(self.store),
            projects=self.projects,
            tasks=TaskService(self.store),
            versions=VersionService(self.store),
            artifacts=self.resolver,
        ))
        revision = FormalScriptRevisionService(
            projects=self.projects,
            tasks=TaskService(self.store),
            workspace=self.base.workspace,
            writes_enabled=True,
            image_workspace=self.workspace,
        )
        agent = FormalExecutableAgentService(
            ExecutableAgentServices(
                readonly=readonly,
                projects=self.projects,
                tasks=TaskService(self.store),
                confirmations=ActionConfirmationService(self.store, b"g" * 32),
                idempotency=ToolIdempotencyService(self.store),
                brief_store=BriefArtifactStore(self.base.generated.root / "data"),
                backend=FormalExecutionBackend(),
            ),
            generation=FakeFormalGeneration(),
            advisor=None,
            writes_enabled=True,
            script_revision=revision,
        )
        session = TaskService(self.store).create_agent_session(
            self.token, project_id=self.project_id,
        )
        candidate_result = agent.submit_message(
            self.token,
            session["agent_session_id"],
            content="把第 3 镜换成刚上传的产品图，文案、时长和其他镜头都不要改。",
        )
        self.assertEqual(candidate_result["response"]["type"], "formal_image_candidates")
        self.assertGreaterEqual(len(candidate_result["response"]["candidates"]), 2)
        chosen = next(
            item for item in candidate_result["response"]["candidates"]
            if item["image_key"] == uploaded["image_key"]
        )
        candidate_public = json.dumps(candidate_result["response"], ensure_ascii=False)
        self.assertNotIn(str(self.base.generated.root), candidate_public)
        self.assertTrue(all(
            item.get("match_basis") and item.get("sources")
            and "reuse_count" in item and "manual_confirmation_required" in item
            for item in candidate_result["response"]["candidates"]
        ))
        client = TestClient(create_app(
            agent_api=agent,
            formal_image_service=self.workspace,
        ))
        self.addCleanup(client.close)
        headers = {
            "X-MPlus-Agent-Auth": self.token,
            "X-MPlus-UI-Token": client.app.state.session_token,
        }
        select_path = f"/api/agent/tasks/{candidate_result['task']['task_id']}/image-selection"
        forged = client.post(
            select_path, headers=headers,
            json={"candidate_key": "candidate_" + "0" * 24},
        )
        self.assertEqual(forged.status_code, 409)
        self.assertEqual(forged.json()["detail"]["code"], "IMAGE_CANDIDATE_INVALID")
        selected_response = client.post(
            select_path, headers=headers,
            json={"candidate_key": chosen["candidate_key"]},
        )
        self.assertEqual(selected_response.status_code, 200)
        agent_proposal = selected_response.json()
        repeated_preview = client.post(
            select_path, headers=headers,
            json={"candidate_key": chosen["candidate_key"]},
        ).json()
        self.assertEqual(repeated_preview["task_id"], agent_proposal["task_id"])
        candidate_parameters = next(
            event["details"]["internal_formal_parameters"]
            for event in reversed(TaskService(self.store).list_task_events(
                self.token, candidate_result["task"]["task_id"],
            ))
            if event["details"].get("internal_formal_parameters")
        )
        direct_selections = []
        for item in before["shots"]:
            if item["shot_no"] != "3":
                continue
            query = candidate_parameters["query"] if item["shot_no"] == "3" else ""
            searched = self.workspace.search_shot(
                self.token, workspace_id=self.workspace_id, project_id=self.project_id,
                shot_no=item["shot_no"], query=query,
                base_version_id=before["base_version_id"],
                expected_project_version=before["project_version"],
            )
            direct_selections.append(ImageShotSelection(
                item["shot_no"],
                uploaded["image_key"] if item["shot_no"] == "3" else item["selected_image_key"],
                True, candidate_snapshot_digest=searched["candidate_snapshot_digest"],
                query=query, reuse_reason=str(item.get("reuse_reason") or ""),
            ))
        direct_proposal = self.workspace.preview_version(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
            base_version_id=before["base_version_id"],
            expected_project_version=before["project_version"],
            selections=tuple(direct_selections), idempotency_key="direct-entry-equivalence",
            allowed_shot_ids=[before["shots"][2]["shot_id"]],
        )
        outer_parameters = next(
            event["details"]["internal_formal_parameters"]
            for event in reversed(TaskService(self.store).list_task_events(
                self.token, agent_proposal["task_id"],
            ))
            if event["details"].get("internal_formal_parameters")
        )
        agent_inner = next(
            event["details"]["internal_formal_parameters"]
            for event in reversed(TaskService(self.store).list_task_events(
                self.token, outer_parameters["image_proposal_id"],
            ))
            if event["details"].get("internal_formal_parameters")
        )
        direct_inner = next(
            event["details"]["internal_formal_parameters"]
            for event in reversed(TaskService(self.store).list_task_events(
                self.token, direct_proposal["proposal_id"],
            ))
            if event["details"].get("internal_formal_parameters")
        )
        self.assertEqual(agent_inner["selections"], direct_inner["selections"])
        self.assertEqual(
            agent_proposal["response"]["rerun_nodes"],
            direct_proposal["rerun_nodes"],
        )
        self.assertEqual(agent_proposal["response"]["affected_shots"], ["3"])
        self.assertEqual(agent_proposal["response"]["after"]["image"]["image_key"], uploaded["image_key"])
        completed = agent.confirm_action(
            self.token,
            agent_proposal["task_id"],
            proposal_digest=agent_proposal["response"]["proposal_digest"],
        )
        self.assertEqual(completed["task"]["status"], "completed")
        version_count_after_confirm = len(self.projects.list_project_versions(self.token, self.project_id))
        replayed_confirm = agent.confirm_action(
            self.token,
            agent_proposal["task_id"],
            proposal_digest=agent_proposal["response"]["proposal_digest"],
        )
        self.assertEqual(replayed_confirm["task"]["task_id"], completed["task"]["task_id"])
        self.assertEqual(
            len(self.projects.list_project_versions(self.token, self.project_id)),
            version_count_after_confirm,
        )
        after = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        after_selected = {
            item["shot_no"]: item["selected_image_key"] for item in after["shots"]
        }
        self.assertEqual(after_selected["3"], uploaded["image_key"])
        self.assertEqual(
            {key: value for key, value in after_selected.items() if key != "3"},
            {key: value for key, value in before_selected.items() if key != "3"},
        )
        self.assertNotEqual(after["base_version_id"], parent_version_id)
        child_context = self.delivery.resolve(
            self.token, self.project_id, after["base_version_id"],
            expected_project_version=after["project_version"],
        )
        child_script_sha = self.resolver.resolve_stage_artifact(
            child_context.run_id, "script_draft",
            expected_manifest_sha256=child_context.manifest.sha256,
        ).sha256
        self.assertEqual(child_script_sha, parent_script_sha)
        original_decisions = self.workspace._read_snapshot(parent_context)
        actual_decisions = self.workspace._read_snapshot(child_context)
        for no, original in original_decisions.items():
            if no != "3":
                self.assertEqual(actual_decisions[no], original)
        feedback_proposal = agent.submit_message(
            self.token,
            session["agent_session_id"],
            content="第 3 镜当前图片不合适，排除后重新找。",
        )
        self.assertEqual(feedback_proposal["response"]["action"], "record_formal_image_feedback")
        feedback_completed = agent.confirm_action(
            self.token,
            feedback_proposal["task_id"],
            proposal_digest=feedback_proposal["response"]["proposal_digest"],
        )
        self.assertEqual(feedback_completed["response"]["type"], "formal_image_feedback_completed")
        refreshed_project = self.projects.get_project(self.token, self.project_id)
        refreshed = self.workspace.search_shot(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            shot_no="3",
            query="",
            base_version_id=after["base_version_id"],
            expected_project_version=refreshed_project["project_version"],
        )
        self.assertEqual(refreshed["current_image"]["image_key"], uploaded["image_key"])
        self.assertNotIn(
            uploaded["image_key"],
            {item["image_key"] for item in refreshed["candidates"]},
        )
        stale_choice = next(
            item for item in candidate_result["response"]["candidates"]
            if item["candidate_key"] != chosen["candidate_key"]
        )
        stale = client.post(
            select_path, headers=headers,
            json={"candidate_key": stale_choice["candidate_key"]},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["detail"]["code"], "PROJECT_VERSION_CONFLICT")

        temporary_candidates = agent.submit_message(
            self.token, session["agent_session_id"],
            content="查看第 3 镜当前图片和候选",
        )
        versions_before_temporary = len(self.projects.list_project_versions(self.token, self.project_id))
        temporary_response = client.post(
            f"/api/agent/tasks/{temporary_candidates['task']['task_id']}/temporary-text-basis",
            headers=headers,
            json={"reason": "客户现场图待确认"},
        )
        self.assertEqual(temporary_response.status_code, 409, temporary_response.text)
        self.assertEqual(temporary_response.json()["detail"]["code"], "IMAGE_CONFIRMATION_REQUIRED")
        temporary_workspace = self.workspace.read_current(
            self.token, workspace_id=self.workspace_id, project_id=self.project_id,
        )
        self.assertEqual(temporary_workspace["base_version_id"], after["base_version_id"])
        temporary_context = self.delivery.resolve(self.token, self.project_id, temporary_workspace["base_version_id"])
        self.assertEqual(self.workspace._read_snapshot(temporary_context), actual_decisions)
        self.assertEqual(len(self.projects.list_project_versions(self.token, self.project_id)), versions_before_temporary)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_image_decision_snapshots_v3 WHERE materialized_version_id=?",
                (parent_version_id,),
            ).fetchone()[0], 1)

    def _install_pass_images(self, count: int) -> None:
        project = self.projects.get_project(self.token, self.project_id)
        context = self.delivery.resolve(
            self.token,
            self.project_id,
            project["current_version_id"],
            expected_project_version=project["project_version"],
        )
        plan = json.loads(self.resolver.resolve_stage_artifact(
            context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256,
        ).path.read_text(encoding="utf-8"))
        draft = json.loads(self.resolver.resolve_stage_artifact(
            context.run_id, "script_draft", expected_manifest_sha256=context.manifest.sha256,
        ).path.read_text(encoding="utf-8"))
        script_rows = list(draft.get("script_rows") or [])
        details_by_shot = {
            str(row.get("镜号") or row.get("shot_no") or ""): detail
            for row, detail in zip(script_rows, draft.get("detail_rows") or [])
        }
        evidence = plan["confirmed_brief_input"]
        with self.store.connection() as connection:
            source = connection.execute(
                """SELECT file_id, source_document_id
                FROM project_files WHERE project_id=? ORDER BY created_at LIMIT 1""",
                (self.project_id,),
            ).fetchone()
        self.assertIsNotNone(source)
        runtime = self.base.generated.root / "runtime" / "artifacts" / "formal-images"
        fixtures = Path(__file__).parent / "fixtures" / "formal_visual_quality"
        inputs = [
            fixtures / "anonymous-launch-atlas-a.png",
            fixtures / "anonymous-launch-atlas-b.png",
        ]
        with self.store.transaction() as connection:
            for index, content in enumerate(_photo_variants(inputs, count), start=1):
                digest = sha256(content).hexdigest()
                target = runtime / digest[:2] / digest
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                connection.execute(
                    """INSERT INTO document_images(
                    document_image_id, file_id, source_document_id, image_sha256,
                    artifact_ref, content_sha256, location_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        new_id("document_image"), source["file_id"],
                        source["source_document_id"], digest,
                        f"artifact://formal-image/{digest}", digest,
                        json.dumps({"image_index": index}),
                    ),
                )
        self.pool.synchronize_confirmed_brief(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            brief_version_id=evidence["brief_version_id"],
            brief_snapshot_id=evidence["brief_snapshot_id"],
            expected_project_lock_version=context.project_lock_version,
        )
        pending = self.pool.resolve_for_planning(
            self.token,
            workspace_id=self.workspace_id,
            project_id=self.project_id,
            brief_version_id=evidence["brief_version_id"],
            brief_snapshot_id=evidence["brief_snapshot_id"],
            expected_project_lock_version=context.project_lock_version,
        )
        command = ManualProjectImageAnalysisCommandService(self.pool)
        visual_functions = {
            1: "匿名品牌 logo 品牌标识主视觉海报",
            2: "发布会场馆搭建施工布景结构",
            3: "匿名品牌 logo 品牌标识主题宣传图",
            4: "发布会主会场舞台全景 LED屏",
            5: "嘉宾签到登记填写资料",
            6: "嘉宾合影交流人群宾客",
            7: "嘉宾操作产品互动体验",
            8: "茶歇饮品服务台社交",
            9: "参观者人流通道",
            10: "匿名品牌 logo 品牌标识品牌海报",
            11: "发布会主会场舞台全景 LED屏",
            12: "演讲者发表演讲",
            13: "观众举起手机录屏并用手机拍摄记录",
            14: "舞台表演节目演出",
            15: "嘉宾颁奖授牌颁发证书",
            16: "嘉宾共同启动仪式启动装置大合影",
            17: "360/会场全貌，发布会主会场舞台全景",
            18: "匿名品牌 logo 片尾落版主视觉 KV 宣传海报",
        }
        function_evidence = {
            "brand_visual": "匿名品牌 logo 品牌标识主视觉海报",
            "construction": "发布会场馆搭建施工布景结构",
            "venue_wide": "发布会主会场舞台全景 LED屏",
            "registration": "嘉宾签到登记填写资料",
            "guest_group": "嘉宾合影交流人群宾客",
            "product_experience": "嘉宾操作产品互动体验",
            "service_social": "茶歇饮品服务台社交",
            "crowd_flow": "参观者人流通道",
            "speech": "演讲者发表演讲",
            "audience_recording": "观众举起手机录屏并用手机拍摄记录",
            "performance": "舞台表演节目演出",
            "award": "嘉宾颁奖授牌颁发证书",
            "startup_group": "嘉宾共同启动仪式启动装置大合影",
            "final_kv": "匿名品牌 logo 片尾落版主视觉 KV 宣传海报",
        }
        shot_plans = list(plan.get("shot_plans") or [])
        for index, image in enumerate(pending.images, start=1):
            slot_index = (index - 1) % len(script_rows) + 1
            row = script_rows[slot_index - 1]
            detail = details_by_shot.get(str(row.get("镜号") or ""), {})
            logical_slot = int(detail.get("逻辑槽位") or detail.get("slot_index") or slot_index)
            planned = shot_plans[slot_index - 1] if slot_index <= len(shot_plans) else {}
            desired = desired_visual_functions(str(planned.get("primary_event") or ""))
            if desired and not set(desired).intersection(
                ALLOWED_FUNCTIONS_BY_SLOT.get(logical_slot, set())
            ):
                visual_function = " ".join(
                    function_evidence[item] for item in desired if item in function_evidence
                )
            else:
                visual_function = visual_functions[logical_slot]
            visual = str(row.get("画面描述") or "匿名发布会现场")
            # This anonymous flow fixture represents fully analysed candidates,
            # including the technical fields now used by current-shot matching.
            technical = "；".join(str(row.get(key) or "") for key in ("类型", "景别", "运镜"))
            verified_visual = f"{visual_function}；{visual}；{technical}"
            command.record_analysis(
                self.token,
                workspace_id=self.workspace_id,
                project_id=self.project_id,
                expected_project_lock_version=context.project_lock_version,
                submission=ManualImageAnalysisInput(
                    image_id=image.image_id,
                    image_sha256=image.image_sha256,
                    brief_version_id=evidence["brief_version_id"],
                    brief_snapshot_id=evidence["brief_snapshot_id"],
                    status="pass",
                    caption=verified_visual,
                    tags=("发布会", visual_function, f"逻辑槽位{logical_slot}"),
                    subjects=("嘉宾", "产品"),
                    scene=verified_visual,
                    purpose=f"{row.get('结构') or '逐镜配图'}；{visual_function}",
                    quality="清晰可用",
                    brand="匿名品牌",
                    evidence=("匿名 fixture 人工核验",),
                ),
            )

    def _counts(self) -> tuple[int, ...]:
        with self.store.connection() as connection:
            return tuple(int(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0]) for table in (
                "project_runs", "project_versions", "outbox",
                "version_image_decision_snapshots_v3", "version_image_decisions_v3",
            ))


def _write_default_reference_workbook(path: Path, image_path: Path) -> None:
    _write_source_with_reference_image(path, image_path)
    workbook = load_workbook(path)
    image_bytes = BytesIO()
    Image.new("RGB", (1280, 720), "#dbeafe").save(image_bytes, format="PNG")
    image_bytes.seek(0)
    try:
        workbook["模版"].cell(7, 9, "匿名品牌 logo 品牌标识主视觉海报")
        workbook["创意库"].cell(7, 9, "匿名品牌 logo 品牌标识主视觉海报")
        workbook["创意库"].add_image(SpreadsheetImage(image_bytes), "D7")
        workbook.save(path)
    finally:
        workbook.close()
        image_bytes.close()


def _photo_variants(paths: list[Path], count: int):
    for index in range(count):
        atlas_index = (index // 18) % len(paths)
        panel_index = index % 18
        with Image.open(paths[atlas_index]) as source:
            image = source.convert("RGB")
            width, height = image.size
            column, row = panel_index % 4, panel_index // 4
            left, right = round(column * width / 4), round((column + 1) * width / 4)
            top, bottom = round(row * height / 6), round((row + 1) * height / 6)
            inset = 8
            image = image.crop(
                (left + inset, top + inset, right - inset, bottom - inset)
            ).resize((1280, 720))
            output = BytesIO()
            image.save(output, format="JPEG", quality=82, optimize=True)
            yield output.getvalue()


if __name__ == "__main__":
    unittest.main()
