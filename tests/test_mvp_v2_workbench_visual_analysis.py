from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from mvp_v2 import image_understanding as vision


class WorkbenchVisualAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mplus-visual-observation-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.payload = {
            "caption": "人物在前景手持话筒，屏幕中的人物在远处背景。",
            "tags": ["人物", "话筒", "屏幕"], "brand_names": ["无"],
            "subjects": ["现场人物", "话筒", "屏幕中的人物"],
            "main_subject": "现场手持话筒的人", "foreground_subjects": ["现场人物", "话筒"],
            "background_subjects": ["屏幕中的人物"], "scene_type": "室内舞台",
            "shot_type": "中景", "action_type": "现场人物手持话筒", "camera_angle": "平视",
            "quality_flags": [], "negative_flags": [], "uncertainties": ["不能确认人物身份"],
        }

    def _asset(self, path):
        return {"image_id": "anonymous", "原图路径": str(path)}

    def _project_payload(self):
        return {
            "status": "pass", "caption": self.payload["caption"], "tags": ["人物", "话筒"],
            "subjects": ["现场人物"], "scene": "室内舞台", "purpose": "人物构图参考",
            "quality": "主体清晰", "brand": "未见可辨品牌", "evidence": ["前景人物手持话筒"],
            "quality_flags": [], "negative_flags": [],
            "visual_observations": {key: deepcopy(self.payload[key]) for key in (
                "main_subject", "foreground_subjects", "background_subjects", "scene_type",
                "shot_type", "action_type", "camera_angle", "uncertainties",
            )},
        }

    def test_library_gif_uses_fresh_external_frame_and_keeps_existing_cache_untouched(self):
        package = self.root / "frozen-package"
        package.mkdir()
        source = package / "anonymous.gif"
        old_cache = package / "_minimax_frames"
        old_cache.mkdir()
        old_frame = old_cache / "anonymous.png"
        Image.new("RGB", (32, 24), "green").save(old_frame)
        old_bytes = old_frame.read_bytes()
        observed = []

        def provider(command, path, key, timeout_seconds, *, prompt):
            self.assertTrue(path.is_file())
            self.assertNotIn(package, path.parents)
            with Image.open(path) as image:
                observed.append((path, image.convert("RGB").getpixel((0, 0))))
            return json.dumps(self.payload)

        for color in ("red", "blue"):
            Image.new("RGB", (32, 24), color).save(source, format="GIF")
            before = source.read_bytes()
            with patch.object(vision, "_run_minimax_vision", side_effect=provider):
                vision.generate_minimax_visual_analysis_row(self._asset(source), "")
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(old_frame.read_bytes(), old_bytes)
            self.assertFalse(observed[-1][0].parent.exists())
        self.assertEqual([color for _, color in observed], [(255, 0, 0), (0, 0, 255)])
        self.assertNotEqual(observed[0][0], observed[1][0])
        self.assertEqual({item.name for item in package.iterdir()}, {"anonymous.gif", "_minimax_frames"})

    def test_library_gif_provider_failure_removes_only_its_temporary_frame(self):
        source = self.root / "frozen.gif"
        Image.new("RGB", (32, 24), "red").save(source, format="GIF")
        before = source.read_bytes()
        used = []

        def provider(command, path, *args, **kwargs):
            used.append(path)
            self.assertTrue(path.exists())
            raise vision.VisionProviderError("anonymous provider failure")

        with patch.object(vision, "_run_minimax_vision", side_effect=provider) as invoke:
            with self.assertRaisesRegex(vision.VisionProviderError, "anonymous provider failure"):
                vision.generate_minimax_visual_analysis_row(self._asset(source), "")
        self.assertEqual(invoke.call_count, 1)
        self.assertFalse(used[0].parent.exists())
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_project_gif_diagnostics_bind_original_bytes_and_prompt_after_frame_cleanup(self):
        source = self.root / "project.gif"
        Image.new("RGB", (32, 24), "red").save(source, format="GIF")
        content = source.read_bytes()
        diagnostics = self.root / "diagnostics"
        used = []

        def provider(command, path, *args, **kwargs):
            used.append(path)
            self.assertEqual(path.suffix, ".png")
            self.assertTrue(path.is_file())
            return json.dumps(self._project_payload())

        with patch.object(vision, "minimax_credentials_available", return_value=True), patch.object(
            vision, "_run_minimax_vision", side_effect=provider,
        ):
            result = vision.generate_minimax_project_image_analysis(source, diagnostics_dir=diagnostics)
        self.assertEqual(result.visual_observations["action_type"], "现场人物手持话筒")
        self.assertEqual(source.read_bytes(), content)
        self.assertFalse(used[0].parent.exists())
        record = json.loads(next(diagnostics.glob("*.json")).read_text())
        self.assertEqual(record["image_sha256"], sha256(content).hexdigest())
        self.assertEqual(record["invocation"]["prompt_version"], vision.PROJECT_IMAGE_VISION_PROMPT_VERSION)
        self.assertEqual(record["status"], "succeeded")

    def test_new_sparse_library_analysis_marks_each_unrecorded_observation_without_inference(self):
        source = self.root / "anonymous.png"
        Image.new("RGB", (40, 30), "blue").save(source)
        sparse = {"caption": "源图显示人物和背景建筑", "tags": ["人物", "建筑"], "brand_names": ["无"]}
        with patch.object(vision, "_run_minimax_vision", return_value=json.dumps(sparse)):
            row = vision.generate_minimax_visual_analysis_row(self._asset(source), "")
        for field in ("main_subject", "scene_type", "shot_type", "action_type", "camera_angle"):
            self.assertEqual(row[field], "")
        for field in ("subjects", "foreground_subjects", "background_subjects"):
            self.assertEqual(row[field], [])
        for label in ("可见主体", "主主体", "前景", "背景", "场景", "景别", "可见动作", "机位"):
            self.assertIn(f"{label}信息未记录，待核对", row["uncertainties"])
        self.assertEqual(row["review_status"], "unreviewed")

    def test_unknown_normalization_preserves_uncertainty_provenance_and_is_idempotent(self):
        original = {
            "caption": "旧图观察", "main_subject": "unknown", "camera_angle": None,
            "background_subjects": ["未知"], "uncertainties": ["文字无法辨认"],
            "source": "legacy", "source_record_key": "original-key", "prompt_version": "old-version",
            "negative_flags": ["forbidden_content"],
        }
        before = deepcopy(original)
        row = vision.normalize_visual_analysis(original)
        self.assertEqual(original, before)
        self.assertEqual(row["main_subject"], "")
        self.assertEqual(row["camera_angle"], "")
        self.assertIn("主主体未知，待核对", row["uncertainties"])
        self.assertIn("机位未知，待核对", row["uncertainties"])
        self.assertIn("背景未知，待核对", row["uncertainties"])
        self.assertIn("文字无法辨认", row["uncertainties"])
        self.assertNotIn("foreground_subjects", row)
        self.assertNotIn("action_type", row)
        for field in ("source", "source_record_key", "prompt_version", "negative_flags"):
            self.assertEqual(row[field], original[field])
        self.assertEqual(vision.normalize_visual_analysis(row), row)

    def test_complete_observations_keep_visible_roles_and_do_not_invent_new_unknowns(self):
        source = self.root / "anonymous.png"
        Image.new("RGB", (40, 30), "blue").save(source)
        with patch.object(vision, "_run_minimax_vision", return_value=json.dumps(self.payload)) as provider:
            row = vision.generate_minimax_visual_analysis_row(self._asset(source), "")
        for field in ("subjects", "main_subject", "foreground_subjects", "background_subjects", "action_type", "camera_angle"):
            self.assertEqual(row[field], self.payload[field])
        self.assertEqual(row["uncertainties"], self.payload["uncertainties"])
        self.assertEqual(row["camera_movement"], "")
        self.assertEqual(row["prompt_version"], vision.VISION_PROMPT_VERSION)
        prompt = provider.call_args.kwargs["prompt"]
        self.assertIn("不能把背景人物的动作移给主主体", prompt)
        self.assertIn("广告牌或屏幕里的人物", prompt)
        self.assertIn("uncertainties 逐项说明原因", prompt)

    def test_project_unknowns_remain_uncertain_and_legacy_missing_fields_stay_missing(self):
        payload = self._project_payload()
        payload["visual_observations"] = {"main_subject": "unknown", "camera_angle": "未知"}
        result = vision._project_image_result(payload)
        self.assertEqual(result.visual_observations["main_subject"], "")
        self.assertIn("主主体未知，待核对", result.visual_observations["uncertainties"])
        self.assertIn("机位未知，待核对", result.visual_observations["uncertainties"])
        self.assertNotIn("background_subjects", result.visual_observations)
        del payload["visual_observations"]
        self.assertEqual(vision._project_image_result(payload).visual_observations, {})

    def test_vision_prompts_require_safe_quoted_text_and_strict_json_with_new_cache_versions(self):
        prompts = [
            (vision._minimax_prompt("年会"), vision.VISION_PROMPT_VERSION),
            (vision._minimax_brand_retry_prompt("年会"), vision.VISION_PROMPT_VERSION),
            (vision._project_image_prompt(), vision.PROJECT_IMAGE_VISION_PROMPT_VERSION),
        ]
        for prompt, version in prompts:
            with self.subTest(version=version, prompt=prompt[:30]):
                self.assertIn("字符串内部的引语使用中文引号", prompt)
                self.assertIn("对ASCII双引号做JSON转义", prompt)
                self.assertIn("数组元素中均不得出现未转义的ASCII双引号", prompt)
                self.assertIn("不要Markdown代码块或说明文字", prompt)
                self.assertIn("JSON能完整解析", prompt)
                self.assertTrue(version.endswith("json-syntax"))


if __name__ == "__main__":
    unittest.main()
