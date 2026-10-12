"""Persist paragraph image choices through the existing image authorities."""
from copy import deepcopy
from hashlib import sha256
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from mvp_v2.creative_images import public_references
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.formal_script_workspace import FormalScriptWorkspaceService, FormalScriptWorkspaceServices
from mvp_v2.generation_stages import read_script_plan
from mvp_v2.manual_review_commands import ManualReviewCommandService, ManualReviewCommandServices
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_creative_image_binding as image_fixture
from tests.test_mvp_v2_library_browse import build_anonymous_library_package


class ApplicationImageReferenceTests(unittest.TestCase):
    def setUp(self):
        self.base = image_fixture.CreativeImageBindingTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.service, self.image = self.base.service, self.base.image
        self.repo, self.fixture = self.base.base.repo, self.base.base.fixture
        self.scope, self.token = self.base.scope, self.base.token
        self.library = None

    def _arguments(self, selections, *, replace=(), operation="save-image-references"):
        state = self.repo.read()
        return dict(**self.scope, expected_project_version=state["project_version"],
            expected_revision=state["revision"], operation_key=operation,
            selections=selections, replace_block_ids=list(replace), library_service=self.library)

    def _save(self, selections, *, replace=(), operation="save-image-references"):
        return self.service.save_creative_image_references(
            self.token, **self._arguments(selections, replace=replace, operation=operation))

    def _generated(self, block_id, key):
        return self.base._prepared_reference(block_id, key)["task_id"]

    def _project_image(self, block_id, key):
        args = self.base._prepared_reference(block_id, key)
        return self.service.adopt_image_creation(self.token, **args)["image_key"]

    def _read_refs(self):
        # A fresh application service must project the same persisted identities.
        fresh = self.fixture._service(self.service.provider_policy)
        return fresh.read_creative_draft(self.token, **self.scope)["image_references"]

    def _snapshot(self):
        with self.fixture.store.connection() as connection:
            database = tuple(connection.iterdump())
        return deepcopy(self.repo.read()), database

    def _enable_library(self):
        package = build_anonymous_library_package(self.fixture.root / "anonymous-reference-library")
        self.library = FormalDefaultImageService(
            projects=self.service.services.projects, tasks=self.service.services.tasks,
            delivery=self.image.services.delivery, artifacts=self.service.services.artifacts,
            image_pool=self.service.services.images, image_workspace=self.image,
            asset_package_resolver=lambda _: package, writes_enabled=True,
        )
        self.service.creative_revision.default_images = self.library
        return self.library.candidates(self.token, **self.scope,
            expected_project_version=self.repo.read()["project_version"], view="library")["library_candidates"]

    def _assert_one_primary(self, refs):
        for block_id in {ref["block_id"] for ref in refs}:
            self.assertEqual(sum(ref["primary"] for ref in refs if ref["block_id"] == block_id), 1)

    def test_multi_image_assignment_survives_refresh_and_replace_is_limited_to_target(self):
        first, second = self._generated("block_1", "first"), self._generated("block_2", "second")
        selections = [
            {"block_id": "block_1", "task_id": first, "primary": True},
            {"block_id": "block_1", "task_id": second},
            {"block_id": "block_2", "task_id": second},
            {"block_id": "block_3", "task_id": first},
        ]
        args = self._arguments(selections, replace=["block_1", "block_2", "block_3"])
        saved = self.service.save_creative_image_references(self.token, **args)
        self.assertEqual(saved["count"], 4)
        persisted = self._snapshot()
        refs = self._read_refs()
        self.assertEqual(len(refs), 4)
        self.assertEqual(len({ref["reference_id"] for ref in refs}), 4)
        self._assert_one_primary(refs)
        self.assertEqual(refs, public_references(persisted[0]["body"], **self.scope,
            project_version=persisted[0]["project_version"]))
        self.assertEqual(self.service.save_creative_image_references(self.token, **args), saved)
        self.assertEqual(self._snapshot(), persisted)
        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.service.save_creative_image_references(self.token, **{**args, "selections": []})
        self.assertEqual(self._snapshot(), persisted)
        for ref in refs:
            content, mime = self.service.preview_creative_image_reference(self.token, **self.scope,
                reference_id=ref["reference_id"])
            self.assertEqual(mime, "image/png")
            self.assertEqual(sha256(content).hexdigest(), ref["content_sha256"])

        untouched = [ref for ref in refs if ref["block_id"] != "block_1"]
        self._save([{"block_id": "block_1", "task_id": second, "primary": True}],
            operation="change-primary-only")
        changed = self._read_refs()
        self.assertEqual(len(changed), 4)
        self.assertEqual([ref for ref in changed if ref["block_id"] != "block_1"], untouched)
        self.assertEqual(next(ref for ref in changed if ref["block_id"] == "block_1" and ref["primary"])["task_id"], second)
        self._assert_one_primary(changed)

        self._save([{"block_id": "block_1", "task_id": first}], replace=["block_1"],
            operation="replace-first-block-only")
        replaced = self._read_refs()
        self.assertEqual(len(replaced), 3)
        self.assertEqual([ref for ref in replaced if ref["block_id"] != "block_1"], untouched)
        self._assert_one_primary(replaced)
        self._save([], replace=["block_1"], operation="remove-first-block-only")
        self.assertEqual(self._read_refs(), untouched)

    def test_stale_and_invalid_batches_leave_references_receipts_and_database_unchanged(self):
        first, second = self._generated("block_1", "first"), self._generated("block_2", "second")
        stale = self._arguments([{"block_id": "block_2", "task_id": second}], operation="old-reference-save")
        self._save([{"block_id": "block_1", "task_id": first}], operation="current-reference-save")
        before = self._snapshot()
        with self.assertRaisesRegex(WorkspaceServiceError, "DRAFT_REVISION_CONFLICT"):
            self.service.save_creative_image_references(self.token, **stale)
        self.assertEqual(self._snapshot(), before)
        valid = {"block_id": "block_2", "task_id": second}
        invalid_batches = [
            ("wrong-block", [valid, {"block_id": "missing-block", "task_id": first}], "IMAGE_SCOPE_MISMATCH"),
            ("wrong-project-image", [valid, {"block_id": "block_1", "image_key": "foreign-image"}], "IMAGE_SCOPE_MISMATCH"),
            ("wrong-content", [valid, {"block_id": "block_1", "task_id": first, "content_sha256": "0" * 64}], "IMAGE_ARTIFACT_DRIFT"),
            ("duplicate-image", [valid, valid], "INVALID_INPUT"),
            ("two-primaries", [{**valid, "primary": True}, {"block_id": "block_2", "task_id": first, "primary": True}], "INVALID_INPUT"),
            ("ambiguous-source", [{**valid, "image_key": "foreign-image"}], "INVALID_INPUT"),
        ]
        for key, selections, code in invalid_batches:
            with self.subTest(key=key):
                with self.assertRaisesRegex(WorkspaceServiceError, code):
                    self._save(selections, replace=["block_1", "block_2"], operation="invalid-" + key)
                self.assertEqual(self._snapshot(), before)

    def test_real_library_identity_is_preserved_with_project_and_generated_references(self):
        candidates = self._enable_library()
        library = candidates[0]
        project_image = self._project_image("block_3", "project-source")
        generated = self._generated("block_2", "generated-source")
        version = self.repo.read()["project_version"]
        expected = self.library.progress_references(self.token, **self.scope,
            expected_project_version=version, candidate_keys=[library["candidate_key"]])[library["candidate_key"]]
        with patch.object(self.library, "progress_references", wraps=self.library.progress_references) as resolver:
            self._save([
                {"block_id": "block_1", "library_candidate_key": library["candidate_key"], "primary": True},
                {"block_id": "block_1", "image_key": project_image},
                {"block_id": "block_1", "task_id": generated},
            ], replace=["block_1"], operation="save-three-real-sources")
        resolver.assert_called_once()
        refs = self._read_refs()
        self._assert_one_primary(refs)
        group = [ref for ref in refs if ref["block_id"] == "block_1"]
        self.assertEqual({ref["origin_label"] for ref in group}, {"创意库参考", "项目图片", "AI 生成图"})
        frozen = next(ref for ref in self.repo.read()["body"]["image_references"] if ref.get("kind") == "library")
        for field in ("candidate_key", "media_id", "content_sha256", "sources", "package_id",
                      "package_version", "package_manifest_sha256", "package_sources"):
            self.assertEqual(frozen[field], expected[field], field)
        with patch.object(self.library, "preview", side_effect=AssertionError("Saved previews must use frozen pixels")):
            content, mime = self.service.preview_creative_image_reference(self.token, **self.scope,
                reference_id=frozen["reference_id"])
        self.assertEqual(mime, "image/png")
        self.assertEqual(sha256(content).hexdigest(), expected["content_sha256"])

        before = self._snapshot()
        with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_CANDIDATE_INVALID"):
            self._save([
                {"block_id": "block_2", "library_candidate_key": library["candidate_key"]},
                {"block_id": "block_3", "library_candidate_key": "absent-library-candidate"},
            ], operation="invalid-library-batch")
        self.assertEqual(self._snapshot(), before)
        other = next(candidate for candidate in candidates if candidate["content_sha256"] != library["content_sha256"])
        original_preview = self.library.preview

        def drifting_preview(*args, **kwargs):
            if kwargs["media_id"] == other["candidate_key"]:
                return b"changed-during-save", "image/png"
            return original_preview(*args, **kwargs)

        with patch.object(self.library, "preview", side_effect=drifting_preview):
            with self.assertRaisesRegex(WorkspaceServiceError, "IMAGE_ARTIFACT_DRIFT"):
                self._save([
                    {"block_id": "block_2", "library_candidate_key": library["candidate_key"]},
                    {"block_id": "block_3", "library_candidate_key": other["candidate_key"]},
                ], operation="library-pixels-drift-in-batch")
        self.assertEqual(self._snapshot(), before)

    def test_formal_conversion_freezes_all_sources_but_transfers_only_each_primary(self):
        library = self._enable_library()[0]
        project_image = self._project_image("block_2", "project-primary")
        generated = self._generated("block_3", "generated-primary")
        self._save([
            {"block_id": "block_1", "library_candidate_key": library["candidate_key"], "primary": True},
            {"block_id": "block_1", "task_id": generated},
            {"block_id": "block_2", "image_key": project_image, "primary": True},
            {"block_id": "block_2", "library_candidate_key": library["candidate_key"]},
            {"block_id": "block_3", "task_id": generated, "primary": True},
            {"block_id": "block_3", "image_key": project_image},
        ], replace=["block_1", "block_2", "block_3"], operation="save-conversion-references")
        refs = self.repo.read()["body"]["image_references"]
        plan = self.base.base.plan()
        frozen = read_script_plan(self.service.services.artifacts.resolve_stage_artifact(
            self.service._run_id_from_ref(plan["plan_ref"]), "script_plan").path)
        self.assertEqual(frozen.constraints["creative_draft_binding"]["image_references"], refs)
        for shot in frozen.shot_plans:
            block = shot.get("creative_block")
            if block:
                self.assertEqual(block["image_references"], [ref for ref in refs if ref["block_id"] == block["block_id"]])
        result = self.base.generate(plan)
        self.assertEqual(result["creative_images"]["status"], "progress_saved", result["creative_images"])
        self.assertEqual(result["creative_images"]["count"], 3)
        progress = self.image.read_progress(self.token, **self.scope, library_service=self.library)
        self.assertEqual(len(progress["selections"]), 3)
        numbers = {shot["creative_block"]["block_id"]: str(shot["shot_no"]) for shot in plan["shots"] if shot.get("creative_block")}
        selections = {row["shot_no"]: row for row in progress["selections"]}
        self.assertEqual(selections[numbers["block_1"]]["library_candidate_key"], library["candidate_key"])
        self.assertEqual(selections[numbers["block_2"]]["image_key"], project_image)
        self.assertTrue(selections[numbers["block_3"]]["image_key"])
        self.assertNotEqual(selections[numbers["block_3"]]["image_key"], project_image)
        self.assertTrue(all(not row["checked"] and row["selection_active"] and not row["unavailable"]
                            for row in progress["selections"]))
        self.assertFalse(progress["review_eligible"])
        self.assertEqual(self.repo.read()["body"]["image_references"], refs)
        projected = progress["creative_references"]
        self.assertEqual(len(projected), 6)
        self.assertEqual({ref["reference_id"] for ref in projected}, {ref["reference_id"] for ref in refs})
        self._assert_one_primary(projected)
        for ref in projected:
            self.assertEqual(ref["shot_id"], selections[numbers[ref["block_id"]]]["shot_id"])
            stored = next(item for item in refs if item["reference_id"] == ref["reference_id"])
            self.assertEqual(ref["primary"], stored["primary"])
            self.assertEqual(ref["sources"], stored.get("sources", []))
            self.assertEqual(ref["content_sha256"], stored["content_sha256"])
            query = parse_qs(urlsplit(ref["preview_url"]).query)
            self.assertEqual(query["version_id"], [result["version_id"]])
            content, mime = self.service.preview_creative_image_reference(self.token, **self.scope,
                reference_id=ref["reference_id"], version_id=query["version_id"][0])
            self.assertEqual(mime, "image/png")
            self.assertEqual(sha256(content).hexdigest(), stored["content_sha256"])

        target = selections[numbers["block_3"]]
        self.image.save_progress(self.token, **self.scope, base_version_id=result["version_id"],
            expected_project_version=progress["project_version"], expected_revision=progress["revision"],
            expected_script_revision=progress["script_revision"],
            selections=[{"shot_id": target["shot_id"], "image_key": project_image, "checked": False}],
            idempotency_key="human-changed-after-conversion", library_service=self.library)
        later = self.image.read_progress(self.token, **self.scope, library_service=self.library)
        self.service.inherit_creative_images(self.token, **self.scope, version_id=result["version_id"])
        self.assertEqual(self.image.read_progress(self.token, **self.scope, library_service=self.library), later)
        commands = ManualReviewCommandService(ManualReviewCommandServices(
            store=self.fixture.store, delivery=self.image.services.delivery,
            quality=None, final_confirmation=None, evidence_authority=None))
        script = FormalScriptWorkspaceService(FormalScriptWorkspaceServices(
            store=self.fixture.store, projects=self.service.services.projects,
            delivery=self.image.services.delivery, commands=commands,
            artifacts=self.service.services.artifacts, image_workspace=self.image),
            runs_root=self.fixture.root / "runs", source_root=self.fixture.source_root)
        current = script.read_current(self.token, **self.scope)
        removed = current["shots"][0]
        changed = script.apply_revision_operation(self.token, **self.scope,
            expected_project_version=current["project_version"], expected_revision=current["revision"],
            operation={"operation": "delete_shot", "shot_no": removed["shot_no"], "shot_id": removed["shot_id"]},
            idempotency_key="delete-first-shot-keep-reference-identities")
        self.assertEqual(changed["shots"][0]["shot_no"], "1")
        self.assertEqual(changed["shots"][0]["shot_id"], current["shots"][1]["shot_id"])
        refreshed = self.image.read_progress(self.token, **self.scope, library_service=self.library)
        expected_refs = [ref for ref in projected if ref["shot_id"] != removed["shot_id"]]
        self.assertEqual(len(expected_refs), 4)
        self.assertEqual(refreshed["creative_references"], expected_refs)
        self.assertFalse(any(ref["shot_id"] == removed["shot_id"] for ref in refreshed["creative_references"]))
        with self.fixture.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM version_review_human_decisions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
