from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from mvp_v2 import image_understanding as vision
from mvp_v2.asset_package import create_asset_package, load_asset_package, bundle_asset_package_v2
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.run_contract import sha256_file
from tests.test_mvp_v2_asset_package import _write_source_with_reference_image


class ImageAnalysisCompatibilityTests(unittest.TestCase):
    def test_valid_provider_json_keeps_escaped_caption_quotes_and_every_observation(self):
        value = {
            "caption": '屏幕写着 "ANONYMOUS"，人物站在屏幕前。',
            "tags": ["现场人物", "屏幕"], "brand_names": ["未知"],
            "main_subject": "站立的人", "background_subjects": ["显示文字的屏幕"],
            "foreground_subjects": ["人物"], "subjects": ["人物", "屏幕"],
            "camera_angle": "平视", "uncertainties": ["屏幕文字不是现场品牌判定依据"],
            "confidence": 0.7, "quality_flags": [], "negative_flags": [],
            "ocr_text": ['ANONYMOUS', '第二行'],
        }
        for raw in (json.dumps(value), json.dumps({"content": "```json\n" + json.dumps(value) + "\n```"})):
            with self.subTest(raw=raw):
                self.assertEqual(vision._parse_minimax_output(raw), value)

    def test_partial_json_recovery_preserves_independent_observations_and_marks_uncertainty(self):
        malformed = '{"caption":"人物站立","tags":["人物"] "main_subject":"人物", "background_subjects":["屏幕"], "camera_angle":"平视", "quality_flags":[]}'
        recovered = vision._parse_minimax_output(malformed)
        self.assertEqual(recovered["main_subject"], "人物")
        self.assertEqual(recovered["background_subjects"], ["屏幕"])
        self.assertEqual(recovered["camera_angle"], "平视")
        self.assertTrue(recovered.get("_parse_warning"))

    def test_quality_uses_file_pixels_and_keeps_unverified_provider_description_separate(self):
        value = {"caption": "广告牌中的人物持杯", "tags": ["广告牌"], "brand_names": ["无"],
                 "main_subject": "广告牌", "quality": "1080p，画质很好", "quality_flags": ["low_resolution"],
                 "ocr_text": ["ANONYMOUS", "部分文字"], "camera_angle": "unknown", "action_type": "none"}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "anonymous.png"
            Image.new("RGB", (1000, 563), "blue").save(path)
            with patch("mvp_v2.image_understanding._run_minimax_vision", return_value=json.dumps(value)) as provider:
                row = vision.generate_minimax_visual_analysis_row({"image_id": "anonymous", "原图路径": str(path)}, "")
            self.assertEqual((row["pixel_width"], row["pixel_height"]), (1000, 563))
            self.assertIn("1000×563", row["quality"])
            self.assertNotIn("1080p", row["quality"])
            self.assertIn("分辨率较低", row["quality"])
            self.assertEqual(row["quality_observation"], "1080p，画质很好")
            self.assertEqual(row["ocr_text"], "ANONYMOUS、部分文字")
            self.assertEqual(row["camera_angle"], "")
            self.assertEqual(row["action_type"], "")
            prompt = provider.call_args.kwargs["prompt"]
            for requirement in ("1000×563", "广告牌", "屏幕", "现场真人", "俯拍", "航拍", "不要猜写"):
                self.assertIn(requirement, prompt)

    def test_project_vision_accepts_optional_observations_and_keeps_legacy_response_valid(self):
        value = {"status": "pass", "caption": "广告牌中出现持杯人物", "tags": ["广告牌"],
                 "subjects": ["广告牌"], "scene": "街道", "purpose": "广告牌参考", "quality": "清晰",
                 "brand": "未见品牌标识", "evidence": ["人物只出现在广告图中"], "quality_flags": [], "negative_flags": []}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "anonymous.png"
            Image.new("RGB", (60, 40), "blue").save(path)
            for observations in (None, {"main_subject": "广告牌", "background_subjects": ["建筑"], "camera_angle": "unknown", "uncertainties": ["画中人物非现场真人"]}):
                with self.subTest(observations=observations):
                    payload = {**value, **({"visual_observations": observations} if observations is not None else {})}
                    with patch("mvp_v2.image_understanding.minimax_credentials_available", return_value=True), patch("mvp_v2.image_understanding._run_minimax_vision", return_value=json.dumps(payload)) as provider:
                        result = vision.generate_minimax_project_image_analysis(path)
                    if observations is None:
                        self.assertEqual(result.visual_observations, {})
                    else:
                        self.assertEqual(result.visual_observations["main_subject"], "广告牌")
                        self.assertEqual(result.visual_observations["camera_angle"], "")
                    self.assertIn("现场真人", provider.call_args.kwargs["prompt"])
                    self.assertIn("不能证明航拍", provider.call_args.kwargs["prompt"])

    def test_false_dictionary_flags_remain_false_after_legacy_tokenization(self):
        structured = {
            "is_blurry": False, "is_low_resolution": False,
            "is_overexposed": "否", "severe_blur": 0,
        }
        # The old writer split repr(dict) at commas and spaces before persistence.
        legacy = ["{'is_blurry':", "False", "'is_low_resolution':", "False}"]
        for value in (structured, repr(structured), json.dumps(structured), legacy):
            with self.subTest(value=value):
                self.assertFalse(vision.analysis_asset_unusable({"quality_flags": value}))
                self.assertEqual(vision.normalize_visual_flags(value), [])

    def test_positive_flags_and_list_strings_are_normalized_without_losing_unknown_notes(self):
        examples = [
            ({"is_low_resolution": True, "is_blurry": False}, ["low_resolution"]),
            ({"严重模糊": "是", "过曝": "false"}, ["severe_blur"]),
            ('["slight blur", "低分辨率", "镜头畸变"]', ["slight_blur", "low_resolution", "镜头畸变"]),
            ("['severely_blurred', 'watermark']", ["severe_blur", "watermark"]),
            (["{'is_low_resolution':", "True", "'is_blurry':", "False}"], ["low_resolution"]),
        ]
        for value, expected in examples:
            with self.subTest(value=value):
                self.assertEqual(vision.normalize_visual_flags(value), expected)
        self.assertTrue(vision.analysis_asset_unusable({"quality_flags": {"severe_blur": True}}))
        self.assertFalse(vision.analysis_asset_unusable({"quality_flags": ["轻微运动模糊"]}))

    def test_normalization_preserves_provenance_and_does_not_fill_missing_visual_observations(self):
        raw = {
            "image_id": "anonymous", "action_type": "['站立', '手持话筒']",
            "quality_flags": {"is_low_resolution": False},
            "main_subject": "unknown",
            "confidence": 0.8, "source": "minimax", "source_record_key": "frozen-key",
        }
        normalized = vision.normalize_visual_analysis(raw)
        self.assertEqual(normalized["action_type"], "站立、手持话筒")
        self.assertEqual(normalized["quality_flags"], [])
        self.assertEqual(normalized["source_record_key"], "frozen-key")
        self.assertEqual(normalized["confidence"], 0.8)
        self.assertEqual(normalized.get("main_subject", ""), "")
        self.assertEqual(normalized.get("background_subjects", []), [])
        self.assertNotIn("prompt_version", normalized)
        self.assertEqual(raw["action_type"], "['站立', '手持话筒']")

    def test_normalizing_forbidden_content_does_not_erase_existing_safety_finding(self):
        self.assertTrue(vision.analysis_asset_unusable({"negative_flags": ["禁用内容"]}))
        self.assertTrue(vision.analysis_asset_unusable({"negative_flags": ["forbidden_content"]}))
        self.assertFalse(vision.analysis_asset_unusable({"negative_flags": {"forbidden_content": False}}))

    def test_new_library_vision_records_observation_boundaries_and_honest_confidence(self):
        parsed = {
            "caption": "一人手持话筒，背景为舞台屏幕", "tags": ["人物", "话筒"],
            "brand_names": ["无"], "scene_type": "室内舞台", "shot_type": "中景",
            "action_type": ["手持话筒"], "quality_flags": {"is_low_resolution": False},
            "negative_flags": [], "main_subject": "手持话筒的人",
            "subjects": ["人物", "话筒"], "background_subjects": ["舞台屏幕"],
            "camera_angle": "平视", "camera_movement": "推进",
            "uncertainties": ["不能确认人物身份"], "confidence": 0.61,
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "anonymous.png"
            Image.new("RGB", (40, 30), "blue").save(path)
            with patch("mvp_v2.image_understanding._run_minimax_vision", return_value=json.dumps(parsed)) as provider:
                row = vision.generate_minimax_visual_analysis_row({"image_id": "anonymous", "原图路径": str(path)}, "")
            self.assertEqual(row["main_subject"], "手持话筒的人")
            self.assertEqual(row["background_subjects"], ["舞台屏幕"])
            self.assertEqual(row["camera_angle"], "平视")
            self.assertEqual(row["camera_movement"], "")
            self.assertEqual(row["observation_basis"], "single_frame")
            self.assertEqual(row["action_type"], "手持话筒")
            self.assertEqual(row["confidence"], 0.61)
            self.assertEqual(row["confidence_basis"], "model_self_reported")
            self.assertIn("不能确认人物身份", row["uncertainties"])
            self.assertEqual(row["analysis_schema_version"], "visual-observation/v1")
            self.assertIn("单帧", provider.call_args.kwargs["prompt"])
            self.assertIn("不得推断", provider.call_args.kwargs["prompt"])
            parsed.pop("confidence")
            with patch("mvp_v2.image_understanding._run_minimax_vision", return_value=json.dumps(parsed)):
                unknown = vision.generate_minimax_visual_analysis_row({"image_id": "anonymous", "原图路径": str(path)}, "")
            self.assertIsNone(unknown["confidence"])
            self.assertEqual(unknown["confidence_basis"], "unknown")

    def test_manual_visual_fields_are_preserved_without_source_text_inference(self):
        seed = {"anonymous": {
            "caption": "背向镜头的人", "tags": ["人物"], "brand_names": ["无"],
            "main_subject": "人物背影", "background_subjects": ["墙面"],
            "camera_angle": "背面", "action_type": ["站立"],
            "quality_flags": {"is_low_resolution": False},
            "uncertainties": ["无法辨认表情"],
        }}
        row = vision.import_manual_visual_analysis(
            [{"image_id": "anonymous"}], seed, source_file="anonymous.json", annotator="reviewer",
        )[0]
        self.assertEqual(row["main_subject"], "人物背影")
        self.assertEqual(row["camera_angle"], "背面")
        self.assertEqual(row["action_type"], "站立")
        self.assertEqual(row["quality_flags"], [])
        self.assertEqual(row["source"], "manual_seed")
        self.assertIsNone(row["confidence"])
        self.assertEqual(row["confidence_basis"], "unknown")

    def test_default_package_projects_actual_vision_and_quality_without_inventing_source_observations(self):
        with tempfile.TemporaryDirectory(prefix="mplus-analysis-compatibility-") as temporary:
            root = Path(temporary)
            source, media, metadata = (root / name for name in ("source", "media", "metadata"))
            for directory in (source, media, metadata):
                directory.mkdir()
            reference = media / "reference.png"
            _write_source_with_reference_image(source / "M+发布会类通用脚本库.xlsx", reference)
            manifests, analyses = [], []
            for index in range(6):
                path = reference if index == 0 else media / f"anonymous-{index}.png"
                if index:
                    Image.new("RGB", (60 + index, 40), (index * 30, 40, 80)).save(path)
                manifests.append({"image_id": f"anonymous-{index}", "原图路径": str(path)})
                analyses.append({
                    "image_id": f"anonymous-{index}", "caption": "人物站在室内舞台", "tags": ["人物", "舞台"],
                    "brand_names": ["无"], "scene_type": "室内舞台", "shot_type": "中景",
                    "action_type": "['站立']", "quality_flags": {"is_low_resolution": False},
                    "negative_flags": [], "review_status": "unreviewed", "source": "minimax",
                    "quality": "细节可辨认" if index == 1 else "",
                })
            for filename, rows in (("image_manifest.jsonl", manifests), ("image_analysis.minimax.jsonl", analyses)):
                (metadata / filename).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
            manifest = create_asset_package(root / "package", package_id="anonymous-images", source_root=source, media_roots=[media], metadata_roots=[metadata])
            package = load_asset_package(manifest)
            before = {str(path): sha256_file(path) for path in (manifest, Path(package["index_path"]), reference)}
            service = FormalDefaultImageService(projects=None, tasks=None, delivery=None, artifacts=None, image_pool=None, image_workspace=None, asset_package_resolver=lambda _: manifest, writes_enabled=False)
            _, items = service._package_media({"category_id": "launch"}, {"脚本类型": "发布会"})
            self.assertEqual(len(items), 6)
            for item in items:
                self.assertEqual(item["scene"], "室内舞台")
                self.assertEqual(item["shot_type"], "中景")
                self.assertEqual(item["action_type"], "站立")
                self.assertEqual(item["subjects"], [])
                self.assertEqual(item["quality_flags"], [])
                self.assertNotEqual(item["quality"], "资产包校验通过")
                self.assertIn(item["quality"], {"细节可辨认", "画质待核对"})
                self.assertEqual(item["visual_analysis"]["quality_flags"], [])
            self.assertTrue(any(item["source_links"] for item in items))
            self.assertEqual(before, {path: sha256_file(path) for path in before})
            self.assertTrue(all(not item['visual_analysis'].get('content_sha256') for item in items))
            frozen_manifest = bundle_asset_package_v2(manifest, root / 'frozen-package', package_version=2)
            frozen_package = load_asset_package(frozen_manifest)
            frozen_before = {str(path): sha256_file(path) for path in
                (frozen_manifest, Path(frozen_package['index_path']))}
            service = FormalDefaultImageService(projects=None, tasks=None, delivery=None, artifacts=None,
                image_pool=None, image_workspace=None, asset_package_resolver=lambda _: frozen_manifest, writes_enabled=False)
            _, frozen_items = service._package_media({'category_id': 'launch'}, {'脚本类型': '发布会'})
            for item in frozen_items:
                visual = item['visual_analysis']
                self.assertEqual(visual['content_sha256'], item['content_sha256'])
                self.assertEqual(visual['evidence_binding']['media_id'], item['media_id'])
                self.assertEqual(visual['evidence_binding']['index_sha256'], frozen_package['index_sha256'])
                self.assertEqual(visual['caption'], '人物站在室内舞台')
                self.assertNotIn('uncertainties', visual)
            self.assertEqual(frozen_before, {path: sha256_file(path) for path in frozen_before})

    def test_default_candidate_preserves_static_variant_identity_and_visible_quality(self):
        media = [{
            "media_id": "variant-b", "content_sha256": "a" * 64,
            "source": {"file": "anonymous.xlsx", "sheet": "模版", "row": 7},
            "main_subject": "人物", "background_subjects": ["舞台"],
            "quality": "分辨率较低", "quality_flags": ["low_resolution"],
        }]
        ranked = {"image_id": "variant-b", "summary": "舞台人物", "tags": ["人物"],
                  "score": 72, "match_reason": "可见主体", "duplicate_group_id": "static-group",
                  "duplicate_of": "variant-a", "duplicate_count": 1}
        public = FormalDefaultImageService._public_candidate(ranked, "anonymous", media)
        self.assertEqual(public["candidate_key"], "variant-b")
        self.assertEqual(public["duplicate_of"], "variant-a")
        self.assertEqual(public["duplicate_group_id"], "static-group")
        self.assertEqual(public["duplicate_count"], 1)
        self.assertEqual(public["visual_evidence"]["quality_flags"], ["low_resolution"])
        self.assertEqual(public["visual_evidence"]["main_subject"], "人物")


if __name__ == "__main__":
    unittest.main()
