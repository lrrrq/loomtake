from types import SimpleNamespace
from copy import deepcopy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests import test_mvp_v2_library_browse as library_tests
import mvp_v2.formal_default_images as default_images
from mvp_v2.formal_console import create_formal_console_app


class LibraryFilterTests(unittest.TestCase):
    setUp = library_tests.LibraryBrowseTests.setUp
    browse = library_tests.LibraryBrowseTests.browse
    def scripted(self, bindings=None):
        original = self.service._context

        def context(*args):
            project, version, plan, _, binding = original(*args)
            plan.shot_plans = [{"primary_event": "现场人气"}] * 3
            draft = SimpleNamespace(script_rows=[
                {"镜号": str(i), "画面描述": "外场入口人群交流"} for i in range(1, 4)
            ], detail_rows=[])
            if bindings is not None:
                binding = SimpleNamespace(bindings=bindings)
            return project, version, plan, draft, binding
        return patch.object(self.service, "_context", side_effect=context)

    def test_explicit_all_library_does_not_compute_shot_recommendations(self):
        with self.scripted(), patch.object(default_images, "search_package_candidates", wraps=default_images.search_package_candidates) as search:
            result = self.browse(shot_no="2", match_scope="all", page_size=12)
        self.assertEqual(search.call_count, 0)
        self.assertEqual(result["match_scope"], "all")
        self.assertEqual(result["page_groups"], 12)
        self.assertGreater(result["total_groups"], 12)
        self.assertEqual(result["shots"], [])

    def test_current_shot_recommendation_filters_before_top_k_and_only_searches_once(self):
        with self.scripted(), patch.object(default_images, "search_package_candidates", wraps=default_images.search_package_candidates) as search:
            result = self.service.candidates(self.base.token, **self.args, shot_no="2",
                match_scope="current_shot", query="编号27")
        self.assertEqual(search.call_count, 1)
        self.assertEqual([shot["shot_no"] for shot in result["shots"]], ["2"])
        self.assertEqual(len(result["shots"][0]["candidates"]), 1)
        self.assertIn("编号27", result["shots"][0]["candidates"][0]["caption"])

    def test_facets_use_known_metadata_and_do_not_claim_package_rows_are_excel(self):
        result = self.browse(match_scope="all", source="package", scene="__unlabeled__")
        self.assertGreater(result["filtered_groups"], 0)
        self.assertTrue(all(item["source_status"] == "trusted_package" for item in result["library_candidates"]))
        self.assertEqual(result["filters"]["source"], "package")
        self.assertIn("__unlabeled__", [item["value"] for item in result["facets"]["scene"]])
        self.assertGreater(result["inventory_records"], result["total_images"])

    def test_recommended_query_without_explicit_scope_is_no_longer_ignored(self):
        with self.scripted():
            result = self.service.candidates(self.base.token, **self.args, query="编号27")
        self.assertEqual(len(result["shots"]), 3)
        self.assertTrue(all(len(shot["candidates"]) == 1 for shot in result["shots"]))
        self.assertTrue(all("编号27" in shot["candidates"][0]["caption"] for shot in result["shots"]))

    def test_api_empty_shot_scope_keeps_category_multi_and_unfocused_script_recommendations(self):
        agent = library_tests.library_confirmation_agent(root=self.root, store=self.base.store,
            projects=self.base.projects, service=self.service)
        project = self.base.projects.get_project(self.base.token, self.project_id)
        self.assertIsNone(project["current_version_id"])
        package, media = self.service._package_media(project, {"脚本类型": "发布会"})
        media = deepcopy(media)
        # Give two anonymous media identities usable source rows for category recommendations.
        # HTTP permissions and canonical matching still run through the real services.
        for index, item in enumerate(media, start=1):
            if not any(number in item["caption"] for number in ("编号26", "编号27")):
                continue
            item["source_links"] = [{"reference_kind": "creative", "source_file": "匿名图库.xlsx",
                "source_sheet": "素材", "source_row": index, "structure": "主体", "shot_no": "27",
                "visual_desc": item["caption"], "shot_function": "人群交流", "visual_subject": "人群",
                "tags": "外场 人群 交流", "source_brand": "无"}]
        params = {key: value for key, value in self.args.items() if key != "project_id"}
        params.update(view="recommended", match_scope="", shot_no="", page_size=12)
        with patch.object(self.service, "_package_media", return_value=(package, media)), TestClient(create_formal_console_app(agent_api=agent)) as client:
            endpoint = f"/api/formal/projects/{self.project_id}/images/default-candidates"
            headers = {"X-MPlus-Agent-Auth": self.base.token}
            category = client.get(endpoint, params=params, headers=headers)
            self.assertEqual(category.status_code, 200, category.text)
            self.assertEqual(category.json()["selection_mode"], "category_library_multi")
            self.assertEqual(category.json()["shots"], [])
            self.assertGreater(len(category.json()["library_candidates"]), 1, category.text)
            with self.scripted():
                unfocused = client.get(endpoint, params={**params, "query": "编号27"}, headers=headers)
            self.assertEqual(unfocused.status_code, 200, unfocused.text)
            self.assertEqual([shot["shot_no"] for shot in unfocused.json()["shots"]], ["1", "2", "3"])
            self.assertTrue(all("编号27" in shot["candidates"][0]["caption"] for shot in unfocused.json()["shots"]))
            invalid = client.get(endpoint, params={**params, "match_scope": "current_shot"}, headers=headers)
            self.assertEqual(invalid.status_code, 409, invalid.text)
            self.assertEqual(invalid.json()["detail"]["code"], "INVALID_INPUT")

    def test_empty_search_is_not_reported_as_a_safety_block(self):
        with self.scripted():
            result = self.service.candidates(self.base.token, **self.args,
                shot_no="2", match_scope="current_shot", query="无此匿名画面")
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["shots"][0]["candidates"], [])
        self.assertIn("筛选", result["shots"][0]["gap_reason"])
        self.assertFalse(any("阻断" in warning for warning in result["warnings"]))

    def test_cached_search_reuses_matching_but_keeps_filters_independent(self):
        with self.scripted(), patch.object(self.service, "_library_matches", wraps=self.service._library_matches) as search:
            first = self.browse(match_scope="all", query="编号27")
            second = self.browse(match_scope="all", query="编号26")
            self.assertEqual(search.call_count, 1)
        self.assertNotEqual(first["library_candidates"][0]["candidate_key"], second["library_candidates"][0]["candidate_key"])
        self.assertEqual(first["filtered_groups"], second["filtered_groups"])

    def test_warm_current_recommendation_reuses_preparation_and_authoritative_ranking(self):
        with self.scripted(), patch.object(default_images, "_package_search_inputs", wraps=default_images._package_search_inputs) as prepare, patch.object(default_images, "search_package_candidates", wraps=default_images.search_package_candidates) as search:
            first = self.service.candidates(self.base.token, **self.args,
                shot_no="2", match_scope="current_shot", query="编号27")
            second = self.service.candidates(self.base.token, **self.args,
                shot_no="2", match_scope="current_shot", query="编号26")
        self.assertEqual(prepare.call_count, 1)
        self.assertEqual(search.call_count, 1)
        self.assertNotEqual(first["shots"][0]["candidates"][0]["candidate_key"],
                            second["shots"][0]["candidates"][0]["candidate_key"])

    def test_current_shot_recommendation_only_resolves_its_own_legacy_binding(self):
        candidate = self.browse(match_scope="all")["library_candidates"][0]["media_id"]
        bindings = [{"shot_no": str(index), "media_id": candidate} for index in range(1, 4)]
        with self.scripted(bindings), patch.object(default_images, "resolve_legacy_visual_identity",
                wraps=default_images.resolve_legacy_visual_identity) as resolve:
            result = self.service.candidates(self.base.token, **self.args,
                shot_no="2", match_scope="current_shot")
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(result["shots"][0]["identity_mapping"]["media_id"], candidate)

    def test_cache_does_not_keep_candidates_after_changed_authoritative_metadata(self):
        first = self.browse(match_scope="all", query="编号27")
        package, media = self.service._package_media({"category_id": "launch"}, {"脚本类型": "发布会"})
        media = deepcopy(media)
        target = next(item for item in media if item["media_id"] == first["library_candidates"][0]["candidate_key"])
        target["negative_flags"] = ["forbidden_content"]
        target["visual_analysis"]["negative_flags"] = ["forbidden_content"]
        with patch.object(self.service, "_package_media", return_value=(package, media)):
            changed = self.browse(match_scope="all", query="编号27")
        self.assertEqual(changed["filtered_groups"], 0)
