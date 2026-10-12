from __future__ import annotations

import json
from io import BytesIO
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from fastapi.testclient import TestClient
from openpyxl import load_workbook

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.formal_generation_service import FormalTextProviderPolicy
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.identity_service import LocalIdentityAdapter, require_permission
from mvp_v2.local_ui import create_app
from mvp_v2.manual_review_commands import ManualReviewCommandService, ManualReviewCommandServices
from mvp_v2 import manual_review_commands as manual_review_commands_module
from mvp_v2.project_service import ProjectService
from mvp_v2.readonly_agent import ReadonlyAgentService, ReadonlyAgentServices
from mvp_v2.review_evidence_artifacts import FileReviewEvidenceArtifactStore, ReviewEvidenceAuthority
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_quality import AuthenticatedFinalConfirmationService
from mvp_v2.version_quality_artifacts import FileVersionQualityArtifactStore
from mvp_v2.version_quality_factory import build_workspace_version_quality_resolver
from mvp_v2.version_review_repository import WorkspaceVersionReviewRepository
from mvp_v2.version_review_service import VersionReviewService
from mvp_v2.task_service import TaskService
from mvp_v2.version_service import VersionService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from tests import test_mvp_v2_formal_generation_service as formal_generation_tests


class _ActorResolver:
    def __init__(self, store):
        self.store = store

    def resolve_reviewer(self, token, *, workspace_id, project_id):
        with self.store.connection() as connection:
            return require_permission(
                connection, token, workspace_id=workspace_id,
                project_id=project_id, permission="review.record",
            )["actor_id"]


class FormalScriptWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.generated = formal_generation_tests.FormalGenerationServiceTests("runTest")
        self.generated.setUp()
        self.addCleanup(self.generated.doCleanups)
        self.calls = []
        policy = FormalTextProviderPolicy(
            writer=self.generated._provider("writer", self.calls),
            editor=self.generated._provider("editor", self.calls),
            writer_provider="anonymous-writer", writer_model="anonymous-writer-model",
            editor_provider="anonymous-editor", editor_model="anonymous-editor-model",
        )
        generation = self.generated._service(policy)
        self.generation = generation
        preview = generation.prepare_plan(
            self.generated.token, workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"], expected_project_version=2,
            generation_mode="text_only_draft",
        )
        self.generated.expected_shot_count = preview["shot_count"]
        created = generation.confirm_plan_and_generate(
            self.generated.token, workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"], plan_id=preview["plan_id"],
            expected_project_version=3,
        )
        self.parent_version_id = created["version_id"]
        self.store = self.generated.store
        self.projects = ProjectService(self.store)
        self.resolver = RunArtifactResolver(self.generated.root / "runs")
        self.delivery = VersionDeliveryService(self.store, self.resolver)
        project = self.projects.get_project(self.generated.token, self.generated.project["project_id"])
        context = self.delivery.resolve(
            self.generated.token, self.generated.project["project_id"], self.parent_version_id,
            expected_project_version=project["project_version"],
        )
        review = VersionReviewService(WorkspaceVersionReviewRepository(
            self.store, auth_token=self.generated.token,
            workspace_id=context.workspace_id, project_id=context.project_id,
            version_id=context.version_id, run_id=context.run_id,
            expected_project_lock_version=context.project_lock_version,
        ))
        quality = build_workspace_version_quality_resolver(
            store=self.store, delivery_service=self.delivery,
            run_artifacts=self.resolver, reviews=review,
            evidence_artifacts=FileVersionQualityArtifactStore(self.generated.root / "quality-evidence"),
        )
        commands = ManualReviewCommandService(ManualReviewCommandServices(
            store=self.store, delivery=self.delivery, quality=quality,
            final_confirmation=AuthenticatedFinalConfirmationService(
                resolver=quality, actor_resolver=_ActorResolver(self.store),
            ),
            evidence_authority=ReviewEvidenceAuthority(
                FileReviewEvidenceArtifactStore(self.generated.root / "review-evidence")
            ),
        ))
        self.workspace = FormalScriptWorkspaceService(
            FormalScriptWorkspaceServices(
                store=self.store, projects=self.projects, delivery=self.delivery,
                commands=commands, artifacts=self.resolver,
            ),
            runs_root=self.generated.root / "runs", source_root=self.generated.source_root,
        )

    def _identity(self, token, workspace_id, project_id, *, shot_no="1", workspace=None):
        current = (workspace or self.workspace).read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        shot = next(item for item in current["shots"] if item["shot_no"] == str(shot_no))
        return current["project_version"], shot["shot_id"]

    def test_current_saved_version_exports_original_layout_review_workbook(self):
        project_id = self.generated.project["project_id"]
        result = self.workspace.build_review_workbook(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=project_id,
            version_no=1,
        )
        self.assertTrue(result["filename"].endswith("-脚本复核稿-V1.xlsx"))
        workbook = load_workbook(BytesIO(result["content"]), data_only=True)
        self.addCleanup(workbook.close)
        sheet = workbook["脚本"]
        self.assertIn("复核稿，非正式交付", sheet["A1"].value)
        self.assertIn("版本 1", sheet["A1"].value)
        self.assertEqual(sheet.max_row - 6, self.generated.expected_shot_count)
        self.assertIn("抽调明细", workbook.sheetnames)
        cell_text = "\n".join(
            str(cell.value or "") for row in sheet.iter_rows() for cell in row
        )
        self.assertNotIn(str(self.generated.root), cell_text)
        self.assertNotIn("artifact://", cell_text)
        self.assertNotIn(self.generated.token, cell_text)

        project_version, shot_id = self._identity(
            self.generated.token, self.generated.workspace_id, project_id,
        )
        self.workspace.apply_table_change(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=project_id,
            expected_project_version=project_version,
            expected_revision=0,
            shot_no="1", shot_id=shot_id,
            field="subtitle",
            value="尚未保存的草稿",
            idempotency_key="review-workbook-dirty-1",
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "请先保存新版本"):
            self.workspace.build_review_workbook(
                self.generated.token,
                workspace_id=self.generated.workspace_id,
                project_id=project_id,
                version_no=1,
            )

        child = self.workspace.save_version(self.generated.token,
            workspace_id=self.generated.workspace_id, project_id=project_id,
            expected_project_version=project_version, expected_revision=1,
            reason="保存匿名字幕修订", idempotency_key="review-workbook-child")
        self.workspace.apply_table_change(self.generated.token,
            workspace_id=self.generated.workspace_id, project_id=project_id,
            expected_project_version=child['project_version'], expected_revision=0,
            shot_no="1", shot_id=child['shots'][0]['shot_id'], field="subtitle",
            value="另一个未保存的当前草稿", idempotency_key="review-workbook-child-dirty")
        historical = self.workspace.build_review_workbook(self.generated.token,
            workspace_id=self.generated.workspace_id, project_id=project_id, version_no=1)
        old_book = load_workbook(BytesIO(historical['content']), data_only=True)
        self.addCleanup(old_book.close)
        self.assertEqual(list(old_book['脚本'].values), list(sheet.values))

    def test_save_uses_frozen_parent_source_for_fork_and_workbook_without_alias_resolution(self):
        resolved_types = []

        def resolve_source(script_type):
            resolved_types.append(script_type)
            if len(resolved_types) > 1:
                raise AssertionError("同一脚本保存不得二次解析素材包")
            return self.generated.source_root

        workspace = FormalScriptWorkspaceService(
            self.workspace.services,
            runs_root=self.generated.root / "runs",
            source_root=self.generated.root / "missing-fallback-source",
            source_root_resolver=resolve_source,
        )
        project_version, shot_id = self._identity(
            self.generated.token, self.generated.workspace_id,
            self.generated.project["project_id"],
        )
        changed = workspace.apply_table_change(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
            expected_project_version=project_version,
            expected_revision=0,
            shot_no="1", shot_id=shot_id,
            field="visual",
            value="匿名嘉宾从入口进入会场，镜头跟随至主舞台。",
            idempotency_key="script-routed-source-edit",
        )
        saved = workspace.save_version(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
            expected_project_version=project_version,
            expected_revision=changed["revision"],
            reason="验证素材包单次解析",
            idempotency_key="script-routed-source-save",
        )

        self.assertEqual(saved["save_status"], "completed")
        self.assertEqual(resolved_types, [])

    def test_future_template_selection_preserves_unsaved_draft_and_planning_guard(self):
        token = self.generated.token
        scope = {'workspace_id': self.generated.workspace_id, 'project_id': self.generated.project['project_id']}
        initial = self.workspace.read_current(token, **scope)
        self.workspace.apply_table_change(token, **scope,
            expected_project_version=initial['project_version'], expected_revision=initial['revision'],
            shot_no='1', shot_id=initial['shots'][0]['shot_id'], field='visual',
            value='保留中的原脚本修改', idempotency_key='template-keep-existing-draft')
        before = self.workspace.read_current(token, **scope)
        with self.store.connection() as connection:
            versions_before = [tuple(row) for row in connection.execute('SELECT * FROM project_versions')]
        selected = self.projects.select_template(token, project_id=scope['project_id'],
            package_ref='anonymous-future-template@2', package_sha256='c' * 64,
            expected_project_version=before['project_version'])
        after = self.workspace.read_current(token, **scope)
        self.assertEqual(selected['template_package_ref'], 'anonymous-future-template@2')
        for key in ('shots', 'revision', 'base_version_id', 'state'):
            self.assertEqual(after[key], before[key])
        with self.store.connection() as connection:
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM project_versions')], versions_before)
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.generation.prepare_plan(token, **scope, expected_project_version=selected['project_version'],
                generation_mode='text_only_draft')
        self.assertEqual(caught.exception.code, 'SCRIPT_DRAFT_NOT_SAVED')

    def test_table_and_instruction_share_persistent_draft_then_save_child_version(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        initial = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        self.assertEqual((initial["state"], initial["revision"]), ("ready", 0))
        self.assertEqual(initial["pending_confirmations"], [])
        self.assertGreater(len(initial["shots"]), 2)
        for shot in initial["shots"]:
            self.assertTrue(shot["source"]["file"])
            self.assertTrue(shot["source"]["sheet"])
            self.assertGreater(shot["source"]["row"], 0)
        self.assertEqual(initial["base_version_id"], self.parent_version_id)
        # The opaque base identity now binds panel state; other runtime evidence
        # remains excluded from the employee DTO.
        public = json.dumps({key: value for key, value in initial.items() if key != "base_version_id"}, ensure_ascii=False)
        for secret in ("artifact://", "sha256", "draft_id", "version_id", "run_id", "actor_id", str(self.generated.root)):
            self.assertNotIn(secret, public)

        table = self.workspace.apply_table_change(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=initial["project_version"], expected_revision=0,
            shot_no="1", shot_id=initial["shots"][0]["shot_id"], field="visual",
            value="匿名嘉宾从入口进入会场，镜头跟随至主舞台。",
            idempotency_key="script-table-1",
        )
        self.assertEqual((table["state"], table["revision"]), ("editing", 1))
        natural = self.workspace.apply_instruction(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=initial["project_version"], expected_revision=1,
            instruction="把第2镜声音修改为现场掌声后衔接克制的节奏音乐",
            idempotency_key="script-natural-1",
        )
        self.assertEqual(natural["revision"], 2)
        undone = self.workspace.undo(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=2, idempotency_key="script-undo-1",
        )
        self.assertEqual((undone["revision"], undone["can_redo"]), (3, True))

        restarted_store = WorkspaceStore(self.store.database_path)
        restarted = FormalScriptWorkspaceService(
            FormalScriptWorkspaceServices(
                store=restarted_store, projects=ProjectService(restarted_store),
                delivery=VersionDeliveryService(restarted_store, self.resolver),
                commands=self.workspace.services.commands, artifacts=self.resolver,
            ),
            runs_root=self.generated.root / "runs", source_root=self.generated.source_root,
        )
        recovered = restarted.read_current(token, workspace_id=workspace_id, project_id=project_id)
        self.assertEqual((recovered["revision"], recovered["can_redo"]), (3, True))
        redone = restarted.redo(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=3, idempotency_key="script-redo-1",
        )
        self.assertEqual(redone["revision"], 4)
        self.assertEqual(redone["shots"][0]["visual"], "匿名嘉宾从入口进入会场，镜头跟随至主舞台。")
        self.assertEqual(redone["shots"][1]["sound"], "现场掌声后衔接克制的节奏音乐")

        saved = restarted.save_version(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=4, reason="修正第 1 镜画面和第 2 镜声音",
            idempotency_key="script-save-1",
        )
        self.assertEqual((saved["save_status"], saved["state"]), ("completed", "ready"))
        self.assertEqual(saved["revision"], 0)
        self.assertEqual(saved["versions"][-1]["change_summary"], "镜头 1、2 · 声音、画面描述")
        self.assertEqual(saved["versions"][-1]["review_status"], "等待项目经理审核")
        self.assertEqual(saved["versions"][-1]["version_no"], 2)
        self.assertTrue(saved["versions"][-1]["has_diff"])

        comparison = restarted.read_version_diff(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            version_no=2,
        )
        self.assertEqual(comparison["comparison_label"], "版本 1 → 版本 2")
        self.assertEqual(comparison["changed_shot_count"], 2)
        self.assertEqual(comparison["changed_field_count"], 2)
        self.assertGreater(comparison["unchanged_field_count"], 0)
        self.assertEqual(comparison["locked_field_status"], "pass")
        by_change = {
            (item["shot_no"], item["field"]): (item["before"], item["after"])
            for item in comparison["changes"]
        }
        self.assertEqual(by_change[("1", "画面描述")], (
            initial["shots"][0]["visual"],
            "匿名嘉宾从入口进入会场，镜头跟随至主舞台。",
        ))
        self.assertEqual(by_change[("2", "声音")][1], "现场掌声后衔接克制的节奏音乐")
        comparison_public = json.dumps(comparison, ensure_ascii=False)
        for secret in (
            "artifact://", "sha256", "draft_id", "version_id", "run_id",
            "actor_id", str(self.generated.root),
        ):
            self.assertNotIn(secret, comparison_public)
        self.assertEqual(len(self.calls), 2, "保存草稿不得新增 Provider 调用")
        versions = self.projects.list_project_versions(token, project_id)
        self.assertEqual(len(versions), 2)
        child = versions[-1]
        self.assertEqual(child["parent_version_id"], self.parent_version_id)
        project = self.projects.get_project(token, project_id)
        context = self.delivery.resolve(
            token, project_id, child["version_id"],
            expected_project_version=project["project_version"],
        )
        self.assertEqual(context.parent_version_id, self.parent_version_id)
        manifest = self.resolver.read_manifest(context.run_id, expected_sha256=context.manifest.sha256)
        self.assertEqual(manifest["parent_run_id"], versions[0]["run_id"])
        self.assertEqual(manifest["metrics"]["provider_calls"], 0)
        self.assertIn("revision_report", manifest["artifacts"])
        for stage in ("script_plan", "script_draft", "visual_binding", "workbook_render", "machine_audit"):
            self.assertIn(manifest["stages"][stage]["status"], {"succeeded", "succeeded_with_warnings"})
        child_binding = json.loads(self.resolver.resolve_stage_artifact(
            context.run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256,
        ).path.read_text(encoding="utf-8"))
        self.assertEqual(child_binding["script_rows"][0]["画面描述"], "匿名嘉宾从入口进入会场，镜头跟随至主舞台。")
        self.assertEqual(child_binding["script_rows"][1]["声音"], "现场掌声后衔接克制的节奏音乐")
        self.assertEqual(len(child_binding["bindings"]), len(child_binding["script_rows"]))
        report_artifact = self.resolver.resolve_artifact(context.run_id, "revision_report")
        report = json.loads(report_artifact.path.read_text(encoding="utf-8"))
        self.assertEqual(report["schema_version"], "mplus-revision-report/v1")
        self.assertEqual(report["completion"]["status"], "waiting_human_review")
        self.assertEqual(report["actual_impact"]["changed_fields"], ["声音", "画面描述"])
        self.assertEqual(report["actual_impact"]["changed_shot_nos"], ["1", "2"])
        self.assertEqual(report["structured_intent"]["changes"], [
            {"revision": 1, "origin": "table", "shot_no": "1", "field": "画面描述"},
            {"revision": 2, "origin": "natural_language", "shot_no": "2", "field": "声音"},
        ])
        self.assertFalse(report["locked_field_verification"]["violations"])
        self.assertEqual(report["statuses"]["publication"]["status"], "unpublished")
        workbook = load_workbook(self.resolver.resolve_artifact(context.run_id, "workbook").path, data_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["相邻镜头复核"].max_row - 1, len(child_binding["script_rows"]) - 1)
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT name FROM schema_migrations WHERE version=13",
                ).fetchone()[0],
                "version-review-draft-submissions",
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_review_draft_submissions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0], 0)
            origins = [row[0] for row in connection.execute("SELECT origin FROM version_review_cell_patches ORDER BY revision")]
            submission = connection.execute(
                "SELECT revision_report_ref, revision_report_sha256 FROM version_review_draft_submissions",
            ).fetchone()
        self.assertEqual(origins, ["table", "natural_language"])
        self.assertEqual(
            (submission["revision_report_ref"], submission["revision_report_sha256"]),
            (report_artifact.ref, report_artifact.sha256),
        )
        self.assertFalse((self.generated.root / "CURRENT").exists())

        replay = restarted.save_version(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=4, reason="修正第 1 镜画面和第 2 镜声音",
            idempotency_key="script-save-1",
        )
        self.assertEqual(replay["save_status"], "completed")
        self.assertEqual(len(self.projects.list_project_versions(token, project_id)), 2)
        report_artifact.path.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(WorkspaceServiceError, "ARTIFACT_INVALID"):
            restarted.read_version_diff(
                token,
                workspace_id=workspace_id,
                project_id=project_id,
                version_no=2,
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "ARTIFACT_INVALID"):
            restarted.read_current(
                token, workspace_id=workspace_id, project_id=project_id,
            )

    def test_discard_changes_restores_saved_version_without_losing_audit_history(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        initial = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        changed = self.workspace.apply_table_change(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=initial["shots"][0]["shot_id"],
            field="subtitle",
            value="这条修改将被明确放弃",
            idempotency_key="script-discard-change-1",
        )
        first_change = changed
        changed = self.workspace.apply_table_change(token, workspace_id=workspace_id,
            project_id=project_id, expected_project_version=initial["project_version"],
            expected_revision=changed["revision"], shot_no="2", shot_id=initial["shots"][1]["shot_id"],
            field="subtitle", value="第二处也应放弃", idempotency_key="script-discard-change-2")
        self.assertTrue(changed["can_save_version"])

        discarded = self.workspace.discard_changes(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=changed["revision"],
            idempotency_key="script-discard-1",
        )

        self.assertEqual(discarded["state"], "ready")
        self.assertFalse(discarded["can_save_version"])
        self.assertEqual(discarded["shots"][0]["subtitle"], initial["shots"][0]["subtitle"])
        self.assertTrue(discarded["can_redo"])
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM version_review_cell_patches",
                ).fetchone()[0],
                2,
            )
        restored_first = self.workspace.redo(token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=initial["project_version"], expected_revision=discarded["revision"],
            idempotency_key="discard-redo-first")
        self.assertEqual([shot["subtitle"] for shot in restored_first["shots"]],
                         [shot["subtitle"] for shot in first_change["shots"]])
        restored_second = self.workspace.redo(token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=initial["project_version"], expected_revision=restored_first["revision"],
            idempotency_key="discard-redo-second")
        self.assertEqual([shot["subtitle"] for shot in restored_second["shots"]],
                         [shot["subtitle"] for shot in changed["shots"]])
        self.assertFalse(restored_second["can_redo"])

    def test_history_partial_undo_discard_redo_and_new_branch_keep_user_order(self):
        token = self.generated.token
        scope = dict(workspace_id=self.generated.workspace_id, project_id=self.generated.project["project_id"])
        model = self.workspace.read_current(token, **scope)
        initial = [shot["subtitle"] for shot in model["shots"]]
        saved = []
        counter = 0
        def change(index, text):
            nonlocal model, counter
            counter += 1
            model = self.workspace.apply_table_change(token, **scope,
                expected_project_version=model["project_version"], expected_revision=model["revision"],
                shot_no=str(index + 1), shot_id=model["shots"][index]["shot_id"],
                field="subtitle", value=text, idempotency_key=f"ordered-edit-{counter}")
        def action(name):
            nonlocal model, counter
            counter += 1
            model = getattr(self.workspace, name)(token, **scope,
                expected_project_version=model["project_version"], expected_revision=model["revision"],
                idempotency_key=f"ordered-{name}-{counter}")
        def subtitles():
            return [shot["subtitle"] for shot in model["shots"]]
        for i in range(3):
            change(i, f"匿名第{i + 1}处修改")
            saved.append(subtitles())
        action("undo")
        self.assertEqual(subtitles(), saved[1])
        action("discard_changes")
        self.assertEqual(subtitles(), initial)
        self.assertFalse(model["can_save_version"])
        for expected in saved:
            action("redo")
            self.assertEqual(subtitles(), expected)
        self.assertFalse(model["can_redo"])
        action("undo")
        action("undo")
        self.assertEqual(subtitles(), saved[0])
        action("redo")
        self.assertEqual(subtitles(), saved[1])
        change(2, "撤销后的新分支")
        self.assertFalse(model["can_redo"])
        action("undo")
        self.assertEqual(subtitles(), saved[1])
        action("redo")
        self.assertEqual(subtitles()[:2], saved[1][:2])
        self.assertEqual(subtitles()[2], "撤销后的新分支")
        self.assertFalse(model["can_redo"])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_review_cell_patches").fetchone()[0], 4)

    def test_assistant_locks_persist_and_do_not_block_manual_table_edits(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        initial = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        protected_id = initial["shots"][0]["shot_id"]
        stored = self.workspace.set_assistant_locks(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=initial["project_version"],
            locked_shot_ids=[protected_id, "shot_stale_lineage"],
            idempotency_key="assistant-locks-persist-1",
        )
        self.assertEqual(stored["locked_shot_ids"], [protected_id])
        self.assertEqual(stored["ignored_shot_ids"], ["shot_stale_lineage"])

        restarted_store = WorkspaceStore(self.store.database_path)
        restarted = FormalScriptWorkspaceService(
            FormalScriptWorkspaceServices(
                store=restarted_store,
                projects=ProjectService(restarted_store),
                delivery=VersionDeliveryService(restarted_store, self.resolver),
                commands=self.workspace.services.commands,
                artifacts=self.resolver,
            ),
            runs_root=self.generated.root / "runs",
            source_root=self.generated.source_root,
        )
        recovered = restarted.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        self.assertTrue(recovered["shots"][0]["assistant_locked"])
        manually_changed = restarted.apply_table_change(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=recovered["shots"][0]["shot_id"],
            field="subtitle",
            value="人工仍可修改保护镜头",
            idempotency_key="assistant-lock-manual-edit-1",
        )
        self.assertEqual(manually_changed["shots"][0]["subtitle"], "人工仍可修改保护镜头")
        replay = restarted.set_assistant_locks(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=initial["project_version"],
            locked_shot_ids=[protected_id, "shot_stale_lineage"],
            idempotency_key="assistant-locks-persist-1",
        )
        self.assertEqual(replay, stored)
        with self.assertRaisesRegex(WorkspaceServiceError, "幂等键"):
            restarted.set_assistant_locks(
                token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=initial["project_version"],
                locked_shot_ids=[],
                idempotency_key="assistant-locks-persist-1",
            )

    def test_total_shot_change_hands_off_to_formal_planner_without_local_insert(self):
        project_id = self.generated.project["project_id"]
        tasks = TaskService(self.store)
        revision = FormalScriptRevisionService(
            projects=self.projects,
            tasks=tasks,
            workspace=self.workspace,
            writes_enabled=True,
        )
        versions_before = self.projects.list_project_versions(
            self.generated.token, project_id,
        )
        proposal = revision.propose(
            self.generated.token,
            agent_session_id="",
            project_id=project_id,
            instruction="把总镜数增加到 20 镜",
            expected_project_version=self.projects.get_project(
                self.generated.token, project_id,
            )["project_version"],
            expected_revision=0,
            idempotency_key="script-replan-20-1",
            entrypoint="assistant",
            feedback_kind="structure",
            draft_only=True,
        )
        response = proposal["response"]
        self.assertEqual(response["type"], "replan_required")
        self.assertEqual(response["shot_count_after"], 20)
        self.assertEqual(
            self.projects.list_project_versions(self.generated.token, project_id),
            versions_before,
        )
        project = self.projects.get_project(self.generated.token, project_id)
        plan = self.generation.prepare_plan(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=project_id,
            expected_project_version=project["project_version"],
            generation_mode="text_only_draft",
            revision_task_id=response["analysis_task_id"],
        )
        self.assertEqual(plan["shot_count"], 20)
        self.assertEqual(
            self.projects.list_project_versions(self.generated.token, project_id),
            versions_before,
        )

    def test_invalid_instruction_and_stale_revision_fail_before_new_version(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        with self.assertRaisesRegex(WorkspaceServiceError, "INSTRUCTION_AMBIGUOUS"):
            self.workspace.apply_instruction(
                token, workspace_id=workspace_id, project_id=project_id,
                expected_revision=0, instruction="帮我改得更高级",
                idempotency_key="ambiguous-1",
            )
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        self.workspace.apply_table_change(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=current["project_version"], expected_revision=0,
            shot_no="1", shot_id=current["shots"][0]["shot_id"], field="subtitle",
            value="匿名新品正式发布", idempotency_key="subtitle-1",
        )
        with self.assertRaisesRegex(Exception, "DRAFT_REVISION_CONFLICT"):
            self.workspace.apply_table_change(
                token, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=current["project_version"], expected_revision=0,
                shot_no="1", shot_id=current["shots"][0]["shot_id"], field="subtitle",
                value="过期覆盖", idempotency_key="subtitle-stale",
            )
        self.assertEqual(len(self.projects.list_project_versions(token, project_id)), 1)

    def test_stale_page_cannot_write_after_another_page_saves_a_new_project_version(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        page_a = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        page_b_change = self.workspace.apply_table_change(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=page_a["project_version"], expected_revision=0,
            shot_no="1", shot_id=page_a["shots"][0]["shot_id"], field="subtitle", value="页面 B 的修改",
            idempotency_key="cross-page-edit-1",
        )
        self.workspace.save_version(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=page_a["project_version"],
            expected_revision=page_b_change["revision"], reason="页面 B 保存",
            idempotency_key="cross-page-save-1",
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.workspace.apply_table_change(
                token, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=page_a["project_version"], expected_revision=0,
                shot_no="1", shot_id=page_a["shots"][0]["shot_id"], field="subtitle", value="页面 A 的过期覆盖",
                idempotency_key="cross-page-stale-1",
            )
        current = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        self.assertEqual(current["shots"][0]["subtitle"], "页面 B 的修改")

    def test_lost_response_replays_same_table_command_without_new_revision(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        request = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"],
            "expected_revision": 0, "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
            "value": "网络响应丢失后仍可重试", "idempotency_key": "lost-response-1",
        }
        first = self.workspace.apply_table_change(token, **request)
        replay = self.workspace.apply_table_change(token, **request)

        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["applied_revision"], 1)
        self.assertFalse(first["replayed"])
        self.assertEqual(replay["revision"], 1)
        self.assertEqual(replay["applied_revision"], 1)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["shots"][0]["subtitle"], "网络响应丢失后仍可重试")
        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.workspace.apply_table_change(
                token, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=current["project_version"], expected_revision=0,
                shot_no="1", shot_id=current["shots"][0]["shot_id"], field="subtitle", value="同键不同请求",
                idempotency_key="lost-response-1",
            )

    def test_consumed_table_command_recovers_after_domain_write_crash_and_replays_original_revision(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        shot_id = current["shots"][0]["shot_id"]
        request = {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "expected_project_version": current["project_version"],
            "expected_revision": 0,
            "shot_no": "1",
            "shot_id": shot_id,
            "field": "subtitle",
            "value": "consume 后恢复的字幕",
            "idempotency_key": "crash-after-consume-1",
        }
        commands = self.workspace.services.commands
        original_execute_domain = commands._execute_domain
        failed = False

        def crash_once(auth_token, action, parameters, context, **kwargs):
            nonlocal failed
            if action == "apply_change_set" and not failed:
                failed = True
                raise RuntimeError("模拟 consume 后 domain 写入前崩溃")
            return original_execute_domain(auth_token, action, parameters, context, **kwargs)

        with patch.object(commands, "_execute_domain", side_effect=crash_once):
            with self.assertRaisesRegex(RuntimeError, "consume 后"):
                self.workspace.apply_table_change(token, **request)

        crashed = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        self.assertEqual(crashed["revision"], 0)
        self.assertNotEqual(crashed["shots"][0]["subtitle"], request["value"])

        recovered = self.workspace.apply_table_change(token, **request)
        self.assertEqual(recovered["applied_revision"], 1)
        self.assertEqual(recovered["shots"][0]["subtitle"], request["value"])
        self.assertEqual(recovered["revision"], 1)

        second = dict(request)
        second.update({
            "expected_revision": 1,
            "value": "第二次变更",
            "idempotency_key": "crash-after-consume-2",
        })
        self.workspace.apply_table_change(token, **second)
        replay_first = self.workspace.apply_table_change(token, **request)
        self.assertEqual(replay_first["applied_revision"], 1)
        self.assertEqual(replay_first["revision"], 2)
        self.assertEqual(replay_first["shots"][0]["subtitle"], "第二次变更")

    def test_script_write_requires_project_version_and_shot_identity(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        shot_id = current["shots"][0]["shot_id"]
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.workspace.apply_table_change(
                token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_revision=0,
                shot_no="1",
                shot_id=shot_id,
                field="subtitle",
                value="缺少项目版本不得写入",
                idempotency_key="missing-project-version-1",
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.workspace.apply_table_change(
                token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=current["project_version"],
                expected_revision=0,
                shot_no="1",
                field="subtitle",
                value="缺少镜头身份不得写入",
                idempotency_key="missing-shot-id-1",
            )

    def test_revision_proposal_and_history_actions_require_project_version(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        revision = FormalScriptRevisionService(
            projects=self.projects,
            tasks=TaskService(self.store),
            workspace=self.workspace,
            writes_enabled=True,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            revision.propose(
                token,
                agent_session_id="",
                project_id=project_id,
                instruction="把第1镜字幕修改为缺少版本的提案",
                expected_revision=0,
                idempotency_key="missing-proposal-version-1",
            )
        for action in ("undo", "redo", "discard_changes"):
            with self.subTest(action=action):
                with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
                    getattr(self.workspace, action)(
                        token,
                        workspace_id=workspace_id,
                        project_id=project_id,
                        expected_revision=0,
                        idempotency_key=f"missing-history-version-{action}",
                    )

    def test_delete_shot_requires_server_confirmed_shot_identity(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.workspace.apply_revision_operation(
                token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=current["project_version"],
                expected_revision=0,
                operation={"operation": "delete_shot", "shot_no": "1"},
                idempotency_key="missing-delete-shot-id-1",
            )

    def test_replay_uses_frozen_request_not_current_state_after_later_edit(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        common = {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "expected_project_version": current["project_version"],
            "shot_no": "1",
            "shot_id": current["shots"][0]["shot_id"],
            "field": "subtitle",
        }
        first = {**common, "expected_revision": 0, "value": "第一请求", "idempotency_key": "frozen-replay-1"}
        second = {**common, "expected_revision": 1, "value": "第二请求", "idempotency_key": "frozen-replay-2"}
        self.workspace.apply_table_change(token, **first)
        self.workspace.apply_table_change(token, **second)
        replay = self.workspace.apply_table_change(token, **first)
        self.assertEqual(replay["applied_revision"], 1)
        self.assertEqual(replay["revision"], 2)
        self.assertEqual(replay["shots"][0]["subtitle"], "第二请求")

    def test_replay_second_command_returns_its_exact_revision_after_later_edit(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        common = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"],
            "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
        }
        first = {**common, "expected_revision": 0, "value": "第一条", "idempotency_key": "exact-replay-1"}
        second = {**common, "expected_revision": 1, "value": "第二条", "idempotency_key": "exact-replay-2"}
        self.workspace.apply_table_change(token, **first)
        second_result = self.workspace.apply_table_change(token, **second)
        replay = self.workspace.apply_table_change(token, **second)
        self.assertEqual(second_result["applied_revision"], 2)
        self.assertEqual(replay["applied_revision"], 2)
        self.assertEqual(replay["shots"][0]["subtitle"], "第二条")

    def test_natural_language_replay_preserves_origin_and_exact_request(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        request = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"], "expected_revision": 0,
            "instruction": "把第1镜字幕修改为自然语言旁白修改",
            "idempotency_key": "natural-origin-replay-1",
        }
        first = self.workspace.apply_instruction(token, **request)
        replay = self.workspace.apply_instruction(token, **request)
        self.assertEqual(first["applied_revision"], 1)
        self.assertEqual(replay["applied_revision"], 1)
        with self.store.connection() as connection:
            origins = [row[0] for row in connection.execute(
                "SELECT origin FROM version_review_cell_patches ORDER BY revision",
            )]
        self.assertEqual(origins, ["natural_language"])

    def test_checkpoint_failure_rolls_back_draft_and_retry_applies_once(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        request = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"], "expected_revision": 0,
            "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
            "value": "checkpoint 失败后仍可重试", "idempotency_key": "checkpoint-failure-1",
        }
        with self.store.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER diagnostic_checkpoint_failure
                BEFORE INSERT ON version_review_undo_checkpoints
                BEGIN SELECT RAISE(ABORT, 'diagnostic checkpoint fault'); END"""
            )
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "diagnostic checkpoint fault"):
                self.workspace.apply_table_change(token, **request)
        finally:
            with self.store.transaction() as connection:
                connection.execute("DROP TRIGGER diagnostic_checkpoint_failure")
        failed = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        self.assertEqual(failed["revision"], 0)
        self.assertNotEqual(failed["shots"][0]["subtitle"], request["value"])
        recovered = self.workspace.apply_table_change(token, **request)
        self.assertEqual(recovered["applied_revision"], 1)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_review_cell_patches",
            ).fetchone()[0], 1)

    def test_create_draft_crash_recovers_same_command_without_second_draft(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        request = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"], "expected_revision": 0,
            "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
            "value": "创建草稿前中断后恢复", "idempotency_key": "create-crash-recovery-1",
        }
        commands = self.workspace.services.commands
        original = commands._execute_domain
        failed = False

        def crash_once(auth_token, action, parameters, context, **kwargs):
            nonlocal failed
            if action == "create_draft" and not failed:
                failed = True
                raise RuntimeError("create_draft domain crash")
            return original(auth_token, action, parameters, context, **kwargs)

        with patch.object(commands, "_execute_domain", side_effect=crash_once):
            with self.assertRaisesRegex(RuntimeError, "create_draft domain crash"):
                self.workspace.apply_table_change(token, **request)
        recovered = self.workspace.apply_table_change(token, **request)
        self.assertEqual(recovered["applied_revision"], 1)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_review_drafts",
            ).fetchone()[0], 1)

    def test_completed_domain_without_completed_audit_only_finalizes_audit(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        request = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"], "expected_revision": 0,
            "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
            "value": "完整域提交但审计失败", "idempotency_key": "audit-finalize-1",
        }
        commands = self.workspace.services.commands
        original_audit = manual_review_commands_module._audit
        failed = False

        def fail_completed(connection, command_id, context, tool_name, status, request_sha256):
            nonlocal failed
            if status == "completed" and not failed:
                failed = True
                raise RuntimeError("completed audit fault")
            return original_audit(connection, command_id, context, tool_name, status, request_sha256)

        with patch.object(manual_review_commands_module, "_audit", side_effect=fail_completed):
            with self.assertRaisesRegex(RuntimeError, "completed audit fault"):
                self.workspace.apply_table_change(token, **request)
        replay = self.workspace.apply_table_change(token, **request)
        self.assertEqual(replay["applied_revision"], 1)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM manual_command_audits WHERE status='completed'",
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_review_cell_patches",
            ).fetchone()[0], 1)

    def test_concurrent_first_saves_share_one_formal_draft_and_keep_read_current_usable(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        common = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"], "expected_revision": 0,
            "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
        }
        requests = [
            {**common, "value": "并发页面 A", "idempotency_key": "concurrent-first-a"},
            {**common, "value": "并发页面 B", "idempotency_key": "concurrent-first-b"},
        ]
        barrier = threading.Barrier(2)
        commands = self.workspace.services.commands
        original = commands._execute_domain

        def wait_before_create(auth_token, action, parameters, context, **kwargs):
            if action == "create_draft":
                barrier.wait(timeout=10)
            return original(auth_token, action, parameters, context, **kwargs)

        def apply(request):
            try:
                return self.workspace.apply_table_change(token, **request)
            except Exception as error:  # collected so both calls reach their assertions
                return error

        with patch.object(commands, "_execute_domain", side_effect=wait_before_create):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(apply, requests))
        successes = [result for result in results if isinstance(result, dict)]
        failures = [result for result in results if isinstance(result, WorkspaceServiceError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].code, "DRAFT_REVISION_CONFLICT")
        reread = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        self.assertEqual(reread["revision"], 1)
        self.assertIn(reread["shots"][0]["subtitle"], {item["value"] for item in requests})
        continued = self.workspace.apply_table_change(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=current["project_version"], expected_revision=1,
            shot_no="1", shot_id=current["shots"][0]["shot_id"], field="sound",
            value="并发收口后仍可继续修改", idempotency_key="concurrent-first-continued",
        )
        self.assertEqual(continued["revision"], 2)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_review_drafts").fetchone()[0], 1)

    def test_concurrent_same_key_recovery_rechecks_exact_evidence_after_cas_loss(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        request = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"], "expected_revision": 0,
            "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
            "value": "同键恢复只写一次", "idempotency_key": "concurrent-recovery-1",
        }
        commands = self.workspace.services.commands
        original = commands._execute_domain
        failed = False

        def crash_before_apply(auth_token, action, parameters, context, **kwargs):
            nonlocal failed
            if action == "apply_change_set" and not failed:
                failed = True
                raise RuntimeError("同键恢复前首次 domain 中断")
            return original(auth_token, action, parameters, context, **kwargs)

        with patch.object(commands, "_execute_domain", side_effect=crash_before_apply):
            with self.assertRaisesRegex(RuntimeError, "同键恢复前"):
                self.workspace.apply_table_change(token, **request)
        barrier = threading.Barrier(2)

        def wait_on_apply(auth_token, action, parameters, context, **kwargs):
            if action == "apply_change_set":
                barrier.wait(timeout=10)
            return original(auth_token, action, parameters, context, **kwargs)

        def recover():
            try:
                return self.workspace.apply_table_change(token, **request)
            except Exception as error:
                return error

        with patch.object(commands, "_execute_domain", side_effect=wait_on_apply):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: recover(), range(2)))
        self.assertTrue(all(isinstance(result, dict) for result in results), results)
        self.assertTrue(all(result["applied_revision"] == 1 for result in results))
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM version_review_cell_patches WHERE draft_id IN (SELECT draft_id FROM version_review_drafts)",
            ).fetchone()[0], 1)

    def test_concurrent_execute_and_recovery_finalize_one_completed_audit(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        request = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"], "expected_revision": 0,
            "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
            "value": "正常执行和恢复只完成一条审计", "idempotency_key": "concurrent-finalize-1",
        }
        commands = self.workspace.services.commands
        original = commands._execute_domain
        domain_done = threading.Event()
        release = threading.Event()

        def pause_after_domain(auth_token, action, parameters, context, **kwargs):
            result = original(auth_token, action, parameters, context, **kwargs)
            if action == "apply_change_set":
                domain_done.set()
                self.assertTrue(release.wait(timeout=10))
            return result

        with patch.object(commands, "_execute_domain", side_effect=pause_after_domain):
            with ThreadPoolExecutor(max_workers=2) as executor:
                normal_future = executor.submit(self.workspace.apply_table_change, token, **request)
                self.assertTrue(domain_done.wait(timeout=10))
                recovery_future = executor.submit(self.workspace.apply_table_change, token, **request)
                recovery = recovery_future.result(timeout=10)
                release.set()
                normal = normal_future.result(timeout=10)
        self.assertEqual(normal["revision"], recovery["revision"])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM manual_command_audits WHERE tool_name='apply_change_set' AND status='completed'",
            ).fetchone()[0], 1)

    def test_create_command_replay_returns_initial_receipt_after_draft_revision_advances(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        _, context = self.workspace._current_context(token, workspace_id, project_id)
        request = {
            "workspace_id": workspace_id, "project_id": project_id,
            "expected_project_version": current["project_version"], "expected_revision": 0,
            "shot_no": "1", "shot_id": current["shots"][0]["shot_id"], "field": "subtitle",
            "value": "创建回执不随当前修订失效", "idempotency_key": "create-receipt-advances-1",
        }
        self.workspace.apply_table_change(token, **request)
        create_parameters = {
            "project_id": project_id, "version_id": context.version_id,
            "expected_project_lock_version": context.project_lock_version,
            "idempotency_key": f"{request['idempotency_key']}:create",
            "shots": self.workspace._subject_shots(context),
        }
        replay = self.workspace.services.commands.recover_idempotent(
            token, action="create_draft", parameters=create_parameters,
        )
        self.assertEqual(replay["revision"], 0)
        reread = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        self.assertEqual(reread["revision"], 1)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM version_review_drafts").fetchone()[0], 1)

    def test_replayed_create_draft_keeps_formal_single_draft_scope(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        self.workspace.apply_table_change(
            token, workspace_id=workspace_id, project_id=project_id,
            expected_project_version=current["project_version"], expected_revision=0,
            shot_no="1", shot_id=current["shots"][0]["shot_id"], field="subtitle",
            value="先存在的 Formal 草稿", idempotency_key="formal-existing-draft-1",
        )
        _, context = self.workspace._current_context(token, workspace_id, project_id)
        parameters = {
            "project_id": context.project_id,
            "version_id": context.version_id,
            "expected_project_lock_version": context.project_lock_version,
            "idempotency_key": "formal-recovery-scope-1",
            "shots": self.workspace._subject_shots(context),
        }
        self.workspace.services.commands.prepare(
            token, action="create_draft", parameters=parameters,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "SCRIPT_DRAFT_ALREADY_EXISTS"):
            self.workspace._command(
                token, action="create_draft", context=context,
                idempotency_key=parameters["idempotency_key"],
                shots=parameters["shots"],
            )
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM version_review_drafts WHERE project_id=?",
                    (project_id,),
                ).fetchone()[0],
                1,
            )

    def test_table_change_rejects_a_stale_shot_identity(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        current = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.workspace.apply_table_change(
                token, workspace_id=workspace_id, project_id=project_id,
                expected_project_version=current["project_version"], expected_revision=0,
                shot_no="1", shot_id="stale-shot-id", field="subtitle",
                value="不能写入过期镜头", idempotency_key="stale-shot-id-1",
            )

    def test_noop_draft_cannot_create_a_child_version(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        initial = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "DRAFT_NOT_CHANGED"):
            self.workspace.apply_table_change(
                token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=initial["project_version"], expected_revision=0,
                shot_no="1", shot_id=initial["shots"][0]["shot_id"],
                field="visual",
                value=initial["shots"][0]["visual"],
                idempotency_key="noop-table-1",
            )
        self.assertEqual(len(self.projects.list_project_versions(token, project_id)), 1)
        self.assertEqual(len(self.calls), 2)
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM version_review_drafts").fetchone()[0],
                0,
            )

    def test_structured_revision_clarifies_clear_and_delete_then_materializes_n_minus_one(self):
        ambiguous = FormalScriptRevisionService.parse_instruction("把第1镜内容删除")
        self.assertEqual(ambiguous["state"], "needs_clarification")
        clear = FormalScriptRevisionService.parse_instruction("清空第1镜字幕")
        self.assertEqual(clear["operation"], "clear_optional_field")
        deleted = FormalScriptRevisionService.parse_instruction("删除第1镜")
        self.assertEqual(deleted["operation"], "delete_shot")

        initial = self.workspace.read_current(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
        )
        changed = self.workspace.apply_revision_operation(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
            expected_project_version=initial["project_version"], expected_revision=0,
            operation={**deleted, "shot_id": initial["shots"][0]["shot_id"]},
            idempotency_key="delete-shot-revision",
        )
        self.assertEqual(len(changed["shots"]), len(initial["shots"]) - 1)
        self.assertEqual(
            [item["shot_no"] for item in changed["shots"]],
            [str(index) for index in range(1, len(changed["shots"]) + 1)],
        )
        self.assertEqual(changed["shots"][0]["source"], initial["shots"][1]["source"])
        saved = self.workspace.save_version(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
            expected_project_version=initial["project_version"], expected_revision=changed["revision"],
            reason="删除首镜并重排",
            idempotency_key="delete-shot-save",
        )
        self.assertEqual(saved["save_status"], "completed")
        project = self.projects.get_project(
            self.generated.token, self.generated.project["project_id"],
        )
        context = self.delivery.resolve(
            self.generated.token, project["project_id"], project["current_version_id"],
            expected_project_version=project["project_version"],
        )
        binding = json.loads(self.resolver.resolve_stage_artifact(
            context.run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256,
        ).path.read_text(encoding="utf-8"))
        self.assertEqual(len(binding["script_rows"]), len(initial["shots"]) - 1)
        self.assertEqual(len(binding["bindings"]), len(binding["script_rows"]))
        comparison = self.workspace.read_version_diff(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
            version_no=2,
        )
        self.assertTrue(any(item["field"] == "整镜" for item in comparison["changes"]))
        self.assertTrue(any(item["field"] == "镜号" for item in comparison["changes"]))
        workbook = load_workbook(
            self.resolver.resolve_artifact(context.run_id, "workbook").path,
            data_only=True,
        )
        self.addCleanup(workbook.close)
        self.assertEqual(workbook["相邻镜头复核"].max_row - 1, len(binding["script_rows"]) - 1)

    def test_revision_proposal_is_project_scoped_and_read_only_mode_never_promises_execution(self):
        tasks = TaskService(self.store)
        session = tasks.create_agent_session(
            self.generated.token, project_id=self.generated.project["project_id"],
        )
        service = FormalScriptRevisionService(
            projects=self.projects, tasks=tasks, workspace=self.workspace,
            writes_enabled=False,
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "CROSS_PROJECT_DENIED"):
            service.propose(
                self.generated.token,
                agent_session_id=session["agent_session_id"],
                project_id="project_other0000",
                instruction="删除第1镜",
                expected_project_version=self.projects.get_project(
                    self.generated.token, self.generated.project["project_id"],
                )["project_version"],
                expected_revision=0,
                idempotency_key="cross-project-script-proposal",
            )
        proposal = service.propose(
            self.generated.token,
            agent_session_id="",
            project_id=self.generated.project["project_id"],
            instruction="把第1镜字幕修改为匿名活动开始",
            expected_project_version=self.projects.get_project(
                self.generated.token, self.generated.project["project_id"],
            )["project_version"],
            expected_revision=0,
            idempotency_key="readonly-script-proposal",
        )
        self.assertEqual(proposal["status"], "blocked")
        self.assertEqual(proposal["response"]["type"], "read_only_diagnostic")
        self.assertEqual(
            proposal["response"]["error_code"], "FORMAL_SCRIPT_REVISION_DISABLED",
        )
        self.assertNotIn("确认执行", json.dumps(proposal["response"], ensure_ascii=False))

    def test_unified_revision_confirm_creates_child_and_preserves_parent(self):
        tasks = TaskService(self.store)
        service = FormalScriptRevisionService(
            projects=self.projects, tasks=tasks, workspace=self.workspace,
            writes_enabled=True,
        )
        project_id = self.generated.project["project_id"]
        before = self.projects.list_project_versions(self.generated.token, project_id)
        parent = dict(before[-1])
        current = self.workspace.read_current(
            self.generated.token, workspace_id=self.generated.workspace_id,
            project_id=project_id,
        )
        proposal = service.propose_change(
            self.generated.token,
            project_id=project_id,
            expected_project_version=current["project_version"],
            expected_revision=0,
            operation={
                "operation": "replace_field", "shot_no": "1",
                "shot_id": current["shots"][0]["shot_id"],
                "field": "voiceover", "value": "嘉宾从入口走向主舞台，活动正式开始。",
            },
            idempotency_key="unified-table-child-1",
        )
        events = tasks.list_task_events(self.generated.token, proposal["task_id"])
        parameters = next(
            item["details"]["internal_formal_parameters"]
            for item in reversed(events)
            if item["details"].get("internal_formal_parameters")
        )
        result = service.execute(
            self.generated.token,
            project=self.projects.get_project(self.generated.token, project_id),
            parameters=parameters,
        )
        after = self.projects.list_project_versions(self.generated.token, project_id)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[-1]["parent_version_id"], parent["version_id"])
        self.assertEqual(before[-1]["content_sha256"], parent["content_sha256"])
        with self.store.connection() as connection:
            feedback = connection.execute(
                """SELECT feedback_kind, base_version_id, child_version_id
                FROM formal_version_feedback_records WHERE child_version_id=?""",
                (after[-1]["version_id"],),
            ).fetchone()
        self.assertEqual(tuple(feedback), ("copy", parent["version_id"], after[-1]["version_id"]))

    def test_assistant_reselection_rebuilds_whole_shot_proposal_without_another_model_call(self):
        from mvp_v2.formal_script_assistant import FormalScriptAssistantEditorPolicy
        from mvp_v2.action_confirmation import ActionConfirmationService
        from mvp_v2.brief_artifact_store import BriefArtifactStore
        from mvp_v2.executable_agent import ExecutableAgentServices
        from mvp_v2.formal_agent import FormalExecutableAgentService, FormalExecutionBackend
        from mvp_v2.tool_idempotency import ToolIdempotencyService
        token, project_id, workspace_id = self.generated.token, self.generated.project["project_id"], self.generated.workspace_id
        current = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        targets = current["shots"][2:4]
        calls = []
        def editor(prompt, model, max_tokens):
            calls.append(prompt)
            return json.dumps({"schema_version": "mplus-formal-script-assistant-editor/v1", "operations": [
                {"shot_id": shot["shot_id"], "field": "sound", "value": f"持续底乐衔接第{shot['shot_no']}镜现场声。"}
                for shot in targets], "shot_reasons": [
                    {"shot_id": shot["shot_id"], "reason": "让现场声随动作衔接，保留底乐连续性。"} for shot in targets
                ]}, ensure_ascii=False)
        tasks = TaskService(self.store)
        service = FormalScriptRevisionService(projects=self.projects, tasks=tasks, workspace=self.workspace,
            writes_enabled=True, assistant_editor_policy=FormalScriptAssistantEditorPolicy(editor, "test", "test"))
        proposal = service.propose(token, agent_session_id="", project_id=project_id,
            instruction="优化声音", expected_project_version=current["project_version"], expected_revision=0,
            idempotency_key="reselection-original", draft_only=True, entrypoint="assistant")
        self.assertEqual(len(proposal["response"]["shot_reasons"]), 2)
        conflict = service.propose(token, agent_session_id="", project_id=project_id,
            instruction="把第1镜声音修改为一段持续底乐", selected_shot_ids=[targets[0]["shot_id"]],
            expected_project_version=current["project_version"], expected_revision=0,
            idempotency_key="literal-scope-conflict", draft_only=True, entrypoint="assistant")
        self.assertEqual(conflict["response"]["analysis_code"], "scope_conflict")
        with self.assertRaises(WorkspaceServiceError):
            service.reselect_proposal(token, project_id=project_id, task_id=proposal["task_id"],
                retained_shot_ids=[current["shots"][0]["shot_id"]],
                expected_project_version=current["project_version"], expected_revision=0, idempotency_key="invalid-reselection")
        retained = service.reselect_proposal(token, project_id=project_id, task_id=proposal["task_id"],
            retained_shot_ids=[targets[0]["shot_id"]], expected_project_version=current["project_version"],
            expected_revision=0, idempotency_key="reselection-third-only")
        self.assertEqual(len(calls), 1)
        self.assertEqual(tasks.get_task(token, proposal["task_id"])["status"], "cancelled")
        self.assertEqual(retained["response"]["affected_shots"], ["3"])
        self.assertEqual([r["shot_id"] for r in retained["response"]["shot_reasons"]], [targets[0]["shot_id"]])
        self.assertEqual(self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)["revision"], 0)
        readonly = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store, identity=LocalIdentityAdapter(self.store), projects=self.projects,
            tasks=tasks, versions=VersionService(self.store), artifacts=self.resolver))
        agent = FormalExecutableAgentService(ExecutableAgentServices(
            readonly=readonly, projects=self.projects, tasks=tasks,
            confirmations=ActionConfirmationService(self.store, b"r" * 32),
            idempotency=ToolIdempotencyService(self.store),
            brief_store=BriefArtifactStore(self.generated.root / "data"), backend=FormalExecutionBackend()),
            generation=self.generation, advisor=None, writes_enabled=True, script_revision=service)
        confirmed = agent.confirm_action(token, retained["task_id"], proposal_digest=retained["response"]["proposal_digest"])
        self.assertEqual(confirmed["task"]["status"], "completed", confirmed)
        replayed = agent.confirm_action(token, retained["task_id"], proposal_digest=retained["response"]["proposal_digest"])
        self.assertEqual(replayed["task"]["task_id"], confirmed["task"]["task_id"])
        after = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        self.assertEqual(after["revision"], 1)
        for before, actual in zip(current["shots"], after["shots"]):
            if before["shot_id"] != targets[0]["shot_id"]:
                self.assertEqual(actual, before)

    def test_cancel_all_suggestions_replays_without_a_second_task_or_draft_change(self):
        token, project_id = self.generated.token, self.generated.project["project_id"]
        current = self.workspace.read_current(token, workspace_id=self.generated.workspace_id, project_id=project_id)
        tasks = TaskService(self.store)
        service = FormalScriptRevisionService(projects=self.projects, tasks=tasks, workspace=self.workspace, writes_enabled=True)
        proposal = service.propose(token, agent_session_id="", project_id=project_id,
            instruction="把第1镜声音修改为一段持续底乐", selected_shot_ids=[current["shots"][0]["shot_id"]],
            expected_project_version=current["project_version"], expected_revision=0,
            idempotency_key="cancel-all-original", draft_only=True, entrypoint="assistant")
        arguments = dict(project_id=project_id, task_id=proposal["task_id"], retained_shot_ids=[],
            expected_project_version=current["project_version"], expected_revision=0, idempotency_key="cancel-all-once")
        cancelled = service.reselect_proposal(token, **arguments)
        replayed = service.reselect_proposal(token, **arguments)
        self.assertEqual(replayed, cancelled)
        self.assertEqual(cancelled["response"]["type"], "no_changes")
        self.assertEqual(self.workspace.read_current(token, workspace_id=self.generated.workspace_id, project_id=project_id), current)

    def test_literal_assistant_change_cannot_bypass_confirmed_mandatory_wide_requirement(self):
        token, project_id = self.generated.token, self.generated.project["project_id"]
        scope = dict(workspace_id=self.generated.workspace_id, project_id=project_id)
        current = self.workspace.read_current(token, **scope)
        context = self.workspace.assistant_context(token, **scope)
        context["current_script_summary"]["requirements"] = [{"text": "第1镜必须保持全景，交代入口与观众席关系",
            "mandatory": True, "fact_status": "confirmed", "origin": "brief", "decision_source": "brief"}]
        service = FormalScriptRevisionService(projects=self.projects, tasks=TaskService(self.store),
            workspace=self.workspace, writes_enabled=True)
        with patch.object(self.workspace, "assistant_context", return_value=context):
            proposal = service.propose(token, agent_session_id="", project_id=project_id,
                instruction="第1镜的景别改为特写", selected_shot_ids=[current["shots"][0]["shot_id"]],
                expected_project_version=current["project_version"], expected_revision=0,
                idempotency_key="literal-mandatory-wide", draft_only=True, entrypoint="assistant")
        self.assertEqual(proposal["response"]["type"], "needs_clarification")
        self.assertIn("必拍", proposal["response"]["summary"])
        self.assertEqual(self.workspace.read_current(token, **scope), current)

    def test_camera_revision_without_stock_preserves_original_basis_through_undo_and_version(self):
        tasks = TaskService(self.store)
        service = FormalScriptRevisionService(
            projects=self.projects, tasks=tasks, workspace=self.workspace,
            writes_enabled=True,
        )
        project_id = self.generated.project["project_id"]
        before_versions = self.projects.list_project_versions(self.generated.token, project_id)
        before = self.workspace.read_current(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=project_id,
        )
        proposal = service.propose_change(
            self.generated.token,
            project_id=project_id,
            expected_project_version=before["project_version"],
            expected_revision=0,
            operation={
                "operation": "replace_field", "shot_no": "1",
                "shot_id": before["shots"][0]["shot_id"],
                "field": "camera_move", "value": "跟随",
            },
            idempotency_key="visual-source-rematch-required-1",
        )
        self.assertEqual(proposal["response"]["type"], "action_proposal")
        self.assertIn("原创拍摄建议", proposal["response"]["source_impact"])
        after = self.workspace.read_current(self.generated.token, workspace_id=self.generated.workspace_id, project_id=project_id)
        self.assertEqual(after["revision"], before["revision"])
        self.assertEqual(self.projects.list_project_versions(self.generated.token, project_id), before_versions)
        events = tasks.list_task_events(self.generated.token, proposal["task_id"])
        parameters = next(item["details"]["internal_formal_parameters"] for item in reversed(events) if item["details"].get("internal_formal_parameters"))
        operations = parameters["operations"]
        common = {"workspace_id": self.generated.workspace_id, "project_id": project_id,
                  "expected_project_version": before["project_version"]}
        changed = self.workspace.apply_revision_operations(self.generated.token, **common, expected_revision=0,
            operations=operations, idempotency_key="original-apply")
        self.assertEqual(changed["shots"][0]["camera_move"], "跟随")
        self.assertEqual(changed["shots"][0]["source"], before["shots"][0]["source"])
        self.assertEqual(changed["shots"][0]["adaptation"]["origin"], "original_suggestion")
        self.assertEqual(changed["shots"][1:], before["shots"][1:])
        undone = self.workspace.undo(self.generated.token, **common, expected_revision=changed["revision"], idempotency_key="original-undo")
        self.assertEqual(undone["shots"][0]["camera_move"], before["shots"][0]["camera_move"])
        self.assertIsNone(undone["shots"][0]["adaptation"])
        self.assertEqual(undone["shots"], before["shots"])
        redone = self.workspace.redo(self.generated.token, **common, expected_revision=undone["revision"], idempotency_key="original-redo")
        self.assertEqual(redone["shots"][0]["adaptation"], changed["shots"][0]["adaptation"])
        saved = self.workspace.save_version(self.generated.token, **common, expected_revision=redone["revision"],
            reason="原创拍摄建议", idempotency_key="original-save")
        self.assertEqual(saved["save_status"], "completed")
        self.assertEqual(saved["shots"][0]["adaptation"], changed["shots"][0]["adaptation"])
        versions = self.projects.list_project_versions(self.generated.token, project_id)
        self.assertEqual(len(versions), len(before_versions) + 1)
        self.assertEqual(versions[-1]["parent_version_id"], before_versions[-1]["version_id"])
        self.assertEqual(versions[:-1], before_versions)
        _, context = self.workspace._current_context(self.generated.token, self.generated.workspace_id, project_id)
        plan_file = self.workspace.services.artifacts.resolve_stage_artifact(context.run_id, "script_plan", expected_manifest_sha256=context.manifest.sha256).path
        detail = json.loads(plan_file.read_text())["detail_rows"][0]
        basis = json.loads(detail["原创改编依据"])
        self.assertEqual(basis["fields"], {"camera_move": "跟随"})
        self.assertEqual(basis["reference_role"], "创意参考，不证明原创技术参数")
        self.assertEqual(len(basis["basis"]["plan_sha256"]), 64)
        self.assertIn("跟随", detail["改编说明"])

    def test_review_feedback_batches_multiple_shots_into_one_immutable_child(self):
        tasks = TaskService(self.store)
        service = FormalScriptRevisionService(
            projects=self.projects, tasks=tasks, workspace=self.workspace,
            writes_enabled=True,
        )
        project_id = self.generated.project["project_id"]
        before = self.projects.list_project_versions(self.generated.token, project_id)
        proposal = service.propose_review_feedback(
            self.generated.token,
            project_id=project_id,
            note="把第4镜字幕修改为匿名字幕修订；把第7镜声音修改为克制的现场环境声",
            expected_revision=0,
            idempotency_key="review-batch-child-1",
        )
        self.assertEqual(proposal["response"]["affected_shots"], ["4", "7"])
        events = tasks.list_task_events(self.generated.token, proposal["task_id"])
        parameters = next(
            item["details"]["internal_formal_parameters"]
            for item in reversed(events)
            if item["details"].get("internal_formal_parameters")
        )
        result = service.execute(
            self.generated.token,
            project=self.projects.get_project(self.generated.token, project_id),
            parameters=parameters,
        )
        after = self.projects.list_project_versions(self.generated.token, project_id)
        self.assertEqual(result["affected_shots"], ["4", "7"])
        self.assertEqual(len(after), len(before) + 1)
        self.assertEqual(after[-1]["parent_version_id"], before[-1]["version_id"])
        with self.store.connection() as connection:
            feedback = connection.execute(
                """SELECT shot_no, feedback_kind FROM formal_version_feedback_records
                WHERE child_version_id=? ORDER BY CAST(shot_no AS INTEGER)""",
                (after[-1]["version_id"],),
            ).fetchall()
        self.assertEqual([tuple(row) for row in feedback], [("4", "copy"), ("7", "copy")])
        current = self.workspace.read_current(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=project_id,
        )
        self.assertEqual(current["shots"][3]["subtitle"], "匿名字幕修订")
        self.assertEqual(current["shots"][6]["sound"], "克制的现场环境声")

        with self.assertRaisesRegex(WorkspaceServiceError, "INSTRUCTION_AMBIGUOUS"):
            service.parse_review_instructions("第4镜不太好；把第7镜声音修改为更克制")

    def test_editor_role_can_edit_and_save_without_review_permission(self):
        identity = LocalIdentityAdapter(self.store)
        workspace = self.projects.get_workspace(
            self.generated.token, self.generated.workspace_id,
        )
        actor = identity.create_actor(
            self.generated.token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
            display_name="匿名剪辑",
            auth_source="local",
            role="editor",
            expected_workspace_version=workspace["workspace_version"],
        )
        editor_token = identity.issue_auth_session(
            actor_id=actor["actor_id"], token="formal-script-editor-token",
        )["auth_token"]
        initial = self.workspace.read_current(
            editor_token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
        )
        changed = self.workspace.apply_table_change(
            editor_token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
            expected_project_version=initial["project_version"],
            expected_revision=initial["revision"],
            shot_no="1", shot_id=initial["shots"][0]["shot_id"],
            field="notes",
            value="剪辑补充：保留现场真实节奏。",
            idempotency_key="editor-note-1",
        )
        saved = self.workspace.save_version(
            editor_token,
            workspace_id=self.generated.workspace_id,
            project_id=self.generated.project["project_id"],
            expected_project_version=initial["project_version"],
            expected_revision=changed["revision"],
            reason="剪辑补充镜头备注",
            idempotency_key="editor-save-1",
        )
        self.assertEqual(saved["save_status"], "completed")
        self.assertEqual(len(self.calls), 2)
        with self.store.connection() as connection:
            review_permission = connection.execute(
                """SELECT 1 FROM role_assignments ra
                JOIN role_permissions rp ON rp.role_id=ra.role_id
                WHERE ra.actor_id=? AND rp.permission_id='review.record'""",
                (actor["actor_id"],),
            ).fetchone()
        self.assertIsNone(review_permission)

    def test_submission_failure_rolls_back_run_version_pointer_and_outbox(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        initial = self.workspace.read_current(token, workspace_id=workspace_id, project_id=project_id)
        changed = self.workspace.apply_table_change(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=initial["shots"][0]["shot_id"],
            field="notes",
            value="事务回滚匿名测试",
            idempotency_key="rollback-note-1",
        )
        before_project = self.projects.get_project(token, project_id)
        with self.store.connection() as connection:
            before = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in ("project_runs", "project_versions", "outbox"))
        with self.store.transaction() as connection:
            connection.execute(
                """CREATE TRIGGER test_submission_abort
                BEFORE INSERT ON version_review_draft_submissions
                BEGIN SELECT RAISE(ABORT, 'TEST_SUBMISSION_ABORT'); END"""
            )
        with self.assertRaisesRegex(sqlite3.IntegrityError, "TEST_SUBMISSION_ABORT"):
            self.workspace.save_version(
                token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=initial["project_version"],
                expected_revision=changed["revision"],
                reason="验证事务回滚",
                idempotency_key="rollback-save-1",
            )
        after_project = self.projects.get_project(token, project_id)
        with self.store.connection() as connection:
            after = tuple(connection.execute(
                f"SELECT COUNT(*) FROM {table}",
            ).fetchone()[0] for table in ("project_runs", "project_versions", "outbox"))
            submissions = connection.execute(
                "SELECT COUNT(*) FROM version_review_draft_submissions",
            ).fetchone()[0]
        self.assertEqual(after, before)
        self.assertEqual(submissions, 0)
        self.assertEqual(after_project["current_version_id"], before_project["current_version_id"])
        self.assertEqual(after_project["project_version"], before_project["project_version"])

    def test_duration_change_is_versioned_but_creative_standard_avoid_expression_blocks_save(self):
        token = self.generated.token
        workspace_id = self.generated.workspace_id
        project_id = self.generated.project["project_id"]
        initial = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        duration = self.workspace.apply_table_change(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=initial["shots"][0]["shot_id"],
            field="duration_seconds",
            value=initial["shots"][0]["duration_seconds"] + 1,
            idempotency_key="duration-change-1",
        )
        saved_duration = self.workspace.save_version(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=initial["project_version"],
            expected_revision=duration["revision"],
            reason="调整首镜时长并记录总时长变化",
            idempotency_key="duration-save-1",
        )
        self.assertEqual(saved_duration["save_status"], "completed")
        current_after_save = self.workspace.read_current(
            token, workspace_id=workspace_id, project_id=project_id,
        )
        forbidden = self.workspace.apply_table_change(
            token,
            workspace_id=workspace_id,
            project_id=project_id,
            expected_project_version=current_after_save["project_version"],
            expected_revision=0,
            shot_no="1", shot_id=current_after_save["shots"][0]["shot_id"],
            field="visual",
            value="镜头直接虚构事实并作为结论展示。",
            idempotency_key="avoid-change-1",
        )
        with self.assertRaisesRegex(WorkspaceServiceError, "SCRIPT_VERSION_AUDIT_FAILED"):
            self.workspace.save_version(
                token,
                workspace_id=workspace_id,
                project_id=project_id,
                expected_project_version=current_after_save["project_version"],
                expected_revision=forbidden["revision"],
                reason="禁止表达不应保存",
                idempotency_key="avoid-save-1",
            )
        audits = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in self.generated.root.rglob("formal-script.audit.json")
        ]
        blocking = {code for audit in audits for code in audit.get("blocking_items", [])}
        self.assertIn("creative_standard_avoid_expression_hit", blocking)
        self.assertEqual(len(self.projects.list_project_versions(token, project_id)), 2)
        self.assertEqual(len(self.calls), 2)

    def test_strict_http_routes_share_the_same_draft_and_hide_internal_evidence(self):
        agent = ReadonlyAgentService(ReadonlyAgentServices(
            store=self.store,
            identity=LocalIdentityAdapter(self.store),
            projects=self.projects,
            tasks=TaskService(self.store),
            versions=VersionService(self.store),
            artifacts=self.resolver,
        ))
        agent.script_revision = FormalScriptRevisionService(
            projects=self.projects, tasks=TaskService(self.store),
            workspace=self.workspace, writes_enabled=True,
        )
        client = TestClient(create_app(
            agent_api=agent, formal_script_service=self.workspace,
        ))
        self.addCleanup(client.close)
        project_id = self.generated.project["project_id"]
        workspace_id = self.generated.workspace_id
        path = f"/api/formal/projects/{project_id}/script?workspace_id={workspace_id}"
        headers = {"X-MPlus-Agent-Auth": self.generated.token}
        self.assertEqual(client.get(path).status_code, 401)
        self.assertEqual(
            client.get(
                f"/api/formal/projects/{project_id}/script?workspace_id=workspace_other",
                headers=headers,
            ).status_code,
            403,
        )
        initial = client.get(path, headers=headers)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(initial.json()["state"], "ready")
        review_workbook = client.get(
            f"/api/formal/projects/{project_id}/script/versions/1/review-workbook?workspace_id={workspace_id}",
            headers=headers,
        )
        self.assertEqual(review_workbook.status_code, 200, review_workbook.text)
        self.assertIn(
            "脚本复核稿-V1.xlsx",
            unquote(review_workbook.headers["content-disposition"]),
        )
        downloaded = load_workbook(BytesIO(review_workbook.content), data_only=True)
        self.addCleanup(downloaded.close)
        self.assertIn("复核稿，非正式交付", downloaded["脚本"]["A1"].value)
        with self.store.connection() as connection:
            draft_count = connection.execute(
                "SELECT COUNT(*) FROM version_review_drafts",
            ).fetchone()[0]
        rejected = client.post(
            f"{path.rsplit('?', 1)[0]}/table?workspace_id={workspace_id}",
            headers=headers,
            json={
                "expected_revision": 0,
                "shot_no": "1",
                "field": "visual",
                "value": "不会写入",
                "idempotency_key": "http-extra-1",
                "draft_id": "forged",
            },
        )
        self.assertEqual(rejected.status_code, 422)
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM version_review_drafts").fetchone()[0],
                draft_count,
            )
        table = client.post(
            f"{path.rsplit('?', 1)[0]}/table?workspace_id={workspace_id}",
            headers=headers,
            json={
                "expected_project_version": initial.json()["project_version"],
                "expected_revision": 0,
                "shot_no": "1", "shot_id": initial.json()["shots"][0]["shot_id"],
                "field": "visual",
                "value": "匿名嘉宾在主舞台说明发布重点。",
                "idempotency_key": "http-table-1",
            },
        )
        self.assertEqual(table.status_code, 200)
        natural = client.post(
            f"{path.rsplit('?', 1)[0]}/instruction?workspace_id={workspace_id}",
            headers=headers,
            json={
                "expected_project_version": initial.json()["project_version"],
                "expected_revision": 1,
                "instruction": "把第2镜声音修改为现场掌声后衔接克制的节奏音乐",
                "idempotency_key": "http-natural-1",
            },
        )
        self.assertEqual(natural.status_code, 200)
        self.assertEqual((table.json()["state"], table.json()["revision"]), ("editing", 1))
        self.assertEqual(table.json()["shots"][0]["visual"], "匿名嘉宾在主舞台说明发布重点。")
        self.assertEqual(natural.json()["response"]["type"], "action_proposal")
        public = json.dumps(natural.json(), ensure_ascii=False)
        for secret in (
            "artifact://", "sha256", "draft_id", "version_id", "run_id",
            "actor_id", str(self.generated.root),
        ):
            self.assertNotIn(secret, public)
        with self.store.connection() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM version_review_drafts").fetchone()[0],
                draft_count + 1,
            )
        self.assertEqual(len(self.calls), 2)
