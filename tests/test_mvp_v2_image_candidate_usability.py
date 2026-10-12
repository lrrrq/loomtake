import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image, PngImagePlugin

from mvp_v2.formal_image_candidates import (
    allocate_diverse_defaults, perceptual_cluster_ids, search_package_candidates,
)
from mvp_v2.image_retrieval import search_images_for_slot


class ImageCandidateUsabilityTests(unittest.TestCase):
    def test_old_reference_quality_warns_without_new_gate_but_project_quality_still_applies(self):
        flags = ["画面分辨率较低", "夜景噪点明显", "severe_blur"]
        manifest = [{"image_id": str(i), "asset_role": "source_reference"} for i in range(3)]
        analyses = [{"image_id": str(i), "caption": "顾客在现场交流互动", "tags": ["顾客", "互动"],
                     "brand_names": ["无"], "quality_flags": [flag]} for i, flag in enumerate(flags)]
        args = ({"镜号": "1", "画面描述": "顾客在现场交流互动"}, {"脚本类型": "品牌活动"})
        for container in ("quality_flags", "negative_flags"):
            with self.subTest(container=container):
                rows = [{**item, "quality_flags": [], container: item["quality_flags"]} for item in analyses]
                result = search_images_for_slot(*args, manifest, rows)
                self.assertEqual(len(result["candidates"]), 3)
                self.assertTrue(all(item["requires_human_review"] and item["quality_warning"] for item in result["candidates"]))
                actual = [{**item, "asset_role": "project_media"} for item in manifest]
                self.assertEqual(search_images_for_slot(*args, actual, rows)["candidates"], [])

    def test_near_similarity_does_not_chain_distinct_scenes_together(self):
        clusters = perceptual_cluster_ids([
            {"image_id": "a", "perceptual_hash": "0000000000000000"},
            {"image_id": "b", "perceptual_hash": "000000000000003f"},
            {"image_id": "c", "perceptual_hash": "0000000000000fff"},
        ])
        self.assertEqual(clusters["a"], clusters["b"])
        self.assertNotEqual(clusters["a"], clusters["c"])

    def test_static_copies_fold_without_losing_sources_or_animation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = Image.new("RGB", (64, 48), (30, 60, 100))
            second = Image.new("RGB", (64, 48), (170, 90, 10))
            first.save(root / "one.png")
            metadata = PngImagePlugin.PngInfo()
            metadata.add_text("encoding-note", "same pixels, different file")
            first.save(root / "two.png", pnginfo=metadata)
            first.save(root / "motion.gif", save_all=True, append_images=[second], duration=100)
            second.save(root / "three.png")
            media = [{
                "media_id": key, "path": root / filename,
                "source": {"file": "anonymous.xlsx", "sheet": "创意库", "row": index + 1},
                "visual_analysis": {"caption": "活动现场", "tags": ["现场"], "brand_names": ["无"]},
            } for index, (key, filename) in enumerate([
                ("a", "one.png"), ("b", "two.png"), ("c", "motion.gif"), ("d", "three.png"),
            ])]
            ranked = [{"image_id": item["media_id"], "score": 100-index} for index, item in enumerate(media)]
            with patch("mvp_v2.formal_image_candidates.search_images_for_slot", return_value={"candidates": ranked}):
                result = search_package_candidates(slot={}, brief={}, media=media, top_k=3)
            rows = result["candidates"]
            by_id = {item["image_id"]: item for item in rows}
            self.assertEqual(set(by_id), {"a", "b", "c", "d"})
            self.assertEqual([item["image_id"] for item in rows if not item["duplicate_of"]], ["a", "c", "d"])
            self.assertEqual(by_id["b"]["duplicate_of"], "a")
            self.assertEqual(by_id["a"]["duplicate_count"], 1)
            self.assertNotEqual(by_id["a"]["duplicate_group_id"], by_id["c"]["duplicate_group_id"])
            clusters = perceptual_cluster_ids([{"image_id": item["media_id"], "path": item["path"]} for item in media])
            self.assertNotEqual(clusters["a"], clusters["c"])

    def test_variety_does_not_replace_a_better_match_but_breaks_equal_score_ties(self):
        ranked = {
            "1": {"candidates": [{"image_id": "a", "score": 99, "perceptual_cluster_id": "one"}]},
            "2": {"candidates": [
                {"image_id": "a", "score": 98, "perceptual_cluster_id": "one"},
                {"image_id": "b", "score": 70, "perceptual_cluster_id": "two"},
            ]},
            "3": {"candidates": [
                {"image_id": "a", "score": 90, "perceptual_cluster_id": "one"},
                {"image_id": "c", "score": 90, "perceptual_cluster_id": "three"},
            ]},
        }
        result = allocate_diverse_defaults(ranked)
        self.assertEqual(result["selected_by_shot"], {"1": "a", "2": "a", "3": "c"})
        self.assertEqual(set(result["reuse_required_by_shot"]), {"2"})
