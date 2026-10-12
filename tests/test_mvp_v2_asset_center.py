from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from PIL import Image

from mvp_v2.asset_center import (
    AssetCenterError,
    AssetCenterService,
    AssetImportFile,
)
from mvp_v2.asset_package import bundle_asset_package_v2, bundle_asset_package_v3, create_asset_package, load_asset_package
from mvp_v2.asset_storage import LocalAssetStorage
from mvp_v2.package_registry import LocalPackageRegistry, build_package_record


FIXTURE = Path(__file__).parent / "fixtures" / "asset_center" / "import_cases.json"


class AssetCenterServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.fixture = fixture
        source = self.root / "source"
        media = self.root / "media"
        source.mkdir()
        media.mkdir()
        _write_source_workbook(source / "M+发布会类通用脚本库.xlsx")
        self.existing_image = media / "existing.png"
        _image_bytes(self.existing_image)
        self.package_manifest = create_asset_package(
            self.root / "package",
            package_id=fixture["package_id"],
            source_root=source,
            media_roots=[media],
            category_id=fixture["category_id"],
        )
        manifest = json.loads(self.package_manifest.read_text(encoding="utf-8"))
        manifest["package_version"] = fixture["package_version"]
        self.package_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        self.storage = LocalAssetStorage(self.root / "asset-blobs")
        self.service = AssetCenterService(self.package_manifest, storage=self.storage)

    def tearDown(self):
        self.temporary.cleanup()

    def test_preflight_accepts_real_image_and_xlsx_without_vision_provider_calls(self):
        image = self._stored_file("anonymous-reference.png", "image", _new_image_bytes("blue"))
        workbook = self._stored_file("anonymous-source.xlsx", "workbook", _workbook_bytes())

        with patch("mvp_v2.image_understanding.generate_minimax_visual_analysis") as provider:
            result = self.service.stage_import(
                import_id="import_anon_valid_001",
                category_id="launch",
                scope="shared",
                files=[image, workbook],
                requested_by_actor_id=self.fixture["actor_ids"]["requester"],
                expected_package_version=3,
            )

        self.assertEqual(result["state"], "staged")
        self.assertEqual(result["machine_review_status"], "passed")
        self.assertEqual([item["validation_status"] for item in result["items"]], ["valid", "valid"])
        provider.assert_not_called()

    def test_sha_duplicate_reuses_existing_media_identity_and_calls_no_provider(self):
        existing_bytes = self.existing_image.read_bytes()
        duplicate = self._stored_file("renamed-copy.png", "image", existing_bytes)

        with patch("mvp_v2.image_understanding.generate_minimax_visual_analysis") as provider:
            result = self.service.stage_import(
                import_id="import_anon_duplicate_001",
                category_id="launch",
                scope="shared",
                files=[duplicate],
                requested_by_actor_id=self.fixture["actor_ids"]["requester"],
                expected_package_version=3,
            )

        item = result["items"][0]
        self.assertEqual(item["validation_status"], "duplicate")
        self.assertEqual(item["asset_id"], f"media_{duplicate.content_sha256[:20]}")
        self.assertEqual(item["duplicate_of"], item["asset_id"])
        provider.assert_not_called()

    def test_same_batch_duplicate_is_collapsed_by_sha(self):
        content = _new_image_bytes("navy")
        first = self._stored_file("same-a.png", "image", content)
        second = AssetImportFile(
            artifact_ref="artifact://upload/anonymous/same-b.png",
            storage_ref=first.storage_ref,
            content_sha256=first.content_sha256,
            filename="same-b.png",
            asset_kind="image",
        )

        result = self.service.stage_import(
            import_id="import_anon_batch_duplicate_001",
            category_id="launch",
            scope="shared",
            files=[first, second],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )

        self.assertEqual(result["items"][0]["validation_status"], "valid")
        self.assertEqual(result["items"][1]["validation_status"], "duplicate")
        self.assertEqual(result["items"][1]["duplicate_of"], result["items"][0]["asset_id"])

    def test_machine_pass_never_publishes_before_explicit_human_review(self):
        image = self._stored_file("review-required.png", "image", _new_image_bytes("green"))
        staged = self.service.stage_import(
            import_id="import_anon_review_001",
            category_id="launch",
            scope="project",
            project_id=self.fixture["project_ids"]["alpha"],
            files=[image],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )

        with self.assertRaisesRegex(AssetCenterError, "REVIEW_REQUIRED"):
            self.service.publish_import(
                staged["import_id"],
                published_by_actor_id=self.fixture["actor_ids"]["publisher"],
                expected_revision=staged["revision"],
            )

        reviewed = self.service.review_import(
            staged["import_id"],
            reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"],
            expected_revision=staged["revision"],
        )
        published = self.service.publish_import(
            reviewed["import_id"],
            published_by_actor_id=self.fixture["actor_ids"]["publisher"],
            expected_revision=reviewed["revision"],
        )

        self.assertEqual(reviewed["state"], "reviewed")
        self.assertEqual(published["state"], "published")
        self.assertEqual(published["items"][0]["availability"], "available")
        self.assertEqual(published["target_project_id"], self.fixture["project_ids"]["alpha"])

    def test_project_scope_requires_target_and_shared_scope_forbids_it(self):
        image = self._stored_file("scope.png", "image", _new_image_bytes("lime"))
        common = dict(
            import_id="import_anon_scope_001",
            category_id="launch",
            files=[image],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )

        with self.assertRaisesRegex(AssetCenterError, "INVALID_INPUT"):
            self.service.stage_import(scope="project", **common)
        with self.assertRaisesRegex(AssetCenterError, "INVALID_INPUT"):
            self.service.stage_import(
                scope="shared",
                project_id=self.fixture["project_ids"]["alpha"],
                **common,
            )

    def test_failed_preflight_is_audited_and_retryable_with_same_import(self):
        content = _new_image_bytes("purple")
        stored = self._stored_file("recoverable.png", "image", content)
        self.storage.path_for(stored.storage_ref).unlink()

        failed = self.service.stage_import(
            import_id="import_anon_retry_001",
            category_id="launch",
            scope="shared",
            files=[stored],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )
        self.assertEqual(failed["machine_review_status"], "failed")
        self.assertEqual(failed["state"], "staged")
        self.assertEqual(failed["last_error_code"], "STORAGE_NOT_FOUND")

        restored_ref, restored_sha = self.storage.put(BytesIO(content))
        self.assertEqual((restored_ref, restored_sha), (stored.storage_ref, stored.content_sha256))
        recovered = self.service.retry_preflight(
            failed["import_id"],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_revision=failed["revision"],
        )

        self.assertEqual(recovered["machine_review_status"], "passed")
        event_types = [event["event_type"] for event in self.service.list_events(failed["import_id"])]
        self.assertEqual(
            event_types,
            ["asset_import.staged", "asset_import.preflight_failed", "asset_import.preflight_retried"],
        )

    def test_audit_events_are_immutable(self):
        image = self._stored_file("immutable.png", "image", _new_image_bytes("orange"))
        result = self.service.stage_import(
            import_id="import_anon_audit_001",
            category_id="launch",
            scope="shared",
            files=[image],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )

        with sqlite3.connect(self.service.catalog_path) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "ASSET_AUDIT_IMMUTABLE"):
                connection.execute(
                    "UPDATE asset_import_events SET event_type='tampered' WHERE import_id=?",
                    (result["import_id"],),
                )

    def test_only_logical_references_are_accepted_or_persisted(self):
        stored_ref, digest = self.storage.put(BytesIO(_new_image_bytes("black")))
        with self.assertRaisesRegex(AssetCenterError, "INVALID_INPUT"):
            self.service.stage_import(
                import_id="import_anon_path_001",
                category_id="launch",
                scope="shared",
                files=[AssetImportFile(
                    artifact_ref=str(self.root / "private.png"),
                    storage_ref=stored_ref,
                    content_sha256=digest,
                    filename="private.png",
                    asset_kind="image",
                )],
                requested_by_actor_id=self.fixture["actor_ids"]["requester"],
                expected_package_version=3,
            )

        with sqlite3.connect(self.service.catalog_path) as connection:
            tables = ["asset_imports", "asset_import_items", "asset_import_events"]
            for table in tables:
                if connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone() is None:
                    continue
                rows = connection.execute(f"SELECT * FROM {table}").fetchall()
                self.assertNotIn(str(self.root), json.dumps(rows, ensure_ascii=False))

    def test_search_contract_redacts_paths_and_returns_source_version_and_availability(self):
        image = self._stored_file("searchable-stage-light.png", "image", _new_image_bytes("yellow"))
        staged = self.service.stage_import(
            import_id="import_anon_search_001",
            category_id="launch",
            scope="shared",
            files=[image],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )
        reviewed = self.service.review_import(
            staged["import_id"],
            reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"],
            expected_revision=staged["revision"],
        )
        self.service.publish_import(
            reviewed["import_id"],
            published_by_actor_id=self.fixture["actor_ids"]["publisher"],
            expected_revision=reviewed["revision"],
        )

        imported = self.service.search_assets("stage light", kind="image")
        base = self.service.search_assets("测试镜头", kind="script")

        self.assertEqual(imported[0]["source"]["package_id"], self.fixture["package_id"])
        self.assertEqual(imported[0]["source"]["package_version"], 3)
        self.assertEqual(imported[0]["availability"], "available")
        self.assertTrue(imported[0]["source"]["artifact_ref"].startswith("artifact://"))
        self.assertEqual(base[0]["source"]["source_sheet"], "模版")
        result_json = json.dumps([*imported, *base], ensure_ascii=False)
        self.assertNotIn(str(self.root), result_json)
        self.assertNotIn("current_path", result_json)

    def test_project_assets_never_leak_to_another_project_or_anonymous_search(self):
        draft = self._stored_file("draft-search.png", "image", _new_image_bytes("black"))
        draft_record = self.service.stage_import(
            import_id="import_anon_draft_search_001",
            category_id="launch",
            scope="project",
            project_id=self.fixture["project_ids"]["alpha"],
            files=[draft],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )
        shared = self._publish_for_scope(
            import_id="import_anon_shared_search_001",
            filename="shared-search.png",
            color="silver",
            scope="shared",
        )
        alpha = self._publish_for_scope(
            import_id="import_anon_alpha_search_001",
            filename="alpha-search.png",
            color="maroon",
            scope="project",
            project_id=self.fixture["project_ids"]["alpha"],
        )
        beta = self._publish_for_scope(
            import_id="import_anon_beta_search_001",
            filename="beta-search.png",
            color="olive",
            scope="project",
            project_id=self.fixture["project_ids"]["beta"],
        )

        anonymous_ids = {
            row["asset_id"] for row in self.service.search_assets("search", kind="image")
        }
        alpha_ids = {
            row["asset_id"]
            for row in self.service.search_assets(
                "search",
                kind="image",
                current_project_id=self.fixture["project_ids"]["alpha"],
            )
        }
        beta_ids = {
            row["asset_id"]
            for row in self.service.search_assets(
                "search",
                kind="image",
                current_project_id=self.fixture["project_ids"]["beta"],
            )
        }

        self.assertEqual(anonymous_ids, {shared["items"][0]["asset_id"]})
        self.assertEqual(alpha_ids, {shared["items"][0]["asset_id"], alpha["items"][0]["asset_id"]})
        self.assertEqual(beta_ids, {shared["items"][0]["asset_id"], beta["items"][0]["asset_id"]})
        self.assertNotIn(beta["items"][0]["asset_id"], alpha_ids)
        self.assertNotIn(alpha["items"][0]["asset_id"], beta_ids)
        self.assertNotIn(draft_record["items"][0]["asset_id"], alpha_ids)

    def test_idempotency_fingerprint_binds_target_project(self):
        image = self._stored_file("bound-project.png", "image", _new_image_bytes("teal"))
        common = dict(
            import_id="import_anon_target_bound_001",
            category_id="launch",
            scope="project",
            files=[image],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )
        self.service.stage_import(project_id=self.fixture["project_ids"]["alpha"], **common)

        with self.assertRaisesRegex(AssetCenterError, "IDEMPOTENCY_CONFLICT"):
            self.service.stage_import(project_id=self.fixture["project_ids"]["beta"], **common)

    def test_idempotent_stage_rejects_same_import_id_with_changed_payload(self):
        first = self._stored_file("first.png", "image", _new_image_bytes("red"))
        second = self._stored_file("second.png", "image", _new_image_bytes("white"))
        kwargs = dict(
            import_id="import_anon_idempotent_001",
            category_id="launch",
            scope="shared",
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )

        initial = self.service.stage_import(files=[first], **kwargs)
        repeated = self.service.stage_import(files=[first], **kwargs)
        self.assertEqual(initial, repeated)
        with self.assertRaisesRegex(AssetCenterError, "IDEMPOTENCY_CONFLICT"):
            self.service.stage_import(files=[second], **kwargs)

    def test_published_v2_package_is_never_mutated_in_place(self):
        v2_manifest = bundle_asset_package_v2(
            self.package_manifest,
            self.root / "published-package-v2",
            package_version=3,
        )
        service = AssetCenterService(v2_manifest, storage=self.storage)
        image = self._stored_file("next-version.png", "image", _new_image_bytes("cyan"))

        with self.assertRaisesRegex(AssetCenterError, "IMMUTABLE_PACKAGE_VERSION"):
            service.stage_import(
                import_id="import_anon_v2_reject_001",
                category_id="launch",
                scope="shared",
                files=[image],
                requested_by_actor_id=self.fixture["actor_ids"]["requester"],
                expected_package_version=3,
            )

        with sqlite3.connect(service.catalog_path) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_imports'"
            ).fetchone())

    def test_review_rejects_import_bound_to_stale_package_version(self):
        image = self._stored_file("stale.png", "image", _new_image_bytes("magenta"))
        staged = self.service.stage_import(
            import_id="import_anon_stale_001",
            category_id="launch",
            scope="shared",
            files=[image],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )
        manifest = json.loads(self.package_manifest.read_text(encoding="utf-8"))
        manifest["package_version"] = 4
        self.package_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(AssetCenterError, "PACKAGE_VERSION_CONFLICT"):
            self.service.review_import(
                staged["import_id"],
                reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"],
                expected_revision=staged["revision"],
            )

    def test_overlapping_publications_do_not_build_or_remove_each_others_target(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        service, registry, base_v3 = self._publication_service()
        original_sha = build_package_record(base_v3)["package_sha256"]
        image = self._stored_file_for(service, "overlap.png", "image", _new_image_bytes("cyan"))
        staged = service.stage_import(import_id="import_anon_overlap_001", category_id="launch",
            scope="shared", files=[image], requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3)
        reviewed = service.review_import(staged["import_id"],
            reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"], expected_revision=staged["revision"])
        start = Barrier(2)
        def publish(_):
            start.wait(timeout=10)
            try:
                return service.publish_import(reviewed["import_id"],
                    published_by_actor_id=self.fixture["actor_ids"]["publisher"], expected_revision=reviewed["revision"])
            except AssetCenterError:
                return None
        with patch.object(service.package_publication, "_bundler", wraps=service.package_publication._bundler) as build:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(publish, (1, 2)))
            self.assertEqual(build.call_count, 1)
        self.assertEqual(len([value for value in results if value]), 1)
        self.assertEqual(len(registry.list()["packages"]), 2)
        self.assertEqual(registry.resolve_formal("launch")["package_version"], 4)
        self.assertEqual(build_package_record(base_v3)["package_sha256"], original_sha)
        self.assertEqual(service.get_import(reviewed["import_id"])["state"], "published")

    def test_reviewed_import_publishes_v4_and_moves_formal_alias_without_mutating_v3(self):
        service, registry, base_v3 = self._publication_service()
        before_sha = build_package_record(base_v3)["package_sha256"]
        image = self._stored_file_for(service, "published-v4.png", "image", _new_image_bytes("cyan"))

        staged = service.stage_import(
            import_id="import_anon_publish_v4_001",
            category_id="launch",
            scope="shared",
            files=[image],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )
        reviewed = service.review_import(
            staged["import_id"],
            reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"],
            expected_revision=staged["revision"],
        )
        published = service.publish_import(
            reviewed["import_id"],
            published_by_actor_id=self.fixture["actor_ids"]["publisher"],
            expected_revision=reviewed["revision"],
        )

        self.assertEqual(published["state"], "published")
        self.assertEqual(published["package_version"], 4)
        self.assertEqual(len(published["package_sha256"]), 64)
        self.assertTrue(published["alias_switched"])
        self.assertEqual(registry.resolve_formal("launch")["package_version"], 4)
        self.assertEqual(build_package_record(base_v3)["package_sha256"], before_sha)
        package = load_asset_package(registry.resolve_formal("launch")["local_path"])
        self.assertEqual(package["imported_assets"][0]["content_sha256"], image.content_sha256)
        self.assertTrue((package["package_root"] / package["imported_assets"][0]["materialized_path"]).is_file())
        self.assertNotIn(str(self.root), json.dumps(published, ensure_ascii=False))
        self.assertEqual(service.get_import(staged["import_id"])["package_version"], 4)

        second_image = self._stored_file_for(service, "published-v5.png", "image", _new_image_bytes("navy"))
        second_staged = service.stage_import(
            import_id="import_anon_publish_v5_001", category_id="launch", scope="shared",
            files=[second_image], requested_by_actor_id=self.fixture["actor_ids"]["requester"], expected_package_version=4,
        )
        second_reviewed = service.review_import(
            second_staged["import_id"], reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"], expected_revision=second_staged["revision"],
        )
        second_published = service.publish_import(
            second_reviewed["import_id"], published_by_actor_id=self.fixture["actor_ids"]["publisher"], expected_revision=second_reviewed["revision"],
        )
        self.assertEqual(second_published["package_version"], 5)
        self.assertEqual(registry.resolve("launch", "launch-core@4")["package_version"], 4)
        self.assertEqual(registry.resolve_formal("launch")["package_version"], 5)

    def test_publication_build_failure_keeps_reviewed_import_and_is_retryable(self):
        service, registry, base_v3 = self._publication_service()
        image = self._stored_file_for(service, "retry-v4.png", "image", _new_image_bytes("orange"))
        staged = service.stage_import(
            import_id="import_anon_publish_retry_001", category_id="launch", scope="shared",
            files=[image], requested_by_actor_id=self.fixture["actor_ids"]["requester"], expected_package_version=3,
        )
        reviewed = service.review_import(
            staged["import_id"], reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"], expected_revision=staged["revision"],
        )
        original_bundler = service.package_publication._bundler
        service.package_publication._bundler = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected build failure"))
        with self.assertRaisesRegex(AssetCenterError, "PACKAGE_BUILD_FAILED"):
            service.publish_import(reviewed["import_id"], published_by_actor_id=self.fixture["actor_ids"]["publisher"], expected_revision=reviewed["revision"])
        self.assertEqual(service.get_import(staged["import_id"])["state"], "reviewed")
        self.assertEqual(registry.resolve_formal("launch")["package_version"], 3)
        service.package_publication._bundler = original_bundler
        published = service.publish_import(
            reviewed["import_id"], published_by_actor_id=self.fixture["actor_ids"]["publisher"], expected_revision=reviewed["revision"],
        )
        self.assertEqual(published["package_version"], 4)

    def test_publication_promote_failure_keeps_alias_and_retry_succeeds(self):
        service, registry, _base_v3 = self._publication_service()
        image = self._stored_file_for(service, "retry-promote.png", "image", _new_image_bytes("purple"))
        staged = service.stage_import(
            import_id="import_anon_promote_retry_001", category_id="launch", scope="shared",
            files=[image], requested_by_actor_id=self.fixture["actor_ids"]["requester"], expected_package_version=3,
        )
        reviewed = service.review_import(
            staged["import_id"], reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"], expected_revision=staged["revision"],
        )
        publication_registry = service.package_publication.registry
        original_promote = publication_registry.promote
        publication_registry.promote = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected promote failure"))
        with self.assertRaisesRegex(AssetCenterError, "PACKAGE_ALIAS_PROMOTE_FAILED"):
            service.publish_import(reviewed["import_id"], published_by_actor_id=self.fixture["actor_ids"]["publisher"], expected_revision=reviewed["revision"])
        self.assertEqual(service.get_import(staged["import_id"])["state"], "reviewed")
        self.assertEqual(registry.resolve_formal("launch")["package_version"], 3)
        publication_registry.promote = original_promote
        published = service.publish_import(
            reviewed["import_id"], published_by_actor_id=self.fixture["actor_ids"]["publisher"], expected_revision=reviewed["revision"],
        )
        self.assertEqual(published["state"], "published")
        self.assertEqual(registry.resolve_formal("launch")["package_version"], 4)

    def test_publication_register_failure_cleans_unregistered_target_for_retry(self):
        service, registry, _base_v3 = self._publication_service()
        image = self._stored_file_for(service, "retry-register.png", "image", _new_image_bytes("teal"))
        staged = service.stage_import(
            import_id="import_anon_register_retry_001", category_id="launch", scope="shared",
            files=[image], requested_by_actor_id=self.fixture["actor_ids"]["requester"], expected_package_version=3,
        )
        reviewed = service.review_import(
            staged["import_id"], reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"], expected_revision=staged["revision"],
        )
        publication_registry = service.package_publication.registry
        original_register = publication_registry.register_records
        publication_registry.register_records = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("injected register failure"))
        with self.assertRaisesRegex(AssetCenterError, "PACKAGE_REGISTER_FAILED"):
            service.publish_import(reviewed["import_id"], published_by_actor_id=self.fixture["actor_ids"]["publisher"], expected_revision=reviewed["revision"])
        self.assertEqual(service.get_import(staged["import_id"])["state"], "reviewed")
        self.assertEqual(registry.resolve_formal("launch")["package_version"], 3)
        self.assertNotIn("launch-core@4", registry.list()["packages"])
        publication_registry.register_records = original_register
        published = service.publish_import(
            reviewed["import_id"], published_by_actor_id=self.fixture["actor_ids"]["publisher"], expected_revision=reviewed["revision"],
        )
        self.assertEqual(published["package_version"], 4)

    def _publication_service(self):
        source = self.root / "publication-source"
        source.mkdir()
        _write_source_workbook(source / "M+发布会类通用脚本库.xlsx")
        editable = create_asset_package(
            self.root / "publication-editable", package_id="launch-core", source_root=source,
            category_id="launch",
        )
        base_v2 = bundle_asset_package_v2(editable, self.root / "publication-base" / "v3", package_version=3)
        base_v3 = bundle_asset_package_v3(base_v2, self.root / "publication-base" / "v3-self", package_version=3)
        registry = LocalPackageRegistry(self.root / "publication-data" / "package_registry.json")
        registry.register_records([build_package_record(base_v3)], promote_formal=True)
        storage = LocalAssetStorage(self.root / "publication-data" / "uploads")
        service = AssetCenterService(
            editable,
            storage=storage,
            package_registry_path=registry.path,
            packages_root=self.root / "publication-data" / "packages",
        )
        return service, registry, base_v3

    def _stored_file_for(self, service, filename, kind, content):
        storage_ref, digest = service.storage.put(BytesIO(content))
        return AssetImportFile(
            artifact_ref=f"artifact://upload/anonymous/{filename}",
            storage_ref=storage_ref,
            content_sha256=digest,
            filename=filename,
            asset_kind=kind,
        )

    def _stored_file(self, filename: str, kind: str, content: bytes) -> AssetImportFile:
        storage_ref, digest = self.storage.put(BytesIO(content))
        return AssetImportFile(
            artifact_ref=f"artifact://upload/anonymous/{filename}",
            storage_ref=storage_ref,
            content_sha256=digest,
            filename=filename,
            asset_kind=kind,
        )

    def _publish_for_scope(
        self,
        *,
        import_id: str,
        filename: str,
        color: str,
        scope: str,
        project_id: str | None = None,
    ) -> dict:
        image = self._stored_file(filename, "image", _new_image_bytes(color))
        staged = self.service.stage_import(
            import_id=import_id,
            category_id="launch",
            scope=scope,
            project_id=project_id,
            files=[image],
            requested_by_actor_id=self.fixture["actor_ids"]["requester"],
            expected_package_version=3,
        )
        reviewed = self.service.review_import(
            staged["import_id"],
            reviewed_by_actor_id=self.fixture["actor_ids"]["reviewer"],
            expected_revision=staged["revision"],
        )
        return self.service.publish_import(
            reviewed["import_id"],
            published_by_actor_id=self.fixture["actor_ids"]["publisher"],
            expected_revision=reviewed["revision"],
        )


