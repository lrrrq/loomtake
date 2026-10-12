from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from PIL import Image, ImageEnhance

from mvp_v2.formal_image_candidates import (
    allocate_diverse_defaults,
    perceptual_cluster_ids,
)
from mvp_v2.brand_policy import conflicting_visual_brand_names
from mvp_v2.formal_image_workspace import (
    _details_by_script_shot,
    _effective_visual_slot,
    formal_image_search_slot,
)
from mvp_v2.image_retrieval import search_images_for_slot, slot_forbidden_visuals
from mvp_v2.visual_assets import _visual_conflicts
from mvp_v2.visual_function import desired_visual_functions


class FormalVisualQualityTests(unittest.TestCase):
    def test_provider_no_brand_sentence_is_not_treated_as_an_external_brand(self):
        self.assertEqual(
            conflicting_visual_brand_names(
                "匿名项目", ["画面中未见品牌标识。"],
            ),
            [],
        )
        self.assertEqual(
            conflicting_visual_brand_names("匿名项目", ["美的"]),
            ["美的"],
        )

    def test_dynamic_script_shots_keep_ordered_logical_slot_details(self):
        mapped = _details_by_script_shot(
            [{"镜号": "1"}, {"镜号": "2"}, {"镜号": "3"}],
            [
                {"模板镜号": "4", "逻辑槽位": 4},
                {"模板镜号": "4", "逻辑槽位": 5},
                {"模板镜号": "7", "逻辑槽位": 7},
            ],
        )
        self.assertEqual(
            [mapped[str(index)]["逻辑槽位"] for index in range(1, 4)],
            [4, 5, 7],
        )

    def test_planned_event_projects_to_existing_canonical_visual_functions(self):
        self.assertEqual(
            desired_visual_functions("产品演示"),
            ("product_experience",),
        )
        result = search_images_for_slot(
            {
                "slot_index": 4,
                "画面描述": "参与者操作产品装置完成产品演示",
                "槽位功能": "产品演示",
                "必须视觉证据": "产品演示",
                "required_visual_functions": ("product_experience",),
                "采用来源行": 7,
            },
            {"脚本类型": "发布会", "项目名称": "匿名项目"},
            [
                {"image_id": "product", "原图路径": "/product.png", "来源行": 7, "是否低清": False},
                {"image_id": "stage", "原图路径": "/stage.png", "来源行": 4, "是否低清": False},
            ],
            [
                {"image_id": "product", "caption": "参与者操作产品装置", "tags": ["产品体验", "操作设备"], "brand_names": ["无"]},
                {"image_id": "stage", "caption": "空主舞台与大屏全景", "tags": ["主会场", "舞台全景"], "brand_names": ["无"]},
            ],
        )
        self.assertEqual([item["image_id"] for item in result["candidates"]], ["product"])

    def test_dynamic_closing_event_uses_existing_final_kv_slot(self):
        self.assertEqual(
            _effective_visual_slot(10, desired_visual_functions("品牌落版·动线推进")),
            18,
        )
        self.assertEqual(
            _effective_visual_slot(7, desired_visual_functions("产品演示")),
            7,
        )
        self.assertEqual(
            formal_image_search_slot(
                row={"镜号": "17"},
                detail={"逻辑槽位": 10},
                planned={"primary_event": "品牌落版·动线推进"},
                fallback_index=17,
            )["逻辑槽位"],
            18,
        )
        current = formal_image_search_slot(
            row={"镜号": "17", "画面描述": "人物在外场道别，保留环境声"},
            detail={"逻辑槽位": 10},
            planned={"primary_event": "品牌落版·动线推进"},
            fallback_index=17,
        )
        self.assertTrue(current["current_visual_authority"])
        self.assertEqual(current["逻辑槽位"], 10)
        self.assertEqual(current["画面描述"], "人物在外场道别，保留环境声")

    def test_crops_and_contrast_variants_share_perceptual_cluster(self):
        fixture = (
            Path(__file__).parent
            / "fixtures"
            / "v10_image_delivery"
            / "cc0-participant-group.jpg"
        )
        with tempfile.TemporaryDirectory(prefix="mplus-visual-cluster-") as temporary:
            root = Path(temporary)
            paths = []
            with Image.open(fixture) as source:
                for index in range(3):
                    image = source.convert("RGB")
                    width, height = image.size
                    inset = index * 4
                    image = image.crop((inset, inset, width - inset, height - inset))
                    image = ImageEnhance.Contrast(image).enhance(0.94 + index * 0.03)
                    output = root / f"variant-{index}.png"
                    image.save(output)
                    paths.append(output)
            unrelated = root / "unrelated.png"
            Image.new("RGB", (1280, 720), "white").save(unrelated)
            clusters = perceptual_cluster_ids([
                {"image_id": f"variant-{index}", "path": path}
                for index, path in enumerate(paths)
            ] + [{"image_id": "unrelated", "path": unrelated}])
        self.assertEqual(len({clusters[f"variant-{index}"] for index in range(3)}), 1)
        self.assertNotEqual(clusters["variant-0"], clusters["unrelated"])

    def test_allocator_preserves_better_semantic_score_before_variety(self):
        ranked = {
            "1": {"candidates": [
                {"image_id": "image-a", "score": 95, "perceptual_cluster_id": "cluster-a"},
                {"image_id": "image-b", "score": 88, "perceptual_cluster_id": "cluster-b"},
            ]},
            "2": {"candidates": [
                {"image_id": "image-a", "score": 94, "perceptual_cluster_id": "cluster-a"},
                {"image_id": "image-c", "score": 87, "perceptual_cluster_id": "cluster-c"},
            ]},
            "3": {"candidates": [
                {"image_id": "image-a-variant", "score": 91, "perceptual_cluster_id": "cluster-a"},
            ]},
        }
        allocated = allocate_diverse_defaults(ranked)
        self.assertEqual(allocated["selected_by_shot"], {
            "1": "image-a",
            "2": "image-a",
            "3": "image-a-variant",
        })
        self.assertEqual(set(allocated["reuse_required_by_shot"]), {"1", "2"})

    def test_allocator_marks_reuse_only_when_no_safe_cluster_remains(self):
        ranked = {
            "1": {"candidates": [
                {"image_id": "image-a", "score": 95, "perceptual_cluster_id": "cluster-a"},
            ]},
            "2": {"candidates": [
                {"image_id": "image-a-variant", "score": 94, "perceptual_cluster_id": "cluster-a"},
            ]},
        }
        allocated = allocate_diverse_defaults(ranked)
        self.assertEqual(set(allocated["selected_by_shot"]), {"1", "2"})
        self.assertEqual(set(allocated["reuse_required_by_shot"]), {"2"})

    def test_authoritative_matcher_rejects_hard_quality_and_penalizes_soft_quality(self):
        slot = {"slot_index": 1, "画面描述": "会议现场人物"}
        brief = {"脚本类型": "活动记录"}
        manifest = [
            {"image_id": "clear", "原图路径": "/clear.png", "是否低清": False},
            {"image_id": "soft", "原图路径": "/soft.png", "是否低清": False},
            {"image_id": "blurred", "原图路径": "/blurred.png", "是否低清": False},
        ]
        analysis = [
            {"image_id": "clear", "caption": "会议现场人物", "tags": ["会议", "人物"], "brand_names": ["无"], "quality_flags": []},
            {"image_id": "soft", "caption": "会议现场人物", "tags": ["会议", "人物"], "brand_names": ["无"], "quality_flags": ["slight_blur"]},
            {"image_id": "blurred", "caption": "会议现场人物", "tags": ["会议", "人物"], "brand_names": ["无"], "quality_flags": ["severe_blur"]},
        ]
        result = search_images_for_slot(slot, brief, manifest, analysis, top_k=3)
        candidates = {item["image_id"]: item for item in result["candidates"]}
        self.assertNotIn("blurred", candidates)
        self.assertGreater(candidates["clear"]["score"], candidates["soft"]["score"])
        self.assertEqual(candidates["clear"]["quality_score"], 10)
        self.assertEqual(candidates["soft"]["quality_score"], 5)

    def test_empty_audience_seating_is_not_mistaken_for_an_active_crowd(self):
        self.assertEqual(
            slot_forbidden_visuals(
                4,
                "完成搭建的空主舞台、大屏与空观众席全景",
                ["主舞台", "空场", "观众席", "无人"],
            ),
            [],
        )
        self.assertIn(
            "观众",
            slot_forbidden_visuals(
                4,
                "主舞台前观众举起手机拍摄",
                ["主舞台", "观众", "手机拍摄"],
            ),
        )

    def test_completed_stage_is_not_mistaken_for_active_construction(self):
        self.assertEqual(
            _visual_conflicts(
                "主会场空镜与舞台光束",
                "完成搭建的空主舞台与大屏全景",
            ),
            [],
        )
        self.assertIn(
            "需要成型后的主会场空镜但画面仍在搭建施工",
            _visual_conflicts(
                "主会场空镜与舞台光束",
                "工作人员正在搭建舞台桁架",
            ),
        )


if __name__ == "__main__":
    unittest.main()
