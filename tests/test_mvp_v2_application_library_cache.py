"""Gallery pages keep real counts and reuse immutable content evidence."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import unittest
from unittest.mock import patch

from mvp_v2 import formal_default_images as defaults
from mvp_v2.asset_package import bundle_asset_package_v3, create_asset_package
from mvp_v2.package_registry import LocalPackageRegistry
from mvp_v2.workspace_store import WorkspaceServiceError
from tests import test_mvp_v2_library_browse as fixtures


class ApplicationLibraryCacheTests(unittest.TestCase):
    setUp = fixtures.LibraryBrowseTests.setUp
    browse = fixtures.LibraryBrowseTests.browse

    def category_media(self):
        project = self.base.projects.get_project(self.base.token, self.project_id)
        package, original = self.service._package_media(project, {"脚本类型": "发布会"})
        media = deepcopy(original)
        for index, item in enumerate(media, start=1):
            item["source_links"] = [{
                "reference_kind": "creative", "source_file": "匿名图库.xlsx",
                "source_sheet": "素材", "source_row": index,
                "structure": "现场", "shot_no": "2",
                "visual_desc": item["caption"], "shot_function": "人群交流",
                "visual_subject": "人群", "tags": "外场 人群 交流", "source_brand": "无",
            }]
        return package, media

    def test_category_recommendation_counts_and_pages_describe_visible_images(self):
        with patch.object(self.service, "_package_media", return_value=self.category_media()):
            first = self.service.candidates(self.base.token, **self.args, page_size=8)
            second = self.service.candidates(self.base.token, **self.args, page_size=8, page=2)
            empty = self.service.candidates(self.base.token, **self.args, query="不存在的画面")
        self.assertEqual(first["selection_mode"], "category_library_multi")
        self.assertGreater(first["inventory_groups"], 8)
        self.assertGreater(first["filtered_groups"], 8)
        self.assertEqual(first["page_records"], len(first["library_candidates"]))
        self.assertEqual(first["page_groups"], len({item["duplicate_group_id"] for item in first["library_candidates"]}))
        self.assertEqual(first["page_groups"], 8)
        self.assertEqual(second["page"], 2)
        self.assertTrue(first["has_more"])
        self.assertFalse({item["duplicate_group_id"] for item in first["library_candidates"]}
                         & {item["duplicate_group_id"] for item in second["library_candidates"]})
        self.assertEqual(empty["inventory_records"], first["inventory_records"])
        self.assertEqual(empty["filtered_groups"], 0)
        self.assertEqual(empty["page_groups"], 0)
        self.assertEqual(empty["page_records"], 0)
        self.assertEqual(empty["library_candidates"], [])

    def test_category_search_and_inventory_reuse_evidence_across_filters(self):
        with patch.object(self.service, "_package_media", return_value=self.category_media()), \
                patch.object(defaults, "search_package_library_candidates", wraps=defaults.search_package_library_candidates) as search, \
                patch.object(defaults, "_present_distinct_candidates", wraps=defaults._present_distinct_candidates) as inventory:
            first = self.service.candidates(self.base.token, **self.args, query="编号26")
            second = self.service.candidates(self.base.token, **self.args, query="编号27")
        self.assertEqual(search.call_count, 1, "changing filters must not rebuild the full category ranking")
        self.assertEqual(inventory.call_count, 1, "the same frozen inventory must not be regrouped for each page")
        self.assertEqual(first["filtered_groups"], 1)
        self.assertEqual(second["filtered_groups"], 1)
        self.assertNotEqual(first["library_candidates"][0]["candidate_key"], second["library_candidates"][0]["candidate_key"])

    def test_cached_inventory_does_not_hide_changed_content_or_permissions(self):
        first = self.browse(match_scope="all")
        with self.assertRaises(WorkspaceServiceError):
            self.service.candidates("invalid-auth", **self.args, view="library", match_scope="all")
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            self.browse(match_scope="all", expected_project_version=self.args["expected_project_version"] + 1)
        project = self.base.projects.get_project(self.base.token, self.project_id)
        _, media = self.service._package_media(project, {"脚本类型": "发布会"})
        target = next(item for item in media if item["media_id"] == first["library_candidates"][0]["candidate_key"])
        path = Path(target["path"])
        path.write_bytes(path.read_bytes() + b"content changed after cached page")
        with self.assertRaisesRegex(WorkspaceServiceError, "ASSET_PACKAGE_BLOB_DRIFT"):
            self.browse(match_scope="all")

    def test_cached_category_query_tracks_changed_authoritative_metadata(self):
        package, media = self.category_media()
        with patch.object(self.service, "_package_media", return_value=(package, media)):
            first = self.service.candidates(self.base.token, **self.args, query="编号27")
        self.assertEqual(len(first["library_candidates"]), 1)
        changed = deepcopy(media)
        target = next(item for item in changed if item["media_id"] == first["library_candidates"][0]["candidate_key"])
        target["negative_flags"] = ["forbidden_content"]
        target["visual_analysis"]["negative_flags"] = ["forbidden_content"]
        with patch.object(self.service, "_package_media", return_value=(package, changed)):
            second = self.service.candidates(self.base.token, **self.args, query="编号27")
        self.assertEqual(second["library_candidates"], [])
        self.assertEqual(second["filtered_groups"], 0)
        self.assertGreater(second["status_counts"]["forbidden"], first["status_counts"]["forbidden"])

    def test_shared_catalog_resolves_each_registered_package_once_per_request(self):
        registry = LocalPackageRegistry(self.root / "library_registry.json")
        annual_seed = create_asset_package(self.root / "annual-seed", package_id="anonymous-annual",
            category_id="annual", source_root=self.root / "source",
            source_workbook=self.root / "source" / "M+发布会类通用脚本库.xlsx",
            media_roots=[self.root / "library-media"], metadata_roots=[self.root / "library-media"])
        annual = bundle_asset_package_v3(annual_seed, self.root / "annual-package", package_version=1)
        registry.register_records([registry.register(self.package), registry.register(annual)], promote_formal=True)
        self.service.package_registry = registry
        self.service.asset_package_resolver = lambda _: {**registry.resolve_formal("launch"), "alias": "formal"}
        with patch.object(registry, "resolve", wraps=registry.resolve) as resolve:
            result = self.browse(match_scope="all")
        self.assertGreater(result["page_groups"], 0)
        self.assertEqual(resolve.call_count, 2)
        sources = result["library_candidates"][0]["package_sources"]
        self.assertEqual({item["category_id"] for item in sources}, {"launch", "annual"})

    def test_simultaneous_cold_reads_prepare_each_verified_image_once(self):
        registry = LocalPackageRegistry(self.root / "concurrent_registry.json")
        registry.register_records([registry.register(self.package)], promote_formal=True)
        self.service.package_registry = registry
        self.service.asset_package_resolver = lambda _: {**registry.resolve_formal("launch"), "alias": "formal"}
        ready = Barrier(3)
        resolve = self.service._resolved_package_path

        def resolved(value):
            result = resolve(value)
            ready.wait(timeout=5)
            return result

        def read(_):
            return self.service._single_package_media({"category_id": "launch"}, {"脚本类型": "发布会"})

        with patch.object(self.service, "_resolved_package_path", side_effect=resolved), \
                patch.object(defaults, "package_image_similarity", wraps=defaults.package_image_similarity) as pixels, \
                ThreadPoolExecutor(max_workers=3) as pool:
            result = list(pool.map(read, range(3)))
        self.assertEqual(pixels.call_count, len(result[0][1]))
        self.assertEqual([item["media_id"] for item in result[0][1]], [item["media_id"] for item in result[-1][1]])


if __name__ == "__main__":
    unittest.main()
