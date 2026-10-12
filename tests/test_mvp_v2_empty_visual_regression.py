from __future__ import annotations

import unittest

from mvp_v2.audit import _required_term_present
from mvp_v2.image_retrieval import search_images_for_slot
from mvp_v2.visual_function import audit_visual_function_quality


BRIEF = {
    "项目名称": "林氏家居生活方式发布会",
    "品牌或客户": "林氏家居",
    "必须出现的内容": "品牌logo、活动主会场、主题装置、产品体验、人流互动、核心发布仪式",
}


def _manifest(image_id: str, filename: str) -> dict:
    return {
        "image_id": image_id,
        "原图路径": f"/tmp/{filename}",
        "预览路径": f"/tmp/{filename}",
        "来源文件": f"/tmp/{filename}",
        "来源 sheet": "项目图片池",
        "是否来自项目图片池": True,
        "是否低清": False,
    }


def _analysis(
    image_id: str,
    caption: str,
    tags: list[str],
    *,
    people_count: int,
) -> dict:
    return {
        "image_id": image_id,
        "caption": caption,
        "tags": tags,
        "brand_names": ["林氏家居"],
        "ocr_text": "林氏家居",
        "negative_flags": [],
        "quality_flags": [],
        "people_count": people_count,
        "source": "minimax",
    }


class EmptyVisualRegressionTests(unittest.TestCase):
    def test_slot_4_accepts_indoor_stage_with_lamp_installation(self):
        manifest = [_manifest("venue-installation", "venue.png")]
        analyses = [_analysis(
            "venue-installation",
            "发布会现场舞台全景，聚光灯交错照射，中央矗立复古路灯装置，背景为城市建筑剪影投影。",
            ["发布会", "舞台", "聚光灯", "复古路灯", "大屏幕"],
            people_count=0,
        )]

        result = search_images_for_slot(
            {"slot_index": 4, "画面描述": "主会场空镜，多束白色光束向舞台中心汇聚"},
            BRIEF,
            manifest,
            analyses,
        )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["image_id"], "venue-installation")

    def test_slot_5_uses_real_participant_interaction_not_empty_room(self):
        manifest = [
            _manifest("empty-room", "empty.png"),
            _manifest("social-participation", "social.png"),
        ]
        analyses = [
            _analysis(
                "empty-room",
                "林氏家居发布会家居体验空间，房间内陈列床、沙发与健身器材。",
                ["家居体验", "沉浸式体验"],
                people_count=0,
            ),
            _analysis(
                "social-participation",
                "紫色霓虹灯环绕的空间里，四人围坐在餐桌旁互动，形成真实现场参与。",
                ["发布会", "餐桌", "互动", "宾客"],
                people_count=4,
            ),
        ]

        result = search_images_for_slot(
            {"slot_index": 5, "画面描述": "签到墙顾客互动"},
            BRIEF,
            manifest,
            analyses,
        )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["image_id"], "social-participation")
        rejected = {item["image_id"]: item["reason"] for item in result["rejected"]}
        self.assertIn("必须出现人物", rejected["empty-room"])

    def test_slot_5_can_adapt_to_crowd_interaction_when_no_sign_in_exists(self):
        manifest = [_manifest("autograph-crowd", "autograph.png")]
        analyses = [_analysis(
            "autograph-crowd",
            "发布会现场，一位男性嘉宾在桌前为观众签名，人群围聚互动。",
            ["发布会", "签名", "互动", "嘉宾", "人群"],
            people_count=8,
        )]

        result = search_images_for_slot(
            {"slot_index": 5, "画面描述": "签到墙顾客互动"},
            BRIEF,
            manifest,
            analyses,
        )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["image_id"], "autograph-crowd")

    def test_slot_5_participation_and_slot_9_crowd_heat_are_valid_progression(self):
        checks = audit_visual_function_quality([
            {
                "slot_index": 5,
                "模板镜号": "5",
                "image_id": "autograph-crowd",
                "视觉内容摘要": "发布会现场嘉宾签名，人群围聚互动",
            },
            {
                "slot_index": 9,
                "模板镜号": "9",
                "image_id": "crowd-heat",
                "视觉内容摘要": "展区通道人群密集，参观者持续流动",
            },
        ])

        self.assertNotIn(
            "duplicate_visual_function",
            {check["检查项"] for check in checks},
        )

    def test_slot_7_recognizes_riding_experience_as_product_experience(self):
        manifest = [_manifest("bike-experience", "bike.png")]
        analyses = [_analysis(
            "bike-experience",
            "发布会现场，一位男士在霓虹家居空间内骑动感单车，体验现场产品。",
            ["发布会", "动感单车", "健身", "骑行", "家居"],
            people_count=1,
        )]

        result = search_images_for_slot(
            {"slot_index": 7, "画面描述": "客户在主题空间互动"},
            BRIEF,
            manifest,
            analyses,
        )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["image_id"], "bike-experience")
        self.assertTrue(_required_term_present("产品体验", analyses[0]["caption"]))

    def test_slot_15_accepts_formal_guest_stage_node_without_fake_award(self):
        manifest = [_manifest("formal-guests", "guests.png")]
        analyses = [_analysis(
            "formal-guests",
            "发布会现场，舞台上四位嘉宾站在一起，背景为品牌大屏与正式舞台灯光。",
            ["发布会", "舞台", "嘉宾", "品牌活动", "LED屏"],
            people_count=4,
        )]

        result = search_images_for_slot(
            {"slot_index": 15, "画面描述": "颁发仪式"},
            BRIEF,
            manifest,
            analyses,
        )

        self.assertEqual(result["status"], "auto")
        self.assertEqual(result["candidates"][0]["image_id"], "formal-guests")

    def test_required_content_aliases_match_verified_real_visuals(self):
        self.assertTrue(_required_term_present("主题装置", "舞台中央矗立复古路灯装置"))
        self.assertTrue(_required_term_present("产品体验", "顾客在现场骑动感单车"))
        self.assertTrue(_required_term_present("人流互动", "人群围聚，从空间切入真实现场互动参与"))


if __name__ == "__main__":
    unittest.main()
