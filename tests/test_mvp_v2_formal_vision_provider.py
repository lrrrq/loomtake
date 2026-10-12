from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from mvp_v2.image_understanding import (
    ProjectImageVisionResult,
    VisionProviderError,
    generate_minimax_project_image_analysis,
    _project_image_result,
)


class FormalVisionProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-formal-vision-")
        self.addCleanup(self.temporary.cleanup)
        self.image = Path(self.temporary.name) / "anonymous.png"
        Image.new("RGB", (32, 20), "blue").save(self.image)

    def test_document_page_requires_purpose_review_but_brand_text_photo_remains_usable(self):
        base = {"status": "pass", "tags": ["参考"], "subjects": ["可见主体"],
                "scene": "室内", "purpose": "项目参考", "quality": "清晰", "brand": "未見品牌",
                "evidence": ["主体可见"], "quality_flags": [], "negative_flags": []}
        for caption, expected in (("白底黑字的需求说明页", "low_confidence"),
                                  ("米白色随行杯，杯身品牌文字清晰", "pass"),
                                  ("这是产品照片，不是文档页面", "pass")):
            with self.subTest(caption=caption):
                result = _project_image_result({**base, "caption": caption})
                self.assertEqual(result.status, expected)
                if expected == "low_confidence":
                    self.assertIn("资料参考", result.purpose)
                    self.assertIn("人工核对", result.purpose)

    def test_strict_minimax_business_result(self):
        value = {
            "status": "low_confidence",
            "caption": "会议现场中的参与者",
            "tags": ["会议", "人群"],
            "subjects": ["参与者"],
            "scene": "室内会场",
            "purpose": "活动氛围镜头",
            "quality": "画面清晰",
            "brand": "未见品牌标识",
            "evidence": ["画面可见多名参与者"],
            "quality_flags": ["slight_blur"],
            "negative_flags": [],
        }
        raw = json.dumps({"content": json.dumps(value, ensure_ascii=False)})
        with patch(
            "mvp_v2.image_understanding.minimax_credentials_available",
            return_value=True,
        ), patch(
            "mvp_v2.image_understanding._run_minimax_vision", return_value=raw,
        ) as provider:
            result = generate_minimax_project_image_analysis(self.image)
        self.assertIsInstance(result, ProjectImageVisionResult)
        self.assertEqual(result.status, "low_confidence")
        self.assertEqual(result.tags, ("会议", "人群"))
        self.assertEqual(result.quality_flags, ("slight_blur",))
        self.assertEqual(provider.call_args.args[1].suffix, ".png")
        prompt = provider.call_args.kwargs["prompt"]
        self.assertIn("不得猜测", prompt)
        self.assertIn("low_confidence", prompt)

    def test_loose_or_extra_provider_output_is_rejected(self):
        invalid = json.dumps({
            "content": json.dumps({
                "status": "pass", "caption": "画面", "tags": ["活动"],
                "subjects": ["人物"], "scene": "会场", "purpose": "脚本参考",
                "quality": "清晰", "brand": "未见品牌", "evidence": ["可见"],
                "confidence": 0.99,
                "quality_flags": [], "negative_flags": [],
            }, ensure_ascii=False),
        })
        with patch(
            "mvp_v2.image_understanding.minimax_credentials_available",
            return_value=True,
        ), patch(
            "mvp_v2.image_understanding._run_minimax_vision", return_value=invalid,
        ):
            with self.assertRaisesRegex(VisionProviderError, "字段不完整"):
                generate_minimax_project_image_analysis(self.image)


if __name__ == "__main__":
    unittest.main()