def _new_image_bytes(color: str) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (48, 32), color).save(stream, format="PNG")
    return stream.getvalue()


def _image_bytes(path: Path) -> bytes:
    content = _new_image_bytes("red")
    path.write_bytes(content)
    return content


def _workbook_bytes() -> bytes:
    stream = BytesIO()
    workbook = Workbook()
    workbook.active.title = "匿名素材"
    workbook.active.append(["匿名编号", "说明"])
    workbook.active.append(["A-001", "测试"])
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _write_source_workbook(path: Path) -> None:
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    template.cell(5, 1, "结构")
    template.cell(5, 2, "镜号")
    for column, value in enumerate(
        ["画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"],
        3,
    ):
        template.cell(6, column, value)
    template.cell(7, 1, "开场造势")
    template.cell(7, 2, 1)
    template.cell(7, 5, "转场")
    template.cell(7, 6, "实拍")
    template.cell(7, 7, "全景")
    template.cell(7, 8, "推进")
    template.cell(7, 9, "测试镜头画面")
    creative = workbook.create_sheet("创意库")
    creative.cell(5, 1, "镜号")
    creative.cell(5, 2, "结构")
    for column, value in enumerate(
        ["画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
        4,
    ):
        creative.cell(6, column, value)
    creative.cell(7, 1, "C001")
    creative.cell(7, 2, "开场造势")
    creative.cell(7, 5, "转场")
    creative.cell(7, 6, "实拍")
    creative.cell(7, 7, "全景")
    creative.cell(7, 8, "推进")
    creative.cell(7, 9, "测试创意画面")
    workbook.save(path)
    workbook.close()


if __name__ == "__main__":
    unittest.main()
