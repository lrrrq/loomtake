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


class FormalVisionOcrCompatibilityTests(unittest.TestCase):
    """Known OCR placement compatibility without repairing other provider facts."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-ocr-compatibility-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.image = self.root / "anonymous.png"
        Image.new("RGB", (48, 32), "blue").save(self.image)

    @staticmethod
    def _payload():
        return {
            "status": "low_confidence",
            "caption": "匿名产品参考图",
            "tags": ["产品"],
            "subjects": ["匿名产品"],
            "scene": "白色背景",
            "purpose": "产品外观参考",
            "quality": "画面清晰",
            "brand": "未见可辨品牌",
            "evidence": ["可见产品轮廓"],
            "quality_flags": [],
            "negative_flags": [],
        }

    def _generate(self, raw, directory):
        with patch.object(vision, "minimax_credentials_available", return_value=True), patch.object(
            vision, "_run_minimax_vision", return_value=raw,
        ) as provider:
            result = vision.generate_minimax_project_image_analysis(
                self.image, api_key="anonymous-test-key", diagnostics_dir=directory,
            )
        provider.assert_called_once()
        return result

    @staticmethod
    def _envelope(payload):
        return json.dumps({"content": json.dumps(payload, ensure_ascii=False)}, ensure_ascii=False)

    def test_top_level_ocr_matches_native_observation_without_input_mutation(self):
        original = {**self._payload(), "ocr_text": "匿名可见文字"}
        before = deepcopy(original)
        expected = vision._project_image_result({
            **self._payload(), "visual_observations": {"ocr_text": "匿名可见文字"},
        })
        result = vision._project_image_result(original)
        self.assertEqual(result, expected)
        self.assertEqual(result.status, "low_confidence")
        self.assertEqual(original, before)
        self.assertNotIn("visual_observations", original)

    def test_empty_or_same_nested_ocr_preserves_other_observations_and_source(self):
        for nested in ("", "匿名可见文字"):
            with self.subTest(nested=nested):
                payload = {
                    **self._payload(), "ocr_text": "匿名可见文字",
                    "visual_observations": {
                        "ocr_text": nested, "main_subject": "匿名产品",
                        "foreground_subjects": ["匿名产品"], "uncertainties": ["小字不可辨"],
                    },
                }
                before = deepcopy(payload)
                result = vision._project_image_result(payload)
                self.assertEqual(result.visual_observations["ocr_text"], "匿名可见文字")
                self.assertEqual(result.visual_observations["main_subject"], "匿名产品")
                self.assertEqual(result.visual_observations["foreground_subjects"], ["匿名产品"])
                self.assertEqual(result.visual_observations["uncertainties"], ["小字不可辨"])
                self.assertEqual(payload, before)

    def test_conflicting_ocr_is_not_overwritten(self):
        payload = {
            **self._payload(), "ocr_text": "匿名甲",
            "visual_observations": {"ocr_text": "匿名乙"},
        }
        before = deepcopy(payload)
        with self.assertRaisesRegex(vision.VisionProviderError, "VISION_RESPONSE_FIELDS_INVALID"):
            vision._project_image_result(payload)
        self.assertEqual(payload, before)

    def test_top_level_ocr_requires_string_and_observation_container(self):
        for invalid in (None, True, 1, 0.5, ["文字"], {"text": "文字"}):
            with self.subTest(invalid=invalid):
                payload = {**self._payload(), "ocr_text": invalid}
                before = deepcopy(payload)
                with self.assertRaisesRegex(vision.VisionProviderError, "VISION_RESPONSE_FIELDS_INVALID"):
                    vision._project_image_result(payload)
                self.assertEqual(payload, before)
        for invalid in (None, [], "文字"):
            with self.subTest(container=invalid):
                with self.assertRaisesRegex(vision.VisionProviderError, "VISION_RESPONSE_FIELDS_INVALID"):
                    vision._project_image_result({
                        **self._payload(), "ocr_text": "匿名文字", "visual_observations": invalid,
                    })

    def test_unknown_top_level_fields_still_fail_with_known_ocr(self):
        payload = {**self._payload(), "ocr_text": "匿名文字", "confidence": 0.99}
        before = deepcopy(payload)
        with self.assertRaisesRegex(vision.VisionProviderError, "VISION_RESPONSE_FIELDS_INVALID"):
            vision._project_image_result(payload)
        self.assertEqual(payload, before)

    def test_required_fields_still_fail_with_known_ocr(self):
        payload = {**self._payload(), "ocr_text": "匿名文字"}
        del payload["brand"]
        with self.assertRaisesRegex(vision.VisionProviderError, "VISION_RESPONSE_FIELDS_MISSING"):
            vision._project_image_result(payload)

    def test_unknown_nested_fields_and_invalid_status_are_not_normalized(self):
        for addition in (
            {"visual_observations": {"undeclared_observation": "匿名"}},
            {"status": "approved"},
        ):
            with self.subTest(addition=addition):
                with self.assertRaises(vision.VisionProviderError):
                    vision._project_image_result({**self._payload(), "ocr_text": "匿名文字", **addition})

    def test_ocr_mapping_preserves_status_and_flags(self):
        for status in ("pass", "low_confidence", "blocked"):
            with self.subTest(status=status):
                payload = {
                    **self._payload(), "status": status, "ocr_text": "匿名文字",
                    "quality_flags": ["slight_blur"], "negative_flags": ["external_brand"],
                }
                result = vision._project_image_result(payload)
                self.assertEqual(result.status, status)
                self.assertEqual(result.quality_flags, ("slight_blur",))
                self.assertEqual(result.negative_flags, ("external_brand",))

    def test_success_diagnostics_keep_original_raw_parsed_and_extra_field(self):
        payload = {
            **self._payload(), "ocr_text": "匿名可见文字",
            "visual_observations": {"main_subject": "匿名产品"},
        }
        raw = self._envelope(payload)
        directory = self.root / "success-diagnostics"
        result = self._generate(raw, directory)
        paths = list(directory.glob("*.json"))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(result.status, "low_confidence")
        self.assertEqual(result.visual_observations["ocr_text"], "匿名可见文字")
        self.assertEqual(record["status"], "succeeded")
        self.assertEqual(record["parsed"], payload)
        self.assertNotIn("ocr_text", record["parsed"]["visual_observations"])
        self.assertEqual(record["raw_response"], raw)
        self.assertEqual(record["raw_response_sha256"], sha256(raw.encode("utf-8")).hexdigest())
        self.assertEqual(record["image_sha256"], sha256(self.image.read_bytes()).hexdigest())
        self.assertEqual(record["missing_fields"], [])
        self.assertEqual(record["extra_fields"], ["ocr_text"])
        normalized = json.dumps(record["normalizations"], ensure_ascii=False)
        self.assertIn("ocr_text", normalized)
        self.assertIn("visual_observations.ocr_text", normalized)
        self.assertEqual(record["parsing"]["status"], "parsed")
        self.assertEqual(record["stages"], [
            {"stage": "provider", "status": "succeeded"},
            {"stage": "parse", "status": "succeeded"},
            {"stage": "validation", "status": "succeeded"},
        ])

    def test_failed_validation_diagnostics_keep_raw_without_claiming_normalization(self):
        payload = {**self._payload(), "ocr_text": "匿名甲", "visual_observations": {"ocr_text": "匿名乙"}}
        raw = self._envelope(payload)
        directory = self.root / "conflict-diagnostics"
        with self.assertRaisesRegex(vision.VisionProviderError, "VISION_RESPONSE_FIELDS_INVALID"):
            self._generate(raw, directory)
        record = json.loads(next(directory.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["failure_stage"], "validation")
        self.assertEqual(record["extra_fields"], ["ocr_text"])
        self.assertEqual(record["parsed"], payload)
        self.assertEqual(record["raw_response"], raw)
        self.assertFalse(record.get("normalizations"))

    def test_malformed_or_truncated_json_is_never_promoted_from_salvage(self):
        payload = {**self._payload(), "ocr_text": "匿名文字"}
        complete = json.dumps(payload, ensure_ascii=False)
        head = json.dumps({key: payload[key] for key in ("status", "caption", "tags")}, ensure_ascii=False)
        cases = (
            (head + ',"subjects":["匿名产品"]},"scene":"白色背景"}', "VISION_RESPONSE_PARSE_FAILED"),
            (complete + complete, "VISION_RESPONSE_PARSE_FAILED"),
            (complete + "\n以下为补充说明。", "VISION_RESPONSE_PARSE_FAILED"),
            (complete[:-1], "VISION_RESPONSE_TRUNCATED"),
        )
        for index, (body, code) in enumerate(cases):
            with self.subTest(index=index, code=code):
                raw = json.dumps({"content": body}, ensure_ascii=False)
                parsed, diagnostics = vision._parse_minimax_response(raw)
                self.assertNotEqual(diagnostics["status"], "parsed")
                self.assertTrue(parsed, "Recovery may retain evidence but must not become a valid response")
                directory = self.root / f"invalid-diagnostics-{index}"
                with self.assertRaisesRegex(vision.VisionProviderError, code):
                    self._generate(raw, directory)
                record = json.loads(next(directory.glob("*.json")).read_text(encoding="utf-8"))
                self.assertEqual(record["status"], "failed")
                self.assertEqual(record["failure_stage"], "parse")
                self.assertEqual(record["error"]["code"], code)
                self.assertEqual(record["raw_response"], raw)
                self.assertEqual(record["stored_response_sha256"], sha256(raw.encode("utf-8")).hexdigest())
                self.assertFalse(record.get("normalizations"))


if __name__ == "__main__":
    unittest.main()
