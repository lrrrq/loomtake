from __future__ import annotations

from unittest import TestCase
from unittest.mock import patch

from openpyxl import load_workbook

from mvp_v2.formal_script_revision import FormalScriptRevisionService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_formal_image_workspace as image_fixture


class UnifiedCreativeSaveTests(TestCase):
    def setUp(self):
        self.fixture = image_fixture.FormalImageWorkspaceTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.images, self.script = self.fixture.workspace, self.fixture.base.workspace
        self.token = self.fixture.token
        self.scope = dict(workspace_id=self.fixture.workspace_id, project_id=self.fixture.project_id)
        self.revision = FormalScriptRevisionService(projects=self.fixture.projects,
            tasks=TaskService(self.fixture.store), workspace=self.script, image_workspace=self.images,
            writes_enabled=True)

    def _versions(self):
        return self.fixture.projects.list_project_versions(self.token, self.fixture.project_id)

    def _confirm_progress(self):
        current = self.script.read_current(self.token, **self.scope)
        progress = self.images.read_progress(self.token, **self.scope)
        selections = []
        for shot in current["shots"]:
            found = self.images.search_shot(self.token, **self.scope, base_version_id=current["base_version_id"],
                expected_project_version=current["project_version"], shot_no=shot["shot_no"], query="")
            self.assertTrue(found["candidates"], shot["shot_no"])
            selections.append(dict(shot_id=shot["shot_id"], image_key=found["candidates"][0]["image_key"],
                checked=True, reuse_reason="匿名验收：同一场景跨镜承接"))
        return self.images.save_progress(self.token, **self.scope, base_version_id=current["base_version_id"],
            expected_project_version=current["project_version"], expected_revision=progress["revision"],
            expected_script_revision=current["revision"], selections=selections, idempotency_key="checked-progress")

    def _edit_sound(self):
        current = self.script.read_current(self.token, **self.scope)
        shot = current["shots"][1]
        return self.script.apply_table_change(self.token, **self.scope,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            shot_id=shot["shot_id"], shot_no=shot["shot_no"], field="sound",
            value="统一保存验证：音乐渐强后衔接现场声音。", idempotency_key="sound-edit")

    def test_missing_images_keep_text_progress_without_new_version(self):
        current = self._edit_sound()
        versions = self._versions()
        with self.assertRaises(WorkspaceServiceError) as caught:
            self.revision.propose_draft_save(self.token, project_id=self.fixture.project_id,
                expected_revision=current["revision"], expected_project_version=current["project_version"],
                reason="完整保存", idempotency_key="missing-images")
        self.assertEqual(caught.exception.code, "IMAGE_CONFIRMATION_REQUIRED")
        self.assertEqual(len(caught.exception.issues), len(current["shots"]))
        self.assertEqual(self._versions(), versions)
        self.assertEqual(self.script.read_current(self.token, **self.scope)["shots"], current["shots"])

    def test_text_and_all_images_create_one_version_and_replay(self):
        progress = self._confirm_progress()
        current = self._edit_sound()
        versions = self._versions()
        proposal = self.revision.propose_draft_save(self.token, project_id=self.fixture.project_id,
            expected_revision=current["revision"], expected_project_version=current["project_version"],
            expected_progress_revision=progress["revision"], reason="文字与配图一起保存", idempotency_key="unified-proposal")
        self.assertEqual(len(proposal["response"]["preview"]["image_changes"]), len(current["shots"]))
        prepared = self.images.prepare_working_version(self.token, **self.scope,
            expected_project_version=current["project_version"], expected_script_revision=current["revision"],
            expected_progress_revision=progress["revision"])
        kwargs = dict(**self.scope, expected_revision=current["revision"],
            expected_project_version=current["project_version"], expected_progress_revision=progress["revision"],
            image_snapshot_digest=prepared["snapshot_digest"], reason="文字与配图一起保存", idempotency_key="unified-save")
        saved = self.script.save_version(self.token, **kwargs)
        self.assertEqual(len(self._versions()), len(versions) + 1)
        self.assertEqual(saved["shots"][1]["sound"], current["shots"][1]["sound"])
        self.assertEqual(self.script.save_version(self.token, **kwargs), saved)
        image_model = self.images.read_current(self.token, **self.scope)
        self.assertTrue(image_model["review_eligible"])
        self.assertEqual(self.images.read_progress(self.token, **self.scope)["selections"], [])
        context = self.fixture.delivery.resolve(self.token, self.fixture.project_id, saved["base_version_id"],
            expected_project_version=saved["project_version"])
        workbook = load_workbook(self.fixture.resolver.resolve_artifact(context.run_id, "workbook").path)
        self.assertEqual(len(workbook["脚本"]._images), len(current["shots"]))
        comparison = self.script.read_version_diff(self.token, **self.scope, version_no=len(versions) + 1)
        self.assertEqual(comparison["state"], "verified")
        image_changes = [item for item in comparison["changes"] if item["field"] == "配图"]
        self.assertEqual(len(image_changes), len(current["shots"]))
        self.assertTrue(all(item["after_image_key"] for item in image_changes))
        self.assertTrue(any(item["field"] == "声音" for item in comparison["changes"]))
        self.assertTrue(any("配图按已确认快照更新" in item for item in comparison["verification_items"]))
        with patch.object(self.images, '_read_snapshot', return_value={}):
            with self.assertRaises(WorkspaceServiceError) as drift:
                self.script.read_version_diff(self.token, **self.scope, version_no=len(versions) + 1)
        self.assertEqual(drift.exception.code, 'ARTIFACT_INVALID')

    def test_progress_change_during_render_preserves_both_drafts_and_version(self):
        progress = self._confirm_progress()
        current = self._edit_sound()
        versions = self._versions()
        original = self.script.materializer.materialize

        def concurrent_progress(**kwargs):
            rendered = original(**kwargs)
            self.images.save_progress(self.token, **self.scope, base_version_id=current["base_version_id"],
                expected_project_version=current["project_version"], expected_revision=progress["revision"],
                expected_script_revision=current["revision"], selections=[dict(
                    shot_id=current["shots"][0]["shot_id"], remove=True)], idempotency_key="concurrent-remove")
            return rendered

        with patch.object(self.script.materializer, "materialize", side_effect=concurrent_progress):
            with self.assertRaises(WorkspaceServiceError) as caught:
                self.script.save_version(self.token, **self.scope, expected_revision=current["revision"],
                    expected_project_version=current["project_version"], expected_progress_revision=progress["revision"],
                    reason="并发保存", idempotency_key="concurrent-save")
        self.assertEqual(caught.exception.code, "IMAGE_PROGRESS_CONFLICT")
        self.assertEqual(self._versions(), versions)
        self.assertTrue(self.script.read_current(self.token, **self.scope)["can_save_version"])

    def test_image_only_save_uses_same_entry_and_one_version(self):
        progress = self._confirm_progress()
        current = self.script.read_current(self.token, **self.scope)
        versions = self._versions()
        prepared = self.images.prepare_working_version(self.token, **self.scope,
            expected_project_version=current["project_version"], expected_script_revision=current["revision"])
        kwargs = dict(**self.scope, expected_revision=current["revision"],
            expected_project_version=current["project_version"], expected_progress_revision=progress["revision"],
            image_snapshot_digest=prepared["snapshot_digest"], reason="仅配图保存", idempotency_key="image-only-unified")
        saved = self.script.save_version(self.token, **kwargs)
        self.assertEqual(len(self._versions()), len(versions) + 1)
        self.assertEqual(self.script.save_version(self.token, **kwargs), saved)

    def test_bulk_partial_confirmation_is_progress_only_and_replays(self):
        current = self._edit_sound()
        progress = self.images.read_progress(self.token, **self.scope)
        shots = current["shots"][:3]
        found = self.images.search_shot(self.token, **self.scope, base_version_id=current["base_version_id"],
            expected_project_version=current["project_version"], shot_no=shots[0]["shot_no"], query="")
        selections = [dict(shot_id=shot["shot_id"], image_key=found["candidates"][0]["image_key"] if i < 2 else "",
            reuse_reason="匿名同场景用途") for i, shot in enumerate(shots)]
        args = dict(**self.scope, base_version_id=current["base_version_id"],
            expected_project_version=current["project_version"], expected_revision=progress["revision"],
            expected_script_revision=current["revision"], selections=selections)
        preview = self.images.preview_progress_confirmation(self.token, **args)
        self.assertEqual(preview["processable_count"], 2)
        self.assertEqual(preview["pending_count"], 1)
        versions = self._versions()
        args.update(preview_digest=preview["preview_digest"],
            accepted_shot_ids=[s["shot_id"] for s in shots[:2]], idempotency_key="partial-confirm")
        result = self.images.confirm_progress(self.token, **args)
        self.assertEqual(result["confirmed_count"], 2)
        self.assertTrue(all(item["checked"] for item in result["selections"]))
        self.assertEqual(self._versions(), versions)
        self.assertEqual(self.images.confirm_progress(self.token, **args), result)
